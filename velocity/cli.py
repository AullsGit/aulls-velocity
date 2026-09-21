"""Linha de comandos: python -m velocity <analyze|detect|make> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from velocity.detector.velocity_detector import detect
from velocity.evaluate.effects_fidelity import compare_effects
from velocity.evaluate.metrics import compare, compare_plan
from velocity.evaluate.vibe_fidelity import compare_vibe
from velocity.matching.engine import MatchWeights, match
from velocity.matching.plan import build_timeline
from velocity.media.analyzer import analyze_clip
from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render


def _size(s: str):
    w, h = s.lower().split("x")
    return int(w), int(h)


def cmd_analyze(a):
    dna = analyze_reference(a.ref)
    js = dna.model_dump_json(indent=1)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(js)
    else:
        print(js)


def cmd_detect(a):
    dna = analyze_reference(a.ref)
    d = detect(dna)
    print(json.dumps({"is_velocity": d.is_velocity, "score": d.score, "subscores": d.subscores, "reasons": d.reasons}, ensure_ascii=False, indent=1))
    return 0 if d.is_velocity else 2


def cmd_make(a):
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:5.1f}s] {m}", flush=True)
    dna = analyze_reference(a.ref)
    det = detect(dna)
    log(f"referência: {dna.signature.n_shots} planos, {dna.duration_s:.1f}s | Velocity={det.is_velocity} (score {det.score}) {det.reasons[:1]}")
    if not det.is_velocity and not a.force:
        log("Referência NÃO parece um Velocity suportado. Use --force para continuar mesmo assim.")
        return 2
    locator = None
    if a.subject_enhance:
        from velocity.media.segment import PersonSegmenter
        locator = PersonSegmenter()
        log(f"segmentador de pessoas em {locator.device} (primeira análise de cada clip fica em cache)")
    clips = [analyze_clip(p, locator) for p in a.clips]
    log(f"material: {len(clips)} clips, {sum(c.duration_s for c in clips):.1f}s no total")
    w = MatchWeights()
    ms = match(dna, clips, w)
    tl, info = build_timeline(dna, clips, ms, size=_size(a.size), music_path=a.music, weights=w, zoom_gain=a.zoom_gain, fx=not a.no_fx, tone=a.tone, subject_enhance=a.subject_enhance, denoise=a.denoise, sr=a.sr)
    if not a.music and a.audio_ref and dna.audio.has_audio:
        tl.music_path, tl.music_offset_s = a.ref, 0.0  # sem música: usa o áudio da referência (cortes já coincidem com ele)
        info = {**info, "audio": "da referência"}
    log(f"matching feito; música: {info}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    masks = {c.path: c.masks for c in clips if c.masks is not None}
    render(tl, a.out, crf=a.crf, progress=lambda i, n: None, masks=masks)
    log(f"render concluído -> {a.out}")
    out_dna = analyze_reference(a.out, with_audio=bool(tl.music_path))
    fxrep = compare_effects(dna, out_dna)
    # ciclo fechado: aproxima as magnitudes dos efeitos E o look global (brilho/saturação/contraste)
    # do output ao da referência, medidos com o mesmo estimador. No máximo a.fx_iters re-renders.
    import math
    for it in range(a.fx_iters):
        changes = []
        sug = fxrep["suggested_fx_scale"]
        if sug and not a.no_fx and fxrep["invented_ratio"] < 0.2 and tl.fx_scale * sug <= 1.4:
            tl.fx_scale = round(tl.fx_scale * sug, 3)
            changes.append(f"fx_scale={tl.fx_scale}")
        lk_r, lk_o = dna.look, out_dna.look
        if lk_r and lk_o and a.tone > 0:
            if abs(lk_o.luma - lk_r.luma) > 10 and 5 < lk_o.luma < 250 and 5 < lk_r.luma < 250:
                tl.post_gamma = round(min(max(tl.post_gamma * min(max(math.log(lk_r.luma / 255) / math.log(lk_o.luma / 255), 0.7), 1.25), 0.7), 1.25), 3)  # limite ACUMULADO
                tl.post_pivot = float(lk_r.luma)
                changes.append(f"post_gamma={tl.post_gamma}")
            if lk_o.saturation > 1 and not 0.85 <= lk_r.saturation / lk_o.saturation <= 1.15:
                tl.post_sat = round(min(max(tl.post_sat * min(max(lk_r.saturation / lk_o.saturation, 0.6), 1.6), 0.6), 1.35), 3)
                changes.append(f"post_sat={tl.post_sat}")
            if lk_o.sharpness > 1 and lk_r.sharpness > 1 and lk_o.sharpness / lk_r.sharpness < 0.75:  # só sobe se estiver mais mole que a referência
                tl.post_sharp = round(min(tl.post_sharp + 0.5 * (1.0 - lk_o.sharpness / lk_r.sharpness), 0.9), 3)
                changes.append(f"post_sharp={tl.post_sharp}")
            if lk_o.contrast > 1 and not 0.85 <= lk_r.contrast / lk_o.contrast <= 1.15:
                tl.post_contrast = round(min(max(tl.post_contrast * min(max(lk_r.contrast / lk_o.contrast, 0.6), 1.5), 0.75), 1.3), 3)
                changes.append(f"post_contrast={tl.post_contrast}")
        vf_ = compare_vibe(dna, out_dna)
        if vf_.get("over_effected") and not a.no_fx and tl.fx_scale > 0.5:  # EXAGEROU em relação à referência -> reduz
            tl.fx_scale = round(max(0.5, tl.fx_scale * 0.75), 3)
            tl.effects = [e_ for e_ in tl.effects if e_.intensity >= 0.3 or e_.type in ("zoom_punch",)]
            changes.append(f"vibe: exagerou (efeitos {vf_['effect_density_ratio']}x, agressividade {vf_['aggressiveness']['delta']:+.2f}) -> fx_scale={tl.fx_scale}, efeitos fracos removidos")
        if not changes:
            break
        log(f"protetor de fidelidade (iteração {it + 1}): {', '.join(changes)}; a re-renderizar")
        render(tl, a.out, crf=a.crf, progress=lambda i, n: None, masks=masks)
        out_dna = analyze_reference(a.out, with_audio=bool(tl.music_path))
        fxrep = compare_effects(dna, out_dna)
    log(f"efeitos: {fxrep['types']} | inventados={fxrep['invented']} | look={fxrep['look']} | ok={fxrep['ok']} {fxrep['reasons']}")
    vibe_rep = compare_vibe(dna, out_dna)
    log(f"vibe: score={vibe_rep.get('score')} energia(corr)={vibe_rep.get('energy_corr')} impactos={vibe_rep.get('impacts')} agressividade={vibe_rep.get('aggressiveness')} exagerou={vibe_rep.get('over_effected')} aquém={vibe_rep.get('under_effected')}")
    rep = compare(dna, out_dna)
    rep["execution"] = compare_plan([sh.start_frame for sh in tl.shots[1:]], out_dna)
    log(f"execução (cortes planeados presentes no output): {rep['execution']}")
    log(f"fidelidade (ref vs output re-analisado): {rep['fidelity']}  componentes={rep['components']}")
    if a.report:
        json.dump({"detector": det.__dict__, "match": [m.__dict__ | {"speed": None} for m in ms], "retime": info, "eval": rep, "effects_eval": fxrep, "vibe_eval": vibe_rep, "decisions": info.get("decisions", []), "fx_scale": tl.fx_scale, "post": [tl.post_gamma, tl.post_sat, tl.post_contrast, tl.post_sharp]},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="velocity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze"); p.add_argument("ref"); p.add_argument("--out"); p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("detect"); p.add_argument("ref"); p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("make")
    p.add_argument("--ref", required=True); p.add_argument("--clips", nargs="+", required=True)
    p.add_argument("--music"); p.add_argument("--out", required=True)
    p.add_argument("--size", default="540x960"); p.add_argument("--crf", type=int, default=18)
    p.add_argument("--audio-ref", action="store_true", help="sem --music, usa o áudio da referência"); p.add_argument("--zoom-gain", type=float, default=1.0); p.add_argument("--force", action="store_true")
    p.add_argument("--no-fx", action="store_true", help="desliga os efeitos medidos na referência"); p.add_argument("--fx-iters", type=int, default=2); p.add_argument("--sr", action="store_true", help="EXPERIMENTAL: super-resolução nos planos muito ampliados (lenta; melhor com a GPU sem limite de energia)"); p.add_argument("--denoise", type=float, default=0.5, help="redução de ruído temporal 0-1"); p.add_argument("--subject-enhance", action="store_true", help="EXPERIMENTAL: realce local do sujeito por máscara (halos com a máscara atual)"); p.add_argument("--tone", type=float, default=0.85, help="força do ajuste de tom (0-1) ao look da referência")
    p.add_argument("--report"); p.set_defaults(fn=cmd_make)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
