"""Faixas de efeito medidas na referência + perfil de look.

Regra: só registamos o que o sinal mostra (com limiares), e cada faixa guarda a sua âncora
(corte/batida) para o planeador a transferir. Nada aqui inventa efeitos.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.ndimage import median_filter

from velocity.reference.signals import Signals
from velocity.schemas.edit_dna import EffectTrack, FxEvent, LookProfile, Transition


def _ramp(x, lo, hi):
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def _env(x) -> list[float]:
    """Perfil normalizado (pico=1) da grandeza medida ao longo do efeito."""
    x = np.asarray(x, np.float32)
    m = float(x.max()) if len(x) else 0.0
    return [round(float(v), 3) for v in (x / m)] if m > 1e-9 else []


def _ma(x, w):
    return np.convolve(np.pad(x, (w // 2, w // 2), mode="edge"), np.ones(w) / w, mode="valid")[: len(x)]


def _runs(mask: np.ndarray, min_len: int, max_len: int, gap: int = 1):
    """Corridas True [a,b) com fusão de lacunas <= gap e filtro de duração."""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and (mask[j + 1] or mask[j + 1 : j + 2 + gap].any()):
                j += 1
            while j > i and not mask[j]:
                j -= 1
            b = j + 1
            if min_len <= b - i <= max_len:
                out.append((i, b))
            i = b
        else:
            i += 1
    return out


def _mask(n, transitions: list[Transition], fx: list[FxEvent], pad=2):
    m = np.zeros(n, bool)
    for t in transitions:
        m[max(0, t.start_frame - pad): min(n, t.end_frame + pad)] = True
    for f in fx:
        m[max(0, f.start_frame - pad): min(n, f.end_frame + pad)] = True
    return m


def _anchor(peak: int, cuts: list[int], beat_frames: list[int]):
    if cuts:
        k = int(np.argmin([abs(peak - c) for c in cuts]))
        if abs(peak - cuts[k]) <= 10:
            return "cut", k, peak - cuts[k]
    if beat_frames:
        k = int(np.argmin([abs(peak - b) for b in beat_frames]))
        if abs(peak - beat_frames[k]) <= 3:
            return "beat", k, peak - beat_frames[k]
    return "free", None, 0


def _rgb_shift(frame_rgb: np.ndarray):
    r = np.abs(cv2.Sobel(frame_rgb[..., 0].astype(np.float32), cv2.CV_32F, 1, 0))
    b = np.abs(cv2.Sobel(frame_rgb[..., 2].astype(np.float32), cv2.CV_32F, 1, 0))
    (dx, _), resp = cv2.phaseCorrelate(r, b)
    return abs(dx), resp


def detect_effects(rgb: np.ndarray, sig: Signals, transitions: list[Transition], fx: list[FxEvent],
                   beats: list[float], fps: float):
    n = sig.n
    mask = _mask(n, transitions, fx)
    cuts = sorted(t.cut_frame for t in transitions)
    beat_frames = [int(round(b * fps)) for b in beats]
    tracks: list[EffectTrack] = []
    w = rgb.shape[2]

    # --- zoom punch: mudança rápida de escala, fora das transições ---
    z = np.where(mask, 0.0, sig.zoom)
    zs = _ma(z, 3)
    thr = max(0.010, float(np.median(np.abs(zs)) + 4 * np.median(np.abs(zs - np.median(zs)))))
    low = max(0.004, 0.35 * thr)  # histerese: expande a corrida até a taxa de zoom cair
    for sgn in (1, -1):
        for a, b in _runs(sgn * zs > thr, 2, 24, gap=1):
            while a > 0 and sgn * zs[a - 1] > low and not mask[a - 1] and b - a < 30:
                a -= 1
            while b < n and sgn * zs[b] > low and not mask[b] and b - a < 30:
                b += 1
            mag = float(z[a:b].sum())
            if abs(mag) < 0.045:
                continue
            peak = a + int(np.argmax(np.abs(zs[a:b])))
            k, ref, off = _anchor(peak, cuts, beat_frames)
            tracks.append(EffectTrack(type="zoom_punch", start_frame=a, end_frame=b, peak_frame=peak,
                                      magnitude=round(mag, 4), intensity=round(_ramp(abs(mag), 0.045, 0.35), 3),
                                      anchor=k, anchor_ref=ref, anchor_offset=off, confidence=0.7,
                                      envelope=_env(np.abs(zs[a:b]))))

    # --- shake: alta frequência da translação ---
    tx = np.where(mask, 0.0, sig.tx)
    ty = np.where(mask, 0.0, sig.ty)
    hf = np.hypot(tx - _ma(tx, 5), ty - _ma(ty, 5))
    thr = max(0.006, 4 * float(np.median(hf)))
    for a, b in _runs(hf > thr, 3, 90, gap=1):
        amp = float(hf[a:b].mean())
        peak = a + int(np.argmax(hf[a:b]))
        k, ref, off = _anchor(peak, cuts, beat_frames)
        tracks.append(EffectTrack(type="shake", start_frame=a, end_frame=b, peak_frame=peak, magnitude=round(amp, 4),
                                  intensity=round(_ramp(amp, 0.006, 0.04), 3), anchor=k, anchor_ref=ref, anchor_offset=off, confidence=0.6,
                                  envelope=_env(hf[a:b])))

    # --- rgb split: desalinhamento horizontal R/B (fora das transições) ---
    shifts = np.zeros(n)
    for i in range(n):
        if mask[i]:
            continue
        dx, resp = _rgb_shift(rgb[i])
        shifts[i] = dx if resp > 0.05 else 0.0
    for a, b in _runs(shifts >= 1.0, 2, 30, gap=1):
        amp = float(shifts[a:b].mean() / w)
        peak = a + int(np.argmax(shifts[a:b]))
        k, ref, off = _anchor(peak, cuts, beat_frames)
        tracks.append(EffectTrack(type="rgb_split", start_frame=a, end_frame=b, peak_frame=peak, magnitude=round(amp, 4),
                                  intensity=round(_ramp(amp, 0.005, 0.04), 3), anchor=k, anchor_ref=ref, anchor_offset=off, confidence=0.4,
                                  envelope=_env(shifts[a:b])))

    # --- pulsos de exposição / contraste / saturação (só se houver evidência) ---
    g = rgb.astype(np.float32).mean(axis=3)
    contrast = g.reshape(n, -1).std(axis=1)
    mask_p = _mask(n, transitions, fx, pad=6)  # margem maior: as caudas de blur/flash mexem no brilho
    bounds = [0] + cuts + [n]
    for name, x, thr_rel in (("exposure_pulse", sig.lum, 0.12), ("contrast_pulse", contrast, 0.15), ("sat_pulse", sig.sat, 0.15)):
        base = np.zeros(n, np.float32)
        for s0, s1 in zip(bounds[:-1], bounds[1:]):  # linha de base DENTRO de cada plano (não atravessa cortes)
            seg_x = x[s0:s1].astype(np.float32)
            win = min(31, len(seg_x) // 2 * 2 - 1) if len(seg_x) >= 5 else 1
            base[s0:s1] = median_filter(seg_x, size=max(win, 1), mode="nearest")
        rel = np.where(mask_p, 0.0, (x - base) / (base + 1e-6))
        for sgn in (1, -1):
            for a, b in _runs(sgn * rel > thr_rel, 3, 20, gap=1):
                seg = rel[a:b]
                pk = int(np.argmax(np.abs(seg)))
                k, ref, off = _anchor(a + pk, cuts, beat_frames)
                tracks.append(EffectTrack(type=name, start_frame=a, end_frame=b, peak_frame=a + pk, magnitude=round(float(seg[pk]), 3),
                                          intensity=round(_ramp(float(np.abs(seg).max()), thr_rel, 0.5), 3), anchor=k, anchor_ref=ref,
                                          anchor_offset=off, confidence=0.5, envelope=_env(np.abs(seg))))
    # pulso de saturação que coincide com rgb_split/shake é consequência desse efeito, não um efeito próprio
    cover = [(t.start_frame - 3, t.end_frame + 3) for t in tracks if t.type in ("rgb_split", "shake")]
    tracks = [t for t in tracks if not (t.type == "sat_pulse" and any(a <= t.peak_frame < b for a, b in cover))]
    tracks.sort(key=lambda t: (t.start_frame, t.type))
    return tracks, mask, contrast


def build_look(sig: Signals, mask: np.ndarray, contrast: np.ndarray, tracks: list[EffectTrack],
               transitions: list[Transition], duration_s: float) -> LookProfile:
    stable = ~mask
    stable[0] = False
    if stable.sum() < 5:
        stable = np.ones(sig.n, bool)
        stable[0] = False
    en = sig.flow_mag[stable]
    non_cut = sum(1 for t in transitions if t.type != "cut")
    cps = len(transitions) / duration_s if duration_s else 0.0
    trans_rate = non_cut / duration_s if duration_s else 0.0
    punch_rate = sum(1 for t in tracks if t.type == "zoom_punch") / duration_s if duration_s else 0.0
    shake_frames = sum(t.end_frame - t.start_frame for t in tracks if t.type == "shake")
    rgb_frames = sum(t.end_frame - t.start_frame for t in tracks if t.type == "rgb_split")
    m = float(en.mean())
    aggr = (0.40 * _ramp(m, 0.0040, 0.0110) + 0.20 * _ramp(trans_rate, 0.3, 1.0) + 0.15 * _ramp(punch_rate, 0.2, 1.5)
            + 0.15 * _ramp(cps, 0.5, 1.4) + 0.10 * _ramp(shake_frames / max(sig.n, 1), 0.0, 0.15))
    desc = ["aggressive" if aggr >= 0.55 else ("soft" if aggr <= 0.40 else "medium"),
            "fast" if cps >= 1.0 else ("calm" if cps < 0.5 and aggr < 0.5 else "moderate")]
    return LookProfile(
        sharpness=round(float(np.median(sig.sharp[stable])), 1), contrast=round(float(contrast[stable].mean()), 2),
        saturation=round(float(sig.sat[stable].mean()), 2), luma=round(float(sig.lum[stable].mean()), 2),
        motion_energy_mean=round(m, 5), motion_energy_p90=round(float(np.percentile(en, 90)), 5),
        cuts_per_second=round(cps, 3), effect_transition_rate=round(trans_rate, 3), punch_rate=round(punch_rate, 3),
        shake_fraction=round(shake_frames / max(sig.n, 1), 3), rgb_split_fraction=round(rgb_frames / max(sig.n, 1), 3),
        aggressiveness=round(float(aggr), 3), descriptors=desc,
    )
