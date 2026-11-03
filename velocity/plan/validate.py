"""Validação do plano ANTES do render. Encontra num instante o que antes só aparecia a meio de um
render de minutos ("timeline incoerente", janela vazia, ficheiro em falta).

Dois níveis:
  * `validate_plan(plan)`: coerência do próprio plano, independente do motor (intervalos,
    referências, parâmetros fora dos limites, listas por frame com o tamanho certo);
  * `check_media(plan)`: os ficheiros existem e os instantes pedidos cabem nas fontes (precisa
    de acesso aos ficheiros: no worker, depois de os descarregar).
Os limites de cada MOTOR (pistas, transições, efeitos suportados) são verificados pelo próprio
motor (velocity/engine), que acrescenta problemas do tipo "unsupported".
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from velocity.plan.schema import PLAN_MAJOR, AudioItem, Plan, TextItem, VideoItem

MAX_FPS = 240.0
MAX_SPEED = 16.0
MAX_DURATION_S = 15 * 60


@dataclass
class Issue:
    level: str  # error | warning
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.code} @ {self.where}: {self.message}"


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == "error"]


def _finite(*vals) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


def validate_plan(plan: Plan) -> list[Issue]:
    out: list[Issue] = []

    def err(code, where, msg):
        out.append(Issue("error", code, where, msg))

    def warn(code, where, msg):
        out.append(Issue("warning", code, where, msg))

    try:
        major = int(plan.plan_version.split(".")[0])
    except ValueError:
        major = -1
    if major != PLAN_MAJOR:
        err("version", "plan_version", f"versão {plan.plan_version} não suportada (esperado {PLAN_MAJOR}.x)")
    o = plan.output
    if o.width < 16 or o.height < 16 or o.width % 2 or o.height % 2:
        err("output_size", "output", f"tamanho inválido {o.width}x{o.height} (mínimo 16, valores pares)")
    if not (0 < o.fps <= MAX_FPS):
        err("output_fps", "output", f"fps fora dos limites: {o.fps}")
    if o.duration_frames < 1:
        err("output_duration", "output", "duração tem de ser pelo menos 1 frame")
    elif o.fps > 0 and o.duration_frames / o.fps > MAX_DURATION_S:
        err("output_duration", "output", f"duração acima de {MAX_DURATION_S} s")
    total = o.duration_frames

    ids: set[str] = set()
    for t in plan.tracks:
        for it in t.items:
            where = f"{t.id}/{it.id}"
            if it.id in ids:
                err("duplicate_id", where, "id repetido")
            ids.add(it.id)
            if it.type != t.kind:
                err("track_kind", where, f"item '{it.type}' numa pista '{t.kind}'")
            src = getattr(it, "source", None)
            if src is not None and src not in plan.sources:
                err("missing_source", where, f"fonte '{src}' não declarada em sources")
            if it.duration < 1:
                err("duration", where, "duração tem de ser pelo menos 1 frame")
            if it.at < 0 or it.at + it.duration > total:
                err("range", where, f"[{it.at}, {it.at + it.duration}) fora do vídeo [0, {total})")
            if isinstance(it, VideoItem):
                _video_item(it, where, total, err, warn)
            elif isinstance(it, AudioItem):
                if not (0.0 <= it.volume <= 2.0):
                    err("volume", where, f"volume {it.volume} fora de 0..2")
                if it.offset_s < 0:
                    err("offset", where, "offset negativo")
            elif isinstance(it, TextItem):
                if not it.text.strip():
                    err("text_empty", where, "texto vazio")
                if not (0 <= it.x <= 1 and 0 <= it.y <= 1 and 0 < it.width <= 1 and 0 < it.height <= 0.5):
                    err("text_box", where, "posição/tamanho do texto fora do frame")

    # efeitos que passam do fim são inofensivos (aplicam-se só aos frames que existem): aviso
    for i, e in enumerate(plan.effects):
        if not (e.start_frame <= e.peak_frame < e.end_frame) or e.start_frame < 0 or e.start_frame >= total:
            err("effect_range", f"effects[{i}]", f"{e.type}: frames {e.start_frame}/{e.peak_frame}/{e.end_frame} incoerentes")
        elif e.end_frame > total:
            warn("effect_overrun", f"effects[{i}]", f"{e.type} termina depois do fim do vídeo")
        if not _finite(e.magnitude):
            err("effect_value", f"effects[{i}]", "magnitude não finita")
    for i, ev in enumerate(plan.fx_events):
        if ev.start_frame >= ev.end_frame or ev.start_frame < 0 or ev.start_frame >= total:
            err("fx_event_range", f"fx_events[{i}]", "frames incoerentes")
    if not (0 < plan.zoom_gain <= 4):
        err("zoom_gain", "plan", f"zoom_gain {plan.zoom_gain} fora de (0, 4]")
    if not (0 <= plan.effects_scale <= 4):
        err("effects_scale", "plan", f"effects_scale {plan.effects_scale} fora de [0, 4]")
    lk = plan.look
    if not (0.3 <= lk.gamma <= 3 and 0 <= lk.saturation <= 3 and 0.2 <= lk.contrast <= 3 and 0 <= lk.sharpen <= 1):
        err("look", "look", "correção global fora dos limites")
    if lk.matrix is not None and (len(lk.matrix) != 3 or any(len(r) != 4 or not _finite(*r) for r in lk.matrix)):
        err("look_matrix", "look", "matriz de cor global tem de ser 3x4 finita")
    _dissolves(plan, err)
    return out


def _dissolves(plan: Plan, err) -> None:
    """Uma dissolução vive dos dois lados do corte: a janela tem de caber nos dois planos."""
    for t in plan.track("video"):
        items = sorted(t.items, key=lambda i: i.at)
        for prev, it in zip(items, items[1:]):
            tr = it.transition_in
            if tr is None or tr.type != "dissolve":
                continue
            if tr.cut_frame - tr.start_frame > prev.duration or tr.end_frame - tr.cut_frame > it.duration:
                err("dissolve_window", f"{t.id}/{it.id}", "a dissolução é mais longa do que um dos planos que junta")


def _video_item(it: VideoItem, where: str, total: int, err, warn):
    tm, tr, co = it.time, it.transform, it.color
    if tm.src_start_s < 0:
        err("src_start", where, "início na fonte negativo")
    sp = tm.speed
    if len(sp.t) != len(sp.v) or not sp.t or sorted(sp.t) != list(sp.t) or not (0 <= sp.t[0] and sp.t[-1] <= 1):
        err("speed_curve", where, "curva de velocidade mal formada (t crescente em 0..1, mesmo tamanho que v)")
    elif any(not _finite(v) or v <= 0 or v > MAX_SPEED for v in sp.v):
        err("speed_value", where, f"velocidade fora de (0, {MAX_SPEED}]")
    z = tr.zoom
    if len(z.t) != len(z.v) or not z.t or any(not _finite(v) or abs(v) > 3 for v in z.v):
        err("zoom_curve", where, "curva de zoom mal formada ou fora de ±3 (ln)")
    if not (all(-0.5 <= c <= 1.5 for c in tr.center) and all(-1 <= p <= 1 for p in tr.pan)):
        err("framing", where, "centro/pan fora dos limites")
    if tm.src_frames is not None:
        if len(tm.src_frames) != it.duration:
            err("per_frame_length", where, f"src_frames tem {len(tm.src_frames)} valores para {it.duration} frames")
        if not tm.src_fps or tm.src_fps <= 0:
            err("src_fps", where, "src_frames sem src_fps")
        if any(f < 0 for f in tm.src_frames):
            err("src_frames", where, "frame da fonte negativo")
    if tr.warps is not None:
        if len(tr.warps) != it.duration:
            err("per_frame_length", where, f"warps tem {len(tr.warps)} valores para {it.duration} frames")
        if any(not _finite(*w) for w in tr.warps):
            err("warp_value", where, "transformação com valores não finitos")
        elif any(abs(w[0] * w[4] - w[1] * w[3]) < 1e-6 for w in tr.warps):
            err("warp_degenerate", where, "transformação degenerada (escala zero)")
    if (tm.src_frames is None) != (tr.warps is None):
        warn("partial_exact", where, "rasto por frame só no tempo ou só na geometria: o motor usa o modo paramétrico")
    if it.measured is not None and len(it.measured) != it.duration:
        err("per_frame_length", where, "measured com tamanho diferente da duração")
    if co.matrix is not None and (len(co.matrix) != 3 or any(len(r) != 4 or not _finite(*r) for r in co.matrix)):
        err("color_matrix", where, "matriz de cor tem de ser 3x4 finita")
    if not (0.2 <= co.gamma <= 5 and 0 <= co.saturation <= 3 and 0 <= co.subject_sharpen <= 1):
        err("color_value", where, "ajuste de cor fora dos limites")
    if not (0.0 <= it.audio_volume <= 2.0):
        err("volume", where, f"volume do som do clip {it.audio_volume} fora de 0..2")
    t = it.transition_in
    if t is not None:
        if not (t.start_frame <= t.cut_frame <= t.end_frame):
            err("transition", where, f"transição {t.type}: início/corte/fim incoerentes")
        if t.cut_frame != it.at:
            err("transition_cut", where, f"o corte da transição ({t.cut_frame}) não coincide com o início do item ({it.at})")
        if t.start_frame < 0:
            err("transition_range", where, "transição começa antes do vídeo")
        elif t.end_frame > total:
            warn("transition_overrun", where, "transição termina depois do fim do vídeo")


def check_media(plan: Plan, probe_fn=None) -> list[Issue]:
    """Ficheiros existem e os instantes pedidos cabem nas fontes (tolerância de 2 frames)."""
    from velocity.io.probe import probe as default_probe
    probe_fn = probe_fn or default_probe
    out: list[Issue] = []
    infos = {}
    for sid, s in plan.sources.items():
        if not os.path.exists(s.uri):
            out.append(Issue("error", "missing_file", f"sources/{sid}", f"ficheiro não encontrado: {s.uri}"))
            continue
        try:
            infos[sid] = probe_fn(s.uri)
        except Exception as e:  # noqa: BLE001 — ficheiro corrompido/ilegível é um problema do plano, não um crash
            out.append(Issue("error", "unreadable_file", f"sources/{sid}", f"não foi possível ler: {e}"[:200]))
    fps_out = plan.output.fps
    for it in plan.video_items():
        info = infos.get(it.source)
        if info is None or plan.sources[it.source].kind == "image":
            continue
        fps_src = float(info.fps_nominal or info.fps_avg or 30.0)
        dur = float(info.duration)
        if it.time.src_frames is not None:
            last = max(it.time.src_frames) / (it.time.src_fps or fps_src)
        else:
            sp = it.time.speed.v
            mean_speed = sum(sp) / len(sp) if sp else 1.0
            last = it.time.src_start_s + it.duration / fps_out * mean_speed
        if last > dur + 2.0 / fps_src:
            out.append(Issue("warning", "source_overrun", f"video/{it.id}",
                             f"pede até {last:.2f}s de uma fonte com {dur:.2f}s (o motor repete o último frame)"))
    return out
