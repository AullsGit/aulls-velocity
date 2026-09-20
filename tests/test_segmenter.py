"""Segmentador sobre frames sintéticos (sem I/O de vídeo): corte seco e blur com posição conhecida."""
import cv2
import numpy as np

from velocity.reference.segmenter import segment
from velocity.reference.signals import compute_signals


def _texture(seed, h=96, w=96):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h // 4, w // 4, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_CUBIC)
    tint = rng.integers(0, 80, 3)
    return np.clip(img.astype(int) + tint, 0, 255).astype(np.uint8)


def _shot(seed, n):
    base = _texture(seed)
    return [np.roll(base, i // 4, axis=1) for i in range(n)]  # leve movimento


def test_hard_cut_detected_within_one_frame():
    rgb = np.stack(_shot(1, 45) + _shot(2, 45) + _shot(3, 40))
    seg = segment(rgb, compute_signals(rgb, 30.0))
    cuts = [t.cut_frame for t in seg.transitions]
    assert len(cuts) == 2
    assert abs(cuts[0] - 45) <= 1 and abs(cuts[1] - 90) <= 1
    assert all(t.type == "cut" for t in seg.transitions)


def test_no_false_cuts_in_static_shot():
    rgb = np.stack(_shot(7, 90))
    seg = segment(rgb, compute_signals(rgb, 30.0))
    assert seg.transitions == []
