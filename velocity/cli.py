"""Linha de comandos: python -m velocity <analyze|detect|make> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from velocity.detector.velocity_detector import detect
from velocity.evaluate.metrics import compare, compare_plan
from velocity.matching.engine import MatchWeights, match
from velocity.matching.plan import build_timeline
from velocity.media.analyzer import analyze_clip
from velocity.reference.analyzer import analyze_reference
from velocity.render.compositor import render


def _size(s: str):
    w, h = s.lower().split("x")
    return int(w), int(h)


def cmd_analyze(a):
    dna = analyze_reference(a.ref)
    js = dna.model_dump_json(indent=1)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(js)
    else:
        print(js)


def cmd_detect(a):
    dna = analyze_reference(a.ref)
    d = detect(dna)
    print(json.dumps({"is_velocity": d.is_velocity, "score": d.score, "subscores": d.subscores, "reasons": d.reasons}, ensure_ascii=False, indent=1))
    return 0 if d.is_velocity else 2


def cmd_make(a):
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:5.1f}s] {m}", flush=True)
    dna = analyze_reference(a.ref)
    det = detect(dna)
    log(f"referência: {dna.signature.n_shots} planos, {dna.duration_s:.1f}s | Velocity={det.is_velocity} (score {det.score}) {det.reasons[:1]}")
    if not det.is_velocity and not a.force:
        log("Referência NÃO parece um Velocity suportado. Use --force para continuar mesmo assim.")
        return 2
    clips = [analyze_clip(p) for p in a.clips]
    log(f"material: {len(clips)} clips, {sum(c.duration_s for c in clips):.1f}s no total")
    w = MatchWeights()
    ms = match(dna, clips, w)
    tl, info = build_timeline(dna, clips, ms, size=_size(a.size), music_path=a.music, weights=w, zoom_gain=a.zoom_gain)
    log(f"matching feito; música: {info}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    render(tl, a.out, crf=a.crf, progress=lambda i, n: None)
    log(f"render concluído -> {a.out}")
    out_dna = analyze_reference(a.out, with_audio=bool(a.music))
    rep = compare(dna, out_dna)
    rep["execution"] = compare_plan([sh.start_frame for sh in tl.shots[1:]], out_dna)
    log(f"execução (cortes planeados presentes no output): {rep['execution']}")
    log(f"fidelidade (ref vs output re-analisado): {rep['fidelity']}  componentes={rep['components']}")
    if a.report:
        json.dump({"detector": det.__dict__, "match": [m.__dict__ | {"speed": None} for m in ms], "retime": info, "eval": rep},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="velocity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze"); p.add_argument("ref"); p.add_argument("--out"); p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("detect"); p.add_argument("ref"); p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("make")
    p.add_argument("--ref", required=True); p.add_argument("--clips", nargs="+", required=True)
    p.add_argument("--music"); p.add_argument("--out", required=True)
    p.add_argument("--size", default="540x960"); p.add_argument("--crf", type=int, default=18)
    p.add_argument("--zoom-gain", type=float, default=1.0); p.add_argument("--force", action="store_true")
    p.add_argument("--report"); p.set_defaults(fn=cmd_make)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
