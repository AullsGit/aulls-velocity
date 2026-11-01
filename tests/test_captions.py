"""Legenda copiada da referência: limpeza do OCR (sem correr o OCR) e desenho no estilo caixa."""

import numpy as np

from velocity.reference.captions import Caption, clean_captions, to_overlays
from velocity.render import textfx


def _cap(a, b, text, cx=0.5, cy=0.92, w=0.3):
    return Caption(start_frame=a, end_frame=b, text=text, cx=cx, cy=cy, text_h=0.05, box_w=w,
                   text_rgb=(240, 210, 80), box_rgb=(10, 10, 10), box_alpha=0.65, confidence=0.9)


def test_limpeza_repara_leituras_tortas_e_fecha_falhas():
    caps = [_cap(0, 30, "eu não gosto de você"),
            _cap(32, 40, "Em eu não gosto de você", cx=0.43),  # texto da cena colado à legenda
            _cap(42, 60, "eu não gosto de você"),
            _cap(70, 80, "S por fa-", cx=0.45),  # 1.ª palavra espúria, karaoke a crescer
            _cap(80, 100, "por favor não me ligue", w=0.4),
            _cap(103, 110, "icdo", cy=0.82)]  # lixo fora da linha da legenda
    got = [(c.start_frame, c.end_frame, c.text) for c in clean_captions(caps)]
    assert got == [(0, 60, "eu não gosto de você"), (70, 80, "por fa-"), (80, 100, "por favor não me ligue")]


def test_legenda_em_caixa_com_a_cor_e_a_posicao_medidas():
    caps = [_cap(0, 30, "por favor não me ligue mais", w=0.46)]
    t = to_overlays(caps, ref_fps=30, out_fps=30, out_size=(360, 640), ref_size=(768, 576))[0]
    assert t.style == "box" and (t.start_frame, t.end_frame) == (0, 30)
    img = np.full((640, 360, 3), 200, np.uint8)
    out = textfx.draw(img, t, 10)
    y = int(t.y * 640)
    band = out[y - 8:y + 8, 60:300].reshape(-1, 3).astype(int)
    assert (band.max(axis=1) < 80).mean() > 0.4  # caixa escura semitransparente
    assert ((band[:, 0] > 180) & (band[:, 2] < 140)).any()  # texto amarelo medido
    assert (out[:y - 40] == 200).all()  # o resto da imagem fica intacto
    assert (textfx.draw(img, t, 30) == img).all()  # fora do intervalo não desenha
