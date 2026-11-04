"""Tratamento de cor da referência: uma transformação 3x4 que leva a cor dos clips para a dela.

Usa a transferência linear de Monge-Kantorovich entre as distribuições RGB (média e covariância):
não muda a imagem de sítio, só a sua paleta, e é a forma mais simples que iguala contraste,
saturação e dominante de cor ao mesmo tempo. `strength` mistura com a identidade (1 = igualar por
completo; a referência pode ter sido filmada com luz muito diferente, por isso limita-se o ganho).
"""
from __future__ import annotations

import numpy as np

MAX_GAIN = 2.2


def _sqrtm(a: np.ndarray) -> np.ndarray:
    w, v = np.linalg.eigh(a)
    return (v * np.sqrt(np.maximum(w, 1e-6))) @ v.T


def grade_matrix(ref_frames: np.ndarray, src_frames: np.ndarray, strength: float = 0.8) -> list[list[float]]:
    """Frames [n,H,W,3] RGB uint8 de cada lado. Devolve a matriz 3x4 (valores 0..255)."""
    r = ref_frames.reshape(-1, 3).astype(np.float64)
    s = src_frames.reshape(-1, 3).astype(np.float64)
    mu_r, mu_s = r.mean(axis=0), s.mean(axis=0)
    cov_r = np.cov(r, rowvar=False) + np.eye(3) * 1e-3
    cov_s = np.cov(s, rowvar=False) + np.eye(3) * 1e-3
    sq_s = _sqrtm(cov_s)
    inv_sq_s = np.linalg.inv(sq_s)
    T = inv_sq_s @ _sqrtm(sq_s @ cov_r @ sq_s) @ inv_sq_s
    u, d, vt = np.linalg.svd(T)  # ganho limitado: nunca estica o ruído de um clip quase cinzento
    T = (u * np.clip(d, 1 / MAX_GAIN, MAX_GAIN)) @ vt
    k = float(np.clip(strength, 0.0, 1.0))
    T = (1 - k) * np.eye(3) + k * T
    t = (mu_s + k * (mu_r - mu_s)) - T @ mu_s
    return [[round(float(T[i, j]), 5) for j in range(3)] + [round(float(t[i]), 3)] for i in range(3)]


def apply_matrix(img: np.ndarray, m: list[list[float]]) -> np.ndarray:
    import cv2
    return cv2.transform(img, np.array(m, np.float32))


def describe(m: list[list[float]]) -> dict:
    """Em palavras que se podem explicar ao utilizador (ganho médio, saturação, dominante)."""
    T = np.array([r[:3] for r in m])
    t = np.array([r[3] for r in m])
    gray = np.full(3, 128.0)
    out = T @ gray + t
    luma_gain = float(np.trace(T) / 3)
    return {"contrast_gain": round(luma_gain, 3), "brightness_shift": round(float(out.mean() - 128.0), 1),
            "warmth": round(float(out[0] - out[2]), 1), "tint": round(float(out[1] - (out[0] + out[2]) / 2), 1)}
