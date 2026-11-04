"""Operações sobre o Plano, a linguagem com que o agente edita.

Cada operação é um dict `{"op": nome, ...}`. O agente nunca escreve frames nem caminhos:
trabalha em segundos e com ids de ficheiro, e tudo o que vier fora de limites é recusado ou
limitado aqui. As operações aplicam-se em bloco: ou todas, ou nenhuma (`OpError` deixa o plano
como estava). Efeitos medidos acompanham o plano onde estavam quando ele é cortado ou movido.

Python puro (sem numpy): corre na API de produção, dentro do ciclo do agente.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from velocity.plan.convert import plan_to_timeline, timeline_to_plan
from velocity.plan.schema import Plan, Provenance
from velocity.schemas.edit_dna import Curve, EffectTrack, FxEvent, Transition
from velocity.schemas.timeline import ClipUse, ExactTrack, TextOverlay, Timeline, TimelineShot

MIN_SHOT_FRAMES = 3
MAX_TOTAL_S = 180.0
TRANSITIONS = ("cut", "flash", "dip_black", "blur", "glitch", "dissolve")
EFFECTS = ("zoom_punch", "shake", "rgb_split", "exposure_pulse", "contrast_pulse", "sat_pulse")
FX_EVENTS = ("flash", "glitch", "blur", "dip_black")
DEFAULT_TRANSITION_S = {"flash": 0.2, "dip_black": 0.4, "blur": 0.25, "glitch": 0.2, "dissolve": 0.5}
ICONS = ("none", "tiktok", "instagram")
WATERMARK_Y = 0.125  # abaixo das abas do TikTok/Reels, onde os botões da app não tapam
WATERMARK_X = 0.045


class OpError(ValueError):
    """Operação impossível: a mensagem é devolvida ao modelo para ele corrigir."""


@dataclass
class OpContext:
    """O que as operações podem usar além do plano. `files`: id -> {key, kind, name}."""

    files: dict[str, dict] = field(default_factory=dict)
    reference_key: str | None = None
    beats: list[float] = field(default_factory=list)
    captions: list[dict] = field(default_factory=list)  # {text, start_s, end_s}, lidas da referência
    caption_style: dict = field(default_factory=dict)  # {size, x, y, color, box_alpha}
    grade: list[list[float]] | None = None  # matriz 3x4 medida (força total)
    source_durations: dict[str, float] = field(default_factory=dict)  # chave -> duração em segundos, quando conhecida

    def key(self, file_id: str) -> str:
        f = self.files.get(file_id)
        if not f:
            raise OpError(f"o ficheiro '{file_id}' não existe neste projeto (usa os ids de get_project_state)")
        return f["key"]


@dataclass
class _Row:
    """Um plano em edição: o clip, a duração e tudo o que lhe pertence, em tempos relativos."""

    clip: ClipUse
    n: int
    trans: dict | None = None  # {type, before, after, intensity, blur_kind, direction, motion, envelope}, sem frames absolutos
    effects: list = field(default_factory=list)  # (início relativo, EffectTrack)
    events: list = field(default_factory=list)
    prov: Provenance | None = None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _num(op: dict, k: str, default=None):
    v = op.get(k, default)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise OpError(f"{op.get('op')}: '{k}' tem de ser um número")
    if not math.isfinite(v):
        raise OpError(f"{op.get('op')}: '{k}' não é finito")
    return v


def _rows(tl: Timeline, plan: Plan) -> list[_Row]:
    items = sorted(plan.video_items(), key=lambda v: v.at)
    rows = []
    for k, s in enumerate(tl.shots):
        tr = None
        t = s.transition_in
        if t is not None:
            tr = {"type": t.type, "before": t.cut_frame - t.start_frame, "after": t.end_frame - t.cut_frame, "intensity": t.intensity,
                  "blur_kind": t.blur_kind, "direction": t.direction, "motion": dict(t.motion), "envelope": list(t.envelope),
                  "peak_luma": t.peak_luma}
        rows.append(_Row(clip=s.clip.model_copy(deep=True), n=s.n_frames, trans=tr, prov=items[k].provenance if k < len(items) else None))
    for e in tl.effects:
        for s, r in zip(tl.shots, rows):
            if s.start_frame <= e.start_frame < s.end_frame:
                r.effects.append((e.start_frame - s.start_frame, e))
                break
    for e in tl.fx_events:
        for s, r in zip(tl.shots, rows):
            if s.start_frame <= e.start_frame < s.end_frame:
                r.events.append((e.start_frame - s.start_frame, e))
                break
    return rows


def _rebuild(tl: Timeline, rows: list[_Row]) -> tuple[Timeline, list[Provenance]]:
    if not rows:
        raise OpError("o vídeo ficou sem planos")
    shots, effects, events, prov = [], [], [], []
    cursor = 0
    for k, r in enumerate(rows):
        r.n = max(MIN_SHOT_FRAMES, r.n)
        trans = None
        if k > 0 and r.trans is not None:
            d = r.trans
            before, after = d["before"], d["after"]
            if d["type"] == "dissolve":  # vive dos dois lados do corte: tem de caber nos dois planos
                before, after = min(before, rows[k - 1].n), min(after, r.n)
            trans = Transition(type=d["type"], start_frame=cursor - before, end_frame=cursor + after, cut_frame=cursor,
                               intensity=d.get("intensity") or 0.0, blur_kind=d.get("blur_kind") or "none",
                               direction=d.get("direction") or "none", motion=d.get("motion") or {}, envelope=d.get("envelope") or [],
                               peak_luma=d.get("peak_luma"))
        c = r.clip
        if c.exact is not None and len(c.exact.src_frames) != r.n:
            # duração mudou: o rasto medido só vale se ainda cobrir o plano inteiro
            ex: ExactTrack = c.exact
            if r.n < len(ex.src_frames):
                c.exact = ex.model_copy(update={"src_frames": ex.src_frames[:r.n], "warps": ex.warps[:r.n],
                                                "measured": ex.measured[:r.n] if ex.measured else []})
            else:
                c.exact = None
        shots.append(TimelineShot(index=k, start_frame=cursor, end_frame=cursor + r.n, clip=c, transition_in=trans))
        prov.append(r.prov or Provenance(by="agent"))
        for rel, e in r.effects:
            if rel < r.n:
                shift = cursor + rel - e.start_frame
                effects.append(e.model_copy(update={"start_frame": e.start_frame + shift, "end_frame": e.end_frame + shift,
                                                    "peak_frame": e.peak_frame + shift}))
        for rel, e in r.events:
            if rel < r.n:
                shift = cursor + rel - e.start_frame
                events.append(e.model_copy(update={"start_frame": e.start_frame + shift, "end_frame": e.end_frame + shift}))
        cursor += r.n
    if cursor > MAX_TOTAL_S * tl.fps:
        raise OpError(f"o vídeo não pode passar de {MAX_TOTAL_S:.0f} s")
    texts = []
    for t in tl.texts:
        a, b = t.start_frame, min(t.end_frame, cursor)
        if t.style == "watermark":  # a conta a seguir está sempre lá, seja qual for a duração final
            texts.append(t.model_copy(update={"start_frame": 0, "end_frame": cursor}))
        elif a < cursor and b > a:
            texts.append(t.model_copy(update={"end_frame": b}))
    return tl.model_copy(update={"shots": shots, "effects": effects, "fx_events": events, "n_frames": cursor, "texts": texts}), prov


def _shot_fields(op: dict, r: _Row, fps: float, ctx: OpContext) -> None:
    """Campos de um plano que o agente pode mudar; o rasto medido deixa de valer se a fonte mudar."""
    c = r.clip
    changed_framing = False
    if op.get("file") is not None:
        c.source_path = ctx.key(str(op["file"]))
        changed_framing = True
    if op.get("src_start_s") is not None:
        c.src_start_s = max(0.0, _num(op, "src_start_s"))
        changed_framing = True
    if op.get("duration_s") is not None:
        r.n = max(MIN_SHOT_FRAMES, int(round(_num(op, "duration_s") * fps)))
    if op.get("speed") is not None:
        s = _clamp(_num(op, "speed"), 0.1, 8.0)
        c.speed = Curve(t=[0.0, 1.0], v=[s, s])
        changed_framing = True
    if op.get("speed_curve") is not None:
        vals = op["speed_curve"]
        try:
            v = [_clamp(float(x), 0.1, 8.0) for x in vals]
        except (TypeError, ValueError):
            raise OpError("update_shot: speed_curve tem de ser uma lista de velocidades (ex.: [1, 3, 0.5])")
        if len(v) < 2:
            raise OpError("update_shot: speed_curve precisa de pelo menos 2 valores")
        c.speed = Curve(t=[k / (len(v) - 1) for k in range(len(v))], v=v)
        changed_framing = True
    if op.get("slowmo") is not None:
        if op["slowmo"] not in ("blend", "hold"):
            raise OpError("update_shot: slowmo é 'blend' (suave) ou 'hold' (frames reais)")
        c.slowmo = op["slowmo"]
    z0, z1 = op.get("zoom_start"), op.get("zoom_end")
    if op.get("zoom") is not None:
        z0 = z1 = op["zoom"]
    if z0 is not None or z1 is not None:
        a = math.log(_clamp(float(z0 if z0 is not None else z1), 1.0, 4.0))
        b = math.log(_clamp(float(z1 if z1 is not None else z0), 1.0, 4.0))
        c.zoom = Curve(t=[0.0, 1.0], v=[round(a, 4), round(b, 4)])
        changed_framing = True
    if op.get("center") is not None:
        cc = op["center"]
        c.center = (_clamp(cc[0], 0.0, 1.0), _clamp(cc[1], 0.0, 1.0))
        changed_framing = True
    if op.get("pan") is not None:
        pp = op["pan"]
        c.pan = (_clamp(pp[0], -0.5, 0.5), _clamp(pp[1], -0.5, 0.5))
        changed_framing = True
    if op.get("flip") is not None:
        c.flip = bool(op["flip"])
    if op.get("gamma") is not None:
        c.tone_gamma = _clamp(_num(op, "gamma"), 0.4, 2.5)
    if op.get("saturation") is not None:
        c.tone_sat = _clamp(_num(op, "saturation"), 0.0, 2.5)
    if op.get("audio_volume") is not None:
        c.audio_volume = _clamp(_num(op, "audio_volume"), 0.0, 2.0)
    if changed_framing:
        c.exact = None
    r.prov = Provenance(by="agent", notes=[str(op["why"])[:200]] if op.get("why") else [])


def _transition_spec(kind: str, duration_s: float | None, intensity: float | None, fps: float, direction: str | None = None) -> dict | None:
    if kind not in TRANSITIONS:
        raise OpError(f"transição desconhecida: {kind} (tipos: {', '.join(TRANSITIONS)})")
    if kind == "cut":
        return {"type": "cut", "before": 0, "after": 1, "intensity": 0.0}
    n = max(2, int(round((duration_s or DEFAULT_TRANSITION_S[kind]) * fps)))
    return {"type": kind, "before": n // 2, "after": n - n // 2, "intensity": _clamp(intensity if intensity is not None else 0.7, 0.0, 1.0),
            "blur_kind": "radial" if kind == "blur" else "none", "direction": direction or "none", "motion": {}, "envelope": []}


def _new_row(op: dict, fps: float, ctx: OpContext) -> _Row:
    if op.get("file") is None or op.get("src_start_s") is None or op.get("duration_s") is None:
        raise OpError("plano novo precisa de file, src_start_s e duration_s")
    r = _Row(clip=ClipUse(source_path="", src_start_s=0.0), n=MIN_SHOT_FRAMES)
    _shot_fields(op, r, fps, ctx)
    tr = op.get("transition")
    if tr:
        r.trans = _transition_spec(tr, _num(op, "transition_s"), _num(op, "transition_intensity"), fps)
    return r


def _index(op: dict, rows: list[_Row], key: str = "index") -> int:
    if op.get(key) is None:
        raise OpError(f"{op.get('op')}: falta '{key}'")
    k = int(op[key])
    if not 0 <= k < len(rows):
        raise OpError(f"{op.get('op')}: não há plano {k} (o plano tem {len(rows)} planos, de 0 a {len(rows) - 1})")
    return k


def _indices(op: dict, rows: list[_Row]) -> list[int]:
    sel = op.get("indices", op.get("index"))
    if sel in (None, "all"):
        return list(range(len(rows)))
    if isinstance(sel, (int, float)):
        sel = [int(sel)]
    out = []
    for k in sel:
        k = int(k)
        if not 0 <= k < len(rows):
            raise OpError(f"{op.get('op')}: não há plano {k}")
        out.append(k)
    return out


def _speed_of(c: ClipUse) -> float:
    v, t = c.speed.v, c.speed.t
    if len(v) < 2:
        return float(v[0]) if v else 1.0
    area = sum((t[k + 1] - t[k]) * (v[k + 1] + v[k]) / 2 for k in range(len(v) - 1))
    return area / (t[-1] - t[0]) if t[-1] > t[0] else 1.0


def _split(rows: list[_Row], at_frame: int, fps: float) -> None:
    cursor = 0
    for k, r in enumerate(rows):
        if cursor < at_frame < cursor + r.n:
            a = at_frame - cursor
            if a < MIN_SHOT_FRAMES or r.n - a < MIN_SHOT_FRAMES:
                raise OpError(f"split_shot: o corte em {at_frame / fps:.2f}s fica a menos de {MIN_SHOT_FRAMES} frames de outro corte")
            second = _Row(clip=r.clip.model_copy(deep=True), n=r.n - a, prov=r.prov)
            speed = _speed_of(r.clip)
            second.clip.src_start_s = r.clip.src_start_s + a / fps * speed
            if r.clip.exact is not None:
                ex = r.clip.exact
                second.clip.exact = ex.model_copy(update={"src_frames": ex.src_frames[a:], "warps": ex.warps[a:],
                                                          "measured": ex.measured[a:] if ex.measured else []})
                second.clip.src_start_s = second.clip.exact.src_frames[0] / ex.src_fps
                r.clip.exact = ex.model_copy(update={"src_frames": ex.src_frames[:a], "warps": ex.warps[:a],
                                                     "measured": ex.measured[:a] if ex.measured else []})
            second.effects = [(rel - a, e) for rel, e in r.effects if rel >= a]
            second.events = [(rel - a, e) for rel, e in r.events if rel >= a]
            r.effects = [(rel, e) for rel, e in r.effects if rel < a]
            r.events = [(rel, e) for rel, e in r.events if rel < a]
            r.n = a
            rows.insert(k + 1, second)
            return
        cursor += r.n
    raise OpError(f"split_shot: {at_frame / fps:.2f}s não está dentro de nenhum plano")


def _set_cuts(rows: list[_Row], cuts_s: list[float], fps: float, total: int) -> None:
    """Move os cortes existentes para estes instantes (a duração total mantém-se, os planos também)."""
    if len(cuts_s) != len(rows) - 1:
        raise OpError(f"set_cuts: o plano tem {len(rows) - 1} cortes, recebi {len(cuts_s)} instantes (usa split_shot/remove_shot para mudar o número)")
    edges = [0] + [int(round(float(c) * fps)) for c in cuts_s] + [total]
    for a, b in zip(edges, edges[1:]):
        if b - a < MIN_SHOT_FRAMES:
            raise OpError(f"set_cuts: cortes em {a / fps:.2f}s e {b / fps:.2f}s demasiado próximos")
    for r, a, b in zip(rows, edges, edges[1:]):
        r.n = b - a


def _snap_to_beats(rows: list[_Row], beats: list[float], max_shift_s: float, fps: float) -> dict:
    if not beats:
        raise OpError("snap_cuts_to_beats: ainda não há batidas medidas para esta música (corre analyze_audio)")
    total = sum(r.n for r in rows)
    edges, moved = [0], 0
    cursor = 0
    for r in rows[:-1]:
        cursor += r.n
        best = min(beats, key=lambda b: abs(b - cursor / fps))
        target = int(round(best * fps)) if abs(best - cursor / fps) <= max_shift_s else cursor
        moved += target != cursor
        edges.append(max(edges[-1] + MIN_SHOT_FRAMES, target))
    if total - edges[-1] < MIN_SHOT_FRAMES:
        raise OpError("snap_cuts_to_beats: o último plano ficaria demasiado curto")
    edges.append(total)
    for r, a, b in zip(rows, edges, edges[1:]):
        r.n = b - a
    return {"cuts_moved": moved, "cuts": len(rows) - 1}


def _set_audio(op: dict, tl: Timeline, rows: list[_Row], ctx: OpContext) -> Timeline:
    up = {}
    bed = op.get("bed")
    if bed is not None:
        if bed == "none":
            up["music_path"] = None
        elif bed == "reference":
            if not ctx.reference_key:
                raise OpError("set_audio: este projeto não tem referência")
            up["music_path"], up["music_offset_s"] = ctx.reference_key, 0.0
        else:
            up["music_path"], up["music_offset_s"] = ctx.key(str(bed)), 0.0
    if op.get("bed_volume") is not None:
        up["music_volume"] = _clamp(_num(op, "bed_volume"), 0.0, 2.0)
    if op.get("bed_offset_s") is not None:
        up["music_offset_s"] = max(0.0, _num(op, "bed_offset_s"))
    if op.get("clip_volume") is not None:
        v = _clamp(_num(op, "clip_volume"), 0.0, 2.0)
        for r in rows:
            r.clip.audio_volume = v
    return tl.model_copy(update=up)


def _caption_overlays(items: list[dict], tl: Timeline, op: dict, ctx: OpContext) -> list[TextOverlay]:
    st = dict(ctx.caption_style)
    size = _clamp(_num(op, "size", st.get("size", 0.036)), 0.015, 0.12)  # corpo da letra em fração da LARGURA
    x = _clamp(_num(op, "x", st.get("x", 0.5)), 0.05, 0.95)
    y = _clamp(_num(op, "y", st.get("y", 0.92)), 0.05, 0.97)
    color = op.get("color") or st.get("color") or (243, 215, 84)
    alpha = _clamp(_num(op, "box_alpha", st.get("box_alpha", 0.8)), 0.0, 1.0)
    off = _num(op, "offset_s", 0.0) or 0.0
    fps = tl.fps
    out = []
    for c in items:
        text = str(c.get("text", "")).strip()[:120]
        if not text:
            continue
        a = int(round((float(c["start_s"]) + off) * fps))
        b = int(round((float(c["end_s"]) + off) * fps))
        if b > a >= 0:
            out.append(TextOverlay(text=text, start_frame=a, end_frame=b, x=x, y=y, width=0.92, height=size * tl.width / tl.height, style="box",
                                   color=tuple(int(v) for v in color)[:3], box_color=(0, 0, 0), box_alpha=alpha))
    return out


def _watermarks(op: dict, tl: Timeline) -> list[TextOverlay]:
    hs = op.get("handles") or []
    if not hs:
        raise OpError("set_watermark: indica as contas (handles: [{platform, handle}])")
    size = _clamp(_num(op, "size", 0.031), 0.012, 0.08)
    x = _clamp(_num(op, "x", WATERMARK_X), 0.0, 0.9)
    y = _clamp(_num(op, "y", WATERMARK_Y), 0.02, 0.97)
    opacity = _clamp(_num(op, "opacity", 0.62), 0.05, 1.0)
    height = size * tl.width / tl.height
    out = []
    for k, h in enumerate(hs[:4]):
        icon = h.get("platform", "none")
        if icon not in ICONS:
            icon = "none"
        text = str(h.get("handle", "")).strip()[:40]
        if not text:
            raise OpError("set_watermark: conta sem nome")
        if not text.startswith("@") and icon != "none":
            text = "@" + text
        out.append(TextOverlay(text=text, start_frame=0, end_frame=tl.n_frames, x=x, y=y + k * 1.55 * height, width=0.6, height=height,
                               style="watermark", icon=icon, opacity=opacity))
    return out


def _blend_grade(m: list[list[float]], strength: float) -> list[list[float]]:
    k = _clamp(strength, 0.0, 1.0)
    out = []
    for i in range(3):
        row = [round((1 - k) * (1.0 if i == j else 0.0) + k * m[i][j], 5) for j in range(3)]
        out.append(row + [round(k * m[i][3], 3)])
    return out


def _set_look(op: dict, tl: Timeline, ctx: OpContext) -> Timeline:
    up = {}
    for field_, key, lo, hi in (("post_gamma", "gamma", 0.4, 2.5), ("post_sat", "saturation", 0.0, 2.5),
                                ("post_contrast", "contrast", 0.4, 2.0), ("post_sharp", "sharpen", 0.0, 0.9)):
        if op.get(key) is not None:
            up[field_] = _clamp(_num(op, key), lo, hi)
    grade = op.get("grade")
    if grade == "none":
        up["post_matrix"] = None
    elif grade == "reference":
        if not ctx.grade:
            raise OpError("set_look: a cor da referência ainda não foi medida (corre build_edit primeiro)")
        up["post_matrix"] = _blend_grade(ctx.grade, _num(op, "strength", 0.8))
    elif grade is not None:
        raise OpError("set_look: grade é 'reference' ou 'none'")
    return tl.model_copy(update=up)


def _set_effects(op: dict, tl: Timeline, rows: list[_Row], fps: float) -> Timeline:
    up = {}
    if op.get("scale") is not None:
        up["fx_scale"] = _clamp(_num(op, "scale"), 0.0, 3.0)
    if op.get("clear"):
        for r in rows:
            r.effects, r.events = [], []
    if op.get("remove_at_s") is not None:
        f = int(round(_num(op, "remove_at_s") * fps))
        cursor = 0
        for r in rows:
            r.effects = [(rel, e) for rel, e in r.effects if not (cursor + rel <= f < cursor + rel + (e.end_frame - e.start_frame))]
            r.events = [(rel, e) for rel, e in r.events if not (cursor + rel <= f < cursor + rel + (e.end_frame - e.start_frame))]
            cursor += r.n
    for a in op.get("add") or []:
        kind = a.get("type")
        n = max(2, int(round(float(a.get("duration_s", 0.3)) * fps)))
        f = int(round(float(a.get("at_s", 0.0)) * fps))
        cursor = 0
        for r in rows:
            if cursor <= f < cursor + r.n:
                rel = f - cursor
                if kind in EFFECTS:
                    mag = float(a.get("magnitude", 0.12))
                    r.effects.append((rel, EffectTrack(type=kind, start_frame=f, end_frame=f + n, peak_frame=f + n // 2, magnitude=mag,
                                                       intensity=_clamp(abs(mag) * 4, 0.0, 1.0), anchor="free", confidence=1.0)))
                elif kind in FX_EVENTS:
                    r.events.append((rel, FxEvent(type=kind, start_frame=f, end_frame=f + n, intensity=_clamp(float(a.get("intensity", 0.7)), 0.0, 1.0))))
                else:
                    raise OpError(f"set_effects: efeito desconhecido '{kind}' (efeitos: {', '.join(EFFECTS + FX_EVENTS)})")
                break
            cursor += r.n
        else:
            raise OpError(f"set_effects: {a.get('at_s')}s está fora do vídeo")
    return tl.model_copy(update=up)


def _set_output(op: dict, tl: Timeline) -> Timeline:
    w, h = int(_num(op, "width", tl.width)), int(_num(op, "height", tl.height))
    w, h = max(144, min(w, 2160)) // 2 * 2, max(144, min(h, 2160)) // 2 * 2
    if max(w, h) > 1920:
        raise OpError("set_output: o máximo é 1920 no lado maior")
    return tl.model_copy(update={"width": w, "height": h})


def apply_ops(plan: Plan, ops: list[dict], ctx: OpContext | None = None) -> tuple[Plan, list[str]]:
    """Devolve (plano novo, notas). `OpError` se alguma operação não puder ser aplicada."""
    ctx = ctx or OpContext()
    tl = plan_to_timeline(plan)
    fps = tl.fps
    rows = _rows(tl, plan)
    notes: list[str] = []
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            raise OpError("cada operação é um objeto com o campo 'op'")
        name = op["op"]
        total = sum(r.n for r in rows)
        if name == "update_shot":
            for k in _indices(op, rows):
                _shot_fields(op, rows[k], fps, ctx)
        elif name == "insert_shot":
            pos = int(op.get("index", len(rows)))
            if not 0 <= pos <= len(rows):
                raise OpError(f"insert_shot: posição {pos} fora de 0..{len(rows)}")
            rows.insert(pos, _new_row(op, fps, ctx))
        elif name == "set_shots":
            lst = op.get("shots") or []
            if not lst:
                raise OpError("set_shots: lista vazia")
            rows = [_new_row(s, fps, ctx) for s in lst]
            tl = tl.model_copy(update={"effects": [], "fx_events": []})
        elif name == "remove_shot":
            rows.pop(_index(op, rows))
        elif name == "move_shot":
            r = rows.pop(_index(op, rows, "from"))
            to = int(op.get("to", 0))
            if not 0 <= to <= len(rows):
                raise OpError(f"move_shot: destino {to} fora de 0..{len(rows)}")
            rows.insert(to, r)
        elif name == "split_shot":
            _split(rows, int(round(_num(op, "at_s") * fps)), fps)
        elif name == "set_cuts":
            _set_cuts(rows, op.get("cuts_s") or [], fps, total)
        elif name == "snap_cuts_to_beats":
            res = _snap_to_beats(rows, ctx.beats, _clamp(_num(op, "max_shift_s", 0.25), 0.02, 1.0), fps)
            notes.append(f"{res['cuts_moved']} de {res['cuts']} cortes movidos para a batida")
        elif name == "set_transition":
            kind = op.get("type", "cut")
            spec = _transition_spec(kind, _num(op, "duration_s"), _num(op, "intensity"), fps, op.get("direction"))
            for k in _indices(op, rows):
                if k > 0:
                    rows[k].trans = dict(spec) if spec else None
        elif name == "strip_to_cuts":  # só cortes secos, imagem intacta
            for k, r in enumerate(rows):
                r.trans = dict(_transition_spec("cut", None, None, fps)) if k else None
                r.effects, r.events = [], []
                c = r.clip
                c.zoom, c.center, c.pan, c.flip = Curve(t=[0.0, 1.0], v=[0.0, 0.0]), (0.5, 0.5), (0.0, 0.0), False
                c.tone_gamma = c.tone_sat = c.subj_gamma = c.bg_gamma = 1.0
                c.subj_sharp = 0.0
                c.exact = None
                s = _speed_of(c)
                if abs(s - 1.0) > 1e-3 or len(set(c.speed.v)) > 1:
                    c.speed = Curve(t=[0.0, 1.0], v=[1.0, 1.0])
            tl = tl.model_copy(update={"fx_scale": 1.0, "post_gamma": 1.0, "post_sat": 1.0, "post_contrast": 1.0, "post_sharp": 0.0,
                                       "post_matrix": None, "denoise": 0.0, "zoom_gain": 1.0})
        elif name == "trim_total":
            target = int(round(_num(op, "duration_s") * fps))
            if target < MIN_SHOT_FRAMES:
                raise OpError("trim_total: duração demasiado curta")
            kept, cursor = [], 0
            for r in rows:
                if cursor >= target:
                    break
                r.n = min(r.n, target - cursor)
                kept.append(r)
                cursor += r.n
            rows = kept
        elif name == "set_audio":
            tl = _set_audio(op, tl, rows, ctx)
        elif name == "set_captions":
            items = op.get("items")
            if op.get("from_reference"):
                if not ctx.captions:
                    raise OpError("set_captions: ainda não há legendas lidas da referência (corre extract_captions)")
                items = ctx.captions
            if not items:
                raise OpError("set_captions: indica items ou from_reference")
            keep = [t for t in tl.texts if t.style != "box"]
            tl = tl.model_copy(update={"texts": keep + _caption_overlays(items, tl, op, ctx)})
        elif name == "clear_captions":
            tl = tl.model_copy(update={"texts": [t for t in tl.texts if t.style != "box"]})
        elif name == "add_text":
            a, b = int(round(_num(op, "start_s") * fps)), int(round(_num(op, "end_s") * fps))
            if b <= a:
                raise OpError("add_text: end_s tem de ser maior que start_s")
            text = str(op.get("text", "")).strip()[:120]
            if not text:
                raise OpError("add_text: texto vazio")
            entry = op.get("entry", "pop")
            if entry not in ("pop", "fade", "none"):
                entry = "pop"
            tl = tl.model_copy(update={"texts": tl.texts + [TextOverlay(
                text=text, start_frame=a, end_frame=b, x=_clamp(_num(op, "x", 0.5), 0.05, 0.95), y=_clamp(_num(op, "y", 0.5), 0.05, 0.95), width=0.9,
                height=_clamp(_num(op, "size", 0.05), 0.015, 0.2), entry=entry)]})
        elif name == "clear_text":
            tl = tl.model_copy(update={"texts": [t for t in tl.texts if t.style != "outline"]})
        elif name == "set_watermark":
            keep = [t for t in tl.texts if t.style != "watermark"]
            tl = tl.model_copy(update={"n_frames": total})
            tl = tl.model_copy(update={"texts": keep + _watermarks(op, tl)})
        elif name == "clear_watermark":
            tl = tl.model_copy(update={"texts": [t for t in tl.texts if t.style != "watermark"]})
        elif name == "set_look":
            tl = _set_look(op, tl, ctx)
        elif name == "set_output":
            tl = _set_output(op, tl)
        elif name == "set_effects":
            tl = _set_effects(op, tl, rows, fps)
        else:
            raise OpError(f"operação desconhecida: {name}")
        if not rows:
            raise OpError("o vídeo ficou sem planos")
    new_tl, prov = _rebuild(tl, rows)
    return timeline_to_plan(new_tl, provenance=prov, meta=dict(plan.meta)), notes


def scale_output(plan: Plan, short_side: int) -> Plan:
    """Mesmo plano noutra resolução (pré-visualização rápida vs final): tudo o resto é relativo."""
    o = plan.output
    k = short_side / min(o.width, o.height)
    p = plan.model_copy(deep=True)
    p.output.width = max(2, int(round(o.width * k / 2)) * 2)
    p.output.height = max(2, int(round(o.height * k / 2)) * 2)
    return p


def no_music_variant(plan: Plan) -> Plan:
    """Versão para publicar com o som oficial da app: sem faixa principal, com o som dos clips."""
    p = plan.model_copy(deep=True)
    p.tracks = [t for t in p.tracks if t.kind != "audio"]
    for it in p.video_items():
        it.audio_volume = 1.0
    used = {it.source for it in p.video_items()}
    p.sources = {k: v for k, v in p.sources.items() if k in used}
    return p
