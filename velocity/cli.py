"""Linha de comandos: python -m velocity <analyze|detect|make> ..."""
from __future__ import annotations

import argparse
import json
import sys

from velocity.detector.velocity_detector import detect
from velocity.pipeline import PROFILES, MakeParams, run_make
from velocity.reference.analyzer import analyze_reference


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
    p = MakeParams(
        ref=a.ref, clips=a.clips, out=a.out, music=a.music, audio_ref=a.audio_ref, profile=a.profile,
        size=_size(a.size) if a.size else None, crf=a.crf, preset=a.preset, fx_iters=a.fx_iters, sr=a.sr,
        zoom_gain=a.zoom_gain, tone=a.tone, denoise=a.denoise, subject_enhance=a.subject_enhance,
        no_fx=a.no_fx, force=a.force, text=a.text, target_min=a.target_min, target_max=a.target_max,
        no_extend=a.no_extend, report=a.report,
    )
    res = run_make(p, log=lambda m: print(m, flush=True))
    if res.rejected:
        return 2
    return 0 if res.ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="velocity")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze"); p.add_argument("ref"); p.add_argument("--out"); p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("detect"); p.add_argument("ref"); p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("make")
    p.add_argument("--ref", required=True); p.add_argument("--clips", nargs="+", required=True)
    p.add_argument("--music"); p.add_argument("--out", required=True)
    p.add_argument("--profile", choices=list(PROFILES), default="standard", help="atalho de qualidade/velocidade: fast (pré-visualização) | standard (padrão) | hd (entrega final)")
    p.add_argument("--size", default=None); p.add_argument("--crf", type=int, default=None)
    p.add_argument("--preset", default=None, help="preset de compressão do libx264 (ex.: veryfast, fast, medium)")
    p.add_argument("--audio-ref", action="store_true", default=True, help="sem --music, usa o áudio da referência (ligado por defeito)")
    p.add_argument("--sem-audio-ref", dest="audio_ref", action="store_false", help="não usar o áudio da referência quando não há --music (vídeo fica mudo)")
    p.add_argument("--zoom-gain", type=float, default=1.0); p.add_argument("--force", action="store_true")
    p.add_argument("--no-fx", action="store_true", help="desliga os efeitos medidos na referência")
    p.add_argument("--fx-iters", type=int, default=None)
    p.add_argument("--sr", action="store_true", help="EXPERIMENTAL: super-resolução nos planos muito ampliados (lenta; melhor com a GPU sem limite de energia)")
    p.add_argument("--denoise", type=float, default=0.5, help="redução de ruído temporal 0-1")
    p.add_argument("--subject-enhance", action="store_true", help="EXPERIMENTAL: realce local do sujeito por máscara")
    p.add_argument("--tone", type=float, default=0.85, help="força do ajuste de tom (0-1) ao look da referência")
    p.add_argument("--text", nargs="*", help="texto(s) a sobrepor; só é usado se a REFERÊNCIA tiver texto sobreposto (timing/posição/entrada copiados)")
    p.add_argument("--target-min", type=float, default=25.0, help="duração mínima do produto final, em segundos")
    p.add_argument("--target-max", type=float, default=31.0, help="duração máxima do produto final, em segundos")
    p.add_argument("--no-extend", action="store_true", help="desliga o ajuste automático de duração (usa a duração exata da referência)")
    p.add_argument("--report"); p.set_defaults(fn=cmd_make)
    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except Exception as e:
        print(f"\n[ERRO] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if a.cmd == "make" and getattr(a, "report", None):
            try:
                json.dump({"error": str(e), "error_type": type(e).__name__}, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
