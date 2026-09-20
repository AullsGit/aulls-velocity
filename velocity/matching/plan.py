"""EditDNA + matches (+ música) -> Timeline executável."""
from __future__ import annotations

import numpy as np

from velocity.io.frames import load_audio
from velocity.matching.engine import Match, MatchWeights, reframe
from velocity.matching.fx_plan import plan_effects
from velocity.media.analyzer import ClipProfile
from velocity.reference.audio import ALIGN_TOL_S, SR, _beat_analysis
from velocity.schemas.edit_dna import Curve, EditDNA, Transition
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot

MIN_SHOT = 4
TONE_STRENGTH = 0.85  # 0 = não toca no tom do utilizador; 1 = aproxima totalmente da referência


def tone_for(sh, c: ClipProfile, m: Match, strength: float, ref_sharp: float = 500.0, use_masks: bool = False) -> dict:
    """Ajuste de tom do trecho escolhido para aproximar o plano da referência.

    COM máscara do sujeito: o FUNDO vai para o brilho-alvo (clima da referência) e o SUJEITO nunca fica
    abaixo de um piso visível (é levantado localmente). SEM máscara: gamma global limitado (<=1.25 para
    não esmagar o sujeito). Lição do teste visual: igualar só a luma média transformava a pessoa em silhueta.
    """
    out = {"tone_gamma": 1.0, "tone_sat": 1.0, "subj_gamma": 1.0, "bg_gamma": 1.0, "subj_sharp": 0.0}
    if strength <= 0:
        return out
    win = slice(m.start_frame, m.start_frame + m.need_frames)
    tgt_l = float(np.clip(sh.appearance.mean_luma, 8.0, 247.0))
    cur_s, tgt_s = float(max(c.sat[win].mean(), 5.0)), float(max(sh.appearance.saturation, 5.0))
    out["tone_sat"] = round(float(np.clip((tgt_s / cur_s) ** strength, 0.35, 1.7)), 3)
    have_mask = use_masks and c.masks is not None and c.subj_lum is not None and np.isfinite(c.subj_lum[win]).mean() > 0.6
    if have_mask:
        s_cur = float(np.clip(np.nanmean(c.subj_lum[win]), 8.0, 247.0))
        b_cur = float(np.clip(c.bg_lum[win].mean(), 8.0, 247.0))
        g_bg = np.log(tgt_l / 255.0) / np.log(b_cur / 255.0)
        out["bg_gamma"] = round(float(np.clip(g_bg ** strength, 0.6, 3.2)), 3)
        floor = float(np.clip(0.5 * tgt_l + 35.0, 55.0, 100.0))  # sujeito visível: ~55 em refs escuras, até 100 em claras
        if s_cur < floor:
            out["subj_gamma"] = round(float(np.clip(np.log(floor / 255.0) / np.log(s_cur / 255.0), 0.45, 1.0)), 3)
        out["subj_sharp"] = 0.6 if ref_sharp >= 450 else 0.25
    else:
        cur_l = float(np.clip(c.lum[win].mean(), 8.0, 247.0))
        g = np.log(tgt_l / 255.0) / np.log(cur_l / 255.0)
        out["tone_gamma"] = round(float(np.clip(g ** strength, 0.45, 1.25)), 3)
    return out


def _retime_to_music(dna: EditDNA, music_path: str) -> tuple[list[int], float, dict]:
    """Novos frames de corte para a música, se a referência estiver presa às batidas. Devolve
    (cortes, music_offset_s, info). Sem retime, devolve os cortes originais."""
    orig = dna.cut_frames()
    info = {"retimed": False}
    a = dna.audio
    if not (a.has_audio and a.cut_beat_alignment is not None and a.chance_alignment is not None and len(orig) >= 2):
        info["reason"] = "referência sem áudio/alinhamento mensurável"
        return orig, 0.0, info
    if a.cut_beat_alignment - a.chance_alignment < 0.25:
        info["reason"] = f"cortes da referência não presos às batidas (alinhamento {a.cut_beat_alignment:.2f} vs acaso {a.chance_alignment:.2f})"
        return orig, 0.0, info
    y = load_audio(music_path, SR)
    if y is None or len(y) < SR * 3:
        info["reason"] = "música inválida"
        return orig, 0.0, info
    bpm, mb, _ = _beat_analysis(y)
    tr = dna.transitions()
    if len(mb) < 4 or any(t.beat_index is None for t in tr):
        info["reason"] = "batidas insuficientes"
        return orig, 0.0, info
    rb0 = dna.audio.beats[0] if dna.audio.beats else 0.0
    offset = max(0.0, float(mb[0]) - rb0)
    idx = np.arange(len(mb))
    new_t = np.interp([t.beat_index for t in tr], idx, mb) - offset
    frames = [int(round(x * dna.fps)) for x in new_t]
    ok = all(b - a_ >= MIN_SHOT for a_, b in zip([0] + frames, frames + [dna.n_frames])) and frames[-1] < dna.n_frames - MIN_SHOT
    if not ok:
        info["reason"] = "retime geraria planos demasiado curtos"
        return orig, 0.0, info
    info.update(retimed=True, music_bpm=round(bpm, 1), ref_bpm=a.bpm)
    return frames, offset, info


def build_timeline(dna: EditDNA, clips: list[ClipProfile], matches: list[Match], size=(1080, 1920),
                   music_path: str | None = None, weights: MatchWeights | None = None,
                   zoom_gain: float = 1.0, fx: bool = True, fx_scale: float = 1.0, tone: float = TONE_STRENGTH, subject_enhance: bool = False, denoise: float = 0.0, sr: bool = False) -> tuple[Timeline, dict]:
    w = weights or MatchWeights()
    cuts, offset, info = _retime_to_music(dna, music_path) if music_path else (dna.cut_frames(), 0.0, {"retimed": False})
    bounds = [0] + cuts + [dna.n_frames]
    shots: list[TimelineShot] = []
    for k, sh in enumerate(dna.shots):
        m = matches[k]
        c = clips[m.clip_index]
        center, base = reframe(c, m, w)
        z_ref = np.array(sh.motion.zoom_curve.v) if sh.motion.confidence > 0.2 else np.zeros(2)
        zt = sh.motion.zoom_curve.t if sh.motion.confidence > 0.2 else [0.0, 1.0]
        z_abs = base + (z_ref - z_ref.min())
        t_in = None
        if sh.transition_in is not None:
            t0 = sh.transition_in
            delta = bounds[k] - t0.cut_frame
            t_in = Transition(**{**t0.model_dump(), "start_frame": max(0, t0.start_frame + delta),
                                 "end_frame": min(dna.n_frames, t0.end_frame + delta), "cut_frame": bounds[k]})
        tn = tone_for(sh, c, m, tone, ref_sharp=dna.look.sharpness if dna.look else 500.0, use_masks=subject_enhance)
        pan = (float(np.clip(-sh.motion.pan_x * 0.5, -0.12, 0.12)), float(np.clip(-sh.motion.pan_y * 0.5, -0.12, 0.12)))
        shots.append(TimelineShot(
            index=k, start_frame=bounds[k], end_frame=bounds[k + 1],
            clip=ClipUse(source_path=c.path, src_start_s=m.start_frame / c.fps, speed=m.speed or Curve(t=[0, 1], v=[1, 1]),
                         zoom=Curve(t=list(zt), v=[round(float(v), 4) for v in z_abs]), center=center, pan=pan, **tn),
            transition_in=t_in))
    effects, fx_info = plan_effects(dna, cuts) if fx else ([], {"desligado": True})
    info["fx"] = fx_info
    tl = Timeline(fps=dna.fps, width=size[0], height=size[1], n_frames=dna.n_frames, shots=shots,
                  fx_events=dna.fx_events, effects=effects, fx_scale=fx_scale, music_path=music_path,
                  music_offset_s=offset, zoom_gain=zoom_gain, denoise=denoise, sr=sr)
    return tl, info
