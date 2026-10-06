"""Editor leve: as edições reconstroem a timeline sem perder as transições/efeitos da IA."""
import pytest

from velocity.edit.timeline_ops import EditDoc, EditShot, EditText, apply_edit, doc_from_timeline
from velocity.schemas.edit_dna import Curve, EffectTrack, Transition
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot

FILES = {"a": "uploads/u/p/a.mp4", "b": "uploads/u/p/b.mp4", "m": "uploads/u/p/m.mp3"}


def _tl() -> Timeline:
    # 3 planos de 30, 20 e 40 frames; transições flash (no 2º) e blur (no 3º); 1 efeito por plano
    shots, cur = [], 0
    for i, (n, src) in enumerate([(30, "a"), (20, "b"), (40, "a")]):
        t = None
        if i == 1:
            t = Transition(type="flash", start_frame=cur - 2, end_frame=cur + 3, cut_frame=cur, intensity=0.8)
        if i == 2:
            t = Transition(type="blur", start_frame=cur - 3, end_frame=cur + 3, cut_frame=cur, intensity=0.5,
                           blur_kind="radial")
        shots.append(TimelineShot(index=i, start_frame=cur, end_frame=cur + n, transition_in=t,
                                  clip=ClipUse(source_path=FILES[src], src_start_s=1.0 + i)))
        cur += n
    effects = [EffectTrack(type="zoom_punch", start_frame=s, end_frame=s + 6, peak_frame=s + 2, magnitude=0.1)
               for s in (5, 33, 60)]
    return Timeline(width=720, height=1280, n_frames=cur, shots=shots, effects=effects, music_path=FILES["m"])


def test_sem_alteracoes_reproduz_a_timeline():
    tl = _tl()
    out = apply_edit(tl, doc_from_timeline(tl), FILES)
    assert out.n_frames == tl.n_frames
    assert [(s.start_frame, s.end_frame) for s in out.shots] == [(s.start_frame, s.end_frame) for s in tl.shots]
    assert [s.transition_in and (s.transition_in.type, s.transition_in.cut_frame) for s in out.shots] == \
           [s.transition_in and (s.transition_in.type, s.transition_in.cut_frame) for s in tl.shots]
    assert [(e.start_frame, e.peak_frame) for e in out.effects] == [(e.start_frame, e.peak_frame) for e in tl.effects]
    assert out.music_path == tl.music_path


def test_apagar_plano_remove_os_seus_efeitos_e_puxa_os_seguintes():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots.pop(1)  # apaga o plano do meio (20 frames, efeito no frame 33)
    out = apply_edit(tl, doc, FILES)
    assert out.n_frames == 70
    assert [e.start_frame for e in out.effects] == [5, 40]  # 60 -> 40 (puxado 20 frames)
    assert out.shots[1].transition_in.type == "blur" and out.shots[1].transition_in.cut_frame == 30


def test_reordenar_leva_a_transicao_com_o_plano():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots = [doc.shots[2], doc.shots[0], doc.shots[1]]
    out = apply_edit(tl, doc, FILES)
    assert out.shots[0].transition_in is None  # o 1º plano nunca tem transição de entrada
    assert out.shots[1].transition_in.type == "cut"  # o plano original 0 não tinha transição
    assert out.shots[2].transition_in.type == "flash" and out.shots[2].transition_in.cut_frame == 70
    assert out.shots[0].clip.src_start_s == 3.0


def test_dividir_plano_nao_cria_transicao_a_meio_e_reparte_efeitos():
    tl = _tl()
    doc = doc_from_timeline(tl)
    third = doc.shots[2]  # 40 frames a partir de src 3.0s, efeito em +7 (frame 57) -> (60-50)=10
    first = third.model_copy(update={"duration_s": 0.5})
    second = third.model_copy(update={"duration_s": 40 / 30 - 0.5, "src_start_s": 3.0 + 0.5, "transition": "keep"})
    doc.shots[2:3] = [first, second]
    out = apply_edit(tl, doc, FILES)
    assert out.n_frames == tl.n_frames
    assert out.shots[2].transition_in.type == "blur"
    assert out.shots[3].transition_in.type == "cut"  # metade 2: sem transição inventada
    assert out.shots[3].clip.src_start_s == pytest.approx(3.5)
    assert sorted(e.start_frame for e in out.effects) == [5, 33, 60]  # efeito caiu na metade certa, mesmo sítio


def test_cortar_inicio_avanca_a_fonte_e_encurta():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots[0] = doc.shots[0].model_copy(update={"trim_in_s": 10 / 30, "duration_s": 20 / 30})
    out = apply_edit(tl, doc, FILES)
    assert out.shots[0].clip.src_start_s == pytest.approx(1.0 + 10 / 30)
    assert out.shots[0].n_frames == 20
    # o efeito do frame 5 ficava na parte cortada -> desaparece; os seguintes recuam 10 frames
    assert [e.start_frame for e in out.effects] == [23, 50]


def test_velocidade_zoom_texto_look_e_volume():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots[0] = doc.shots[0].model_copy(update={"speed": 9.0, "zoom": 1.5, "flip": True})
    doc.texts = [EditText(text="NEVER STOP", start_s=0.2, end_s=99, y=0.85)]
    doc.look.filter = "mono"
    doc.music_volume = 5.0
    out = apply_edit(tl, doc, FILES)
    assert out.shots[0].clip.speed.v == [4.0, 4.0]  # limitado a 4x
    assert out.shots[0].clip.flip is True
    assert out.texts[0].end_frame == out.n_frames  # texto não passa do fim do vídeo
    assert out.post_sat == 0.0 and out.music_volume == 2.0


def test_substituir_clip_e_trocar_musica():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots[1] = doc.shots[1].model_copy(update={"file": "a", "src_start_s": 7.0})
    doc.music_file = "m"
    out = apply_edit(tl, doc, FILES)
    assert out.shots[1].clip.source_path == FILES["a"] and out.shots[1].clip.src_start_s == 7.0
    assert out.music_path == FILES["m"] and out.music_offset_s == 0.0


def test_recusa_ficheiro_de_fora_do_projeto_e_videos_gigantes():
    tl = _tl()
    doc = doc_from_timeline(tl)
    doc.shots[0] = doc.shots[0].model_copy(update={"file": "ficheiro-de-outro-utilizador"})
    with pytest.raises(ValueError):
        apply_edit(tl, doc, FILES)
    with pytest.raises(ValueError):
        apply_edit(tl, EditDoc(shots=[EditShot(orig=0, duration_s=50.0), EditShot(orig=1, duration_s=50.0)]), FILES)
    with pytest.raises(ValueError):
        apply_edit(tl, EditDoc(shots=[EditShot(orig=99, duration_s=1.0)]), FILES)
