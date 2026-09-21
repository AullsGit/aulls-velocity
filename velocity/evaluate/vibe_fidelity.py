"""Fidelidade de VIBE: o output 'sente-se' como a referência? Compara curvas de energia, impactos, arco e densidades.

Serve o ciclo de autocorreção: se o output EXAGEROU (mais efeitos/agressividade do que a referência) reduz-se;
se ficou aquém, só se regista (a regra da evidência impede de inventar efeitos).
"""
from __future__ import annotations

import numpy as np

from velocity.schemas.edit_dna import EditDNA


def _resample(x, k):
    x = np.asarray(x, np.float32)
    if len(x) < 2:
        return np.full(k, float(x[0]) if len(x) else 0.0)
    return np.interp(np.linspace(0, 1, k), np.linspace(0, 1, len(x)), x)


def compare_vibe(ref: EditDNA, out: EditDNA) -> dict:
    a, b = ref.vibe, out.vibe
    if not a or not b:
        return {"available": False}
    k = 32
    ca, cb = _resample(a.energy_curve, k), _resample(b.energy_curve, k)
    corr = float(np.corrcoef(ca, cb)[0, 1]) if ca.std() > 1e-6 and cb.std() > 1e-6 else 0.0

    tol = 0.30
    ia, ib = [i.t for i in a.impacts], [i.t for i in b.impacts]
    matched = 0
    used: set[int] = set()
    for t in ia:
        j = min((j for j in range(len(ib)) if j not in used), key=lambda j: abs(ib[j] - t), default=None)
        if j is not None and abs(ib[j] - t) <= tol:
            matched += 1
            used.add(j)
    prec = matched / len(ib) if ib else (1.0 if not ia else 0.0)
    rec = matched / len(ia) if ia else 1.0
    imp_f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0

    def ratio(x, y):
        return round(y / x, 3) if x > 1e-9 else None

    d_aggr = round(b.aggressiveness - a.aggressiveness, 3)
    d_smooth = round(b.smoothness - a.smoothness, 3)
    eff_ratio = ratio(a.effect_density, b.effect_density)
    tr_ratio = ratio(a.transition_density, b.transition_density)
    parts = {
        "energy_curve": max(0.0, corr),
        "impacts": imp_f1,
        "aggressiveness": 1.0 - min(1.0, abs(d_aggr) / 0.5),
        "smoothness": 1.0 - min(1.0, abs(d_smooth) / 0.5),
        "arc": 1.0 if a.arc == b.arc else 0.4,
        "effect_density": 1.0 - min(1.0, abs(np.log(eff_ratio)) / np.log(3)) if eff_ratio else 0.5,
    }
    w = {"energy_curve": 0.25, "impacts": 0.15, "aggressiveness": 0.2, "smoothness": 0.1, "arc": 0.1, "effect_density": 0.2}
    score = sum(parts[k_] * w[k_] for k_ in parts)
    over = (eff_ratio is not None and eff_ratio > 1.35) or d_aggr > 0.2
    under = (eff_ratio is not None and eff_ratio < 0.6) or d_aggr < -0.25
    return {
        "available": True, "score": round(float(score), 3), "parts": {k_: round(float(v), 3) for k_, v in parts.items()},
        "energy_corr": round(corr, 3), "impacts": {"ref": len(ia), "out": len(ib), "matched": matched, "f1": round(imp_f1, 3)},
        "aggressiveness": {"ref": a.aggressiveness, "out": b.aggressiveness, "delta": d_aggr},
        "smoothness_delta": d_smooth, "effect_density_ratio": eff_ratio, "transition_density_ratio": tr_ratio,
        "arc": {"ref": a.arc, "out": b.arc}, "over_effected": bool(over), "under_effected": bool(under),
    }
