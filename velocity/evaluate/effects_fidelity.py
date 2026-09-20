"""Protetor de fidelidade dos efeitos e do look (ref vs output re-analisado).

Compara SEMPRE no domínio da medição (mesmo estimador nos dois lados), por isso o viés do
estimador (ex.: zoom subestimado em zonas sem textura) cancela-se. Devolve um veredito e uma
sugestão de fx_scale para o ciclo fechado.
"""
from __future__ import annotations

import numpy as np

from velocity.evaluate.metrics import match_cuts
from velocity.schemas.edit_dna import EditDNA

GATED = ("zoom_punch", "shake", "rgb_split")
TOL_FRAMES = 8


def _ratio(a: float, b: float) -> float:
    return a / b if abs(b) > 1e-9 else float("nan")


def compare_effects(ref: EditDNA, out: EditDNA, tol: int = TOL_FRAMES) -> dict:
    res: dict = {"types": {}}
    invented = 0
    n_out_gated = 0
    for typ in GATED:
        r = [t for t in ref.effects if t.type == typ]
        o = [t for t in out.effects if t.type == typ]
        pairs = match_cuts([t.peak_frame for t in r], [t.peak_frame for t in o], tol)
        matched_o = {j for _, j in pairs}
        prec = len(pairs) / len(o) if o else (1.0 if not r else 0.0)
        rec = len(pairs) / len(r) if r else 1.0
        mags = [abs(o[j].magnitude) / abs(r[i].magnitude) for i, j in pairs if abs(r[i].magnitude) > 1e-9]
        res["types"][typ] = {"ref": len(r), "out": len(o), "matched": len(pairs), "precision": round(prec, 3),
                             "recall": round(rec, 3), "magnitude_ratio": round(float(np.median(mags)), 3) if mags else None}
        invented += len(o) - len(matched_o)
        n_out_gated += len(o)
    res["invented"] = invented
    res["invented_ratio"] = round(invented / n_out_gated, 3) if n_out_gated else 0.0

    look = {}
    if ref.look and out.look:
        a, b = ref.look, out.look
        look = {
            "aggressiveness": {"ref": a.aggressiveness, "out": b.aggressiveness, "delta": round(b.aggressiveness - a.aggressiveness, 3)},
            "motion_energy_ratio": round(_ratio(b.motion_energy_mean, a.motion_energy_mean), 3),
            "sharpness_ratio": round(_ratio(b.sharpness, a.sharpness), 3),
            "contrast_ratio": round(_ratio(b.contrast, a.contrast), 3),
            "saturation_ratio": round(_ratio(b.saturation, a.saturation), 3),
            "luma_delta": round(b.luma - a.luma, 1),
        }
    res["look"] = look

    reasons = []
    if res["invented_ratio"] > 0.35:
        reasons.append(f"{res['invented']} efeito(s) no output sem correspondente na referência (efeitos inventados)")
    if look:
        if abs(look["aggressiveness"]["delta"]) > 0.25:
            reasons.append(f"agressividade do output difere da referência ({look['aggressiveness']['delta']:+.2f})")
        for k in ("sharpness_ratio", "contrast_ratio", "saturation_ratio"):
            v = look[k]
            if v == v and not (0.6 <= v <= 1.6):
                reasons.append(f"{k} fora da tolerância ({v})")
    res["ok"] = not reasons
    res["reasons"] = reasons
    res["suggested_fx_scale"] = _suggest(res)
    return res


def _suggest(res: dict) -> float | None:
    """Fator multiplicativo para fx_scale que aproxima as magnitudes medidas (None = manter)."""
    ratios = [v["magnitude_ratio"] for k, v in res["types"].items() if v["magnitude_ratio"] and v["matched"] >= 2]
    if not ratios:
        return None
    r = float(np.median(ratios))
    if 0.75 <= r <= 1.33:
        return None
    return round(float(np.clip(1.0 / r, 0.5, 2.5)), 3)
