"""Localização do sujeito principal por frame. Interface + implementação v1.

v1 (sem modelos de IA, sem downloads):
  * câmara fixa  -> subtração de fundo (mediana temporal) + maior componente conexa;
  * câmara móvel -> saliência espectral (spectral residual, OpenCV contrib).
Devolve por frame (cx, cy, altura_rel, presença). Substituível por um detetor de pessoas
(YOLO/MediaPipe) implementando o mesmo Protocol.
"""
from __future__ import annotations

from typing import Protocol

import cv2
import numpy as np


class SubjectLocator(Protocol):
    def locate(self, rgb: np.ndarray, cam_motion: np.ndarray) -> np.ndarray:
        """rgb [N,H,W,3]; cam_motion [N] (movimento global). -> [N,4] (cx, cy, height_frac, present)."""


class BackgroundSubtractionLocator:
    def __init__(self, static_thresh: float = 0.004, diff_thresh: float = 28.0, min_area_frac: float = 0.002):
        self.static_thresh, self.diff_thresh, self.min_area_frac = static_thresh, diff_thresh, min_area_frac

    def locate(self, rgb: np.ndarray, cam_motion: np.ndarray) -> np.ndarray:
        n, h, w, _ = rgb.shape
        out = np.tile(np.array([0.5, 0.5, 0.0, 0.0], np.float32), (n, 1))
        gray = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in rgb]).astype(np.float32)
        gray = np.stack([cv2.GaussianBlur(g, (5, 5), 0) for g in gray])
        static = float(np.median(cam_motion[1:])) < self.static_thresh if n > 2 else True
        if static:
            bg = np.median(gray[:: max(1, n // 40)], axis=0)
            for i in range(n):
                mask = (np.abs(gray[i] - bg) > self.diff_thresh).astype(np.uint8)
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
                mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
                nlab, lab, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
                if nlab <= 1:
                    continue
                k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                area = stats[k, cv2.CC_STAT_AREA]
                if area < self.min_area_frac * h * w:
                    continue
                x, y, bw, bh = stats[k, :4]
                out[i] = ((x + bw / 2) / w, (y + bh / 2) / h, bh / h, 1.0)
        else:
            sal = cv2.saliency.StaticSaliencySpectralResidual_create()
            for i in range(n):
                ok, m = sal.computeSaliency(cv2.cvtColor(rgb[i], cv2.COLOR_RGB2BGR))
                if not ok:
                    continue
                m = cv2.GaussianBlur(m.astype(np.float32), (0, 0), 3)
                thr = m.mean() + 1.5 * m.std()
                ys, xs = np.nonzero(m > thr)
                if len(xs) < 20:
                    continue
                out[i] = (xs.mean() / w, ys.mean() / h, (ys.max() - ys.min()) / h, 0.5)
        # suavização temporal (mediana móvel) para o centro não tremer
        for c in range(3):
            pad = np.pad(out[:, c], (3, 3), mode="edge")
            out[:, c] = np.array([np.median(pad[i : i + 7]) for i in range(n)])
        return out
