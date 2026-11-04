"""Plano v1: conversão sem perdas, validação antes do render, limites do motor, projetos antigos."""
import json

import pytest

from velocity.engine import PlanRejected, get_engine
from velocity.plan import (Plan, PlanVersionError, errors, load_plan, plan_to_timeline, timeline_to_plan,
                           validate_plan)
from velocity.schemas.edit_dna import Curve, EffectTrack, FxEvent, Transition
from velocity.schemas.timeline import ClipUse, ExactTrack, TextOverlay, Timeline, TimelineShot


def _timeline() -> Timeline:
    """Timeline com tudo o que o compositor sabe fazer: rasto exato, curvas, cor, transições,
    efeitos, eventos, texto, música e correção global."""
    n = 12
    exact = ExactTrack(src_fps=30.0, src_frames=[100 + 2 * k for k in range(n)], warps=[(0.8, 0.0, 0.1, 0.0, 0.8, 0.1)] * n,
                       color=[[1.1, 0, 0, -3], [0, 1, 0, 0], [0, 0, 0.9, 2]], confidence=0.97, measured=[True] * 10 + [False] * 2)
    shots = [
        TimelineShot(index=0, start_frame=0, end_frame=20, clip=ClipUse(
            source_path="uploads/u/a.mp4", src_start_s=1.5, speed=Curve(t=[0, 0.5, 1], v=[1, 0.6, 1.4]),
            zoom=Curve(t=[0, 1], v=[0.0, 0.2]), center=(0.45, 0.55), pan=(0.05, 0.0), flip=True, tone_gamma=0.9,
            tone_sat=1.1, subj_gamma=0.95, bg_gamma=1.05, subj_sharp=0.2)),
        TimelineShot(index=1, start_frame=20, end_frame=20 + n, clip=ClipUse(
            source_path="uploads/u/b.mp4", src_start_s=100 / 30, exact=exact),
            transition_in=Transition(type="flash", start_frame=18, end_frame=23, cut_frame=20, intensity=0.8)),
        TimelineShot(index=2, start_frame=20 + n, end_frame=50, clip=ClipUse(source_path="uploads/u/a.mp4", src_start_s=4.0),
                     transition_in=Transition(type="blur", start_frame=30, end_frame=35, cut_frame=32, blur_kind="radial")),
    ]
    return Timeline(fps=24.0, width=960, height=540, n_frames=50, shots=shots,
                    fx_events=[FxEvent(type="glitch", start_frame=5, end_frame=8, intensity=0.5)],
                    texts=[TextOverlay(text="GOAL", start_frame=10, end_frame=30, x=0.5, y=0.8, width=0.9, height=0.06, entry="pop")],
                    effects=[EffectTrack(type="zoom_punch", start_frame=2, end_frame=8, peak_frame=4, magnitude=0.12)],
                    fx_scale=0.9, post_gamma=1.05, post_sat=1.1, post_contrast=0.95, sr=False, denoise=0.3, post_sharp=0.2,
                    post_pivot=100.0, music_path="uploads/u/m.mp3", music_offset_s=2.0, music_volume=0.8, zoom_gain=1.2)


def test_conversao_timeline_plano_timeline_e_exata():
    tl = _timeline()
    plan = timeline_to_plan(tl)
    back = plan_to_timeline(plan)
    assert back.model_dump() == tl.model_dump()
    # e sobrevive à serialização JSON (o que se guarda na base de dados)
    again = plan_to_timeline(load_plan(json.loads(plan.model_dump_json())))
    assert again.model_dump() == tl.model_dump()


def test_plano_tem_origem_versao_e_pistas():
    plan = timeline_to_plan(_timeline())
    assert plan.plan_version.startswith("1.")
    assert [t.kind for t in plan.tracks] == ["video", "audio", "text"]
    v = plan.video_items()
    assert v[0].provenance.by == "matcher" and v[1].provenance.by == "source_align"
    assert v[1].provenance.confidence == pytest.approx(0.97) and v[1].time.per_frame
    assert validate_plan(plan) == [] and get_engine().validate(plan) == []


def test_timeline_antiga_guardada_abre_como_plano():
    legacy = json.loads(_timeline().model_dump_json())  # o que os projetos têm hoje na coluna `timeline`
    plan = load_plan(legacy)
    assert plan.meta.get("migrated_from") == "timeline"
    assert plan_to_timeline(plan).model_dump() == _timeline().model_dump()


def test_versao_futura_e_recusada_com_mensagem_clara():
    doc = json.loads(timeline_to_plan(_timeline()).model_dump_json())
    doc["plan_version"] = "2.0"
    with pytest.raises(PlanVersionError):
        load_plan(doc)


@pytest.mark.parametrize("mutate, code", [
    (lambda p: setattr(p.video_items()[0], "duration", 0), "duration"),
    (lambda p: setattr(p.video_items()[2], "duration", 99), "range"),
    (lambda p: setattr(p.video_items()[0], "source", "s9"), "missing_source"),
    (lambda p: p.video_items()[1].time.src_frames.pop(), "per_frame_length"),
    (lambda p: setattr(p.video_items()[1].transform, "warps", [(0, 0, 0, 0, 0, 0)] * 12), "warp_degenerate"),
    (lambda p: setattr(p.video_items()[0].time, "speed", Curve(t=[0, 1], v=[1, -1])), "speed_value"),
    (lambda p: setattr(p.video_items()[1].color, "matrix", [[1, 0, 0]]), "color_matrix"),
    (lambda p: setattr(p.video_items()[1].transition_in, "cut_frame", 19), "transition_cut"),
    (lambda p: setattr(p.video_items()[1].transition_in, "start_frame", 21), "transition"),
    (lambda p: setattr(p.output, "width", 961), "output_size"),
    (lambda p: setattr(p, "plan_version", "7.0"), "version"),
])
def test_validacao_apanha_erros_antes_do_render(mutate, code):
    plan = timeline_to_plan(_timeline())
    mutate(plan)
    assert code in {i.code for i in errors(validate_plan(plan))}


def test_motor_recusa_o_que_nao_sabe_fazer_antes_de_renderizar(tmp_path):
    plan = timeline_to_plan(_timeline())
    plan.video_items()[1].transition_in.type = "wipe"  # tipo que o compositor não executa
    plan.video_items()[2].at = 30  # sobreposição com o item anterior (cross-fade/PiP)
    codes = {(i.code, i.where) for i in get_engine().validate(plan)}
    assert ("unsupported", "V1/v1") in codes and ("unsupported", "V1/v2") in codes
    with pytest.raises(PlanRejected):
        get_engine().render(plan, str(tmp_path / "x.mp4"))
    assert not (tmp_path / "x.mp4").exists()


def test_schema_json_publicado_esta_atualizado():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docs", "plan.schema.json")
    assert json.load(open(path, encoding="utf-8")) == Plan.model_json_schema(), \
        "docs/plan.schema.json desatualizado: corre `python -m velocity.plan --schema docs/plan.schema.json`"
