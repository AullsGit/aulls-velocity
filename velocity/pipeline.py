"""Pipeline de alto nível: referência + clips (+ música/texto) -> vídeo final + relatório.

Extraído de cli.cmd_make para ser reutilizável por dois consumidores:
  1. a CLI (`python -m velocity make`), que continua a existir tal e qual;
  2. o Studio (servidor local, ver studio/server.py), que chama isto diretamente NO MESMO
     processo — evita repagar o arranque de modelos (EasyOCR, segmentador de pessoas) a cada
     pedido, o maior custo fixo por geração nesta máquina.

`log(msg: str)` é chamado a cada passo relevante; o Studio passa uma função que acumula as
linhas por job para a interface mostrar progresso em tempo real.
"""
from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from velocity.detector.velocity_detector import Detection, detect
from velocity.evaluate.effects_fidelity import compare_effects
from velocity.evaluate.metrics import compare, compare_plan
from velocity.evaluate.vibe_fidelity import compare_vibe
from velocity.matching.engine import MatchWeights, match
from velocity.matching.extend import extend_reference
from velocity.matching.plan import build_timeline
from velocity.media.analyzer import analyze_clip
from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render

# Abaixo deste valor de fidelidade (medida por `evaluate.metrics.compare`), vale a pena pagar
# uma 2ª renderização completa para tentar melhorar; acima, o ganho é tipicamente pequeno e não
# compensa o custo (uma re-renderização demora tanto como a 1ª — ver profiling em docs/STATUS.md).
GOOD_ENOUGH_FIDELITY = 0.85

PROFILES = {
    "fast": {"size": (540, 960), "crf": 21, "fx_iters": 0, "sr": False, "preset": "veryfast"},
    "standard": {"size": (720, 1280), "crf": 19, "fx_iters": 1, "sr": False, "preset": "fast"},
    "hd": {"size": (1080, 1920), "crf": 16, "fx_iters": 1, "sr": False, "preset": "medium"},
}


@dataclass
class MakeParams:
    ref: str
    clips: list[str]
    out: str
    music: str | None = None
    audio_ref: bool = True  # sem --music, usa o áudio da referência (loop automático se estendido)
    profile: str = "standard"
    size: tuple[int, int] | None = None  # None = usa o perfil
    crf: int | None = None
    preset: str | None = None
    fx_iters: int | None = None
    sr: bool | None = None
    zoom_gain: float = 1.0
    tone: float = 0.85
    denoise: float = 0.5
    subject_enhance: bool = False
    no_fx: bool = False
    force: bool = False
    text: list[str] | None = None
    target_min: float = 25.0
    target_max: float = 31.0
    no_extend: bool = False
    report: str | None = None
    dna: object | None = None  # EditDNA já analisada (ex.: no passo "análise" da app) — evita repetir a análise


@dataclass
class MakeResult:
    ok: bool
    rejected: bool = False  # referência não passou no detetor (não é um erro técnico)
    out_path: str | None = None
    detector: Detection | None = None
    duration_s: float | None = None
    fidelity: float | None = None
    vibe_score: float | None = None
    report: dict = field(default_factory=dict)
    error: str | None = None
    timeline: object | None = None  # Timeline final (editável depois, sem nova análise)
    timings: dict = field(default_factory=dict)  # segundos por etapa


def _resolve(p: MakeParams) -> MakeParams:
    prof = PROFILES.get(p.profile, PROFILES["standard"])
    if p.size is None:
        p.size = prof["size"]
    if p.crf is None:
        p.crf = prof["crf"]
    if p.preset is None:
        p.preset = prof["preset"]
    if p.fx_iters is None:
        p.fx_iters = prof["fx_iters"]
    if p.sr is None:
        p.sr = prof["sr"]
    return p


def run_make(p: MakeParams, log=print, progress=None) -> MakeResult:
    """`progress(stage, fraction)` (opcional) recebe as etapas visíveis ao utilizador:
    analyzing_reference, preparing_edit, rendering, applying_effects, finalizing."""
    t0 = time.time()
    tlog = lambda m: log(f"[{time.time() - t0:5.1f}s] {m}")  # noqa: E731
    timings: dict[str, float] = {}
    mark = [time.time()]

    def stage(name: str, frac: float):
        if progress:
            progress(name, round(min(max(frac, 0.0), 1.0), 3))

    def lap(name: str):
        now = time.time()
        timings[name] = round(timings.get(name, 0.0) + now - mark[0], 2)
        mark[0] = now

    p = _resolve(p)

    stage("analyzing_reference", 0.05)
    if p.dna is not None:
        dna = p.dna
        dna.source_path = p.ref
    else:
        dna = analyze_reference(p.ref)
    det = detect(dna)
    lap("reference_analysis")
    tlog(f"referência: {dna.signature.n_shots} planos, {dna.duration_s:.1f}s | Velocity={det.is_velocity} (score {det.score}) {det.reasons[:1]}")
    if not det.is_velocity and not p.force:
        tlog("Referência NÃO parece um Velocity suportado. Repita com force=True para continuar mesmo assim.")
        return MakeResult(ok=False, rejected=True, detector=det, error="referência não reconhecida como Velocity")

    if p.no_extend:
        dna_ext, ext_info = dna, {"acao": "desligado (no_extend)"}
    else:
        dna_ext, ext_info = extend_reference(dna, target_min=p.target_min, target_max=p.target_max)
    tlog(f"duração: {ext_info}")
    stage("preparing_edit", 0.15)

    locator = None
    if p.subject_enhance:
        from velocity.media.segment import PersonSegmenter
        locator = PersonSegmenter()
        tlog(f"segmentador de pessoas em {locator.device} (primeira análise de cada clip fica em cache)")
    if locator is None and len(p.clips) > 1:
        # Cada clip é decodificado/analisado de forma independente e o custo é dominado por
        # chamadas ao ffmpeg (processo à parte, liberta o GIL) — paralelizar aqui dá um ganho
        # real, medido, sem tocar em qualidade. Só quando NÃO há segmentador partilhado
        # (subject_enhance), cuja segurança em threads não está verificada.
        with ThreadPoolExecutor(max_workers=min(len(p.clips), os.cpu_count() or 4)) as ex:
            clips = list(ex.map(lambda c: analyze_clip(c, locator), p.clips))
    else:
        clips = [analyze_clip(c, locator) for c in p.clips]
    tlog(f"material: {len(clips)} clips, {sum(c.duration_s for c in clips):.1f}s no total")
    lap("clip_analysis")
    stage("preparing_edit", 0.25)
    if sum(c.duration_s for c in clips) < 0.6 * dna_ext.duration_s:
        tlog(f"  [aviso] material do utilizador ({sum(c.duration_s for c in clips):.1f}s) é escasso para {dna_ext.duration_s:.1f}s de edição — espera-se reutilização visível de trechos.")

    w = MatchWeights()
    ms = match(dna_ext, clips, w)
    tl, info = build_timeline(dna_ext, clips, ms, size=p.size, music_path=p.music, weights=w, zoom_gain=p.zoom_gain,
                              fx=not p.no_fx, tone=p.tone, subject_enhance=p.subject_enhance, denoise=p.denoise,
                              sr=p.sr, texts=p.text)
    if not p.music and p.audio_ref and dna_ext.audio.has_audio:
        tl.music_path, tl.music_offset_s = p.ref, 0.0
        info = {**info, "audio": "da referência (loop automático)" if ext_info.get("acao") == "repetida" else "da referência"}
    tlog(f"matching feito; música: {info}")
    lap("edit_planning")

    os.makedirs(os.path.dirname(os.path.abspath(p.out)), exist_ok=True)
    masks = {c.path: c.masks for c in clips if c.masks is not None}
    stage("rendering", 0.3)
    render(tl, p.out, crf=p.crf, masks=masks, preset=p.preset,
           progress=lambda i, n: stage("rendering", 0.3 + 0.5 * i / n))
    tlog(f"render concluído -> {p.out}")
    lap("render")

    stage("applying_effects", 0.82)
    out_dna = analyze_reference(p.out, with_audio=bool(tl.music_path), with_text=False)
    fxrep = compare_effects(dna_ext, out_dna)
    pre_fidelity = compare(dna_ext, out_dna)["fidelity"]
    fx_iters = p.fx_iters if pre_fidelity < GOOD_ENOUGH_FIDELITY else 0
    if p.fx_iters and not fx_iters:
        tlog(f"fidelidade já alta ({pre_fidelity:.3f} >= {GOOD_ENOUGH_FIDELITY}) — a saltar a 2ª renderização (~metade do tempo total)")
    for it in range(fx_iters):
        changes = []
        sug = fxrep["suggested_fx_scale"]
        if sug and not p.no_fx and fxrep["invented_ratio"] < 0.2 and tl.fx_scale * sug <= 1.4:
            tl.fx_scale = round(tl.fx_scale * sug, 3)
            changes.append(f"fx_scale={tl.fx_scale}")
        lk_r, lk_o = dna_ext.look, out_dna.look
        if lk_r and lk_o and p.tone > 0:
            if abs(lk_o.luma - lk_r.luma) > 10 and 5 < lk_o.luma < 250 and 5 < lk_r.luma < 250:
                tl.post_gamma = round(min(max(tl.post_gamma * min(max(math.log(lk_r.luma / 255) / math.log(lk_o.luma / 255), 0.7), 1.25), 0.7), 1.25), 3)
                tl.post_pivot = float(lk_r.luma)
                changes.append(f"post_gamma={tl.post_gamma}")
            if lk_o.saturation > 1 and not 0.85 <= lk_r.saturation / lk_o.saturation <= 1.15:
                tl.post_sat = round(min(max(tl.post_sat * min(max(lk_r.saturation / lk_o.saturation, 0.6), 1.6), 0.6), 1.35), 3)
                changes.append(f"post_sat={tl.post_sat}")
            if lk_o.sharpness > 1 and lk_r.sharpness > 1 and lk_o.sharpness / lk_r.sharpness < 0.75:
                tl.post_sharp = round(min(tl.post_sharp + 0.5 * (1.0 - lk_o.sharpness / lk_r.sharpness), 0.9), 3)
                changes.append(f"post_sharp={tl.post_sharp}")
            if lk_o.contrast > 1 and not 0.85 <= lk_r.contrast / lk_o.contrast <= 1.15:
                tl.post_contrast = round(min(max(tl.post_contrast * min(max(lk_r.contrast / lk_o.contrast, 0.6), 1.5), 0.75), 1.3), 3)
                changes.append(f"post_contrast={tl.post_contrast}")
        vf_ = compare_vibe(dna_ext, out_dna)
        if vf_.get("over_effected") and not p.no_fx and tl.fx_scale > 0.5:
            tl.fx_scale = round(max(0.5, tl.fx_scale * 0.75), 3)
            tl.effects = [e_ for e_ in tl.effects if e_.intensity >= 0.3 or e_.type in ("zoom_punch",)]
            changes.append(f"vibe: exagerou -> fx_scale={tl.fx_scale}, efeitos fracos removidos")
        if not changes:
            break
        tlog(f"protetor de fidelidade (iteração {it + 1}): {', '.join(changes)}; a re-renderizar")
        render(tl, p.out, crf=p.crf, masks=masks, preset=p.preset,
               progress=lambda i, n: stage("applying_effects", 0.82 + 0.12 * i / n))
        out_dna = analyze_reference(p.out, with_audio=bool(tl.music_path), with_text=False)
        fxrep = compare_effects(dna_ext, out_dna)
    lap("fidelity_check")
    stage("finalizing", 0.95)

    tlog(f"efeitos: {fxrep['types']} | inventados={fxrep['invented']} | ok={fxrep['ok']} {fxrep['reasons']}")
    vibe_rep = compare_vibe(dna_ext, out_dna)
    tlog(f"vibe: score={vibe_rep.get('score')} agressividade={vibe_rep.get('aggressiveness')}")
    rep = compare(dna_ext, out_dna)
    rep["execution"] = compare_plan([sh.start_frame for sh in tl.shots[1:]], out_dna)
    tlog(f"fidelidade (ref vs output re-analisado): {rep['fidelity']}  componentes={rep['components']}")
    tlog(f"duração final: {out_dna.duration_s:.1f}s")

    full_report = {
        "detector": {"is_velocity": det.is_velocity, "score": det.score, "subscores": det.subscores, "reasons": det.reasons},
        "extend": ext_info, "retime": info, "eval": rep, "effects_eval": fxrep, "vibe_eval": vibe_rep,
        "decisions": info.get("decisions", []), "fx_scale": tl.fx_scale,
        "post": [tl.post_gamma, tl.post_sat, tl.post_contrast, tl.post_sharp], "duration_s": out_dna.duration_s,
    }
    timings["total"] = round(time.time() - t0, 2)
    full_report["timings_s"] = timings
    tlog("tempos por etapa: " + "  ".join(f"[{k.upper()}] {v:.1f}s" for k, v in timings.items()))
    if p.report:
        import json
        json.dump(full_report, open(p.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    return MakeResult(ok=True, out_path=p.out, detector=det, duration_s=out_dna.duration_s,
                      fidelity=rep["fidelity"], vibe_score=vibe_rep.get("score"), report=full_report,
                      timeline=tl, timings=timings)
