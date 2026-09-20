"""Características por plano (movimento, velocidade, aparência) a partir dos sinais.

Só usa frames ESTÁVEIS do plano (fora dos intervalos de transição), porque durante um
blur/flash o fluxo óptico e a nitidez não significam nada sobre o conteúdo.
"""
from __future__ import annotations

import numpy as np

from velocity.reference.signals import Signals
from velocity.schemas.edit_dna import Appearance, Curve, MotionProfile, SpeedProfile


def _resample(x: np.ndarray, k: int = 8) -> np.ndarray:
    if len(x) == 0:
        return np.zeros(k)
    if len(x) == 1:
        return np.full(k, float(x[0]))
    src = np.linspace(0, 1, len(x))
    dst = np.linspace(0, 1, k)
    return np.interp(dst, src, x)


def _curve(x: np.ndarray, k: int = 8) -> Curve:
    y = _resample(x, k)
    return Curve(t=[round(float(t), 4) for t in np.linspace(0, 1, k)], v=[round(float(v), 5) for v in y])


def _smooth(x: np.ndarray, w: int = 3) -> np.ndarray:
    if len(x) < w + 2:
        return x
    k = np.ones(w) / w
    return np.convolve(np.pad(x, (w // 2, w // 2), mode="edge"), k, mode="valid")


def stable_range(start: int, end: int, span_in: tuple[int, int] | None, span_out_start: int | None) -> tuple[int, int]:
    """[a,b) de frames estáveis do plano [start,end): tira o fim da transição de entrada e o começo da seguinte."""
    a = max(start, span_in[1]) if span_in else start
    b = min(end, span_out_start) if span_out_start is not None else end
    if b - a < 3:  # plano quase todo transição: usa o plano inteiro (confiança baixa)
        return start, end
    return a, b


def motion_profile(sig: Signals, a: int, b: int, energy_ref: float) -> MotionProfile:
    sl = slice(max(a, 1), b)  # d/flow no frame 0 é 0 por construção
    ok = sig.flow_ok[sl]
    n_used = max(int(ok.sum()), 0)
    if n_used < 2:
        z = Curve(t=[0.0, 1.0], v=[0.0, 0.0])
        return MotionProfile(energy_mean=0.0, energy_curve=z, zoom_total=0.0, zoom_curve=z, pan_x=0.0, pan_y=0.0,
                             rotation_deg=0.0, shake=0.0, motion_type="unknown", confidence=0.0)
    zoom = np.where(ok, sig.zoom[sl], 0.0)
    tx = np.where(ok, sig.tx[sl], 0.0)
    ty = np.where(ok, sig.ty[sl], 0.0)
    rot = np.where(ok, sig.rot[sl], 0.0)
    mag = sig.flow_mag[sl]

    zoom_curve = np.concatenate([[0.0], np.cumsum(zoom)])
    zoom_total = float(zoom_curve[-1])
    pan_x, pan_y = float(tx.sum()), float(ty.sum())
    rot_deg = float(np.degrees(rot.sum()))
    # shake: alta frequência da translação (o que sobra depois de suavizar)
    hf = np.hypot(tx - _smooth(tx, 5), ty - _smooth(ty, 5))
    shake = float(hf.mean())

    pan_mag = float(np.hypot(pan_x, pan_y))
    if abs(zoom_total) < 0.06 and pan_mag < 0.05 and shake < 0.006 and abs(rot_deg) < 4:
        mt = "static"
    elif shake >= 0.012 and abs(zoom_total) < 0.15 and pan_mag < 0.25:
        mt = "handheld"
    elif abs(zoom_total) >= 0.06 and pan_mag < 0.6 * abs(zoom_total) + 0.05:
        mt = "zoom_in" if zoom_total > 0 else "zoom_out"
    elif pan_mag >= 0.05 and abs(zoom_total) < 0.06:
        mt = "pan"
    else:
        mt = "mixed"

    conf = float(np.clip(n_used / max(b - max(a, 1), 1), 0, 1) * min(1.0, (b - a) / 8))
    return MotionProfile(
        energy_mean=round(float(mag.mean()), 5),
        energy_curve=_curve(mag / max(energy_ref, 1e-6)),
        zoom_total=round(zoom_total, 4), zoom_curve=_curve(zoom_curve),
        pan_x=round(pan_x, 4), pan_y=round(pan_y, 4), rotation_deg=round(rot_deg, 2),
        shake=round(shake, 5), motion_type=mt, confidence=round(conf, 2),
    )


def speed_profile(sig: Signals, a: int, b: int) -> SpeedProfile:
    sl = slice(max(a, 1), b)
    mag = sig.flow_mag[sl]
    d = sig.d[sl]
    n = len(mag)
    if n < 6:
        return SpeedProfile(shape="unknown", curve=Curve(t=[0.0, 1.0], v=[1.0, 1.0]), frozen_ratio=0.0, variation=0.0, confidence=0.0)
    frozen_thr = max(0.35, 0.12 * float(np.median(d)))
    frozen_ratio = float((d < frozen_thr).mean())
    m = _smooth(mag, 5)
    mean = float(m.mean())
    variation = float(m.std() / mean) if mean > 1e-6 else 0.0
    rel = m / mean if mean > 1e-6 else np.ones_like(m)
    third = max(1, n // 3)
    s0, s1, s2 = float(rel[:third].mean()), float(rel[third : n - third].mean() if n > 2 * third else rel.mean()), float(rel[-third:].mean())
    thr = 0.25
    if variation < 0.30:
        shape = "flat"
    elif s1 > s0 + thr and s1 > s2 + thr:
        shape = "peak_middle"
    elif s1 < s0 - thr and s1 < s2 - thr:
        shape = "valley_middle"
    elif s2 > s0 + thr:
        shape = "ramp_up"
    elif s0 > s2 + thr:
        shape = "ramp_down"
    else:
        shape = "flat"
    # Confiança limitada de propósito: é sinal indireto (ver SpeedProfile).
    conf = float(np.clip(min(1.0, n / 20) * 0.5, 0, 0.5))
    return SpeedProfile(shape=shape, curve=_curve(rel), frozen_ratio=round(frozen_ratio, 3), variation=round(variation, 3), confidence=round(conf, 2))


def appearance(rgb: np.ndarray, sig: Signals, a: int, b: int) -> Appearance:
    seg = rgb[a:b]
    if len(seg) == 0:
        seg = rgb[a : a + 1]
    lum = sig.lum[a:b] if b > a else sig.lum[a : a + 1]
    sat = sig.sat[a:b] if b > a else sig.sat[a : a + 1]
    sharp = sig.sharp[a:b] if b > a else sig.sharp[a : a + 1]
    g = seg[:: max(1, len(seg) // 4)].astype(np.float32).mean(axis=3)
    return Appearance(mean_luma=round(float(lum.mean()), 1), contrast=round(float(g.std()), 1),
                      saturation=round(float(sat.mean()), 1), sharpness=round(float(np.median(sharp)), 1))
