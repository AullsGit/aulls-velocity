"""Transições direcionais: renderiza whip esq/dir e zoom in/out com direção conhecida e mede a direção no output."""
import os

import pytest

from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render
from velocity.schemas.edit_dna import Transition
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYN = os.path.join(ROOT, "bench", "data", "synthetic")
pytestmark = pytest.mark.bench

SPEC = [("slide_right", {"dx": 0.5, "dy": 0.0, "zoom": 0.0}), ("slide_left", {"dx": -0.5, "dy": 0.0, "zoom": 0.0}),
        ("zoom_in", {"dx": 0.0, "dy": 0.0, "zoom": 0.4}), ("zoom_out", {"dx": 0.0, "dy": 0.0, "zoom": -0.4})]


def _build():
    lens = [60] * 5
    src = [os.path.join(SYN, f"clean_{c}.mp4") for c in "abcde"]
    shots, start = [], 0
    for i, L in enumerate(lens):
        tin = None
        if i:
            d, mo = SPEC[i - 1]
            tin = Transition(type="blur", start_frame=start - 6, end_frame=start + 6, cut_frame=start, intensity=0.9,
                             blur_kind="horizontal", direction=d, motion=mo, envelope=[0.2, 0.5, 0.8, 1.0, 1.0, 0.8, 0.6, 0.4, 0.3, 0.2, 0.1, 0.05])
        shots.append(TimelineShot(index=i, start_frame=start, end_frame=start + L,
                                  clip=ClipUse(source_path=src[i], src_start_s=0.5 + 1.0 * i), transition_in=tin))
        start += L
    return Timeline(width=360, height=640, n_frames=start, shots=shots)


def test_transitions_render_and_directions_are_measured(tmp_path):
    out = str(tmp_path / "tr.mp4")
    render(_build(), out)
    d = analyze_reference(out, with_audio=False)
    got = [(t.cut_frame, t.type, t.direction) for t in d.transitions()]
    assert len(got) == 4, f"esperava 4 transições, obtive {got}"
    hits = sum(1 for (c, ty, dr), (exp, _) in zip(got, SPEC) if dr == exp)
    assert hits >= 3, f"direções medidas {[g[2] for g in got]} vs esperadas {[s[0] for s in SPEC]}"
