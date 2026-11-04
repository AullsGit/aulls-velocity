"""As operações com que o agente edita: limites, atomicidade e o que acompanha cada plano."""
import subprocess
import sys

import pytest

from velocity.agent.planops import OpContext, OpError, apply_ops, no_music_variant, scale_output
from velocity.agent.report import plan_report, reused_material
from velocity.plan import load_plan, timeline_to_plan, validate_plan
from velocity.plan.validate import errors
from velocity.schemas.edit_dna import EffectTrack, Transition
from velocity.schemas.timeline import ClipUse, TextOverlay, Timeline, TimelineShot

FPS = 30.0
CTX = OpContext(files={"f1": {"key": "uploads/a.mp4", "kind": "source", "name": "a.mp4"}, "f2": {"key": "uploads/b.mp4", "kind": "source", "name": "b.mp4"},
                       "m": {"key": "uploads/m.mp3", "kind": "music", "name": "m.mp3"}},
                reference_key="uploads/ref.mp4", beats=[1.0, 2.0, 3.0, 4.0],
                captions=[{"text": "eu não gosto", "start_s": 0.5, "end_s": 1.5}, {"text": "de você", "start_s": 1.5, "end_s": 2.5}],
                caption_style={"size": 0.036, "x": 0.5, "y": 0.92, "color": [243, 215, 84], "box_alpha": 0.8},
                grade=[[1.2, 0, 0, 5], [0, 1.0, 0, 0], [0, 0, 0.8, -3]])


def _plan(n=4, frames=30):
    shots = []
    for k in range(n):
        trans = Transition(type="flash", start_frame=k * frames - 3, end_frame=k * frames + 3, cut_frame=k * frames, intensity=0.8) if k else None
        shots.append(TimelineShot(index=k, start_frame=k * frames, end_frame=(k + 1) * frames, transition_in=trans,
                                  clip=ClipUse(source_path="uploads/a.mp4" if k % 2 == 0 else "uploads/b.mp4", src_start_s=k * 2.0)))
    effects = [EffectTrack(type="zoom_punch", start_frame=40, end_frame=46, peak_frame=43, magnitude=0.2, intensity=0.5)]  # dentro do plano 1
    tl = Timeline(fps=FPS, width=720, height=1280, n_frames=n * frames, shots=shots, effects=effects, music_path="uploads/ref.mp4")
    return timeline_to_plan(tl)


def _ok(plan):
    assert not errors(validate_plan(plan)), errors(validate_plan(plan))
    return plan


def test_cortar_e_mover_leva_os_efeitos_do_plano_atras():
    p, _ = apply_ops(_plan(), [{"op": "remove_shot", "index": 0}])
    _ok(p)
    assert p.output.duration_frames == 90
    assert p.effects[0].start_frame == 10  # o plano 1 passou a começar em 0; o efeito vem com ele
    p, _ = apply_ops(_plan(), [{"op": "move_shot", "from": 1, "to": 3}])
    assert p.effects[0].start_frame == 100


def test_dividir_continua_a_fonte_onde_o_plano_ia():
    p, _ = apply_ops(_plan(), [{"op": "split_shot", "at_s": 0.5}])
    v = sorted(p.video_items(), key=lambda i: i.at)
    assert [i.duration for i in v[:2]] == [15, 15]
    assert v[0].time.src_start_s == 0.0 and abs(v[1].time.src_start_s - 0.5) < 1e-6
    assert v[1].transition_in is None or v[1].transition_in.type == "cut"
    assert v[2].transition_in.type == "flash"  # a transição do plano seguinte não mexe
    _ok(p)


def test_operacoes_sao_tudo_ou_nada():
    original = _plan()
    with pytest.raises(OpError):
        apply_ops(original, [{"op": "update_shot", "index": 0, "speed": 2}, {"op": "remove_shot", "index": 99}])
    assert all(i.time.speed.v == [1.0, 1.0] for i in original.video_items())


@pytest.mark.parametrize("op,msg", [
    ({"op": "update_shot", "index": 0, "file": "outro"}, "não existe"),
    ({"op": "set_transition", "index": 1, "type": "wipe"}, "desconhecida"),
    ({"op": "voar"}, "desconhecida"),
    ({"op": "set_cuts", "cuts_s": [1.0]}, "cortes"),
    ({"op": "set_captions"}, "items"),
    ({"op": "set_watermark"}, "contas"),
])
def test_erros_explicam_o_que_corrigir(op, msg):
    with pytest.raises(OpError, match=msg):
        apply_ops(_plan(), [op], CTX)


def test_nao_ha_ficheiros_de_fora_do_projeto():
    with pytest.raises(OpError):
        apply_ops(_plan(), [{"op": "update_shot", "index": 0, "file": "C:/Windows/system32/x.mp4"}], CTX)
    with pytest.raises(OpError):
        apply_ops(_plan(), [{"op": "set_audio", "bed": "http://evil/x.mp3"}], CTX)


def test_valores_ficam_dentro_dos_limites():
    p, _ = apply_ops(_plan(), [{"op": "update_shot", "index": 0, "speed": 500, "zoom": 99, "center": [9, -9], "audio_volume": 50}], CTX)
    _ok(p)
    v = sorted(p.video_items(), key=lambda i: i.at)[0]
    assert v.time.speed.v == [8.0, 8.0] and v.audio_volume == 2.0 and v.transform.center == (1.0, 0.0)


def test_mover_cortes_para_as_batidas():
    p, notes = apply_ops(_plan(), [{"op": "snap_cuts_to_beats", "max_shift_s": 0.3}], CTX)
    assert notes and "movidos" in notes[0]
    assert [i.at for i in sorted(p.video_items(), key=lambda i: i.at)] == [0, 30, 60, 90]  # já estavam nas batidas
    p, _ = apply_ops(_plan(), [{"op": "set_cuts", "cuts_s": [1.1, 2.1, 3.1]}], CTX)
    assert [i.at for i in sorted(p.video_items(), key=lambda i: i.at)] == [0, 33, 63, 93]
    p, _ = apply_ops(p, [{"op": "snap_cuts_to_beats", "max_shift_s": 0.3}], CTX)
    assert [i.at for i in sorted(p.video_items(), key=lambda i: i.at)] == [0, 30, 60, 90]


def test_imagem_intacta_tira_tudo_o_que_mexe_na_imagem():
    p, _ = apply_ops(_plan(), [{"op": "update_shot", "index": 1, "speed": 2, "zoom": 1.5, "gamma": 0.8}, {"op": "set_look", "saturation": 1.3},
                               {"op": "strip_to_cuts"}], CTX)
    _ok(p)
    assert not p.effects and p.look.saturation == 1.0
    for it in p.video_items():
        assert it.time.speed.v == [1.0, 1.0] and all(v == 0 for v in it.transform.zoom.v) and it.color.gamma == 1.0
        assert it.transition_in is None or it.transition_in.type == "cut"


def test_audio_legenda_e_marca_de_agua_como_na_edicao_pedida():
    p, _ = apply_ops(_plan(), [{"op": "set_audio", "bed": "reference", "bed_volume": 0.8, "clip_volume": 0.2}, {"op": "set_captions", "from_reference": True},
                               {"op": "set_watermark", "handles": [{"platform": "tiktok", "handle": "Awls"}, {"platform": "instagram", "handle": "Awlls"}]}], CTX)
    _ok(p)
    audio = p.track("audio")[0].items[0]
    assert audio.volume == 0.8 and p.sources[audio.source].uri == "uploads/ref.mp4"
    assert all(i.audio_volume == 0.2 for i in p.video_items())
    texts = p.track("text")[0].items
    caps = [t for t in texts if t.style == "box"]
    marks = [t for t in texts if t.style == "watermark"]
    assert [c.text for c in caps] == ["eu não gosto", "de você"] and caps[0].at == 15 and caps[0].duration == 30
    assert caps[0].color == (243, 215, 84) and caps[0].box_alpha == 0.8
    assert [(m.icon, m.text) for m in marks] == [("tiktok", "@Awls"), ("instagram", "@Awlls")]
    assert marks[1].y > marks[0].y and all(m.at == 0 and m.duration == 120 for m in marks)
    rep = plan_report(p, {"uploads/a.mp4": "f1"})
    assert rep["captions"]["lines"] == 2 and rep["shots"][0]["file"] == "f1"


def test_marca_de_agua_acompanha_a_duracao_e_legenda_nao_passa_do_fim():
    p, _ = apply_ops(_plan(), [{"op": "set_captions", "items": [{"text": "x", "start_s": 3.5, "end_s": 3.9}]},
                               {"op": "set_watermark", "handles": [{"platform": "none", "handle": "marca"}]}], CTX)
    p, _ = apply_ops(p, [{"op": "trim_total", "duration_s": 2.0}], CTX)
    _ok(p)
    texts = p.track("text")[0].items
    assert [t.style for t in texts] == ["watermark"] and texts[0].duration == 60


def test_cor_da_referencia_com_forca_ajustavel():
    p, _ = apply_ops(_plan(), [{"op": "set_look", "grade": "reference", "strength": 0.5}], CTX)
    m = p.look.matrix
    assert m[0][0] == 1.1 and m[0][3] == 2.5 and m[2][2] == 0.9
    p, _ = apply_ops(p, [{"op": "set_look", "grade": "none"}], CTX)
    assert p.look.matrix is None


def test_dissolucao_ajusta_se_aos_planos_vizinhos():
    p, _ = apply_ops(_plan(n=3, frames=10), [{"op": "set_transition", "index": 1, "type": "dissolve", "duration_s": 3.0}], CTX)
    _ok(p)  # 3 s pedidos em planos de 1/3 s: fica do tamanho que cabe


def test_versao_sem_musica_e_em_baixa_resolucao():
    p, _ = apply_ops(_plan(), [{"op": "set_audio", "bed": "reference", "clip_volume": 0.2}], CTX)
    v = no_music_variant(p)
    assert not v.track("audio") and all(i.audio_volume == 1.0 for i in v.video_items())
    assert "uploads/ref.mp4" not in [s.uri for s in v.sources.values()]
    _ok(v)
    s = scale_output(p, 540)
    assert (s.output.width, s.output.height) == (540, 960)
    assert p.output.width == 720  # o original não muda


def test_detecao_de_material_repetido():
    p, _ = apply_ops(_plan(), [{"op": "update_shot", "index": 2, "file": "f1", "src_start_s": 0.0}], CTX)
    rep = reused_material(p)
    assert rep and rep[0]["source"] == "a.mp4" and rep[0]["overlap_s"] == 1.0


def test_ida_e_volta_pelo_json_guardado():
    p, _ = apply_ops(_plan(), [{"op": "set_watermark", "handles": [{"platform": "tiktok", "handle": "a"}]}, {"op": "set_transition", "index": 1, "type": "dissolve"}], CTX)
    doc = p.model_dump(mode="json")
    q = load_plan(doc)
    assert q.model_dump(mode="json") == doc


def test_a_api_leve_corre_o_agente_sem_numpy():
    """A imagem de produção da API não tem numpy, OpenCV nem ffmpeg (Dockerfile.api)."""
    code = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('numpy', 'cv2', 'scipy', 'PIL', 'torch', 'easyocr', 'librosa'):
            raise ImportError('bloqueado: ' + name)
sys.meta_path.insert(0, Block())
import studio.server
import studio.agent.tools, studio.agent.loop, studio.agent.service
from studio.agent.tools import tool_definitions
assert len(tool_definitions()) > 10
print('ok')
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-1500:]


def test_texto_overlay_preservado_pelo_editor_manual():
    from velocity.edit.timeline_ops import apply_edit, doc_from_timeline
    from velocity.plan import plan_to_timeline
    p, _ = apply_ops(_plan(), [{"op": "set_captions", "from_reference": True}, {"op": "set_watermark", "handles": [{"platform": "tiktok", "handle": "a"}]},
                               {"op": "set_audio", "clip_volume": 0.2}], CTX)
    tl = plan_to_timeline(p)
    edited = apply_edit(tl, doc_from_timeline(tl), {})
    assert {t.style for t in edited.texts} == {"box", "watermark"}
    assert all(s.clip.audio_volume == 0.2 for s in edited.shots)
    assert isinstance(edited.texts[0], TextOverlay)


def test_medir_uma_musica_sem_faixa_de_video(tmp_path):
    from velocity.agent.compute import audio_job
    path = str(tmp_path / "m.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "aevalsrc='0.6*sin(2*PI*220*t)*exp(-12*mod(t,0.5))':s=44100:d=8", path], check=True)
    r = audio_job(path)
    assert r["has_audio"] and r["duration_s"] == 8.0
    assert 100 <= r["bpm"] <= 130 or 200 <= r["bpm"] <= 260  # 120 batidas por minuto, ou o dobro
    assert len(r["beats_s"]) >= 8 and r["loudness_2hz"]
