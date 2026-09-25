"""Extensão/aparo de duração ao alvo do produto (~25-30s) — ver velocity/matching/extend.py."""
import os

import pytest

from velocity.matching.extend import extend_reference
from velocity.reference.analyzer import analyze_reference

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.bench


def _load(name="refs_aggr/aggr_10"):
    p = os.path.join(ROOT, "bench", "data", f"{name}.mp4")
    if not os.path.exists(p):
        pytest.skip("material ausente")
    return analyze_reference(p)


def test_short_reference_is_tiled_into_target_range():
    dna = _load()
    assert dna.duration_s < 25.0  # pré-condição do teste: esta referência É curta
    ext, info = extend_reference(dna, target_min=25.0, target_max=31.0)
    assert 25.0 <= ext.duration_s <= 31.0
    assert info["acao"] == "repetida"
    cuts = ext.cut_frames()
    assert all(a < b for a, b in zip(cuts, cuts[1:]))  # sem sobreposição/regressão no tempo
    assert ext.n_frames == ext.shots[-1].end_frame
    assert len(ext.shots) > len(dna.shots)  # o padrão foi de facto repetido


def test_reference_already_in_range_is_untouched():
    dna = _load()
    ext, info = extend_reference(dna, target_min=dna.duration_s - 1, target_max=dna.duration_s + 1)
    assert info["acao"] == "nenhuma"
    assert ext is dna


def test_long_reference_is_trimmed_without_crashing():
    dna = _load()
    ext, info = extend_reference(dna, target_min=5.0, target_max=10.0)
    assert info["acao"] == "aparado"
    assert ext.duration_s <= 10.0 + 1e-6
    assert ext.n_frames == ext.shots[-1].end_frame


def test_extended_dna_renders_to_exact_target_duration(tmp_path):
    from velocity.matching.engine import MatchWeights, match
    from velocity.matching.plan import build_timeline
    from velocity.render.compositor import render

    user_dir = os.path.join(ROOT, "bench", "data", "user")
    clips_paths = [os.path.join(user_dir, f) for f in os.listdir(user_dir) if f.endswith(".mp4")]
    if not clips_paths:
        pytest.skip("material do utilizador ausente")
    from velocity.media.analyzer import analyze_clip

    dna = _load()
    ext, _ = extend_reference(dna, target_min=25.0, target_max=31.0)
    clips = [analyze_clip(p) for p in clips_paths]
    w = MatchWeights()
    ms = match(ext, clips, w)
    tl, _ = build_timeline(ext, clips, ms, size=(360, 640), weights=w, fx=True)
    out = str(tmp_path / "ext.mp4")
    render(tl, out)
    out_dna = analyze_reference(out, with_audio=False, with_text=False)
    assert abs(out_dna.duration_s - ext.duration_s) < 0.05
