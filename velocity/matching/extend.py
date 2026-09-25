"""Estica ou apara o Edit DNA para o alvo de produto (~25-30s), preservando a linguagem de edição.

Decisão de arquitetura: a maioria das referências reais (WhatsApp/TikTok) tem 10-20s, mas o produto
pede 25-30s de saída. Em vez de inventar padrão novo, REPETIMOS o padrão de corte/efeito medido
(cada repetição escolhe material NOVO do utilizador — ver matching/engine.py, que já penaliza reutilização)
e inserimos uma transição de "costura" no ponto de loop, com o estilo mais representativo já medido na
própria referência. O resultado passa a ser o alvo real contra o qual avaliamos o output (o "25-30s
extenso" É a Edit DNA a reproduzir), por isso todos os campos derivados (signature/audio/vibe) são
recalculados sobre a timeline estendida, não copiados.
"""
from __future__ import annotations

import copy
import math

import numpy as np

from velocity.reference.audio import ALIGN_TOL_S, chance_alignment
from velocity.reference.vibe import ENERGY_HZ, _arc
from velocity.schemas.edit_dna import AudioDNA, EditDNA, Shot, Transition, VibeProfile

TARGET_MIN_S = 25.0
TARGET_MAX_S = 31.0


def _wrap_transition_template(shots: list[Shot]) -> Transition | None:
    """Transição a usar no ponto de costura entre repetições: a mais representativa (mediana de intensidade)
    entre as reais da referência, para o loop 'soar' como um corte que a própria referência poderia ter."""
    cands = [s.transition_in for s in shots[1:] if s.transition_in is not None]
    if not cands:
        return None
    cands = sorted(cands, key=lambda t: t.intensity)
    return cands[len(cands) // 2]


def _recompute_audio_and_vibe(shots: list[Shot], fx_events, effects, orig: EditDNA, duration_s: float, fps: float) -> tuple[AudioDNA, VibeProfile | None]:
    a0 = orig.audio
    n_frames = int(round(duration_s * fps))
    beats: list[float] = []
    if a0.has_audio and a0.beats:
        period = orig.duration_s
        reps_needed = math.ceil(duration_s / period) + 1
        beats = [b + r * period for r in range(reps_needed) for b in a0.beats if b + r * period < duration_s]
    # recomputa beat_index/beat_offset_ms de cada transição contra a grelha de batidas estendida
    transitions = [s.transition_in for s in shots if s.transition_in is not None]
    if beats and len(beats) >= 2:
        barr = np.array(beats)
        for t in transitions:
            tt = t.cut_frame / fps
            k = int(np.argmin(np.abs(barr - tt)))
            t.beat_offset_ms = round(float((tt - barr[k]) * 1000), 1)
            t.beat_index = round(float(np.interp(tt, barr, np.arange(len(barr)))), 3)
        cut_times = np.array([t.cut_frame / fps for t in transitions])
        dists = np.array([np.min(np.abs(barr - c)) for c in cut_times]) if len(cut_times) else np.array([])
        align = float((dists <= ALIGN_TOL_S).mean()) if len(dists) else None
        chance = chance_alignment(beats, duration_s)
        interval_beats = round(float(np.median(np.diff(cut_times)) / float(np.median(np.diff(barr)))), 3) if len(cut_times) >= 2 else None
    else:
        align = chance = interval_beats = None
    audio = AudioDNA(has_audio=a0.has_audio, bpm=a0.bpm, beats=[round(b, 3) for b in beats], onsets=[],
                     cut_beat_alignment=round(align, 3) if align is not None else None,
                     chance_alignment=round(chance, 3) if chance is not None else None, cut_interval_beats=interval_beats)

    vibe = None
    if orig.vibe and orig.vibe.energy_curve:
        v0 = orig.vibe
        n_samples = int(round(duration_s * v0.energy_hz))
        curve = (v0.energy_curve * math.ceil(n_samples / max(len(v0.energy_curve), 1)))[:n_samples]
        t_axis = (np.arange(len(curve)) + 0.5) / v0.energy_hz
        third = max(1, len(curve) // 3)
        levels = [round(float(np.mean(curve[:third])), 3), round(float(np.mean(curve[third:2 * third])), 3), round(float(np.mean(curve[2 * third:])), 3)]
        period = orig.duration_s
        reps_needed = math.ceil(duration_s / period) + 1
        impacts = [im.model_copy(update={"t": round(im.t + r * period, 2)}) for r in range(reps_needed) for im in v0.impacts if im.t + r * period < duration_s]
        rests = [(round(a + r * period, 2), round(min(b + r * period, duration_s), 2)) for r in range(reps_needed) for a, b in v0.rests if a + r * period < duration_s]
        non_cut = [t for t in transitions if t.type != "cut"]
        vibe = v0.model_copy(update={
            "energy_curve": [round(float(v), 3) for v in curve], "arc": _arc(levels), "arc_levels": levels,
            "impacts": impacts, "rests": rests,
            "transition_density": round(len(transitions) / duration_s, 3) if duration_s else 0.0,
            "effect_density": round((len(non_cut) + len(fx_events) + sum(1 for e in effects if e.type in ("zoom_punch", "shake", "rgb_split"))) / duration_s, 3) if duration_s else 0.0,
        })
    return audio, vibe


def _rebuild(shots: list[Shot], fx_events, effects, text_events, orig: EditDNA) -> EditDNA:
    from velocity.reference.analyzer import _signature  # import tardio: evita ciclo (analyzer importa este módulo indiretamente via cli)

    n_frames = shots[-1].end_frame
    duration_s = round(n_frames / orig.fps, 3)
    audio, vibe = _recompute_audio_and_vibe(shots, fx_events, effects, orig, duration_s, orig.fps)
    sig = _signature(shots, fx_events, orig.fps, duration_s)
    return orig.model_copy(update={
        "n_frames": n_frames, "duration_s": duration_s, "shots": shots, "fx_events": fx_events,
        "effects": effects, "text_events": text_events, "audio": audio, "vibe": vibe, "signature": sig,
        "warnings": orig.warnings,
    })


def _tile(orig: EditDNA, reps: int) -> EditDNA:
    wrap = _wrap_transition_template(orig.shots)
    n0 = orig.n_frames
    shots: list[Shot] = []
    fx_events: list = []
    effects: list = []
    text_events: list = []
    for r in range(reps):
        off = r * n0
        for s in orig.shots:
            tin = s.transition_in
            if r > 0 and s.index == 0:
                # ponto de costura: usa a transição mais representativa da referência, centrada no loop
                base = wrap or Transition(type="cut", start_frame=0, end_frame=1, cut_frame=0, intensity=0.6)
                span_pre = max(base.cut_frame - base.start_frame, 1)
                span_post = max(base.end_frame - base.cut_frame, 1)
                tin = base.model_copy(update={"start_frame": max(0, off - span_pre), "end_frame": off + span_post, "cut_frame": off})
            elif tin is not None:
                tin = tin.model_copy(update={"start_frame": tin.start_frame + off, "end_frame": tin.end_frame + off, "cut_frame": tin.cut_frame + off})
            shots.append(s.model_copy(update={"index": len(shots), "start_frame": s.start_frame + off, "end_frame": s.end_frame + off, "transition_in": tin}))
        for f in orig.fx_events:
            fx_events.append(f.model_copy(update={"start_frame": f.start_frame + off, "end_frame": f.end_frame + off}))
        for e in orig.effects:
            effects.append(e.model_copy(update={"start_frame": e.start_frame + off, "end_frame": e.end_frame + off, "peak_frame": e.peak_frame + off}))
        for tx in orig.text_events:
            text_events.append(tx.model_copy(update={"start_frame": tx.start_frame + off, "end_frame": tx.end_frame + off}))
    return _rebuild(shots, fx_events, effects, text_events, orig)


def _trim(dna: EditDNA, target_max: float, target_min: float) -> EditDNA:
    fps = dna.fps
    best_end = None
    for s in dna.shots:
        end_s = s.end_frame / fps
        if end_s <= target_max:
            if best_end is None or end_s >= target_min or best_end / fps < target_min:
                best_end = s.end_frame
        else:
            break
    if best_end is None:
        best_end = dna.shots[0].end_frame
    kept = [s for s in dna.shots if s.end_frame <= best_end]
    fx = [f for f in dna.fx_events if f.start_frame < best_end]
    eff = [e for e in dna.effects if e.start_frame < best_end]
    txt = [t for t in dna.text_events if t.start_frame < best_end]
    return _rebuild(kept, fx, eff, txt, dna)


def extend_reference(dna: EditDNA, target_min: float = TARGET_MIN_S, target_max: float = TARGET_MAX_S) -> tuple[EditDNA, dict]:
    """Devolve (dna_para_produzir, info) — info descreve a decisão para o log de decisões."""
    if dna.duration_s > target_max:
        out = _trim(dna, target_max, target_min)
        return out, {"acao": "aparado", "de_s": dna.duration_s, "para_s": out.duration_s,
                     "why": f"referência ({dna.duration_s:.1f}s) acima do alvo do produto ({target_min:.0f}-{target_max:.0f}s)"}
    if dna.duration_s >= target_min:
        return dna, {"acao": "nenhuma", "duracao_s": dna.duration_s, "why": "já dentro do alvo do produto"}
    reps = max(2, math.ceil(target_min / max(dna.duration_s, 0.1)))
    out = _tile(dna, reps)
    if out.duration_s > target_max:
        out2 = _trim(out, target_max, target_min)
        if out2.duration_s >= target_min * 0.85:  # aparar não pode devolver algo drasticamente mais curto que o alvo
            out = out2
    return out, {"acao": "repetida", "repeticoes": reps, "de_s": dna.duration_s, "para_s": out.duration_s,
                 "why": f"referência curta ({dna.duration_s:.1f}s); padrão de edição repetido com material novo a cada volta para atingir {target_min:.0f}-{target_max:.0f}s"}
