"""Cenário A (mesmo material da referência): alinhamento às fontes e reconstrução exata.

Usa uma referência sintética com VERDADE CONHECIDA (velocity/evaluate/synth.py), gerada aqui
em ponto pequeno: cada frame sabe de que fonte veio, em que instante, com que zoom/pan, cor e
transição. Os limiares são de regressão — o banco completo (fidelity_bench) dá os números.
"""
import math

import numpy as np
import pytest

from velocity.edit.timeline_ops import EditDoc, EditShot, apply_edit
from velocity.evaluate import fidelity_bench as fb
from velocity.evaluate import synth
from velocity.evaluate.synth import GTEdit, GTShot, GTTransition, Source, SourceSpec
from velocity.schemas.edit_dna import Curve
from velocity.schemas.timeline import ClipUse, ExactTrack, Timeline, TimelineShot

SIZE = (270, 480)


def _edit() -> GTEdit:
    shots = [
        GTShot(0, 10, 14),
        GTShot(1, 20, 12, speed=2.0, zoom=(1.3, 1.0), ease="out"),
        GTShot(2, 30, 14, zoom=(1.25, 1.25), center=((0.42, 0.5), (0.58, 0.5))),
        GTShot(0, 60, 14, speed=0.5, zoom=(1.15, 1.15), color=(0.04, 1.1, 0.5)),
    ]
    trans = [GTTransition("flash", 4), GTTransition("cut"), GTTransition("dip_black", 4)]
    return GTEdit("tiny", "test", 30.0, SIZE, shots, trans, grade=(1.05, 1.0, 0.9))


@pytest.fixture(scope="module")
def case(tmp_path_factory):
    d = tmp_path_factory.mktemp("recon")
    specs = [SourceSpec(seed=7 + i, size=SIZE, drift=dr, seconds=3.5) for i, dr in enumerate([(1.0, 0.4), (0.4, 1.0), (1.5, 0.0)])]
    srcs = [Source(s) for s in specs]
    a = [s.write(str(d / f"a{i}.mp4")) for i, s in enumerate(srcs)]
    b = [Source(SourceSpec(seed=70 + i, size=SIZE, drift=s.drift, seconds=3.5)).write(str(d / f"b{i}.mp4")) for i, s in enumerate(specs)]
    edit = _edit()
    ref = str(d / "ref.mp4")
    truth = synth.render_reference(edit, srcs, ref)
    from velocity.reference.analyzer import analyze_reference
    dna = analyze_reference(ref, with_audio=False, with_text=False)
    return {"ref": ref, "A": a, "B": b, "truth": truth, "dna": dna, "dir": d}


@pytest.fixture(scope="module")
def aligned(case):
    from velocity.matching.source_align import align_reference
    return align_reference(case["ref"], case["dna"], case["A"])


def test_alinha_todos_os_planos_com_fonte_instante_e_cortes_certos(case, aligned):
    gt = case["truth"]
    assert aligned is not None
    assert all(s.ok for s in aligned.shots)
    assert aligned.bounds == [0] + gt["cuts"] + [gt["n_frames"]]
    errs = []
    for s, g in zip(aligned.shots, gt["shots"]):
        assert s.clip == g["source"]
        for k, j in enumerate(s.src_frames):
            errs.append(abs(j - math.floor(g["src_frame"] + k * g["speed"] + 1e-6)))
        assert s.speed == pytest.approx(g["speed"], rel=0.06)
    assert np.mean(np.array(errs) == 0) >= 0.9 and max(errs) <= 1


def test_material_diferente_nao_e_reconstruido(case):
    from velocity.matching.source_align import align_reference
    assert align_reference(case["ref"], case["dna"], case["B"]) is None


def test_render_reconstruido_reproduz_a_referencia(case, aligned):
    from velocity.matching.reconstruct import reconstruct_timeline
    from velocity.matching.fx_plan import plan_effects
    from velocity.render.compositor import render
    dna = case["dna"]
    effects, _ = plan_effects(dna, dna.cut_frames())
    fallback = Timeline(fps=dna.fps, width=SIZE[0], height=SIZE[1], n_frames=dna.n_frames, shots=[], effects=effects)
    tl, info = reconstruct_timeline(dna, aligned, SIZE, fallback=fallback)
    assert info["exact_shots"] == len(tl.shots) == 4 and tl.n_frames == case["truth"]["n_frames"]
    out = str(case["dir"] / "out.mp4")
    render(tl, out, crf=16, preset="veryfast")
    v = fb.visual_compare(case["ref"], out, case["truth"])
    assert v["frames"] == case["truth"]["n_frames"]
    assert v["ssim_mean"] >= 0.88 and v["ssim_mean"] >= v["ssim_chance"] + 0.25
    assert v["deltaE_frame_mean"] <= 4.0


def test_compositor_exato_e_a_identidade_com_rasto_identidade(case):
    from velocity.io.frames import decode_range
    from velocity.render.compositor import render
    src = case["A"][0]
    n = 12
    ex = ExactTrack(src_fps=30.0, src_frames=list(range(20, 20 + n)), warps=[(1, 0, 0, 0, 1, 0)] * n, confidence=1.0)
    tl = Timeline(fps=30.0, width=SIZE[0], height=SIZE[1], n_frames=n,
                  shots=[TimelineShot(index=0, start_frame=0, end_frame=n, clip=ClipUse(source_path=src, src_start_s=20 / 30, exact=ex))])
    out = str(case["dir"] / "ident.mp4")
    render(tl, out, crf=12, preset="veryfast")
    a = decode_range(out, SIZE[0], 30.0, rgb=True).astype(np.float32)
    b = decode_range(src, SIZE[0], 30.0, 20, n, rgb=True).astype(np.float32)
    psnr = [10 * np.log10(255 ** 2 / max(float(np.mean((x - y) ** 2)), 1e-6)) for x, y in zip(a, b)]
    assert len(psnr) == n and min(psnr) >= 35


def _exact_tl() -> Timeline:
    n = 20
    ex = ExactTrack(src_fps=30.0, src_frames=[100 + 2 * k for k in range(n)], warps=[(0.8, 0, 0.1, 0, 0.8, 0.1)] * n, confidence=0.99)
    clip = ClipUse(source_path="uploads/a.mp4", src_start_s=100 / 30, speed=Curve(t=[0, 1], v=[2, 2]), exact=ex)
    return Timeline(fps=30.0, width=270, height=480, n_frames=n, shots=[TimelineShot(index=0, start_frame=0, end_frame=n, clip=clip)])


def test_editor_corte_preserva_o_rasto_medido():
    tl = _exact_tl()
    out = apply_edit(tl, EditDoc(shots=[EditShot(orig=0, trim_in_s=10 / 30, duration_s=10 / 30)]), {})
    ex = out.shots[0].clip.exact
    assert ex is not None and ex.src_frames == [110 + 2 * k for k in range(10)]
    assert out.shots[0].clip.src_start_s == pytest.approx(110 / 30)


def test_editor_mudar_velocidade_ou_zoom_volta_ao_modo_parametrico():
    tl = _exact_tl()
    for es in (EditShot(orig=0, duration_s=0.5, speed=1.0), EditShot(orig=0, duration_s=0.5, zoom=1.5)):
        assert apply_edit(tl, EditDoc(shots=[es]), {}).shots[0].clip.exact is None
    # mais longo do que o que foi medido: não se inventa o resto
    assert apply_edit(tl, EditDoc(shots=[EditShot(orig=0, duration_s=1.0)]), {}).shots[0].clip.exact is None
