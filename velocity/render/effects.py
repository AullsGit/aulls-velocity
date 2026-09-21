"""Efeitos de transição sintetizados frame a frame (uint8 RGB HxWx3).

Todos são funções de uma "quantidade" `amt` em 0..1 (envelope centrado no corte) e da
`intensity` medida na referência. São determinísticos (glitch usa RNG semeado pelo frame).
"""
from __future__ import annotations

import cv2
import numpy as np

from velocity.schemas.edit_dna import Transition


def envelope(f: int, start: int, cut: int, end: int) -> float:
    """0 fora de [start,end); pico 1 em `cut`; queda linear para as bordas."""
    if f < start or f >= end:
        return 0.0
    half = max(cut - start, end - 1 - cut, 1)
    return float(np.clip(1.0 - abs(f - cut) / (half + 1), 0.0, 1.0))


def flash(img: np.ndarray, amt: float, intensity: float) -> np.ndarray:
    w = float(np.clip(amt * (0.35 + 0.65 * intensity), 0, 1)) ** 0.85
    return cv2.addWeighted(img, 1 - w, np.full_like(img, 255), w, 0)


def dip_black(img: np.ndarray, amt: float, intensity: float) -> np.ndarray:
    w = float(np.clip(amt * (0.5 + 0.5 * intensity), 0, 1))
    return (img.astype(np.float32) * (1 - w)).astype(np.uint8)


def _zoom_about_center(img: np.ndarray, s: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), 0, s)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def blur(img: np.ndarray, amt: float, intensity: float, kind: str) -> np.ndarray:
    h, w = img.shape[:2]
    a = float(np.clip(amt, 0, 1)) * (0.5 + 0.5 * intensity)
    if a < 0.03:
        return img
    if kind == "horizontal":
        k = max(3, int(a * 0.16 * w) | 1)
        out = cv2.blur(img, (k, 1))
    elif kind == "vertical":
        k = max(3, int(a * 0.16 * h) | 1)
        out = cv2.blur(img, (1, k))
    else:  # radial / gaussiano: zoom-blur = média de cópias em escalas crescentes
        n = 8
        acc = np.zeros_like(img, dtype=np.float32)
        for i in range(n):
            acc += _zoom_about_center(img, 1.0 + a * 0.22 * i / (n - 1)).astype(np.float32)
        out = (acc / n).astype(np.uint8)
    return cv2.convertScaleAbs(out, alpha=1.0 + 0.12 * a, beta=8 * a)  # leve subida de exposição típica do whip


def glitch(img: np.ndarray, amt: float, intensity: float, seed: int) -> np.ndarray:
    h, w = img.shape[:2]
    a = float(np.clip(amt, 0, 1)) * (0.4 + 0.6 * intensity)
    if a < 0.03:
        return img
    rng = np.random.default_rng(seed)
    out = img.copy()
    shift = max(1, int(a * 0.035 * w))
    out[..., 0] = np.roll(img[..., 0], shift, axis=1)
    out[..., 2] = np.roll(img[..., 2], -shift, axis=1)
    for _ in range(3):  # fatias horizontais deslocadas
        y = int(rng.integers(0, max(1, h - 4)))
        hh = int(rng.integers(max(2, h // 40), max(3, h // 10)))
        dx = int(rng.integers(-1, 2) * a * 0.08 * w)
        out[y : y + hh] = np.roll(out[y : y + hh], dx, axis=1)
    return cv2.convertScaleAbs(out, alpha=1.0 + 0.25 * a, beta=0)


def apply(img: np.ndarray, t_type: str, amt: float, intensity: float, blur_kind: str, frame_index: int) -> np.ndarray:
    if amt <= 0 or t_type == "cut":
        return img
    if t_type == "flash":
        return flash(img, amt, intensity)
    if t_type == "dip_black":
        return dip_black(img, amt, intensity)
    if t_type == "blur":
        return blur(img, amt, intensity, blur_kind)
    if t_type == "glitch":
        return glitch(img, amt, intensity, frame_index)
    return img


def measured_env(env: list[float], f: int, start: int, end: int, cut: int) -> float:
    """Valor do envelope MEDIDO no frame f (interpola se a duração diferir); sem envelope -> triangular."""
    if env and end > start:
        pos = (f - start) / max(end - start - 1, 1) * (len(env) - 1)
        return float(np.clip(np.interp(pos, np.arange(len(env)), env), 0, 1))
    return envelope(f, start, cut, end)


def _shift(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def apply_transition(img: np.ndarray, t: Transition, f: int) -> np.ndarray:
    """Transição com direção: o CONTEÚDO desliza (whip) ou o zoom atravessa o corte, com blur alinhado ao movimento.
    Saída (f<cut) e entrada (f>=cut) movem-se no mesmo sentido, dando continuidade ao gesto de câmara."""
    if f < t.start_frame or f >= t.end_frame:
        return img
    amt = measured_env(t.envelope, f, t.start_frame, t.end_frame, t.cut_frame)
    h, w = img.shape[:2]
    out = img
    d = t.direction
    outgoing = f < t.cut_frame
    if t.type in ("blur", "glitch") and d.startswith("slide"):
        mag = float(np.clip(max(abs(t.motion.get("dx", 0.0)), abs(t.motion.get("dy", 0.0))) * 0.7, 0.12, 0.5))
        sign = 1.0 if outgoing else -1.0
        dx = dy = 0.0
        if d in ("slide_right", "slide_left"):
            dx = sign * mag * w * amt * (1.0 if d == "slide_right" else -1.0)
        else:
            dy = sign * mag * h * amt * (1.0 if d == "slide_down" else -1.0)
        out = _shift(out, dx, dy)
        kind = "horizontal" if d in ("slide_right", "slide_left") else "vertical"
        return apply(out, t.type, amt, t.intensity, kind, f)
    if t.type in ("blur", "glitch") and d.startswith("zoom"):
        mag = float(np.clip(abs(t.motion.get("zoom", 0.0)) * 0.7, 0.1, 0.45))
        s_ = np.exp(mag * amt * (1.0 if d == "zoom_in" else -1.0))
        cx = cv2.getRotationMatrix2D((w / 2, h / 2), 0, float(s_))
        out = cv2.warpAffine(out, cx, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        return apply(out, t.type, amt, t.intensity, "radial", f)
    return apply(out, t.type, amt, t.intensity, t.blur_kind, f)
