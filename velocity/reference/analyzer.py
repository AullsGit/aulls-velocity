"""Reference Analyzer: vídeo de referência -> EditDNA (Etapa 5-7 do plano)."""
from __future__ import annotations

import numpy as np

from velocity.io.frames import load_frames
from velocity.io.probe import probe
from velocity.reference import audio as audio_mod
from velocity.reference import shot_features as sf
from velocity.reference.effects_detect import build_look, detect_effects
from velocity.reference.vibe import build_vibe
from velocity.reference import text_detect
from velocity.reference.segmenter import SegmenterParams, segment
from velocity.reference.signals import compute_signals, crop_letterbox, detect_letterbox
from velocity.schemas.edit_dna import AudioDNA, EditDNA, Shot, Signature

FPS = 30.0
ZOOM_ACTIVE = 0.06


def _signature(shots: list[Shot], fx_events, fps: float, duration_s: float) -> Signature:
    durs = np.array([s.duration_s for s in shots])
    trans = [s.transition_in for s in shots if s.transition_in]
    counts: dict[str, int] = {}
    for t in trans:
        counts[t.type] = counts.get(t.type, 0) + 1
    n_fx = sum(v for k, v in counts.items() if k != "cut")
    en = np.array([s.motion.energy_mean for s in shots])
    return Signature(
        n_shots=len(shots),
        cuts_per_second=round(len(trans) / duration_s, 3) if duration_s else 0.0,
        median_shot_s=round(float(np.median(durs)), 3),
        shot_s_p10=round(float(np.percentile(durs, 10)), 3),
        shot_s_p90=round(float(np.percentile(durs, 90)), 3),
        transition_counts=counts,
        transition_ratio=round(n_fx / len(trans), 3) if trans else 0.0,
        motion_energy_mean=round(float(en.mean()), 5),
        motion_energy_cv=round(float(en.std() / en.mean()), 3) if en.mean() > 1e-9 else 0.0,
        zoom_active_ratio=round(float(np.mean([abs(s.motion.zoom_total) >= ZOOM_ACTIVE for s in shots])), 3),
        speed_dynamic_ratio=round(float(np.mean([s.speed.shape not in ("flat", "unknown") for s in shots])), 3),
        frozen_ratio_mean=round(float(np.mean([s.speed.frozen_ratio for s in shots])), 3),
        fx_events_per_second=round(len(fx_events) / duration_s, 3) if duration_s else 0.0,
    )


def analyze_reference(path: str, params: SegmenterParams | None = None, with_audio: bool = True, with_text: bool = True) -> EditDNA:
    info = probe(path)
    fr = load_frames(path, fps=FPS, width=192)
    lb = detect_letterbox(fr.rgb)
    rgb = crop_letterbox(fr.rgb, lb)
    sig = compute_signals(rgb, fr.fps)
    seg = segment(rgb, sig, params)
    n = sig.n
    energy_ref = float(np.percentile(sig.flow_mag[1:], 95)) if n > 2 else 1.0

    shots: list[Shot] = []
    trans = seg.transitions
    b = seg.boundaries
    for i in range(len(b) - 1):
        s, e = b[i], b[i + 1]
        t_in = trans[i - 1] if i > 0 else None
        t_next = trans[i] if i < len(trans) else None
        a_, b_ = sf.stable_range(s, e, (t_in.start_frame, t_in.end_frame) if t_in else None, t_next.start_frame if t_next else None)
        shots.append(Shot(
            index=i, start_frame=s, end_frame=e, duration_s=round((e - s) / fr.fps, 3), transition_in=t_in,
            motion=sf.motion_profile(sig, a_, b_, energy_ref), speed=sf.speed_profile(sig, a_, b_),
            appearance=sf.appearance(rgb, sig, a_, b_),
        ))

    duration_s = n / fr.fps
    audio = audio_mod.analyze_audio(path, seg.transitions, fr.fps, duration_s) if (with_audio and info.has_audio) else AudioDNA(has_audio=False)

    beats = audio.beats if audio.has_audio else []
    tracks, fx_mask, contrast = detect_effects(rgb, sig, seg.transitions, seg.fx_events, beats, fr.fps)
    look = build_look(sig, fx_mask, contrast, tracks, seg.transitions, duration_s)
    sig_tmp = _signature(shots, seg.fx_events, fr.fps, duration_s)
    vibe = build_vibe(sig, fr.fps, duration_s, seg.transitions, seg.fx_events, tracks, beats, audio.cut_beat_alignment, audio.chance_alignment, sig_tmp.zoom_active_ratio)

    text_events = text_detect.detect_text(path, beats) if with_text else []

    warnings = []
    if abs(info.aspect - 9 / 16) > 0.06:
        warnings.append(f"aspecto {info.width}x{info.height} ({info.orientation}) difere de 9:16; análise ok, saída será 9:16")
    if lb.top_frac or lb.bottom_frac or lb.left_frac or lb.right_frac:
        warnings.append(f"letterbox removido (topo/baixo {lb.top_frac:.2f}/{lb.bottom_frac:.2f}, esq/dir {lb.left_frac:.2f}/{lb.right_frac:.2f})")
    if duration_s < 20:
        warnings.append(f"referência curta ({duration_s:.1f}s); alvo do produto é 25-30 s")
    if info.is_vfr:
        warnings.append("VFR normalizado para 30 fps")

    return EditDNA(
        source_path=path, width=info.width, height=info.height, fps=fr.fps, n_frames=n, duration_s=round(duration_s, 3),
        letterbox=lb, shots=shots, fx_events=seg.fx_events, effects=tracks, look=look, vibe=vibe, text_events=text_events, audio=audio,
        signature=_signature(shots, seg.fx_events, fr.fps, duration_s), warnings=warnings,
    )
