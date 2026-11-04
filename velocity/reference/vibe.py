"""Análise da 'vibe' da referência: energia no tempo, impactos, descanso, arco, densidades e relação com a música.

Tudo é MEDIDO nos sinais (fluxo óptico, transições, faixas de efeito) — nenhum valor é assumido por estilo.
A curva de energia mistura movimento contínuo (fluxo) com 'eventos' (transições e efeitos com peso próprio),
porque numa edição Velocity a intensidade sentida vem dos dois.
"""
from __future__ import annotations

import numpy as np

from velocity.reference.signals import Signals
from velocity.schemas.edit_dna import EffectTrack, FxEvent, Impact, Transition, VibeProfile

ENERGY_HZ = 4.0
_TRANS_W = {"flash": 1.0, "glitch": 0.9, "blur": 0.8, "dip_black": 0.4, "cut": 0.45, "dissolve": 0.15}
_TRACK_W = {"zoom_punch": 1.0, "shake": 0.7, "rgb_split": 0.6, "exposure_pulse": 0.3, "contrast_pulse": 0.3, "sat_pulse": 0.25}


def _ramp(x, lo, hi):
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def _smooth(x, w):
    if w <= 1 or len(x) < 2:
        return x
    return np.convolve(np.pad(x, (w // 2, w - 1 - w // 2), mode="edge"), np.ones(w) / w, mode="valid")


def _gauss_add(arr, center, sigma, amp):
    lo, hi = max(0, int(center - 4 * sigma)), min(len(arr), int(center + 4 * sigma) + 1)
    x = np.arange(lo, hi)
    arr[lo:hi] += amp * np.exp(-0.5 * ((x - center) / sigma) ** 2)


def energy_series(sig: Signals, transitions: list[Transition], fx: list[FxEvent], tracks: list[EffectTrack]) -> np.ndarray:
    """Energia por frame 0..1: 65% movimento contínuo, 35% eventos (transições/efeitos)."""
    n = sig.n
    mot = _smooth(sig.flow_mag.astype(np.float32), 5)
    ref = float(np.percentile(mot[1:], 95)) if n > 3 else 1.0
    mot_n = np.clip(mot / max(ref, 1e-6), 0, 1.5) / 1.5
    ev = np.zeros(n, np.float32)
    for t in transitions:
        _gauss_add(ev, t.cut_frame, 4.0, _TRANS_W.get(t.type, 0.5) * (0.4 + 0.6 * t.intensity))
    for f in fx:
        _gauss_add(ev, (f.start_frame + f.end_frame) / 2, 3.5, _TRANS_W.get(f.type, 0.5) * (0.4 + 0.6 * f.intensity))
    for k in tracks:
        w = _TRACK_W.get(k.type, 0.3)
        if k.type in ("shake", "rgb_split"):
            ev[k.start_frame:k.end_frame] += w * (0.3 + 0.7 * k.intensity) * 0.6
        else:
            _gauss_add(ev, k.peak_frame, 3.0, w * (0.3 + 0.7 * k.intensity))
    ev_n = np.clip(ev / max(float(np.percentile(ev, 99)), 1e-6), 0, 1) if ev.max() > 0 else ev
    return np.clip(0.65 * mot_n + 0.35 * ev_n, 0, 1)


def _arc(levels: list[float]) -> str:
    e0, e1, e2 = levels
    spread = max(levels) - min(levels)
    if spread < 0.08:
        return "plateau"
    if e0 < e1 < e2 and e2 - e0 > 0.1:
        return "build"
    if e0 > e1 > e2 and e0 - e2 > 0.1:
        return "decay"
    if e1 == max(levels):
        return "peak_middle"
    if e0 == max(levels):
        return "peak_early"
    if e2 == max(levels):
        return "peak_late"
    return "wave"


def build_vibe(sig: Signals, fps: float, duration_s: float, transitions: list[Transition], fx: list[FxEvent],
               tracks: list[EffectTrack], beats: list[float], cut_align: float | None, chance: float | None,
               zoom_active_ratio: float) -> VibeProfile:
    n = sig.n
    e = energy_series(sig, transitions, fx, tracks)
    per = max(1, int(round(fps / ENERGY_HZ)))
    k = n // per
    curve = e[: k * per].reshape(k, per).mean(axis=1) if k else np.array([float(e.mean())])
    curve = np.clip(curve / max(float(curve.max()), 1e-6) * min(1.0, float(curve.max()) * 1.4), 0, 1)  # normaliza sem inflar vídeos calmos
    t_axis = (np.arange(len(curve)) + 0.5) / ENERGY_HZ
    third = max(1, len(curve) // 3)
    levels = [float(curve[:third].mean()), float(curve[third: 2 * third].mean()), float(curve[2 * third:].mean())]

    # impactos = máximos locais fortes, afastados >= 0.6 s
    sm = _smooth(curve, 3)
    thr = max(0.5, float(np.percentile(sm, 70)))
    cand = [i for i in range(1, len(sm) - 1) if sm[i] >= thr and sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1]]
    cand.sort(key=lambda i: -sm[i])
    picked: list[int] = []
    for i in cand:
        if all(abs(t_axis[i] - t_axis[j]) >= 0.6 for j in picked):
            picked.append(i)
    picked = sorted(picked)[:16]
    beats_a = np.array(beats) if beats else np.array([])
    impacts = [Impact(t=round(float(t_axis[i]), 2), strength=round(float(sm[i]), 3),
                      on_beat=(bool(np.min(np.abs(beats_a - t_axis[i])) <= 0.07) if len(beats_a) else None)) for i in picked]

    # descansos = janelas de baixa energia >= 0.5 s
    low = sm < max(0.25, 0.4 * float(sm.max()))
    rests, i = [], 0
    while i < len(low):
        if low[i]:
            j = i
            while j + 1 < len(low) and low[j + 1]:
                j += 1
            if (j - i + 1) / ENERGY_HZ >= 0.5:
                rests.append((round(float(t_axis[i] - 0.125), 2), round(float(t_axis[j] + 0.125), 2)))
            i = j + 1
        else:
            i += 1

    non_cut = [t for t in transitions if t.type != "cut"]
    ndur = max(duration_s, 1e-6)
    styles: dict[str, float] = {}
    for t in transitions:
        key = t.type if t.type != "blur" else f"blur_{t.blur_kind}"
        styles[key] = styles.get(key, 0) + 1
    tot = sum(styles.values()) or 1
    styles = {a: round(b / tot, 3) for a, b in styles.items()}

    eff_events = len(non_cut) + len(fx) + sum(1 for t in tracks if t.type in ("zoom_punch", "shake", "rgb_split") or t.anchor != "free")
    eff_int = [t.intensity for t in non_cut] + [f.intensity for f in fx] + [t.intensity for t in tracks if t.type in ("zoom_punch", "shake", "rgb_split")]
    vocab: dict[str, float] = {}
    for t in transitions:
        vocab[t.type] = vocab.get(t.type, 0) + 1
    for f in fx:
        vocab[f"fx_{f.type}"] = vocab.get(f"fx_{f.type}", 0) + 1
    for t in tracks:
        vocab[t.type] = vocab.get(t.type, 0) + 1
    vocab = {a: round(b / ndur, 3) for a, b in vocab.items()}

    punches = [t for t in tracks if t.type == "zoom_punch"]
    zoom = {"punch_rate": round(len(punches) / ndur, 3),
            "punch_mean": round(float(np.mean([abs(p.magnitude) for p in punches])), 3) if punches else 0.0,
            "punch_in_ratio": round(float(np.mean([p.magnitude > 0 for p in punches])), 3) if punches else 0.0,
            "slow_zoom_ratio": round(zoom_active_ratio, 3)}

    mot_mean = float(sig.flow_mag[1:].mean()) if n > 1 else 0.0
    cps = len(transitions) / ndur
    shake_frac = sum(t.end_frame - t.start_frame for t in tracks if t.type == "shake") / max(n, 1)
    eff_rate = eff_events / ndur
    eff_mean = float(np.mean(eff_int)) if eff_int else 0.0
    # efeitos PESADOS pelo tipo: blur é suave; flash/glitch/corte seco/shake são duros
    hw = {"blur": 0.45, "dip_black": 0.4, "flash": 1.0, "glitch": 1.2, "cut": 0.7, "dissolve": 0.2}
    eff_weighted = (sum(hw.get(t.type, 0.7) for t in transitions if t.type != "cut") + 0.7 * sum(1 for t in transitions if t.type == "cut")
                    + sum(0.8 for _ in fx) + sum(1.0 for t in tracks if t.type in ("zoom_punch", "shake", "rgb_split"))) / ndur
    harsh = (sum(1 for t in transitions if t.type in ("flash", "glitch", "cut")) / len(transitions)) if transitions else 0.0
    aggr = (0.28 * _ramp(mot_mean, 0.0040, 0.0110) + 0.17 * _ramp(eff_weighted, 0.3, 1.6) + 0.13 * _ramp(zoom["punch_rate"], 0.2, 1.5)
            + 0.12 * _ramp(cps, 0.5, 1.4) + 0.08 * _ramp(shake_frac, 0.0, 0.15) + 0.07 * _ramp(eff_mean, 0.2, 0.7)
            + 0.15 * _ramp(harsh, 0.1, 0.6))
    jerk = float(np.mean(np.abs(np.diff(curve)))) if len(curve) > 2 else 0.0
    abrupt = (sum(1 for t in transitions if t.type in ("cut", "glitch")) / max(len(transitions), 1)) if transitions else 0.0
    smooth = 1.0 - float(np.clip(0.5 * _ramp(jerk, 0.03, 0.25) + 0.25 * _ramp(shake_frac, 0.0, 0.15) + 0.25 * abrupt, 0, 1))
    calm = 1.0 - float(np.clip(0.6 * _ramp(mot_mean, 0.002, 0.010) + 0.2 * _ramp(cps, 0.3, 1.2) + 0.2 * _ramp(eff_rate, 0.3, 1.5), 0, 1))
    speed = float(np.clip(0.5 * _ramp(cps, 0.2, 1.4) + 0.5 * _ramp(mot_mean, 0.002, 0.010), 0, 1))

    beat: dict[str, float] = {}
    if cut_align is not None and chance is not None:
        beat["lock"] = round(float(np.clip(cut_align - chance, -1, 1)), 3)
    if len(beats_a):
        bf = np.zeros(n, bool)
        for b in beats_a:
            i = int(round(b * fps))
            bf[max(0, i - 2): min(n, i + 3)] = True
        if bf.any() and (~bf).any():
            beat["energy_on_beat"] = round(float(e[bf].mean() / max(e[~bf].mean(), 1e-6)), 3)
        onb = [im.on_beat for im in impacts if im.on_beat is not None]
        if onb:
            beat["impacts_on_beat"] = round(float(np.mean(onb)), 3)

    desc = ["aggressive" if aggr >= 0.55 else ("soft" if aggr <= 0.40 else "medium"),
            "fast" if speed >= 0.6 else ("slow" if speed <= 0.3 else "moderate"),
            "calm" if calm >= 0.6 else ("intense" if calm <= 0.3 else "balanced"),
            "smooth" if smooth >= 0.7 else ("abrupt" if smooth <= 0.4 else "mixed")]
    if beat.get("lock", 0) >= 0.3:
        desc.append("beat_locked")
    if eff_rate >= 1.0:
        desc.append("effect_driven")
    return VibeProfile(
        aggressiveness=round(float(aggr), 3), smoothness=round(smooth, 3), calm=round(calm, 3),
        intensity=round(float(curve.mean()), 3), speed=round(speed, 3), energy_scale=round(mot_mean, 5),
        energy_hz=ENERGY_HZ, energy_curve=[round(float(v), 3) for v in curve], arc=_arc(levels),
        arc_levels=[round(v, 3) for v in levels], impacts=impacts, rests=rests,
        transition_density=round(cps, 3), transition_style=styles, effect_density=round(eff_rate, 3),
        effect_intensity_mean=round(eff_mean, 3), effect_intensity_p90=round(float(np.percentile(eff_int, 90)), 3) if eff_int else 0.0,
        vocabulary=vocab, zoom=zoom, beat=beat, descriptors=desc,
    )
