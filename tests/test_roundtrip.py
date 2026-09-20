"""Verdade conhecida: renderiza transições em posições/tipos conhecidos e exige que o analisador as recupere."""
import os

import pytest

from velocity.detector.velocity_detector import detect
from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render
from velocity.schemas.edit_dna import Transition
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYN = os.path.join(ROOT, "bench", "data", "synthetic")
pytestmark = pytest.mark.bench


def _timeline():
    lens, starts = [45, 45, 60, 50, 40], [0, 45, 90, 150, 200]
    spec = [None, ("blur", 4, 4, "horizontal", .8), ("flash", 4, 4, "none", .9), ("cut", 0, 1, "none", 1.), ("glitch", 3, 3, "none", .8)]
    src = [os.path.join(SYN, f"clean_{c}.mp4") for c in "abcde"]
    t0 = [1.0, 4.0, 0.5, 6.0, 9.0]
    shots, truth = [], []
    for i, (L, s) in enumerate(zip(lens, starts)):
        tin = None
        if i:
            ty, pre, post, kind, inten = spec[i]
            tin = Transition(type=ty, start_frame=s - pre, end_frame=s + post, cut_frame=s, intensity=inten, blur_kind=kind)
            truth.append((s, ty))
        shots.append(TimelineShot(index=i, start_frame=s, end_frame=s + L, clip=ClipUse(source_path=src[i], src_start_s=t0[i]), transition_in=tin))
    return Timeline(width=360, height=640, n_frames=sum(lens), shots=shots), truth


def test_render_duration_is_exact_and_transitions_recovered(tmp_path):
    tl, truth = _timeline()
    out = str(tmp_path / "rt.mp4")
    render(tl, out)
    dna = analyze_reference(out, with_audio=False)
    assert dna.n_frames == tl.n_frames  # zero drift
    found = [(t.cut_frame, t.type) for t in dna.transitions()]
    assert len(found) == len(truth)
    for (fs, ts), (ft, tt) in zip(sorted(found), truth):
        assert abs(fs - ft) <= 1 and ts == tt


def test_detector_rejects_hard_cut_montage():
    d = analyze_reference(os.path.join(SYN, "neg_hardcuts.mp4"), with_audio=False)
    assert len(d.transitions()) == 5 and all(t.type == "cut" for t in d.transitions())
    assert not detect(d).is_velocity
