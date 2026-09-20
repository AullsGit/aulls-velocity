"""Quality Evaluator: compara o Edit DNA da referência com o do vídeo GERADO (re-analisado).

Independência: o output é analisado pelo mesmo Reference Analyzer, mas o analisador é validado
à parte (round-trip com verdade conhecida, tests/test_roundtrip.py), por isso o erro do gerador
não se confunde com o erro do medidor.
"""
from __future__ import annotations

import numpy as np

from velocity.schemas.edit_dna import EditDNA

TOL_FRAMES = 2


def match_cuts(a: list[int], b: list[int], tol: int = TOL_FRAMES) -> list[tuple[int, int]]:
    """Emparelhamento 1-1 guloso por proximidade (índices em a e b)."""
    pairs, used = [], set()
    cand = sorted(((abs(x - y), i, j) for i, x in enumerate(a) for j, y in enumerate(b) if abs(x - y) <= tol))
    ua: set[int] = set()
    for d, i, j in cand:
        if i in ua or j in used:
            continue
        ua.add(i)
        used.add(j)
        pairs.append((i, j))
    return sorted(pairs)


def events(dna: EditDNA) -> list[tuple[int, str]]:
    """Eventos de edição = transições com troca de conteúdo + efeitos dentro do plano (centro do intervalo).
    Contar ambos torna a métrica justa quando o material tem conteúdo parecido entre planos (o analisador
    não vê troca de conteúdo, mas vê o efeito)."""
    ev = [(t.cut_frame, t.type) for t in dna.transitions()]
    ev += [((f.start_frame + f.end_frame) // 2, f.type) for f in dna.fx_events if f.start_frame > 2 and f.end_frame < dna.n_frames - 2]
    return sorted(ev)


def compare_plan(planned_cuts: list[int], out: EditDNA, tol: int = 3) -> dict:
    """Verificação de EXECUÇÃO: os cortes planeados na timeline aparecem no vídeo re-analisado?"""
    oc = [f for f, _ in events(out)]
    pairs = match_cuts(planned_cuts, oc, tol)
    rec = len(pairs) / len(planned_cuts) if planned_cuts else 1.0
    return {"planned": len(planned_cuts), "found": len(pairs), "recall": round(rec, 3), "tol_frames": tol}


def compare(ref: EditDNA, out: EditDNA, tol: int = TOL_FRAMES) -> dict:
    rt, ot = ref.transitions(), out.transitions()
    rev, oev = events(ref), events(out)
    rc, oc = [f for f, _ in rev], [f for f, _ in oev]
    tol = max(tol, 3)
    pairs = match_cuts(rc, oc, tol)
    prec = len(pairs) / len(oc) if oc else 0.0
    rec = len(pairs) / len(rc) if rc else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0

    def fam(t):  # com efeito vs corte seco
        return "cut" if t == "cut" else "fx"

    type_acc = float(np.mean([rev[i][1] == oev[j][1] for i, j in pairs])) if pairs else 0.0
    fam_acc = float(np.mean([fam(rev[i][1]) == fam(oev[j][1]) for i, j in pairs])) if pairs else 0.0
    dur_err = abs(ref.n_frames - out.n_frames) / max(ref.n_frames, 1)

    zoom_agree = None
    if len(ref.shots) == len(out.shots):
        zs = [1.0 - min(1.0, abs(r.motion.zoom_total - o.motion.zoom_total) / 0.3)
              for r, o in zip(ref.shots, out.shots) if r.motion.confidence > 0.3 and o.motion.confidence > 0.3]
        zoom_agree = float(np.mean(zs)) if zs else None

    beat = None
    ra, oa = ref.audio, out.audio
    if ra.cut_beat_alignment is not None and oa.cut_beat_alignment is not None:
        beat = 1.0 - min(1.0, abs(ra.cut_beat_alignment - oa.cut_beat_alignment))

    counts_ref, counts_out = ref.signature.transition_counts, out.signature.transition_counts
    kinds = set(counts_ref) | set(counts_out)
    tot_r, tot_o = max(sum(counts_ref.values()), 1), max(sum(counts_out.values()), 1)
    mix = 1.0 - 0.5 * sum(abs(counts_ref.get(k, 0) / tot_r - counts_out.get(k, 0) / tot_o) for k in kinds)

    comps = {"cut_f1": f1, "type_acc": type_acc, "duration": 1.0 - min(1.0, dur_err * 5), "transition_mix": mix}
    weights = {"cut_f1": 0.35, "type_acc": 0.20, "duration": 0.10, "transition_mix": 0.15}
    if zoom_agree is not None:
        comps["zoom"], weights["zoom"] = zoom_agree, 0.10
    if beat is not None:
        comps["beat"], weights["beat"] = beat, 0.10
    fidelity = sum(comps[k] * weights[k] for k in comps) / sum(weights[k] for k in comps)
    return {
        "fidelity": round(float(fidelity), 4),
        "components": {k: round(float(v), 4) for k, v in comps.items()},
        "cuts": {"ref": len(rc), "out": len(oc), "matched": len(pairs), "precision": round(prec, 3), "recall": round(rec, 3), "tol_frames": tol},
        "frame_delta": {"ref": ref.n_frames, "out": out.n_frames},
        "type_family_acc": round(fam_acc, 3),
        "transition_counts": {"ref": counts_ref, "out": counts_out},
    }
