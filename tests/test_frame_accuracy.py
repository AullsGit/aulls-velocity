"""Precisão ao frame do motor (prova técnica T1/T2), medida com fontes que têm o número do frame
desenhado: cortes, velocidade 2x, tempo por frame (rampa, congelamento, reverse), conversão
24<->30 fps e fonte VFR. Critério: 0 frames de erro em todos os casos (regra "hold")."""
import pytest

from velocity.evaluate import engine_t1


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return engine_t1.run("compositor", str(tmp_path_factory.mktemp("t1")))


@pytest.mark.parametrize("case", ["cortes_30fps", "velocidade_2x", "tempo_por_frame", "fonte24_saida30",
                                  "fonte30_saida24", "fonte_vfr"])
def test_motor_mostra_exatamente_o_frame_pedido(report, case):
    c = report["cases"][case]
    assert c["frames_out"] == c["frames_expected"], c
    assert c["wrong_source"] == 0 and c["exact"] == 1.0, c["errors"]
