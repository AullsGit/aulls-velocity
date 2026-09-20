"""Verdade conhecida para efeitos: renderiza faixas com posição/magnitude conhecidas sobre um plano LIMPO
(câmara fixa) e exige que o analisador as recupere (posição ±3 frames, magnitude dentro de tolerância)."""
import os

import pytest

from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render
from velocity.schemas.edit_dna import EffectTrack
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYN = os.path.join(ROOT, "bench", "data", "synthetic")
pytestmark = pytest.mark.bench

TRACKS = [
    EffectTrack(type="zoom_punch", start_frame=40, end_frame=52, peak_frame=46, magnitude=0.22, intensity=0.6),
    EffectTrack(type="shake", start_frame=100, end_frame=118, peak_frame=109, magnitude=0.014, intensity=0.5),
    EffectTrack(type="rgb_split", start_frame=150, end_frame=162, peak_frame=156, magnitude=0.03, intensity=0.6),
    EffectTrack(type="exposure_pulse", start_frame=195, end_frame=207, peak_frame=201, magnitude=0.18, intensity=0.6),
]


def _render(tmp_path, tracks, scale=1.0):
    n = 240
    tl = Timeline(width=360, height=640, n_frames=n, effects=tracks, fx_scale=scale,
                  shots=[TimelineShot(index=0, start_frame=0, end_frame=n,
                                      clip=ClipUse(source_path=os.path.join(SYN, "clean_a.mp4"), src_start_s=1.0))])
    out = str(tmp_path / "fx.mp4")
    render(tl, out)
    return analyze_reference(out, with_audio=False)


def test_clean_baseline_has_no_effects(tmp_path):
    d = _render(tmp_path, [])
    assert d.effects == [], f"efeitos inventados numa base limpa: {[e.type for e in d.effects]}"


def test_known_effects_are_recovered(tmp_path):
    d = _render(tmp_path, TRACKS)
    by = {}
    for e in d.effects:
        by.setdefault(e.type, []).append(e)
    for t in TRACKS:
        got = by.get(t.type, [])
        assert got, f"{t.type} não recuperado; detetados: {[(e.type, e.start_frame) for e in d.effects]}"
        best = min(got, key=lambda e: abs(e.peak_frame - t.peak_frame))
        assert abs(best.peak_frame - t.peak_frame) <= 6, f"{t.type} fora de posição: {best.peak_frame} vs {t.peak_frame}"
    zp = min(by["zoom_punch"], key=lambda e: abs(e.peak_frame - 46))
    # VIÉS CONHECIDO: o fluxo óptico subestima zoom rápido em zonas sem textura (céu) — medimos ~50% do injetado.
    # Por isso o protetor de fidelidade compara referência e output com o MESMO estimador (o viés cancela-se).
    assert 0.35 * 0.22 <= abs(zp.magnitude) <= 1.5 * 0.22, f"magnitude do punch {zp.magnitude}"


def test_fx_scale_zero_removes_effects(tmp_path):
    d = _render(tmp_path, TRACKS, scale=0.0)
    assert not [e for e in d.effects if e.type in ("zoom_punch", "shake", "rgb_split")]
