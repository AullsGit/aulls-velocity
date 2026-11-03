"""O que o motor tem de saber fazer para o agente: dissolução real, som dos clips na mistura,
marca de água, grade de cor medida e correspondência sem repetir material."""
import subprocess

import numpy as np
import pytest

from velocity.engine import choose_engine, get_engine
from velocity.evaluate import frame_probe as fp
from velocity.plan import timeline_to_plan, validate_plan
from velocity.plan.validate import errors
from velocity.reference.grade import apply_matrix, grade_matrix
from velocity.render import textfx
from velocity.schemas.edit_dna import Transition
from velocity.schemas.timeline import ClipUse, TextOverlay, Timeline, TimelineShot

W, H = 128, 228


def _decode(path, w=W, h=H):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h, w, 3)


def _flat(path, rgb, n=90, tone=None):
    """Clip de cor sólida; com `tone` (Hz) também tem som."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x{rgb:06x}:s={W}x{H}:r=30:d={n / 30}"]
    if tone:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:duration={n / 30}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", path]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _tl(shots, **kw):
    n = shots[-1].end_frame
    return Timeline(fps=30.0, width=W, height=H, n_frames=n, shots=shots, **kw)


def _shot(i, a, b, src, start=0.0, trans=None, audio=0.0):
    return TimelineShot(index=i, start_frame=a, end_frame=b, transition_in=trans,
                        clip=ClipUse(source_path=src, src_start_s=start, audio_volume=audio))


def test_dissolucao_real_mistura_os_dois_planos_vindos_das_fontes(tmp_path):
    a = _flat(str(tmp_path / "a.mp4"), 0xFF2020)
    b = _flat(str(tmp_path / "b.mp4"), 0x2020FF)
    t = Transition(type="dissolve", start_frame=20, end_frame=40, cut_frame=30, intensity=1.0)
    plan = timeline_to_plan(_tl([_shot(0, 0, 30, a), _shot(1, 30, 60, b, trans=t)]))
    assert not errors(validate_plan(plan))
    eng = choose_engine(plan)
    assert eng.name == "compositor"
    out = str(tmp_path / "o.mp4")
    eng.render(plan, out, crf=12, preset="veryfast")
    fr = _decode(out).astype(np.float32)
    assert len(fr) == 60
    red = fr[..., 0].mean(axis=(1, 2))
    blue = fr[..., 2].mean(axis=(1, 2))
    assert red[10] > 200 and blue[10] < 60
    assert red[50] < 60 and blue[50] > 200
    assert 90 < red[30] < 170 and 90 < blue[30] < 170  # a meio, metade de cada
    assert all(np.diff(red[20:40]) <= 8) and all(np.diff(blue[20:40]) >= -8)  # atravessa sem degrau


def test_dissolucao_maior_que_um_plano_e_recusada(tmp_path):
    a = _flat(str(tmp_path / "a.mp4"), 0xFF2020)
    t = Transition(type="dissolve", start_frame=5, end_frame=25, cut_frame=15, intensity=1.0)
    plan = timeline_to_plan(_tl([_shot(0, 0, 15, a), _shot(1, 15, 20, a, trans=t), _shot(2, 20, 40, a)]))
    assert any(i.code == "dissolve_window" for i in errors(validate_plan(plan)))


def _freq(path, a, b):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(a), "-to", str(b), "-i", path, "-f", "f32le", "-ac", "1", "-ar", "8000", "-"],
                       capture_output=True)
    x = np.frombuffer(r.stdout, np.float32)
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / 8000), sp, x


def test_som_de_cada_clip_alinhado_ao_plano_e_musica_por_baixo(tmp_path):
    a = _flat(str(tmp_path / "a.mp4"), 0xFF2020, tone=500)
    b = _flat(str(tmp_path / "b.mp4"), 0x2020FF, tone=1500)
    music = str(tmp_path / "m.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=3000:duration=3", music], check=True)
    plan = timeline_to_plan(_tl([_shot(0, 0, 45, a, audio=0.2), _shot(1, 45, 90, b, audio=0.2)], music_path=music, music_volume=0.8))
    out = str(tmp_path / "o.mp4")
    get_engine("compositor").render(plan, out, crf=20, preset="veryfast")
    assert len(_decode(out)) == 90

    def energy(f, sp, hz):
        return sp[np.argmin(np.abs(f - hz))]

    f, sp, _ = _freq(out, 0.2, 1.3)  # 1.º plano: tom de A + música; nada de B
    assert energy(f, sp, 500) > 10 * energy(f, sp, 1500)
    assert energy(f, sp, 3000) > 2 * energy(f, sp, 500)  # a música domina (0.8 vs 0.2)
    f, sp, _ = _freq(out, 1.7, 2.8)
    assert energy(f, sp, 1500) > 10 * energy(f, sp, 500)
    assert energy(f, sp, 3000) > 2 * energy(f, sp, 1500)


def test_clip_sem_faixa_de_audio_nao_parte_a_mistura(tmp_path):
    a = _flat(str(tmp_path / "a.mp4"), 0xFF2020)
    plan = timeline_to_plan(_tl([_shot(0, 0, 30, a, audio=0.5)]))
    out = str(tmp_path / "o.mp4")
    get_engine("compositor").render(plan, out, crf=20, preset="veryfast")
    assert len(_decode(out)) == 30


def test_marca_de_agua_discreta_so_toca_no_seu_retangulo():
    img = np.full((1280, 720, 3), 40, np.uint8)
    t = TextOverlay(text="@Awls", start_frame=0, end_frame=10, x=0.05, y=0.12, width=0.5, height=0.017, style="watermark",
                    icon="tiktok", opacity=0.6)
    out = textfx.draw(img, t, 3)
    changed = np.argwhere((out != img).any(axis=2))
    assert len(changed) > 20
    (y0, x0), (y1, x1) = changed.min(axis=0), changed.max(axis=0)
    assert x1 < 360 and y1 - y0 < 60 and 120 < y0 < 180  # linha pequena no canto superior esquerdo
    assert out.max() < 255 * 0.72  # opacidade respeitada: nunca branco puro


def test_grade_medida_leva_a_cor_para_a_da_referencia():
    rng = np.random.default_rng(1)
    src = np.clip(rng.normal([120, 120, 120], [30, 30, 30], (200, 200, 3)), 0, 255).astype(np.uint8)[None]
    ref = np.clip(rng.normal([170, 110, 70], [50, 25, 15], (200, 200, 3)), 0, 255).astype(np.uint8)[None]
    m = grade_matrix(ref, src, strength=1.0)
    out = apply_matrix(src[0], m).astype(np.float32)
    assert np.abs(out.reshape(-1, 3).mean(axis=0) - ref.reshape(-1, 3).mean(axis=0)).max() < 6
    assert np.abs(out.reshape(-1, 3).std(axis=0) - ref.reshape(-1, 3).std(axis=0)).max() < 6
    identity = grade_matrix(ref, src, strength=0.0)
    assert np.allclose(np.array(identity)[:, :3], np.eye(3)) and np.allclose(np.array(identity)[:, 3], 0)


def test_correspondencia_nao_repete_material_enquanto_houver_outro():
    from velocity.matching.engine import match
    from velocity.media.analyzer import ClipProfile
    from velocity.schemas.edit_dna import Appearance, Curve, EditDNA, MotionProfile, Shot, SpeedProfile
    n_shots = 6
    shots = []
    for i in range(n_shots):
        shots.append(Shot(index=i, start_frame=i * 60, end_frame=(i + 1) * 60, duration_s=2.0,
                          transition_in=Transition(type="cut", start_frame=i * 60, end_frame=i * 60 + 1, cut_frame=i * 60) if i else None,
                          motion=MotionProfile.model_construct(pan_x=0.0, zoom_curve=None, energy_mean=0.1, confidence=0.0),
                          speed=SpeedProfile.model_construct(shape="flat", confidence=0.0, curve=Curve(t=[0, 1], v=[1, 1]), frozen_ratio=0.0),
                          appearance=Appearance.model_construct(mean_luma=100.0)))
    dna = EditDNA.model_construct(shots=shots, fps=30.0, n_frames=360, vibe=None)
    N = 900  # 30 s de material para 12 s de edição: há espaço para nunca repetir
    energy = np.full(N, 0.1)
    energy[100:160] = 0.9  # um único pico irresistível, onde o custo base quer pôr TUDO
    clip = ClipProfile(path="a", n=N, fps=30.0, duration_s=30.0, width=720, height=1280, energy=energy,
                       subj=np.tile([0.5, 0.5, 0.4, 1.0], (N, 1)), lum=np.full(N, 100.0), sat=np.full(N, 50.0),
                       sharp=np.full(N, 100.0), bad=np.zeros(N, bool), has_audio=True)
    ms = match(dna, [clip])
    spans = sorted((m.start_frame, m.start_frame + m.need_frames) for m in ms)
    for (a0, a1), (b0, _) in zip(spans, spans[1:]):
        assert b0 >= a1, spans


@pytest.mark.parametrize("speed", [1.0])
def test_plano_com_som_e_grade_faz_ida_e_volta(tmp_path, speed):
    a = _flat(str(tmp_path / "a.mp4"), 0xFF2020)
    tl = _tl([_shot(0, 0, 30, a, audio=0.2)], post_matrix=[[1, 0, 0, 5], [0, 1, 0, 0], [0, 0, 1, 0]],
             texts=[TextOverlay(text="@x", start_frame=0, end_frame=30, x=0.1, y=0.1, width=0.5, height=0.02, style="watermark",
                                icon="instagram", opacity=0.5)])
    from velocity.plan import plan_to_timeline
    back = plan_to_timeline(timeline_to_plan(tl))
    assert back.post_matrix == tl.post_matrix
    assert back.shots[0].clip.audio_volume == 0.2
    assert back.texts[0].icon == "instagram" and back.texts[0].opacity == 0.5
    assert fp is not None
