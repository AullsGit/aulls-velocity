"""
Comparação e Refinamento Iterativo — a "segunda análise" pedida no
objetivo do projeto: depois de gerar o vídeo, compara-o de novo com a
referência (usando a MESMA Edit DNA — ver src/edit_dna.py — desta vez
sobre o vídeo renderizado), mede as diferenças, ajusta parâmetros do
render por regra determinística, e renderiza de novo. Repete até a
melhoria estagnar, um score-alvo ser atingido, ou o nº máximo de
iterações ser alcançado (config.MAX_REFINE_ITERATIONS).

ÂMBITO DESTA VERSÃO (honestidade, como no resto do projeto): os ajustes
automáticos tocam APENAS em parâmetros da fase de RENDER — duração das
transições, magnitude do zoom, color matching. Isto é deliberado: mudar
parâmetros de deteção (ex: SCENE_DETECT_THRESHOLD) obrigaria a repetir
a deteção de cortes E o matching de conteúdo (Fase 4, que pode usar CLIP
— caro de repetir a cada iteração) e re-mapear tudo, o que tornaria o
loop lento e instável para uma primeira versão. O resultado: este loop
otimiza COMO o padrão já detectado é renderizado, não muda a estrutura
de cortes escolhida na Fase 1/2. Alargar isto para também reajustar a
deteção é o próximo passo natural (ver README).

O "motor determinístico" aqui é literal: suggest_adjustments() é uma
função pura, baseada em regras fixas sobre as métricas do diff — não há
nenhum modelo a "decidir" os ajustes NUMÉRICOS. O que pode vir de um
modelo (opcional, desligado por padrão — ver src/ai_analyzer.py) é o
JULGAMENTO de cada plano (zoom_match, motion_match, color_match), que
substitui as heurísticas locais (ORB/histograma) quando ativado — mas
esses vereditos entram no MESMO cálculo agregado e alimentam as MESMAS
regras deterministas de suggest_adjustments, nunca decidem sozinhos.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

import config
import edit_dna
import renderer

try:
    import ai_analyzer
except Exception:
    ai_analyzer = None


DEFAULT_WEIGHTS = {
    "duration": 0.15,
    "cut_count": 0.15,
    "zoom": 0.15,
    "transition": 0.15,
    "color": 0.20,
    "camera_motion": 0.10,
    "speed_curve": 0.10,
}


def _align_shots(ref_shots: list, out_shots: list) -> list:
    """
    Devolve uma lista de pares (ref_entry, out_entry_ou_None).

    Quando o número de planos bate certo (caso mais comum — o EDL tem
    exatamente 1 entrada por plano da referência, por construção),
    alinha por índice: é a correspondência mais correta possível, porque
    sabemos que o plano i do output foi gerado a partir do plano i da
    referência.

    Quando os números divergem (ex: transições longas "engoliram" um
    corte no render, ou o scene detector viu o output de forma
    ligeiramente diferente), faz um alinhamento guloso pelo timestamp de
    início mais próximo — best-effort, e a divergência em si já é um
    sinal reportado no diff (cut_count_delta).
    """
    if len(ref_shots) == len(out_shots):
        return list(zip(ref_shots, out_shots))

    pairs = []
    used = set()
    for r in ref_shots:
        best_idx, best_dist = None, None
        for idx, o in enumerate(out_shots):
            if idx in used:
                continue
            dist = abs(o["start"] - r["start"])
            if best_dist is None or dist < best_dist:
                best_dist, best_idx = dist, idx
        if best_idx is not None:
            used.add(best_idx)
            pairs.append((r, out_shots[best_idx]))
        else:
            pairs.append((r, None))
    return pairs


def _color_delta(color_a, color_b):
    if not color_a or not color_b or "error" in color_a or "error" in color_b:
        return None
    return {
        "brightness": abs(color_a["mean_v"] - color_b["mean_v"]),
        "contrast": abs(color_a["std_v"] - color_b["std_v"]),
        "saturation": abs(color_a["mean_s"] - color_b["mean_s"]),
    }


def _run_ai_overlay(reference_dna: dict, output_dna: dict, pairs: list, per_shot_report: list) -> dict:
    """
    Se config.USE_AI_ANALYZER estiver ligado e o AI Analyzer disponível
    (ver src/ai_analyzer.py), pede vereditos de visão computacional para
    até config.AI_ANALYZER_MAX_SHOTS planos e SUBSTITUI, nesses planos,
    zoom_match/motion_match (por um limiar sobre camera_motion_match) e
    acrescenta color_ai_match/pacing_ai_score — mutando per_shot_report
    DIRETAMENTE, para que a agregação a seguir (sempre derivada de
    per_shot_report) propague automaticamente a substituição, sem
    duplicar lógica. Devolve um resumo para incluir no diff
    ("ai_analyzer"), ou None se a funcionalidade estiver desligada.
    """
    if not getattr(config, "USE_AI_ANALYZER", False) or ai_analyzer is None:
        return None
    if not ai_analyzer.is_available():
        return {"available": False}

    try:
        ai_results = ai_analyzer.judge_all_shots(reference_dna["source_path"], output_dna["source_path"], pairs)
    except Exception as e:
        return {"available": True, "error": str(e)}

    valid = [r for r in ai_results if "error" not in r]
    by_ref_index = {r["index_ref"]: r for r in valid}
    threshold = getattr(config, "AI_ANALYZER_MATCH_THRESHOLD", 0.6)

    for entry in per_shot_report:
        v = by_ref_index.get(entry.get("index_ref"))
        if not v:
            continue
        cam_match = v["camera_motion_match"] >= threshold
        entry["zoom_match"] = cam_match
        entry["motion_match"] = cam_match
        entry["color_ai_match"] = v["color_match"] >= threshold
        entry["pacing_ai_score"] = v["pacing_match"]
        entry["ai_note"] = v.get("note") or None
        entry["source"] = "ai"

    return {
        "available": True,
        "n_shots_judged": len(valid),
        "n_shots_failed": len(ai_results) - len(valid),
        "notes": [v["note"] for v in valid if v.get("note")],
    }


def compare_edit_dna(reference_dna: dict, output_dna: dict, weights: dict = None) -> dict:
    """
    Compara a Edit DNA da referência com a de um vídeo gerado (output),
    plano a plano (ver _align_shots) e agregado, devolvendo um dict com
    as métricas de diferença e um "similarity_score" (0-1, 1 = idêntico
    nos aspetos medidos).

    Passo 1: julgamento heurístico local (ORB/histograma, sempre corre,
    grátis). Passo 2 (opcional): se config.USE_AI_ANALYZER estiver
    ligado, um modelo de visão substitui esse julgamento nos planos que
    consegue avaliar (ver _run_ai_overlay / src/ai_analyzer.py) — a
    agregação abaixo é sempre derivada de per_shot_report, por isso a
    substituição propaga automaticamente para zoom_match_ratio,
    camera_motion_match_ratio, etc., sem lógica duplicada.
    """
    weights = weights or getattr(config, "REFINE_METRIC_WEIGHTS", DEFAULT_WEIGHTS)

    ref_shots = reference_dna["shots"]
    out_shots = output_dna["shots"]
    pairs = _align_shots(ref_shots, out_shots)

    per_shot_report = []
    for ref, out in pairs:
        if out is None:
            per_shot_report.append({"index_ref": ref["index"], "index_out": None, "unmatched": True})
            continue

        d_delta = out["duration"] - ref["duration"]

        if ref["zoom"]["has_zoom"]:
            zoom_match = out["zoom"]["has_zoom"] and (ref["zoom"]["direction"] == out["zoom"]["direction"])
        else:
            zoom_match = not out["zoom"]["has_zoom"]

        transition_match = ref["transition_to_next"] == out["transition_to_next"]

        rm, om = ref.get("camera_motion"), out.get("camera_motion")
        motion_match = None
        if rm and om and "motion_type" in rm and "motion_type" in om:
            if rm.get("confidence", 0) >= 0.4 and om.get("confidence", 0) >= 0.4:
                motion_match = rm["motion_type"] == om["motion_type"]

        # Speed Ramp real (src/speed_ramp.py): só entra em jogo em planos
        # onde a referência detectou uma curva REAL (não "flat") — para
        # planos flat, não há nada a "bater certo" a este respeito, por
        # isso fica de fora da agregação (None) em vez de contar como
        # match ou erro.
        rs, os_ = ref.get("speed_curve"), out.get("speed_curve")
        speed_curve_match = None
        ref_shape = rs.get("shape") if rs and "error" not in rs else None
        out_shape = os_.get("shape") if os_ and "error" not in os_ else None
        if ref_shape and ref_shape != "flat":
            speed_curve_match = (out_shape == ref_shape)

        per_shot_report.append({
            "index_ref": ref["index"], "index_out": out["index"],
            "duration_delta": round(d_delta, 3),
            "zoom_match": bool(zoom_match),
            "transition_match": bool(transition_match),
            "motion_match": motion_match,
            "color_delta": _color_delta(ref.get("color"), out.get("color")),
            "speed_curve_match": speed_curve_match,
            "source": "heuristic",
        })

    ai_report = _run_ai_overlay(reference_dna, output_dna, pairs, per_shot_report)

    # --- Agregação — sempre a partir de per_shot_report (única fonte de
    # verdade), para que a substituição por IA (se houver) se reflita
    # automaticamente aqui sem duplicar lógica. ---
    matched = [e for e in per_shot_report if not e.get("unmatched")]
    n_ref, n_out = len(ref_shots), len(out_shots)
    n_aligned = len(matched)

    duration_deltas = [e["duration_delta"] for e in matched]
    zoom_matches = [e["zoom_match"] for e in matched]
    transition_matches = [e["transition_match"] for e in matched]
    motion_matches = [e["motion_match"] for e in matched if e["motion_match"] is not None]
    speed_curve_matches = [e["speed_curve_match"] for e in matched if e.get("speed_curve_match") is not None]
    color_deltas = [e["color_delta"] for e in matched if e.get("color_delta")]
    pacing_ai_scores = [e["pacing_ai_score"] for e in matched if "pacing_ai_score" in e]
    color_ai_matches = [e["color_ai_match"] for e in matched if "color_ai_match" in e]

    duration_delta_mean = float(np.mean(duration_deltas)) if duration_deltas else 0.0
    duration_delta_abs_mean = float(np.mean(np.abs(duration_deltas))) if duration_deltas else 0.0
    avg_ref_duration = reference_dna.get("style_signature", {}).get("avg_shot_duration") or 1.0

    zoom_match_ratio = float(np.mean(zoom_matches)) if zoom_matches else 1.0
    transition_match_ratio = float(np.mean(transition_matches)) if transition_matches else 1.0
    motion_match_ratio = float(np.mean(motion_matches)) if motion_matches else None
    # None (não 1.0) quando não há nenhum plano com curva real detectada —
    # sinaliza "não avaliado" em vez de fingir um score perfeito; scores["speed_curve"]
    # trata isso como neutro (ver abaixo), não como "bateu tudo certo".
    speed_curve_match_ratio = float(np.mean(speed_curve_matches)) if speed_curve_matches else None

    if color_deltas:
        brightness_delta_mean = float(np.mean([c["brightness"] for c in color_deltas]))
        contrast_delta_mean = float(np.mean([c["contrast"] for c in color_deltas]))
        saturation_delta_mean = float(np.mean([c["saturation"] for c in color_deltas]))
    else:
        brightness_delta_mean = contrast_delta_mean = saturation_delta_mean = 0.0

    cut_count_delta = n_out - n_ref

    # --- Normalização para scores 0-1 (1 = perfeito) ---
    duration_score = max(0.0, 1.0 - min(1.0, duration_delta_abs_mean / max(avg_ref_duration, 0.1)))
    if pacing_ai_scores:
        # Mistura com o julgamento de ritmo da IA em vez de substituir —
        # o modelo pode ser inconsistente ocasionalmente, não lhe damos
        # 100% do peso mesmo quando disponível.
        duration_score = 0.5 * duration_score + 0.5 * float(np.mean(pacing_ai_scores))

    cut_count_score = max(0.0, 1.0 - min(1.0, abs(cut_count_delta) / max(n_ref, 1)))
    zoom_score = zoom_match_ratio
    transition_score = transition_match_ratio
    color_score = max(0.0, 1.0 - min(1.0, (
        brightness_delta_mean / 60.0 + contrast_delta_mean / 60.0 + saturation_delta_mean / 60.0
    ) / 3.0))
    if color_ai_matches:
        color_score = 0.5 * color_score + 0.5 * float(np.mean(color_ai_matches))

    motion_score = motion_match_ratio if motion_match_ratio is not None else duration_score
    # Neutro (1.0) quando não há planos com curva real para avaliar —
    # não penaliza nem beneficia o score total nesse caso.
    speed_curve_score = speed_curve_match_ratio if speed_curve_match_ratio is not None else 1.0

    scores = {
        "duration": duration_score,
        "cut_count": cut_count_score,
        "zoom": zoom_score,
        "transition": transition_score,
        "color": color_score,
        "camera_motion": motion_score,
        "speed_curve": speed_curve_score,
    }
    total_weight = sum(weights.get(k, 0) for k in scores) or 1.0
    similarity_score = sum(scores[k] * weights.get(k, 0) for k in scores) / total_weight

    return {
        "n_shots_reference": n_ref,
        "n_shots_output": n_out,
        "n_aligned": n_aligned,
        "cut_count_delta": cut_count_delta,
        "duration_delta_mean": round(duration_delta_mean, 3),
        "duration_delta_abs_mean": round(duration_delta_abs_mean, 3),
        "zoom_match_ratio": round(zoom_match_ratio, 3),
        "transition_match_ratio": round(transition_match_ratio, 3),
        "camera_motion_match_ratio": round(motion_match_ratio, 3) if motion_match_ratio is not None else None,
        "speed_curve_match_ratio": round(speed_curve_match_ratio, 3) if speed_curve_match_ratio is not None else None,
        "color_delta": {
            "brightness": round(brightness_delta_mean, 2),
            "contrast": round(contrast_delta_mean, 2),
            "saturation": round(saturation_delta_mean, 2),
        },
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "similarity_score": round(float(similarity_score), 4),
        "ai_analyzer": ai_report,
        "per_shot": per_shot_report,
    }


def suggest_adjustments(diff: dict) -> dict:
    """
    Regras deterministas e limitadas (com clamps) que mapeiam as
    métricas do diff para novos valores de config. Só ajusta parâmetros
    de RENDER (ver docstring do módulo) — nunca a estrutura de cortes.
    Devolve {"changes": {nome_config: novo_valor}, "notes": [str, ...]}.
    """
    changes = {}
    notes = []

    cur_transition = getattr(config, "TRANSITION_DURATION", 0.3)
    cur_zoom_factor = getattr(config, "VIDEO_ZOOM_FACTOR", 1.2)
    cur_color_intensity = getattr(config, "COLOR_MATCH_INTENSITY", 1.0)
    use_color = getattr(config, "USE_COLOR_MATCHING", False)
    min_transition = getattr(config, "REFINE_MIN_TRANSITION_DURATION", 0.05)
    max_zoom_factor = getattr(config, "REFINE_MAX_ZOOM_FACTOR", 2.0)

    # 1) Contagem de cortes: se o output mostra MENOS cortes distintos do
    # que a referência, é provável que as transições (xfade) estejam a
    # "engolir" cortes próximos — encurta-las.
    if diff["n_shots_reference"] > 0 and diff["cut_count_delta"] < 0:
        deficit_ratio = abs(diff["cut_count_delta"]) / diff["n_shots_reference"]
        if deficit_ratio > 0.1:
            new_val = max(min_transition, round(cur_transition * 0.7, 3))
            if abs(new_val - cur_transition) > 1e-4:
                changes["TRANSITION_DURATION"] = new_val
                notes.append(
                    f"TRANSITION_DURATION {cur_transition:.2f}s -> {new_val:.2f}s: o render mostrou "
                    f"{abs(diff['cut_count_delta'])} corte(s) a menos que a referência "
                    f"({diff['n_shots_reference']} esperados, {diff['n_shots_output']} detectados na "
                    f"segunda análise) — provável sinal de transições longas demais a fundir cortes próximos."
                )

    # 2) Zoom: direção/presença não bateu certo com o suficiente dos planos.
    if diff["zoom_match_ratio"] < 0.7:
        new_val = min(max_zoom_factor, round(cur_zoom_factor * 1.15, 3))
        if abs(new_val - cur_zoom_factor) > 1e-4:
            changes["VIDEO_ZOOM_FACTOR"] = new_val
            notes.append(
                f"VIDEO_ZOOM_FACTOR {cur_zoom_factor:.2f} -> {new_val:.2f}: só "
                f"{diff['zoom_match_ratio'] * 100:.0f}% dos planos bateram zoom com a referência na "
                f"segunda análise — zoom provavelmente subtil demais para ser detectado como tal."
            )

    # 3) Cor: diferença agregada grande -> liga ou reforça color matching.
    color_delta_total = sum(diff["color_delta"].values())
    if color_delta_total > 25:
        if not use_color:
            changes["USE_COLOR_MATCHING"] = True
            notes.append(
                f"USE_COLOR_MATCHING ligado: diferença de cor média (brilho+contraste+saturação) "
                f"de {color_delta_total:.1f} face à referência."
            )
        else:
            new_val = min(1.0, round(cur_color_intensity + 0.15, 3))
            if abs(new_val - cur_color_intensity) > 1e-4:
                changes["COLOR_MATCH_INTENSITY"] = new_val
                notes.append(
                    f"COLOR_MATCH_INTENSITY {cur_color_intensity:.2f} -> {new_val:.2f}: diferença de "
                    f"cor ainda alta ({color_delta_total:.1f}) com color matching já ligado."
                )

    # 4) Movimento de câmera: só DIAGNÓSTICO por agora — o renderer ainda
    # não reproduz pan/rotação da referência (só o zoom Ken Burns), por
    # isso não há parâmetro de render para ajustar aqui. Fica registado
    # no relatório como limitação conhecida (ver README).
    cm_ratio = diff.get("camera_motion_match_ratio")
    if cm_ratio is not None and cm_ratio < 0.5:
        notes.append(
            f"Movimento de câmera só bateu certo em {cm_ratio * 100:.0f}% dos planos comparáveis "
            f"— pan/rotação da referência ainda não são reproduzidos pelo renderer (só o zoom é). "
            f"Sem ajuste automático possível para isto nesta versão."
        )

    # 5) Speed Ramp: também só DIAGNÓSTICO — quando a forma da curva não
    # bate certo, a causa mais comum é footage insuficiente do clip do
    # utilizador para cobrir a curva completa (o renderer já cai para
    # "sem ramp" nesse caso, ver aviso "[Speed Ramp] ... a saltar" no
    # log), não um parâmetro de render mal calibrado — por isso não há
    # ajuste numérico automático aqui.
    sc_ratio = diff.get("speed_curve_match_ratio")
    if sc_ratio is not None and sc_ratio < 0.7:
        notes.append(
            f"Curva de velocidade só bateu certo em {sc_ratio * 100:.0f}% dos planos com Speed Ramp "
            f"aplicável — normalmente sinal de que o clip do utilizador não tinha footage suficiente a "
            f"partir do in_point escolhido para reproduzir a curva completa (ver avisos '[Speed Ramp] "
            f"... a saltar' no log do render). Sem ajuste automático de render possível para isto além "
            f"de trocar o clip de origem ou o in_point."
        )

    return {"changes": changes, "notes": notes}


def refine_and_render(
    edl: list,
    reference_dna: dict,
    reference_path: str,
    output_filename_base: str = "video_final",
    music_path: str = None,
    max_iterations: int = None,
    improvement_threshold: float = None,
    target_score: float = None,
    report_dir: str = None,
) -> tuple:
    """
    Orquestra o ciclo "gerar -> analisar -> comparar -> ajustar ->
    renderizar de novo" (ver docstring do módulo para o que é ajustado).

    Devolve (best_output_path, history) — history é uma lista de dicts
    {"iteration", "output_path", "score", "report_path"}, uma entrada
    por iteração efetivamente renderizada (inclui a iteração 0 = render
    "normal" sem ajustes, sempre executada).
    """
    max_iterations = max_iterations if max_iterations is not None else getattr(config, "MAX_REFINE_ITERATIONS", 2)
    improvement_threshold = (
        improvement_threshold if improvement_threshold is not None
        else getattr(config, "REFINE_IMPROVEMENT_THRESHOLD", 0.01)
    )
    target_score = target_score if target_score is not None else getattr(config, "REFINE_TARGET_SCORE", 0.92)
    report_dir = report_dir or config.DIR_TEMP
    os.makedirs(report_dir, exist_ok=True)

    history = []
    best_score, best_path = -1.0, None
    prev_score = None

    for it in range(0, max_iterations + 1):
        filename = f"{output_filename_base}.mp4" if it == 0 else f"{output_filename_base}_v{it}.mp4"
        print(f"  [Refinamento] Iteração {it}: renderizando ({filename})...")
        output_path = renderer.render(edl, filename, music_path=music_path, reference_path=reference_path)

        print(f"  [Refinamento] Iteração {it}: segunda análise (comparando com a referência)...")
        output_dna = edit_dna.build_edit_dna(
            output_path, beat_source=None, label=f"output_v{it}",
            analyze_subject=False, analyze_overlays=False,
        )
        diff = compare_edit_dna(reference_dna, output_dna)
        score = diff["similarity_score"]
        sc_ratio = diff.get("speed_curve_match_ratio")
        sc_txt = f"{sc_ratio:.2f}" if sc_ratio is not None else "n/a"
        print(
            f"  [Refinamento] Iteração {it}: similarity_score={score:.3f} "
            f"(cortes ref={diff['n_shots_reference']} vs out={diff['n_shots_output']}, "
            f"zoom={diff['zoom_match_ratio']:.2f}, transição={diff['transition_match_ratio']:.2f}, "
            f"cor Δ={sum(diff['color_delta'].values()):.1f}, speed_curve={sc_txt})"
        )
        ai_rep = diff.get("ai_analyzer")
        if ai_rep and ai_rep.get("available"):
            if "error" in ai_rep:
                print(f"    [AI Analyzer] falhou nesta iteração: {ai_rep['error']}")
            else:
                print(
                    f"    [AI Analyzer] {ai_rep['n_shots_judged']} plano(s) julgado(s) por IA "
                    f"({ai_rep['n_shots_failed']} falharam)."
                )
                for note in ai_rep.get("notes", [])[:5]:
                    print(f"      · {note}")

        report_path = os.path.join(report_dir, f"refine_iteration_{it}.json")
        edit_dna.save_edit_dna({"iteration": it, "output_path": output_path, "diff": diff}, report_path)
        history.append({"iteration": it, "output_path": output_path, "score": score, "report_path": report_path})

        if score > best_score:
            best_score, best_path = score, output_path

        if it >= max_iterations:
            print(f"  [Refinamento] Nº máximo de iterações atingido ({max_iterations}).")
            break
        if score >= target_score:
            print(f"  [Refinamento] Score-alvo atingido ({score:.3f} >= {target_score}) — parando.")
            break
        if prev_score is not None and (score - prev_score) < improvement_threshold:
            print(f"  [Refinamento] Melhoria insuficiente (Δ={score - prev_score:.4f}) — parando.")
            break

        adjustments = suggest_adjustments(diff)
        if not adjustments["changes"]:
            print("  [Refinamento] Nenhum ajuste automático aplicável a este diff — parando.")
            break

        for key, value in adjustments["changes"].items():
            old = getattr(config, key, None)
            setattr(config, key, value)
        for note in adjustments["notes"]:
            print(f"    · {note}")

        prev_score = score

    print(f"  [Refinamento] Melhor resultado: {best_path} (similarity_score={best_score:.3f})")
    return best_path, history


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compara a Edit DNA de dois vídeos já analisados/guardados.")
    parser.add_argument("reference_dna_json")
    parser.add_argument("output_dna_json")
    args = parser.parse_args()

    ref_dna = edit_dna.load_edit_dna(args.reference_dna_json)
    out_dna = edit_dna.load_edit_dna(args.output_dna_json)
    diff = compare_edit_dna(ref_dna, out_dna)
    print(json.dumps(diff, indent=2, ensure_ascii=False))
