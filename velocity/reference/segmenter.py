"""Sinais por frame -> transições tipadas, pontos de corte e planos.

Ideia central (diferente do MVP): uma transição Velocity NÃO é um instante, é um
*intervalo instável* (blur, flash, dip, glitch, ou um corte seco de 1-2 frames) entre
dois trechos estáveis. Detetamos os intervalos por instabilidade e depois decidimos:
  1. houve troca de conteúdo? (compara os frames estáveis antes/depois)  -> corte vs efeito
  2. de que tipo é? (brilho, nitidez, anisotropia do blur, ...)
  3. em que frame o conteúdo troca? (regra por tipo)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.ndimage import median_filter

from velocity.reference.signals import Signals
from velocity.schemas.edit_dna import FxEvent, Transition


@dataclass
class SegmenterParams:
    local_win: int = 31  # janela (frames) das linhas de base locais
    blur_drop_ln: float = 1.1  # queda de ln(nitidez) que vale u_blur=1 (~ nitidez/3)
    d_ratio_base: float = 2.5  # d/mediana_local a partir do qual há sinal de corte
    d_ratio_scale: float = 3.0
    hist_base: float = 0.25
    hist_scale: float = 0.25
    lum_scale: float = 40.0
    u_on: float = 0.9  # limiar de entrada (peak) num intervalo
    u_off: float = 0.45  # limiar de histerese (extensão das bordas)
    merge_gap: int = 2
    min_shot_frames: int = 4
    # Troca de conteúdo: a COR é imune a zoom/movimento; a estrutura (NCC) só conta se for extrema.
    # Calibrado nas 7 referências de bench/ (falsos por zoom em degraus: cor<=0.15; cortes reais: cor>=0.19).
    color_cut_thresh: float = 0.17
    struct_cut_thresh: float = 0.85
    flash_lum_rise: float = 45.0
    flash_clip_hi: float = 0.35
    dip_lum_max: float = 24.0
    blur_ratio_max: float = 0.40  # nitidez mínima / base <= isto => blur
    edge_frames: int = 2  # intervalos que tocam nas pontas = fade de entrada/saída


@dataclass
class Segmentation:
    transitions: list[Transition] = field(default_factory=list)  # só os que trocam conteúdo, ordenados
    fx_events: list[FxEvent] = field(default_factory=list)
    boundaries: list[int] = field(default_factory=list)  # [0, cut1, cut2, ..., n]
    u: np.ndarray | None = None  # instabilidade por frame (debug)
    spans: list[tuple[int, int]] = field(default_factory=list)  # debug


def _local_median(x: np.ndarray, win: int) -> np.ndarray:
    return median_filter(x.astype(np.float32), size=win, mode="nearest")


def instability(sig: Signals, p: SegmenterParams) -> np.ndarray:
    ln_sharp = np.log(np.maximum(sig.sharp, 1.0))
    base_sharp = _local_median(ln_sharp, p.local_win)
    u_blur = np.clip((base_sharp - ln_sharp) / p.blur_drop_ln, 0, 1.6)

    d_med = _local_median(sig.d, p.local_win) + 1.0
    r_d = sig.d / d_med
    u_cut_d = np.clip((r_d - p.d_ratio_base) / p.d_ratio_scale, 0, 1.6)
    u_cut_h = np.clip((sig.hist_d - p.hist_base) / p.hist_scale, 0, 1.6)

    lum_med = _local_median(sig.lum, p.local_win)
    u_lum = np.clip(np.abs(sig.lum - lum_med) / p.lum_scale, 0, 1.6)
    return np.maximum.reduce([u_blur, u_cut_d, u_cut_h, u_lum])


def _find_spans(u: np.ndarray, p: SegmenterParams) -> list[tuple[int, int]]:
    """Intervalos [a,b] inclusivos com histerese: núcleo u>=u_on, expandido enquanto u>=u_off."""
    n = len(u)
    core = u >= p.u_on
    spans: list[list[int]] = []
    i = 0
    while i < n:
        if core[i]:
            a = i
            while a - 1 >= 0 and u[a - 1] >= p.u_off:
                a -= 1
            b = i
            while b + 1 < n and (u[b + 1] >= p.u_off or core[b + 1]):
                b += 1
            spans.append([a, b])
            i = b + 1
        else:
            i += 1
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= p.merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _thumb(rgb_frame: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY)
    g = cv2.resize(g, (24, 24), interpolation=cv2.INTER_AREA).astype(np.float32)
    return g


def _content_distance(rgb: np.ndarray, a: int, b: int) -> tuple[float, float]:
    """(estrutura, cor): distâncias 0..1 entre o trecho estável ANTES de `a` e DEPOIS de `b`.
    Mediana de 3 frames de cada lado; estrutura = 1-NCC (24x24), cor = Bhattacharyya (hist RGB 6x6x6)."""
    n = rgb.shape[0]
    lo = max(0, a - 3)
    hi = min(n, b + 4)
    before = rgb[lo:max(lo + 1, a)] if a > 0 else rgb[:1]
    after = rgb[min(n - 1, b + 1):hi] if b + 1 < n else rgb[-1:]
    fb = np.median(before, axis=0).astype(np.uint8)
    fa = np.median(after, axis=0).astype(np.uint8)
    tb, ta = _thumb(fb), _thumb(fa)
    tb0, ta0 = tb - tb.mean(), ta - ta.mean()
    denom = np.sqrt((tb0**2).sum() * (ta0**2).sum()) + 1e-6
    ncc = float((tb0 * ta0).sum() / denom)  # -1..1
    struct_d = 1.0 - max(0.0, ncc)
    hb = cv2.calcHist([fb], [0, 1, 2], None, [6, 6, 6], [0, 256] * 3)
    ha = cv2.calcHist([fa], [0, 1, 2], None, [6, 6, 6], [0, 256] * 3)
    cv2.normalize(hb, hb, 1, 0, cv2.NORM_L1)
    cv2.normalize(ha, ha, 1, 0, cv2.NORM_L1)
    color_d = float(cv2.compareHist(hb, ha, cv2.HISTCMP_BHATTACHARYYA))
    return struct_d, color_d


def _blur_kind(rgb_frame: np.ndarray) -> str:
    """Anisotropia dos gradientes no frame mais desfocado: blur direcional apaga o gradiente
    ao longo da sua direção. Sem anisotropia + queda de nitidez => radial/zoom-blur/gaussiano."""
    g = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gx = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)).mean() + 1e-6
    gy = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)).mean() + 1e-6
    ratio = gx / gy
    if ratio < 0.55:
        return "horizontal"
    if ratio > 1.8:
        return "vertical"
    return "radial"


def _classify_span(rgb, sig: Signals, a: int, b: int, p: SegmenterParams):
    n = sig.n
    lo, hi = max(0, a - 4), min(n, b + 5)
    idx = np.arange(a, b + 1)
    def _side(x, lo_, hi_, fn):
        return float(fn(x[lo_:hi_])) if hi_ > lo_ else None

    lum_b = _side(sig.lum, lo, a, np.median)
    lum_a = _side(sig.lum, b + 1, hi, np.median)
    sides = [v for v in (lum_b, lum_a) if v is not None] or [float(sig.lum[a])]
    base_hi, base_lo = max(sides), min(sides)  # um flash tem de exceder AMBOS os lados; um dip ficar abaixo de ambos
    base_lum = float(np.mean(sides))
    sharp_sides = [_side(np.log(np.maximum(sig.sharp, 1.0)), lo, a, np.median), _side(np.log(np.maximum(sig.sharp, 1.0)), b + 1, hi, np.median)]
    sharp_sides = [v for v in sharp_sides if v is not None] or [float(np.log(max(sig.sharp[a], 1.0)))]
    ln_sharp_base = float(min(sharp_sides))  # base = o lado MENOS nítido (evita chamar 'blur' a uma troca nítido->suave)
    span_lum = sig.lum[idx]
    peak_lum, min_lum = float(span_lum.max()), float(span_lum.min())
    clip_peak = float(sig.clip_hi[idx].max())
    sharp_min = float(sig.sharp[idx].min())
    sharp_ratio = float(np.exp(np.log(max(sharp_min, 1.0)) - ln_sharp_base))
    d_peak = float(sig.d[idx].max())

    flash = (peak_lum - base_hi) >= p.flash_lum_rise or (clip_peak >= p.flash_clip_hi and peak_lum - base_hi > 20)
    dip = min_lum <= p.dip_lum_max and base_lo > 45
    blur = sharp_ratio <= p.blur_ratio_max and (b - a + 1) >= 2

    if flash and (not dip or (peak_lum - base_hi) >= (base_lo - min_lum)):
        t = "flash"
        cut = a + int(np.argmax(span_lum))
        intensity = float(np.clip((peak_lum - base_hi) / max(255 - base_hi, 1.0), 0, 1))
    elif dip:
        t = "dip_black"
        cut = a + int(np.argmin(span_lum))
        intensity = float(np.clip(1 - min_lum / max(base_lo, 1.0), 0, 1))
    elif blur:
        t = "blur"
        cut = a + int(np.argmin(sig.sharp[idx]))
        intensity = float(np.clip(1 - sharp_ratio, 0, 1))
    elif (b - a + 1) <= 2:
        t = "cut"
        cut = a + int(np.argmax(sig.d[idx]))
        intensity = 1.0
    else:
        t = "glitch"
        cut = a + int(np.argmax(sig.d[idx]))
        intensity = float(np.clip(d_peak / 60.0, 0.2, 1.0))
    kind = _blur_kind(rgb[a + int(np.argmin(sig.sharp[idx]))]) if t == "blur" else "none"
    peak = peak_lum if t == "flash" else (min_lum if t == "dip_black" else None)
    # envelope MEDIDO: perfil de intensidade ao longo do intervalo (0..1)
    if t == "flash":
        env = np.clip((span_lum - base_hi) / max(peak_lum - base_hi, 1e-6), 0, 1)
    elif t == "dip_black":
        env = np.clip((base_lo - span_lum) / max(base_lo - min_lum, 1e-6), 0, 1)
    elif t == "blur":
        env = np.clip((ln_sharp_base - np.log(np.maximum(sig.sharp[idx], 1.0))) / max(ln_sharp_base - np.log(max(sharp_min, 1.0)), 1e-6), 0, 1)
    elif t == "glitch":
        env = np.clip(sig.d[idx] / max(d_peak, 1e-6), 0, 1)
    else:
        env = np.ones(len(idx))
    # direção do movimento durante a transição (com 2 frames de margem): translação/zoom globais acumulados
    lo2, hi2 = max(1, a - 2), min(n, b + 3)
    ok = sig.flow_ok[lo2:hi2]
    dxs, dys, zz = float(np.where(ok, sig.tx[lo2:hi2], 0).sum()), float(np.where(ok, sig.ty[lo2:hi2], 0).sum()), float(np.where(ok, sig.zoom[lo2:hi2], 0).sum())
    ah, aw = rgb.shape[1], rgb.shape[2]
    dys_n = dys * (aw / ah)  # ty foi normalizado pela largura; converte para fração da altura
    z_pre = float(np.where(sig.flow_ok[lo2:max(lo2, cut)], sig.zoom[lo2:max(lo2, cut)], 0).sum())  # zoom ANTES do corte (transição de zoom é simétrica)
    trans_mag = max(abs(dxs), abs(dys_n))
    direction = "none"
    if trans_mag >= 0.06 and trans_mag >= 1.5 * abs(z_pre):
        direction = ("slide_right" if dxs > 0 else "slide_left") if abs(dxs) >= abs(dys_n) else ("slide_down" if dys_n > 0 else "slide_up")
    elif abs(z_pre) >= 0.04 and abs(z_pre) >= 1.5 * trans_mag:
        direction = "zoom_in" if z_pre > 0 else "zoom_out"
    zz = z_pre if direction.startswith("zoom") else zz
    motion = {"dx": round(dxs, 4), "dy": round(dys, 4), "zoom": round(zz, 4)}
    return t, int(cut), intensity, kind, peak, [round(float(v), 3) for v in env], direction, motion


def segment(rgb: np.ndarray, sig: Signals, params: SegmenterParams | None = None) -> Segmentation:
    p = params or SegmenterParams()
    n = sig.n
    u = instability(sig, p)
    u[0] = 0.0  # d[0]=0 por construção; ruído de arranque
    spans = _find_spans(u, p)
    seg = Segmentation(u=u, spans=spans)

    for a, b in spans:
        edge_start, edge_end = a <= p.edge_frames, b >= n - 1 - p.edge_frames
        t, cut, intensity, kind, peak, env, direction, motion = _classify_span(rgb, sig, a, b, p)
        if edge_start or edge_end:
            # fade de entrada/saída (ou início/fim com efeito): evento, não corte
            seg.fx_events.append(FxEvent(type=t if t != "cut" else "glitch", start_frame=a, end_frame=b + 1, intensity=intensity))
            continue
        struct_d, color_d = _content_distance(rgb, a, b)
        changed = color_d >= p.color_cut_thresh or struct_d >= p.struct_cut_thresh
        if t == "cut" and not changed:
            continue  # pico de d sem troca de conteúdo (movimento rápido): ruído
        if changed:
            seg.transitions.append(
                Transition(type=t, start_frame=int(a), end_frame=int(b + 1), cut_frame=cut,
                           intensity=round(intensity, 3), blur_kind=kind, peak_luma=round(peak, 1) if peak is not None else None,
                           direction=direction, motion=motion, envelope=env)
            )
        else:
            seg.fx_events.append(FxEvent(type=t, start_frame=int(a), end_frame=int(b + 1), intensity=round(intensity, 3)))

    # planos mínimos: funde cortes demasiado próximos (fica o de maior intensidade)
    trans = sorted(seg.transitions, key=lambda t: t.cut_frame)
    cleaned: list[Transition] = []
    for t in trans:
        if cleaned and t.cut_frame - cleaned[-1].cut_frame < p.min_shot_frames:
            if t.intensity > cleaned[-1].intensity:
                cleaned[-1] = t
            continue
        if t.cut_frame < p.min_shot_frames or n - t.cut_frame < p.min_shot_frames:
            continue
        cleaned.append(t)
    seg.transitions = cleaned
    seg.boundaries = [0] + [t.cut_frame for t in cleaned] + [n]
    return seg
