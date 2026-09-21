import os

import numpy as np
import pytest

from velocity.evaluate.vibe_fidelity import compare_vibe
from velocity.reference.analyzer import analyze_reference
from velocity.render import textfx
from velocity.schemas.timeline import TextOverlay

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_text_only_inside_its_window_and_not_elsewhere():
    img = np.full((320, 180, 3), 60, np.uint8)
    t = TextOverlay(text="TESTE", start_frame=10, end_frame=40, x=0.5, y=0.2, width=0.6, height=0.05, entry="pop")
    assert np.array_equal(textfx.draw(img, t, 5), img)  # antes: intacto
    assert np.array_equal(textfx.draw(img, t, 50), img)  # depois: intacto
    assert not np.array_equal(textfx.draw(img, t, 25), img)  # durante: desenhado


@pytest.mark.bench
def test_vibe_of_identical_reference_scores_near_one_and_is_not_flagged():
    p = os.path.join(ROOT, "bench", "data", "refs", "ref_04.mp4")
    if not os.path.exists(p):
        pytest.skip("material ausente")
    d = analyze_reference(p, with_text=False)
    r = compare_vibe(d, d)
    assert r["score"] > 0.9 and not r["over_effected"] and not r["under_effected"]
    assert d.vibe.energy_curve and 0 <= d.vibe.aggressiveness <= 1
