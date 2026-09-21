"""Regressão: clip cujas janelas estão TODAS marcadas como instáveis (p.ex. um output anterior usado como input)
não pode fazer o matching rebentar."""
import os

import pytest

from velocity.matching.engine import match
from velocity.media.analyzer import analyze_clip
from velocity.reference.analyzer import analyze_reference

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.bench


def test_match_survives_clip_with_only_unstable_windows():
    ref = os.path.join(ROOT, "bench", "data", "refs", "ref_04.mp4")
    clip = os.path.join(ROOT, "bench", "data", "user", "user_03.mp4")
    if not (os.path.exists(ref) and os.path.exists(clip)):
        pytest.skip("material ausente")
    dna = analyze_reference(ref, with_audio=False, with_text=False)
    c = analyze_clip(clip)
    c.bad[:] = True
    ms = match(dna, [c])
    assert len(ms) == len(dna.shots)
