"""Pilha de efeitos paramétricos: cada efeito é função de uma EffectTrack (medida ou planeada).

  zoom_punch      -> ln(escala) extra acumulado, com smoothstep sobre [start,end) e a persistir até ao fim do plano
                     (um punch-in seguido de punch-out anula-se, como o integral da taxa de zoom medida)
  shake           -> deslocamentos por frame (ruído determinístico) + margem de zoom para não mostrar bordas
  rgb_split       -> desalinhamento horizontal R/B
  *_pulse         -> ganho de exposição / contraste / saturação com envelope triangular

`scale` (fx_scale) multiplica as magnitudes: é a alavanca do protetor de fidelidade.
"""
from __future__ import annotations

import cv2
import numpy as np

from velocity.schemas.edit_dna import EffectTrack


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _env_at(t: EffectTrack, f: int) -> float:
    """Intensidade 0..1 no frame f: envelope MEDIDO se existir (curvas tipo 0→20→60→100→40→0), senão triangular."""
    if t.envelope and t.end_frame > t.start_frame:
        pos = (f - t.start_frame) / max(t.end_frame - t.start_frame - 1, 1) * (len(t.envelope) - 1)
        return float(np.clip(np.interp(pos, np.arange(len(t.envelope)), t.envelope), 0, 1))
    return _tri(f, t.start_frame, t.peak_frame, t.end_frame)


def _tri(f: int, a: int, peak: int, b: int) -> float:
    if f < a or f >= b:
        return 0.0
    if f <= peak:
        return (f - a + 1) / (peak - a + 1)
    return max(0.0, 1.0 - (f - peak) / max(b - peak, 1))


def zoom_extra(start: int, length: int, tracks: list[EffectTrack], scale: float) -> np.ndarray:
    """ln(escala) extra por frame do plano [start, start+length)."""
    f = np.arange(start, start + length, dtype=np.float32)
    out = np.zeros(length, np.float32)
    for t in tracks:
        if t.type != "zoom_punch" or t.end_frame <= start or t.start_frame >= start + length:
            continue
        span = max(t.end_frame - t.start_frame, 1)
        out += t.magnitude * scale * _smoothstep((f - t.start_frame) / span)
    for t in tracks:  # margem para o shake não mostrar bordas
        if t.type == "shake" and t.start_frame < start + length and t.end_frame > start:
            m = np.array([_env_at(t, int(x)) for x in f], np.float32)
            out += np.log1p(3.0 * t.magnitude * scale) * (m > 0)
    return out


def shake_offsets(frame: int, tracks: list[EffectTrack], width: int, scale: float) -> tuple[float, float]:
    """Deslocamento (px) do frame global `frame`. Ruído determinístico por (frame, faixa)."""
    dx = dy = 0.0
    for t in tracks:
        if t.type == "shake" and t.start_frame <= frame < t.end_frame:
            env = min(1.0, (frame - t.start_frame + 1) / 2, (t.end_frame - frame) / 2) * (0.35 + 0.65 * _env_at(t, frame))
            rng = np.random.default_rng(frame * 7919 + t.start_frame)
            amp = t.magnitude * scale * width * 1.6
            dx += float(rng.uniform(-1, 1)) * amp * env
            dy += float(rng.uniform(-1, 1)) * amp * env
    return dx, dy


def post_fx(img: np.ndarray, frame: int, tracks: list[EffectTrack], scale: float) -> np.ndarray:
    h, w = img.shape[:2]
    for t in tracks:
        if not (t.start_frame <= frame < t.end_frame):
            continue
        env = _env_at(t, frame)
        if t.type == "rgb_split":
            px = t.magnitude * scale * w * env
            if px >= 0.5:
                m_r = np.float32([[1, 0, px], [0, 1, 0]])
                m_b = np.float32([[1, 0, -px], [0, 1, 0]])
                img = img.copy()
                img[..., 0] = cv2.warpAffine(img[..., 0], m_r, (w, h), borderMode=cv2.BORDER_REPLICATE)
                img[..., 2] = cv2.warpAffine(img[..., 2], m_b, (w, h), borderMode=cv2.BORDER_REPLICATE)
        elif t.type == "exposure_pulse":
            g = 1.0 + t.magnitude * scale * env
            img = cv2.convertScaleAbs(img, alpha=max(g, 0.2), beta=0)
        elif t.type == "contrast_pulse":
            g = 1.0 + t.magnitude * scale * env
            mean = float(img.mean())
            img = cv2.convertScaleAbs(img, alpha=max(g, 0.2), beta=mean * (1 - max(g, 0.2)))
        elif t.type == "sat_pulse":
            g = 1.0 + t.magnitude * scale * env
            hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV).astype(np.float32)
            hsv[..., 1] = np.clip(hsv[..., 1] * max(g, 0.0), 0, 255)
            img = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    return img
