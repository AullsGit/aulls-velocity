"""Sinais por frame extraídos de UM decode (ver velocity.io.frames).

Tudo aqui é numérico e determinístico; a interpretação (o que é corte, flash, etc.)
vive em segmenter.py. Separar as duas coisas permite testar e afinar cada uma.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from velocity.schemas.edit_dna import Letterbox


@dataclass
class Signals:
    n: int
    fps: float
    lum: np.ndarray  # [n] luma médio 0-255
    clip_hi: np.ndarray  # [n] fração de pixels > 235
    clip_lo: np.ndarray  # [n] fração de pixels < 20
    sharp: np.ndarray  # [n] variância do Laplaciano (nitidez)
    d: np.ndarray  # [n] |frame[i]-frame[i-1]| médio (d[0]=0)
    hist_d: np.ndarray  # [n] distância de histograma HSV vs frame anterior (Bhattacharyya)
    sat: np.ndarray  # [n] saturação média
    # Fluxo óptico global (frame i-1 -> i), ajuste afim robusto. [n], 0 em i=0.
    tx: np.ndarray  # translação / largura
    ty: np.ndarray  # translação / altura
    zoom: np.ndarray  # d ln(escala) por frame (>0 = zoom in)
    rot: np.ndarray  # rotação rad/frame
    flow_mag: np.ndarray  # magnitude média do fluxo / diagonal (movimento total)
    flow_resid: np.ndarray  # resíduo depois do ajuste afim (movimento não-global)
    flow_ok: np.ndarray  # [n] bool, ajuste confiável


def detect_letterbox(rgb: np.ndarray, thresh: float = 14.0) -> Letterbox:
    """Barras pretas fixas: linhas/colunas cujo brilho, em TODO o vídeo, quase nunca sai de ~preto."""
    g = rgb.astype(np.float32).mean(axis=3)  # [N,H,W]
    n, h, w = g.shape
    row = np.percentile(g.mean(axis=2), 98, axis=0)  # [H]
    col = np.percentile(g.mean(axis=1), 98, axis=0)  # [W]

    def run(mask):
        k = 0
        for v in mask:
            if v:
                k += 1
            else:
                break
        return k

    top = run(row < thresh)
    bottom = run(row[::-1] < thresh)
    left = run(col < thresh)
    right = run(col[::-1] < thresh)
    # ignora barras insignificantes (<2%) e nunca corta mais de 40% de cada lado
    def frac(k, total):
        f = k / total
        return round(min(f, 0.4), 4) if f >= 0.02 else 0.0

    return Letterbox(top_frac=frac(top, h), bottom_frac=frac(bottom, h), left_frac=frac(left, w), right_frac=frac(right, w))


def crop_letterbox(rgb: np.ndarray, lb: Letterbox) -> np.ndarray:
    n, h, w, _ = rgb.shape
    t, b = int(round(lb.top_frac * h)), int(round(lb.bottom_frac * h))
    l, r = int(round(lb.left_frac * w)), int(round(lb.right_frac * w))
    return rgb[:, t : h - b, l : w - r]


def _fit_affine_flow(flow: np.ndarray):
    """Ajusta flow(p) ≈ t + M (p - c) por mínimos quadrados robustos (2 passagens).

    Coordenadas normalizadas pela LARGURA (x,y em [-0.5,0.5] × aspeto). Devolve
    (tx, ty, zoom_rate, rot_rate, resid, ok). Em coords normalizadas por w:
    tx,ty são frações da largura; zoom_rate = trace(M)/2 ≈ d ln(escala).
    """
    h, w, _ = flow.shape
    ys, xs = np.mgrid[0:h, 0:w]
    px = (xs - (w - 1) / 2) / w
    py = (ys - (h - 1) / 2) / w
    fx = flow[..., 0] / w
    fy = flow[..., 1] / w
    P = np.stack([np.ones_like(px), px, py], axis=-1).reshape(-1, 3)
    ax = fx.reshape(-1)
    ay = fy.reshape(-1)
    keep = np.ones(P.shape[0], bool)
    for _ in range(2):
        cx, *_ = np.linalg.lstsq(P[keep], ax[keep], rcond=None)
        cy, *_ = np.linalg.lstsq(P[keep], ay[keep], rcond=None)
        rx = ax - P @ cx
        ry = ay - P @ cy
        res = np.hypot(rx, ry)
        thr = max(np.median(res) * 2.5, 1e-6)
        keep = res <= thr
        if keep.sum() < 0.2 * keep.size:
            return 0.0, 0.0, 0.0, 0.0, float(res.mean()), False
    t_x, m11, m12 = cx
    t_y, m21, m22 = cy
    zoom = (m11 + m22) / 2.0
    rot = (m21 - m12) / 2.0
    ok = keep.mean() > 0.5
    return float(t_x), float(t_y), float(zoom), float(rot), float(res[keep].mean()), bool(ok)


def compute_signals(rgb: np.ndarray, fps: float, flow_width: int = 96) -> Signals:
    """rgb: [N,H,W,3] uint8 já sem letterbox."""
    n, h, w, _ = rgb.shape
    lum = np.zeros(n, np.float32)
    clip_hi = np.zeros(n, np.float32)
    clip_lo = np.zeros(n, np.float32)
    sharp = np.zeros(n, np.float32)
    d = np.zeros(n, np.float32)
    hist_d = np.zeros(n, np.float32)
    sat = np.zeros(n, np.float32)
    tx = np.zeros(n, np.float32)
    ty = np.zeros(n, np.float32)
    zoom = np.zeros(n, np.float32)
    rot = np.zeros(n, np.float32)
    flow_mag = np.zeros(n, np.float32)
    flow_resid = np.zeros(n, np.float32)
    flow_ok = np.zeros(n, bool)

    fh = max(16, int(round(flow_width * h / w / 2)) * 2)
    diag_small = float(np.hypot(flow_width, fh))
    prev_gray = None
    prev_small = None
    prev_hist = None
    for i in range(n):
        bgr = cv2.cvtColor(rgb[i], cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        gf = gray.astype(np.float32)
        lum[i] = gf.mean()
        clip_hi[i] = float((gray > 235).mean())
        clip_lo[i] = float((gray < 20).mean())
        sharp[i] = cv2.Laplacian(gray, cv2.CV_32F).var()
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        sat[i] = hsv[..., 1].mean()
        hist = cv2.calcHist([hsv], [0, 1], None, [12, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist, 1.0, 0.0, cv2.NORM_L1)
        small = cv2.resize(gray, (flow_width, fh), interpolation=cv2.INTER_AREA)
        if prev_gray is not None:
            d[i] = float(np.abs(gf - prev_gray).mean())
            hist_d[i] = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
            flow = cv2.calcOpticalFlowFarneback(prev_small, small, None, 0.5, 3, 11, 3, 5, 1.1, 0)
            tx_, ty_, z_, r_, res_, ok_ = _fit_affine_flow(flow)
            tx[i], ty[i], zoom[i], rot[i], flow_resid[i], flow_ok[i] = tx_, ty_, z_, r_, res_, ok_
            flow_mag[i] = float(np.hypot(flow[..., 0], flow[..., 1]).mean() / diag_small)
            flow_resid[i] = flow_resid[i] * flow_width / diag_small  # mesma unidade que flow_mag
        prev_gray, prev_small, prev_hist = gf, small, hist

    return Signals(n, fps, lum, clip_hi, clip_lo, sharp, d, hist_d, sat, tx, ty, zoom, rot, flow_mag, flow_resid, flow_ok)
