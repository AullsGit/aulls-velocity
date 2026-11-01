"""Conversões entre o Plano v1 e a Timeline do compositor, e leitura de documentos guardados.

`timeline_to_plan` e `plan_to_timeline` são inversas para tudo o que a Timeline representa
(testado em tests/test_plan.py): a migração não muda nenhum vídeo. Os projetos gravados antes
do Plano (Timeline sem `plan_version`) continuam a abrir — `load_plan` converte-os.
"""
from __future__ import annotations

from velocity.plan.schema import (PLAN_MAJOR, AudioItem, ColorOps, Look, Output, Plan, Provenance, RenderHints, Source,
                                  TextItem, TimeMap, Track, Transform, VideoItem)
from velocity.schemas.timeline import ClipUse, ExactTrack, TextOverlay, Timeline, TimelineShot


class PlanVersionError(ValueError):
    pass


def timeline_to_plan(tl: Timeline, provenance: list[Provenance] | None = None, meta: dict | None = None) -> Plan:
    """`provenance[i]` (opcional) descreve a decisão do plano i; sem ela: matcher, ou o
    alinhamento medido nos planos com rasto exato."""
    ids: dict[str, str] = {}
    sources: dict[str, Source] = {}

    def sid(uri: str, kind: str) -> str:
        if uri not in ids:
            ids[uri] = f"s{len(ids)}"
            sources[ids[uri]] = Source(uri=uri, kind=kind)
        return ids[uri]

    video = []
    for i, sh in enumerate(tl.shots):
        c, ex = sh.clip, sh.clip.exact
        if provenance is not None and i < len(provenance):
            prov = provenance[i]
        elif ex is not None:
            prov = Provenance(by="source_align", confidence=ex.confidence, method="registo afim + correlação por frame")
        else:
            prov = Provenance(by="matcher")
        video.append(VideoItem(
            id=f"v{i}", source=sid(c.source_path, "video"), at=sh.start_frame, duration=sh.n_frames,
            time=TimeMap(src_start_s=c.src_start_s, speed=c.speed, slowmo="hold" if c.slowmo == "hold" else "blend",
                         src_fps=ex.src_fps if ex else None,
                         src_frames=list(ex.src_frames) if ex else None),
            transform=Transform(zoom=c.zoom, center=c.center, pan=c.pan, flip=c.flip,
                                warps=list(ex.warps) if ex else None),
            color=ColorOps(gamma=c.tone_gamma, saturation=c.tone_sat, subject_gamma=c.subj_gamma,
                           background_gamma=c.bg_gamma, subject_sharpen=c.subj_sharp, matrix=ex.color if ex else None),
            transition_in=sh.transition_in, provenance=prov, measured=list(ex.measured) if ex and ex.measured else None,
        ))
    tracks = [Track(id="V1", kind="video", items=video)]
    if tl.music_path:
        tracks.append(Track(id="A1", kind="audio", items=[AudioItem(
            id="a0", source=sid(tl.music_path, "audio"), at=0, duration=tl.n_frames, offset_s=tl.music_offset_s,
            volume=tl.music_volume)]))
    if tl.texts:
        tracks.append(Track(id="T1", kind="text", items=[TextItem(
            id=f"t{i}", text=t.text, at=t.start_frame, duration=t.end_frame - t.start_frame, x=t.x, y=t.y,
            width=t.width, height=t.height, entry=t.entry if t.entry in ("pop", "fade", "none") else "none")
            for i, t in enumerate(tl.texts)]))
    return Plan(
        output=Output(width=tl.width, height=tl.height, fps=tl.fps, duration_frames=tl.n_frames),
        sources=sources, tracks=tracks, effects=list(tl.effects), fx_events=list(tl.fx_events),
        look=Look(gamma=tl.post_gamma, saturation=tl.post_sat, contrast=tl.post_contrast, sharpen=tl.post_sharp,
                  pivot=tl.post_pivot),
        zoom_gain=tl.zoom_gain, effects_scale=tl.fx_scale,
        render_hints=RenderHints(denoise=tl.denoise, super_resolution=tl.sr), meta=dict(meta or {}),
    )


def plan_to_timeline(plan: Plan) -> Timeline:
    """Para o compositor (uma pista de vídeo sequencial, uma faixa de áudio). Os limites do
    compositor são verificados ANTES, por velocity/engine (`validate`); aqui só se converte."""
    shots = []
    for i, it in enumerate(sorted(plan.video_items(), key=lambda v: v.at)):
        exact = None
        if it.time.src_frames is not None and it.transform.warps is not None:
            exact = ExactTrack(src_fps=it.time.src_fps or 30.0, src_frames=list(it.time.src_frames),
                               warps=list(it.transform.warps), color=it.color.matrix,
                               confidence=it.provenance.confidence or 0.0, measured=list(it.measured or []))
        clip = ClipUse(source_path=plan.sources[it.source].uri, src_start_s=it.time.src_start_s, speed=it.time.speed,
                       slowmo=it.time.slowmo,
                       zoom=it.transform.zoom, center=it.transform.center, pan=it.transform.pan, flip=it.transform.flip,
                       tone_gamma=it.color.gamma, tone_sat=it.color.saturation, subj_gamma=it.color.subject_gamma,
                       bg_gamma=it.color.background_gamma, subj_sharp=it.color.subject_sharpen, exact=exact)
        shots.append(TimelineShot(index=i, start_frame=it.at, end_frame=it.at + it.duration, clip=clip,
                                  transition_in=it.transition_in))
    audio = [it for t in plan.track("audio") for it in t.items]
    texts = [TextOverlay(text=t.text, start_frame=t.at, end_frame=t.at + t.duration, x=t.x, y=t.y, width=t.width,
                         height=t.height, entry=t.entry) for tr in plan.track("text") for t in tr.items]
    music = audio[0] if audio else None
    lk = plan.look
    return Timeline(
        fps=plan.output.fps, width=plan.output.width, height=plan.output.height, n_frames=plan.output.duration_frames,
        shots=shots, fx_events=list(plan.fx_events), texts=texts, effects=list(plan.effects), fx_scale=plan.effects_scale,
        post_gamma=lk.gamma, post_sat=lk.saturation, post_contrast=lk.contrast, post_sharp=lk.sharpen, post_pivot=lk.pivot,
        sr=plan.render_hints.super_resolution, denoise=plan.render_hints.denoise,
        music_path=plan.sources[music.source].uri if music else None, music_offset_s=music.offset_s if music else 0.0,
        music_volume=music.volume if music else 1.0, zoom_gain=plan.zoom_gain,
    )


def load_plan(doc: dict) -> Plan:
    """Lê um documento guardado: Plano v1.x, ou Timeline legada (convertida)."""
    if "plan_version" in doc:
        major = int(str(doc["plan_version"]).split(".")[0])
        if major != PLAN_MAJOR:
            raise PlanVersionError(f"plano versão {doc['plan_version']} não suportado (esperado {PLAN_MAJOR}.x)")
        return Plan.model_validate(doc)
    return timeline_to_plan(Timeline.model_validate(doc), meta={"migrated_from": "timeline"})
