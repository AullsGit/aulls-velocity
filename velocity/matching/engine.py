"""Matching Engine: (EditDNA, [ClipProfile]) -> um trecho por plano.

Não compara pixels. Cada plano da referência descreve uma FUNÇÃO na edição (quão intensa é a
ação, com que forma temporal, quão claro é); procuramos no material do utilizador o trecho que
melhor a desempenha. Pesos e limiares são parâmetros (MatchWeights) para calibrar com dados.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from velocity.media.analyzer import ClipProfile
from velocity.render.compositor import source_frames_needed
from velocity.schemas.edit_dna import Curve, EditDNA, Shot


@dataclass
class MatchWeights:
    action: float = 0.30  # rank de energia do trecho vs rank do plano da referência
    shape: float = 0.30  # forma temporal da energia (o "impacto" no sítio certo)
    brightness: float = 0.10
    subject: float = 0.20  # sujeito presente e bem localizado
    reuse: float = 0.10  # penalização por reutilizar material
    speed_gain: float = 0.6  # quanto da curva de velocidade da referência é aplicado
    target_subject_height: float = 0.55  # tamanho-alvo do sujeito após reenquadramento
    max_punch_in: float = 2.4
    stride: int = 3  # frames entre candidatos


@dataclass
class Match:
    shot_index: int
    clip_index: int
    start_frame: int
    need_frames: int
    cost: float
    parts: dict = field(default_factory=dict)
    speed: Curve | None = None
    freeze_tail: bool = False  # material insuficiente: último frame é repetido


def _resample(x: np.ndarray, k: int = 8) -> np.ndarray:
    if len(x) < 2:
        return np.zeros(k)
    return np.interp(np.linspace(0, 1, k), np.linspace(0, 1, len(x)), x)


def _minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    return (x - lo) / (hi - lo) if hi - lo > 1e-9 else np.full_like(x, 0.5)


def shot_speed_curve(shot: Shot, gain: float) -> Curve:
    """Curva de velocidade de reprodução para o plano. Só se aplica com forma dinâmica detetada."""
    sp = shot.speed
    if sp.shape in ("flat", "unknown") or sp.confidence <= 0.0:
        return Curve(t=[0.0, 1.0], v=[1.0, 1.0])
    rel = np.array(sp.curve.v)
    v = np.clip(1.0 + gain * (rel - 1.0), 0.45, 2.0)
    return Curve(t=list(sp.curve.t), v=[round(float(x), 4) for x in v])


def _window_stats(c: ClipProfile, i: int, L: int):
    e = c.energy[i : i + L]
    s = c.subj[i : i + L]
    return e, s


def _energy_rank_table(clips: list[ClipProfile], L: int = 12) -> np.ndarray:
    vals = []
    for c in clips:
        for i in range(0, max(1, c.n - L), 3):
            vals.append(float(c.energy[i : i + L].mean()))
    return np.sort(np.array(vals)) if vals else np.zeros(1)


def match(dna: EditDNA, clips: list[ClipProfile], w: MatchWeights | None = None) -> list[Match]:
    w = w or MatchWeights()
    shots = dna.shots
    ref_e = np.array([s.motion.energy_mean for s in shots])
    ref_rank = (np.argsort(np.argsort(ref_e)) + 0.5) / len(ref_e)  # percentil de cada plano entre os planos
    table = _energy_rank_table(clips)
    all_lum = np.concatenate([c.lum for c in clips]).mean()

    slots = []
    for s in shots:
        L = s.end_frame - s.start_frame
        sc = shot_speed_curve(s, w.speed_gain)
        need = int(np.ceil(source_frames_needed(L, sc))) + 3
        slots.append((s, L, sc, need))

    used: list[list[tuple[int, int]]] = [[] for _ in clips]
    matches: dict[int, Match] = {}
    # planos mais exigentes (mais material fonte) escolhem primeiro
    order = sorted(range(len(shots)), key=lambda k: -slots[k][3])

    def overlap(ci: int, a: int, b: int) -> bool:
        return any(a < ub + 6 and ua - 6 < b for ua, ub in used[ci])

    for k in order:
        s, L, sc, need = slots[k]
        ref_shape = _minmax(np.array(s.speed.curve.v)) if s.speed.curve.v else np.full(8, 0.5)
        best = None
        for ci, c in enumerate(clips):
            n_need = min(need, c.n)  # clip curto: usa o que há e congela a cauda
            for i in range(0, max(1, c.n - n_need + 1), w.stride):
                if c.bad[i : i + n_need].any():
                    continue
                e, sj = _window_stats(c, i, n_need)
                rank = float(np.searchsorted(table, e.mean()) / len(table))
                a_cost = abs(rank - ref_rank[k])
                ws = _minmax(_resample(np.convolve(e, np.ones(3) / 3, mode="same"), 8))
                corr = float(np.corrcoef(ws, ref_shape)[0, 1]) if ws.std() > 1e-6 and ref_shape.std() > 1e-6 else 0.0
                shape_cost = (1 - corr) / 2
                b_cost = abs(float(c.lum[i : i + n_need].mean()) - s.appearance.mean_luma) / 255.0
                present = float(sj[:, 3].mean())
                subj_cost = 1.0 - present
                reuse_cost = 1.0 if overlap(ci, i, i + n_need) else 0.0
                cost = (w.action * a_cost + w.shape * shape_cost + w.brightness * b_cost
                        + w.subject * subj_cost + w.reuse * reuse_cost)
                if best is None or cost < best.cost:
                    best = Match(s.index, ci, i, n_need, cost,
                                 {"action": a_cost, "shape": shape_cost, "brightness": b_cost, "subject": subj_cost, "reuse": reuse_cost},
                                 sc, freeze_tail=need > c.n)
        if best is None:  # todos os trechos têm cortes próprios: relaxa e usa o mais calmo
            ci = int(np.argmax([c.n for c in clips]))
            best = Match(s.index, ci, 0, min(need, clips[ci].n), 1.0, {"fallback": 1.0}, sc, freeze_tail=True)
        matches[s.index] = best
        used[best.clip_index].append((best.start_frame, best.start_frame + best.need_frames))
    return [matches[s.index] for s in shots]


def reframe(c: ClipProfile, m: Match, w: MatchWeights) -> tuple[tuple[float, float], float]:
    """(centro, ln-escala base) para levar o sujeito ao tamanho-alvo."""
    sj = c.subj[m.start_frame : m.start_frame + m.need_frames]
    pres = sj[:, 3] > 0.4
    if pres.sum() < 2:
        return (0.5, 0.5), 0.0
    cx, cy = float(np.median(sj[pres, 0])), float(np.median(sj[pres, 1]))
    h = float(np.median(sj[pres, 2]))
    scale = np.clip(w.target_subject_height / max(h, 0.05), 1.0, w.max_punch_in)
    return (cx, cy), float(np.log(scale))
