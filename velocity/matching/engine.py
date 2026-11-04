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
    direction: float = 0.08  # sentido do movimento do sujeito vs pan da referência
    unstable: float = 0.6  # penalização por frames instáveis (transições/cortes do PRÓPRIO clip) na janela
    continuity: float = 0.15  # posição do sujeito contínua em cortes secos (match cut)
    subject: float = 0.20  # sujeito presente e bem localizado
    reuse: float = 0.10  # penalização por reutilizar material (qualquer sobreposição, além da proporcional)
    overlap: float = 2.0  # penalização proporcional à fração do trecho já usada: só se repete quando não há outro material
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


def _role_energy(dna: EditDNA) -> tuple[np.ndarray, list[bool], list[bool]]:
    """Energia 'sentida' por plano (curva de vibe: movimento + eventos), e se o plano contém um IMPACTO ou é DESCANSO."""
    vb = dna.vibe
    n = len(dna.shots)
    if not vb or not vb.energy_curve:
        e = np.array([s.motion.energy_mean for s in dna.shots])
        return (np.argsort(np.argsort(e)) + 0.5) / n, [False] * n, [False] * n
    cur = np.array(vb.energy_curve)
    hz = vb.energy_hz
    e, imp, rest = [], [], []
    for s in dna.shots:
        t0, t1 = s.start_frame / dna.fps, s.end_frame / dna.fps
        sl = cur[max(0, int(t0 * hz)): max(int(t0 * hz) + 1, int(np.ceil(t1 * hz)))]
        e.append(float(sl.mean()) if len(sl) else 0.0)
        imp.append(any(t0 <= i.t <= t1 for i in vb.impacts))
        rest.append(any(r0 <= (t0 + t1) / 2 <= r1 for r0, r1 in vb.rests))
    e = np.array(e)
    return (np.argsort(np.argsort(e)) + 0.5) / n, imp, rest


def _cands(dna, clips, w, table, slots, ref_rank, imp, rest, K=600):
    """Para cada plano: os K melhores trechos (custo base sem reutilização), com direção/posição do sujeito."""
    out = []
    for k, (s, L, sc, need) in enumerate(slots):
        ref_shape = _minmax(np.array(s.speed.curve.v)) if s.speed.curve.v else np.full(8, 0.5)
        ref_dir = float(np.sign(s.motion.pan_x)) if abs(s.motion.pan_x) >= 0.05 else 0.0
        lst = []
        for ci, c in enumerate(clips):
            n_need = min(need, c.n)
            cx = c.subj[:, 0]
            for i in range(0, max(1, c.n - n_need + 1), w.stride):
                bad_frac = float(c.bad[i: i + n_need].mean())
                if bad_frac > 0.5:
                    continue  # janela maioritariamente instável (blur/flash/corte do próprio clip)
                e = c.energy[i: i + n_need]
                sj = c.subj[i: i + n_need]
                rank = float(np.searchsorted(table, e.mean()) / len(table))
                # impacto pede energia ALTA; descanso pede energia BAIXA; senão rank relativo
                target = 0.92 if imp[k] else (0.15 if rest[k] else ref_rank[k])
                a_cost = abs(rank - target)
                ws = _minmax(_resample(np.convolve(e, np.ones(3) / 3, mode="same"), 8))
                corr = float(np.corrcoef(ws, ref_shape)[0, 1]) if ws.std() > 1e-6 and ref_shape.std() > 1e-6 else 0.0
                shape_cost = (1 - corr) / 2
                b_cost = abs(float(c.lum[i: i + n_need].mean()) - s.appearance.mean_luma) / 255.0
                subj_cost = 1.0 - float(sj[:, 3].mean())
                vx = float((cx[min(i + n_need - 1, c.n - 1)] - cx[i]) / max(n_need, 1))
                dir_cost = 0.0
                if ref_dir != 0.0 and abs(vx) > 0.0015:
                    dir_cost = 0.0 if np.sign(vx) == ref_dir else 1.0  # movimento do sujeito no mesmo sentido do da referência
                cost = (w.action * a_cost + w.shape * shape_cost + w.brightness * b_cost + w.subject * subj_cost + w.direction * dir_cost
                        + w.unstable * bad_frac)  # prefere janelas limpas, mas aceita as outras se for preciso
                lst.append({"ci": ci, "i": i, "n": n_need, "cost": cost, "cx0": float(cx[i]), "cx1": float(cx[min(i + n_need - 1, c.n - 1)]),
                            "parts": {"action": a_cost, "shape": shape_cost, "brightness": b_cost, "subject": subj_cost, "direction": dir_cost},
                            "freeze": need > c.n})
        lst.sort(key=lambda d: d["cost"])
        out.append(lst[:K])
    return out


def match(dna: EditDNA, clips: list[ClipProfile], w: MatchWeights | None = None) -> list[Match]:
    """Atribuição por 2 fases: (1) gulosa com exclusão de sobreposição; (2) varrimento de refinamento com
    continuidade entre planos vizinhos (posição do sujeito no corte) e penalização de reutilização."""
    w = w or MatchWeights()
    shots = dna.shots
    ref_rank, imp, rest = _role_energy(dna)
    table = _energy_rank_table(clips)
    slots = []
    for s in shots:
        L = s.end_frame - s.start_frame
        sc = shot_speed_curve(s, w.speed_gain)
        need = int(np.ceil(source_frames_needed(L, sc))) + 3
        slots.append((s, L, sc, need))
    cands = _cands(dna, clips, w, table, slots, ref_rank, imp, rest)
    used: list[list[tuple[int, int]]] = [[] for _ in clips]

    def overlap(ci, a, b):
        """Penalização pela fração de [a,b) que já foi usada (margem de 6 frames conta como repetição)."""
        got = sum(max(0, min(b, ub + 6) - max(a, ua - 6)) for ua, ub in used[ci])
        return w.reuse + w.overlap * min(1.0, got / max(b - a, 1)) if got else 0.0

    chosen: dict[int, dict] = {}
    for k in sorted(range(len(shots)), key=lambda k: -slots[k][3]):
        best = None
        for c in cands[k]:
            pen = overlap(c["ci"], c["i"], c["i"] + c["n"])
            if best is None or c["cost"] + pen < best[0]:
                best = (c["cost"] + pen, c)
        if best is None:  # todos os trechos têm cortes próprios: relaxa
            ci = int(np.argmax([c.n for c in clips]))
            best = (1.0, {"ci": ci, "i": 0, "n": min(slots[k][3], clips[ci].n), "cost": 1.0, "cx0": 0.5, "cx1": 0.5, "parts": {"fallback": 1.0}, "freeze": True})
        chosen[k] = best[1]
        used[best[1]["ci"]].append((best[1]["i"], best[1]["i"] + best[1]["n"]))

    # fase 2: continuidade — em cortes SECOS (sem efeito na referência) a posição do sujeito deve ser contínua (match cut)
    for _ in range(2):
        for k in range(len(shots)):
            cur = chosen[k]
            used[cur["ci"]].remove((cur["i"], cur["i"] + cur["n"]))
            tr = shots[k].transition_in
            tr_next = shots[k + 1].transition_in if k + 1 < len(shots) else None
            best = None
            for c in cands[k]:
                cont = 0.0
                if k > 0 and tr is not None and tr.type == "cut":
                    cont += abs(chosen[k - 1]["cx1"] - c["cx0"])
                if tr_next is not None and tr_next.type == "cut":
                    cont += abs(c["cx1"] - chosen[k + 1]["cx0"])
                pen = overlap(c["ci"], c["i"], c["i"] + c["n"])
                tot = c["cost"] + w.continuity * cont + pen
                if best is None or tot < best[0]:
                    best = (tot, c)
            if best is None:  # plano sem candidatos (só o fallback da fase 1): mantém
                best = (0.0, cur)
            chosen[k] = best[1]
            used[best[1]["ci"]].append((best[1]["i"], best[1]["i"] + best[1]["n"]))

    res = []
    for k, sh in enumerate(shots):
        c = chosen[k]
        res.append(Match(sh.index, c["ci"], c["i"], c["n"], c["cost"], c["parts"], slots[k][2], freeze_tail=c["freeze"]))
    return res


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
