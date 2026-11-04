"""Trocas de cena que a instabilidade por frame não vê: dissoluções (com verdade conhecida)."""
import numpy as np

from velocity.reference.segmenter import segment
from velocity.reference.signals import compute_signals
from velocity.reference.soft_cuts import novelty, soft_boundaries


def _scene(rng, n, base, h=108, w=192):
    """Cena com textura própria, a mexer devagar (não é uma imagem parada)."""
    tex = rng.random((h // 12 + 2, w // 12 + 2, 3)) * 120 - 60
    tex = np.kron(tex, np.ones((12, 12, 1)))
    out = []
    for k in range(n):
        dx = k // 4
        out.append(np.clip(np.array(base, np.float32) + np.roll(tex, dx, axis=1)[:h, :w], 0, 255))
    return np.stack(out).astype(np.uint8)


def _dissolve(a, b, k):
    w = np.linspace(0, 1, k)[:, None, None, None]
    return (a[-k:].astype(np.float32) * (1 - w) + b[:k].astype(np.float32) * w).astype(np.uint8)


def _edit(k=20):
    rng = np.random.default_rng(3)
    a, b, c = _scene(rng, 90, (190, 70, 60)), _scene(rng, 90, (50, 90, 200)), _scene(rng, 90, (60, 170, 70))
    return np.concatenate([a[:-k], _dissolve(a, b, k), b[k:-k], _dissolve(b, c, k), c[k:]]), [80, 150]


def test_dissolucao_e_detetada_perto_do_meio_da_mistura():
    rgb, truth = _edit()
    sig = compute_signals(rgb, 30.0)
    seg = segment(rgb, sig)
    cuts = [t.cut_frame for t in seg.transitions]
    for g in truth:
        assert any(abs(c - g) <= 8 for c in cuts), (cuts, truth)
    assert len(cuts) <= len(truth) + 1, cuts
    assert any(t.type == "dissolve" for t in seg.transitions)
    d = next(t for t in seg.transitions if t.type == "dissolve")
    assert d.start_frame < d.cut_frame < d.end_frame


def test_mesma_cena_a_mexer_nao_inventa_trocas():
    rng = np.random.default_rng(5)
    rgb = _scene(rng, 200, (120, 120, 120))
    sig = compute_signals(rgb, 30.0)
    assert soft_boundaries(rgb, sig.d, []) == []
    assert novelty(rgb).max() < 3.0


def test_trocas_ja_conhecidas_nao_se_repetem():
    rgb, truth = _edit()
    sig = compute_signals(rgb, 30.0)
    assert soft_boundaries(rgb, sig.d, truth) == []


def test_video_curto_nao_rebenta():
    rgb = np.zeros((10, 54, 96, 3), np.uint8)
    sig = compute_signals(rgb, 30.0)
    assert soft_boundaries(rgb, sig.d, []) == []
