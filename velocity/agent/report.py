"""Relatórios compactos para o modelo: o que ele precisa de saber, em poucos tokens.

O DNA completo tem centenas de KB; um plano medido tem listas por frame. O modelo trabalha com
tabelas em segundos e arredondadas; os dados completos ficam no estado do projeto. Python puro:
corre na API de produção, que não tem numpy.
"""
from __future__ import annotations

from velocity.plan.schema import Plan
from velocity.schemas.edit_dna import EditDNA


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _ln_to_scale(v: float) -> float:
    import math
    return round(math.exp(v), 2)


def dna_report(dna: EditDNA, summary: dict | None = None) -> dict:
    """Análise profunda da referência, por plano e global."""
    fps = dna.fps
    shots = []
    for s in dna.shots:
        t = s.transition_in
        m = s.motion
        row = {"i": s.index, "t": _r(s.start_frame / fps), "dur": _r(s.duration_s),
               "in": t.type if t else None, "motion": m.motion_type, "energy": _r(m.energy_mean, 4),
               "zoom": _r(m.zoom_total, 3), "pan": [_r(m.pan_x, 3), _r(m.pan_y, 3)],
               "speed": s.speed.shape if s.speed.confidence > 0 else "unknown",
               "luma": _r(s.appearance.mean_luma, 0), "sat": _r(s.appearance.saturation, 0)}
        if t is not None and t.type != "cut":
            row["in_s"] = _r((t.end_frame - t.start_frame) / fps)
            if t.direction and t.direction != "none":
                row["in_dir"] = t.direction
        if m.shake > 0.004:
            row["shake"] = _r(m.shake, 4)
        shots.append(row)
    out: dict = {
        "duration_s": dna.duration_s, "size": [dna.width, dna.height], "n_shots": len(dna.shots), "shots": shots,
        "pace": {"cuts_per_s": dna.signature.cuts_per_second, "median_shot_s": dna.signature.median_shot_s,
                 "p10_s": dna.signature.shot_s_p10, "p90_s": dna.signature.shot_s_p90},
        "transitions": dna.signature.transition_counts,
        "effects": [{"type": e.type, "t": _r(e.peak_frame / fps), "dur": _r((e.end_frame - e.start_frame) / fps),
                     "mag": _r(e.magnitude, 3), "on": e.anchor} for e in dna.effects[:60]],
        "effects_omitted": max(0, len(dna.effects) - 60),
        "fx_events": [{"type": e.type, "t": _r(e.start_frame / fps), "dur": _r((e.end_frame - e.start_frame) / fps)}
                      for e in dna.fx_events[:30]],
        "letterbox": dna.letterbox.model_dump() if any(dna.letterbox.model_dump().values()) else None,
        "text_events": [{"t": _r(e.start_frame / fps), "dur": _r((e.end_frame - e.start_frame) / fps), "pos": e.position,
                         "entry": e.entry} for e in dna.text_events[:20]],
        "warnings": dna.warnings,
    }
    a = dna.audio
    out["audio"] = {"has_audio": a.has_audio, "bpm": _r(a.bpm, 1), "n_beats": len(a.beats), "first_beats_s": [_r(b) for b in a.beats[:16]],
                    "cuts_on_beat": _r(a.cut_beat_alignment), "chance_alignment": _r(a.chance_alignment),
                    "cut_every_beats": _r(a.cut_interval_beats)}
    lk = dna.look
    if lk is not None:
        out["look"] = {"luma": _r(lk.luma, 0), "contrast": _r(lk.contrast, 1), "saturation": _r(lk.saturation, 1),
                       "sharpness": _r(lk.sharpness, 0), "aggressiveness": _r(lk.aggressiveness), "descriptors": lk.descriptors,
                       "shake_fraction": _r(lk.shake_fraction), "rgb_split_fraction": _r(lk.rgb_split_fraction),
                       "punches_per_s": _r(lk.punch_rate)}
    vb = dna.vibe
    if vb is not None:
        step = max(1, int(round(vb.energy_hz)))
        out["vibe"] = {"descriptors": vb.descriptors, "arc": vb.arc, "aggressiveness": _r(vb.aggressiveness), "smoothness": _r(vb.smoothness),
                       "calm": _r(vb.calm), "energy_1hz": [_r(v) for v in vb.energy_curve[::step]],
                       "impacts_s": [_r(i.t) for i in vb.impacts[:24]], "rests_s": [[_r(a_), _r(b_)] for a_, b_ in vb.rests[:12]]}
    if summary:
        out["is_velocity_style"] = summary.get("is_velocity")
        out["tags"] = summary.get("tags")
    return out


def plan_report(plan: Plan, names: dict[str, str] | None = None) -> dict:
    """O plano de trabalho como tabela: é o que o modelo edita com `update_plan`."""
    names = names or {}
    o = plan.output
    fps = o.fps
    rows = []
    for k, it in enumerate(sorted(plan.video_items(), key=lambda v: v.at)):
        uri = plan.sources[it.source].uri
        tr = it.transition_in
        z = it.transform.zoom.v
        sp = it.time.speed.v
        row = {"i": k, "t": _r(it.at / fps), "dur": _r(it.duration / fps), "file": names.get(uri, uri.rsplit("/", 1)[-1]),
               "in_s": _r(it.time.src_start_s)}
        if max(sp) != 1 or min(sp) != 1:
            row["speed"] = [_r(v) for v in sp] if len(sp) <= 6 else [_r(sp[0]), _r(max(sp)), _r(sp[-1])]
        if any(abs(v) > 1e-3 for v in z):
            row["zoom"] = [_ln_to_scale(z[0]), _ln_to_scale(z[-1])]
        if it.transform.center != (0.5, 0.5):
            row["center"] = [_r(c) for c in it.transform.center]
        if any(abs(p) > 1e-3 for p in it.transform.pan):
            row["pan"] = [_r(p, 3) for p in it.transform.pan]
        if it.transform.flip:
            row["flip"] = True
        if tr is not None and tr.type != "cut":
            row["trans"] = {"type": tr.type, "dur": _r((tr.end_frame - tr.start_frame) / fps)}
        if abs(it.color.gamma - 1) > 0.02 or abs(it.color.saturation - 1) > 0.02:
            row["tone"] = [_r(it.color.gamma), _r(it.color.saturation)]
        if it.time.src_frames is not None:
            row["measured"] = True  # rasto exato da referência (cenário A)
        if it.audio_volume > 0:
            row["clip_vol"] = _r(it.audio_volume)
        rows.append(row)
    audio = [it for t in plan.track("audio") for it in t.items]
    texts = [it for t in plan.track("text") for it in t.items]
    box = [t for t in texts if t.style == "box"]
    marks = [t for t in texts if t.style == "watermark"]
    other = [t for t in texts if t.style == "outline"]
    lk = plan.look
    return {
        "output": {"size": [o.width, o.height], "fps": o.fps, "duration_s": _r(o.duration_frames / fps)},
        "shots": rows,
        "audio": [{"file": names.get(plan.sources[a.source].uri, plan.sources[a.source].uri.rsplit("/", 1)[-1]),
                   "volume": _r(a.volume), "offset_s": _r(a.offset_s)} for a in audio],
        "captions": {"lines": len(box), "first": [{"text": t.text, "t": _r(t.at / fps)} for t in box[:4]]} if box else None,
        "watermark": [{"icon": t.icon, "text": t.text} for t in marks] or None,
        "texts": [{"text": t.text, "t": _r(t.at / fps), "dur": _r(t.duration / fps)} for t in other] or None,
        "effects": {"count": len(plan.effects), "scale": plan.effects_scale, "events": len(plan.fx_events),
                    "by_type": _count(e.type for e in plan.effects)},
        "look": {"gamma": lk.gamma, "saturation": lk.saturation, "contrast": lk.contrast, "sharpen": lk.sharpen,
                 "graded": lk.matrix is not None},
    }


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def reused_material(plan: Plan, fps: float | None = None) -> list[dict]:
    """Trechos da mesma fonte mostrados mais do que uma vez (o utilizador nota e não gosta)."""
    fps = fps or plan.output.fps
    spans: dict[str, list] = {}
    for it in sorted(plan.video_items(), key=lambda v: v.at):
        mean = sum(it.time.speed.v) / max(len(it.time.speed.v), 1)
        a = it.time.src_start_s
        spans.setdefault(it.source, []).append((a, a + it.duration / fps * mean, it.at / fps))
    out = []
    for sid, lst in spans.items():
        for i, (a0, a1, t0) in enumerate(lst):
            for b0, b1, t1 in lst[i + 1:]:
                ov = min(a1, b1) - max(a0, b0)
                if ov > 0.25:
                    out.append({"source": plan.sources[sid].uri.rsplit("/", 1)[-1], "overlap_s": round(ov, 2),
                                "shown_at_s": [round(t0, 2), round(t1, 2)]})
    return out
