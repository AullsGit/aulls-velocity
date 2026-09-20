"""Linha de comandos: python -m velocity <analyze|detect|make> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from velocity.detector.velocity_detector import detect
from velocity.evaluate.effects_fidelity import compare_effects
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
    tl, info = build_timeline(dna, clips, ms, size=_size(a.size), music_path=a.music, weights=w, zoom_gain=a.zoom_gain, fx=not a.no_fx)
    if not a.music and a.audio_ref and dna.audio.has_audio:
        tl.music_path, tl.music_offset_s = a.ref, 0.0  # sem música: usa o áudio da referência (cortes já coincidem com ele)
        info = {"audio": "da referência"}
    log(f"matching feito; música: {info}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    render(tl, a.out, crf=a.crf, progress=lambda i, n: None)
    log(f"render concluído -> {a.out}")
    out_dna = analyze_reference(a.out, with_audio=bool(tl.music_path))
    fxrep = compare_effects(dna, out_dna)
    # ciclo fechado: ajusta fx_scale para aproximar as magnitudes medidas, no máximo a.fx_iters vezes
    for it in range(a.fx_iters):
        sug = fxrep["suggested_fx_scale"]
        if a.no_fx or not sug:
            break
        tl.fx_scale = round(tl.fx_scale * sug, 3)
        log(f"protetor de fidelidade: magnitudes fora de tolerância -> fx_scale={tl.fx_scale}; a re-renderizar")
        render(tl, a.out, crf=a.crf, progress=lambda i, n: None)
        out_dna = analyze_reference(a.out, with_audio=bool(tl.music_path))
        fxrep = compare_effects(dna, out_dna)
    log(f"efeitos: {fxrep['types']} | inventados={fxrep['invented']} | look={fxrep['look']} | ok={fxrep['ok']} {fxrep['reasons']}")
    rep = compare(dna, out_dna)
    rep["execution"] = compare_plan([sh.start_frame for sh in tl.shots[1:]], out_dna)
    log(f"execução (cortes planeados presentes no output): {rep['execution']}")
    log(f"fidelidade (ref vs output re-analisado): {rep['fidelity']}  componentes={rep['components']}")
    if a.report:
        json.dump({"detector": det.__dict__, "match": [m.__dict__ | {"speed": None} for m in ms], "retime": info, "eval": rep, "effects_eval": fxrep, "fx_scale": tl.fx_scale},
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
    p.add_argument("--audio-ref", action="store_true", help="sem --music, usa o áudio da referência"); p.add_argument("--zoom-gain", type=float, default=1.0); p.add_argument("--force", action="store_true")
    p.add_argument("--no-fx", action="store_true", help="desliga os efeitos medidos na referência"); p.add_argument("--fx-iters", type=int, default=1)
    p.add_argument("--report"); p.set_defaults(fn=cmd_make)
    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
