"""Segundo motor (MLT) atrás da mesma interface: precisão ao frame e escolha do motor pelas
capacidades do plano. Precisa do `melt` (VELOCITY_MELT=melt, ou docker:aulls-engines em
desenvolvimento — ver bench/engines/Dockerfile); sem ele os testes são saltados."""

import numpy as np
import pytest

from velocity.engine import choose_engine, get_engine
from velocity.evaluate import engine_t1
from velocity.evaluate import frame_probe as fp
from velocity.plan.schema import Output, Plan, Source, TimeMap, Track, VideoItem
from velocity.schemas.edit_dna import Curve, Transition

mlt = get_engine("mlt")
pytestmark = pytest.mark.skipif(not mlt.available(), reason="melt não disponível (VELOCITY_MELT)")
SIZE = engine_t1.SIZE


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return engine_t1.run("mlt", str(tmp_path_factory.mktemp("t1mlt")))


@pytest.mark.parametrize("case", ["cortes_30fps", "velocidade_2x", "tempo_por_frame", "fonte24_saida30",
                                  "fonte30_saida24", "fonte_vfr"])
def test_mlt_mostra_exatamente_o_frame_pedido(report, case):
    c = report["cases"][case]
    assert c["frames_out"] == c["frames_expected"] and c["exact"] == 1.0, c["errors"]


def _item(i, src, at, dur, start, transition=None):
    return VideoItem(id=f"v{i}", source=src, at=at, duration=dur, transition_in=transition,
                     time=TimeMap(src_start_s=start / 30, speed=Curve(t=[0, 1], v=[1, 1])))


def test_dissolucao_vai_para_o_mlt_e_flash_para_o_compositor(tmp_path):
    a = fp.write_source(str(tmp_path / "a.mp4"), 1, 120, 30, SIZE)
    b = fp.write_source(str(tmp_path / "b.mp4"), 2, 120, 30, SIZE)
    W, H = SIZE
    srcs = {"a": Source(uri=a), "b": Source(uri=b)}
    # itens sobrepostos 16 frames: dissolução (o compositor não executa; o MLT sim)
    dissolve = Plan(output=Output(width=W, height=H, fps=30, duration_frames=64), sources=srcs,
                    tracks=[Track(id="V1", kind="video", items=[_item(0, "a", 0, 40, 10), _item(1, "b", 24, 40, 60)])])
    assert choose_engine(dissolve).name == "mlt"
    out = str(tmp_path / "d.mp4")
    choose_engine(dissolve).render(dissolve, out, crf=12, preset="veryfast")
    got = fp.read_video(out)
    assert len(got) == 64
    assert got[:24] == [(1, 10 + k) for k in range(24)] and got[40:] == [(2, 60 + 16 + k) for k in range(24)]
    # na sobreposição, a imagem é uma mistura que vai de A para B
    frames = []
    import subprocess
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    for k in (24, 31, 39):
        img = np.frombuffer(r.stdout[k * W * H:(k + 1) * W * H], np.uint8).reshape(H, W).astype(np.float32)
        A = fp.draw(1, 10 + k, W, H, seed=7919).mean(axis=2)
        B = fp.draw(2, 60 + (k - 24), W, H, seed=2 * 7919).mean(axis=2)
        d = (B - A).ravel()
        frames.append(float(np.clip(np.dot((img - A).ravel(), d) / np.dot(d, d), 0, 1)))
    assert frames[0] < 0.2 and 0.3 < frames[1] < 0.7 and frames[2] > 0.8, frames
    # transição flash (efeito próprio): só o compositor a executa
    flash = Plan(output=Output(width=W, height=H, fps=30, duration_frames=40), sources=srcs, tracks=[Track(
        id="V1", kind="video", items=[_item(0, "a", 0, 20, 10), _item(1, "b", 20, 20, 30, Transition(
            type="flash", start_frame=18, end_frame=23, cut_frame=20, intensity=0.8))])])
    assert choose_engine(flash).name == "compositor"
    assert any(i.code == "unsupported" for i in mlt.validate(flash))
