"""Trocas de cena que a instabilidade por frame não vê: dissoluções e cortes dentro de movimento forte.

O segmentador procura picos entre frames vizinhos. Uma dissolução espalha a troca por 10-30 frames
(nenhum pico) e num trecho com muito movimento um corte fica abaixo da mediana local. Aqui compara-se
o que está ANTES com o que está DEPOIS de cada instante (layout de cor + histograma, imunes a
pulsos de exposição), descontando quanto cada lado já varia por si: isto é uma curva de novidade,
com picos onde a cena muda, seja de repente ou aos poucos. Um pico vale como corte só se a paleta
também mudar (um pan lento muda o layout, não a paleta).

Medido contra edições com verdade conhecida (bench/synth) e contra uma referência real com
16 trocas anotadas à mão: ver tests/test_soft_cuts.py e docs/AGENT.md.
"""
from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import find_peaks

GRID = 6
WINDOW = 10  # frames de cada lado
GUARD = 2  # frames ignorados à volta do instante (a própria transição)
ON = 3.0  # novidade mínima do pico
PROMINENCE = 1.5  # quanto o pico se destaca dos vales vizinhos
MIN_DIST = 8  # frames entre trocas
COLOR_MIN = 0.22  # distância mínima de paleta entre os dois lados (Bhattacharyya)
HARD_D_RATIO = 2.2  # pico de |frame-anterior| acima da mediana local: é um corte seco, não uma dissolução
MAX_HALF = 15  # meia-janela máxima de uma dissolução, em frames


def _features(rgb: np.ndarray) -> np.ndarray:
    n = len(rgb)
    out = np.zeros((n, GRID * GRID * 3 + 3 * 8), np.float32)
    for i, f in enumerate(rgb):
        lab = cv2.cvtColor(f, cv2.COLOR_RGB2LAB)
        small = cv2.resize(lab, (GRID, GRID), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
        small = (small - small.mean()) / (small.std() + 1e-3)
        hs = []
        for c in range(3):
            h = cv2.calcHist([lab], [c], None, [8], [0, 256]).ravel()
            hs.append(h / (h.sum() + 1e-6) * 4)
        out[i] = np.concatenate([small, *hs])
    return out


def novelty(rgb: np.ndarray) -> np.ndarray:
    fe = _features(rgb)
    n = len(fe)
    nov = np.zeros(n)
    for t in range(WINDOW + GUARD, n - WINDOW - GUARD):
        a = fe[t - WINDOW - GUARD:t - GUARD]
        b = fe[t + GUARD:t + WINDOW + GUARD]
        ma, mb = np.median(a, axis=0), np.median(b, axis=0)
        within = 0.5 * (np.linalg.norm(a - ma, axis=1).mean() + np.linalg.norm(b - mb, axis=1).mean())
        nov[t] = np.linalg.norm(ma - mb) / (within + 0.8)
    return nov


def _palette_distance(rgb: np.ndarray, a: int, b: int) -> float:
    def hist(frames):
        m = np.median(frames, axis=0).astype(np.uint8)
        h = cv2.calcHist([m], [0, 1, 2], None, [6, 6, 6], [0, 256] * 3)
        cv2.normalize(h, h, 1, 0, cv2.NORM_L1)
        return h
    return float(cv2.compareHist(hist(rgb[max(0, a - 6):max(1, a)]), hist(rgb[b:min(len(rgb), b + 6)]), cv2.HISTCMP_BHATTACHARYYA))


def soft_boundaries(rgb: np.ndarray, d: np.ndarray, known: list[int]) -> list[dict]:
    """Trocas que faltam a `known` (frames de corte já detetados).

    Devolve dicts {type: "dissolve"|"cut", cut, start, end, score}: `cut` é o primeiro frame em que
    a cena nova domina; [start, end) é a janela da transição."""
    n = len(rgb)
    if n < 2 * (WINDOW + GUARD) + 2:
        return []
    nov = novelty(rgb)
    idx, _ = find_peaks(nov, height=ON, distance=MIN_DIST, prominence=PROMINENCE)
    out: list[dict] = []
    for t in (int(i) for i in idx):
        if any(abs(t - k) <= MIN_DIST for k in known):
            continue
        if _palette_distance(rgb, t - GUARD, t + GUARD) < COLOR_MIN:
            continue  # o layout mudou mas a paleta não: pan ou objeto a passar
        lo, hi = max(1, t - 30), min(n, t + 30)
        base = float(np.median(d[lo:hi])) + 1.0
        near0, near1 = max(1, t - 3), min(n, t + 4)
        j = near0 + int(np.argmax(d[near0:near1]))
        if d[j] >= HARD_D_RATIO * base:
            out.append({"type": "cut", "cut": j, "start": j, "end": j + 1, "score": round(float(nov[t]), 2)})
            continue
        a = t
        while a > 1 and t - a < MAX_HALF and d[a] > 1.25 * base:
            a -= 1
        b = t
        while b < n - 1 and b - t < MAX_HALF and d[b] > 1.25 * base:
            b += 1
        half = int(np.clip(max(t - a, b - t), 4, MAX_HALF))
        out.append({"type": "dissolve", "cut": t, "start": max(1, t - half), "end": min(n - 1, t + half), "score": round(float(nov[t]), 2)})
    return out
