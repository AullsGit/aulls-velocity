"""Media Analyzer: vídeo do utilizador -> ClipProfile (sinais por frame + sujeito)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from velocity.io.frames import load_frames
from velocity.io.probe import probe
from velocity.media.subject import BackgroundSubtractionLocator, SubjectLocator
from velocity.reference.segmenter import SegmenterParams, instability, _find_spans
from velocity.reference.signals import compute_signals

FPS = 30.0


@dataclass
class ClipProfile:
    path: str
    n: int
    fps: float
    duration_s: float
    width: int
    height: int
    energy: np.ndarray  # [n] movimento total (fluxo médio / diagonal)
    subj: np.ndarray  # [n,4] cx, cy, altura_rel, presença
    lum: np.ndarray
    sat: np.ndarray
    sharp: np.ndarray
    bad: np.ndarray  # [n] bool: frame instável (corte/blur/flash do PRÓPRIO clip) -> evitar em janelas
    has_audio: bool
    masks: np.ndarray | None = None  # [n,H,W] uint8 máscara do sujeito (192 px de largura), se disponível
    subj_lum: np.ndarray | None = None  # [n] luma média DENTRO da máscara (nan sem máscara)
    bg_lum: np.ndarray | None = None  # [n] luma média FORA da máscara

    def window_bad(self, i: int, length: int) -> bool:
        return bool(self.bad[i : i + length].any())


def analyze_clip(path: str, locator: SubjectLocator | None = None) -> ClipProfile:
    info = probe(path)
    fr = load_frames(path, fps=FPS, width=192)
    sig = compute_signals(fr.rgb, fr.fps)
    cam = np.hypot(sig.tx, sig.ty) + np.abs(sig.zoom)
    locator = locator or BackgroundSubtractionLocator()
    loc = locator.locate(fr.rgb, cam)
    masks = getattr(locator, "last_masks", None)
    subj_lum = bg_lum = None
    if masks is not None:
        gray = fr.rgb.astype(np.float32).mean(axis=3)
        m = masks > 0
        cnt_in, cnt_out = m.sum(axis=(1, 2)), (~m).sum(axis=(1, 2))
        subj_lum = np.where(cnt_in > 20, (gray * m).sum(axis=(1, 2)) / np.maximum(cnt_in, 1), np.nan)
        bg_lum = (gray * ~m).sum(axis=(1, 2)) / np.maximum(cnt_out, 1)
    p = SegmenterParams()
    u = instability(sig, p)
    u[0] = 0
    bad = np.zeros(fr.n, bool)
    for a, b in _find_spans(u, p):
        bad[max(0, a - 1) : min(fr.n, b + 2)] = True
    return ClipProfile(
        path=path, n=fr.n, fps=fr.fps, duration_s=fr.n / fr.fps, width=info.width, height=info.height,
        energy=sig.flow_mag, subj=loc, lum=sig.lum, sat=sig.sat, sharp=sig.sharp, bad=bad, has_audio=info.has_audio,
        masks=masks, subj_lum=subj_lum, bg_lum=bg_lum,
    )
