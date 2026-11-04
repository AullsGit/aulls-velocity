"""Prova técnica T1/T2 (precisão ao frame e time remapping) para QUALQUER motor atrás de
velocity/engine: constrói planos com cortes, velocidades e mapas de tempo conhecidos sobre fontes
com o número do frame desenhado (frame_probe), renderiza e lê de volta que frame da fonte aparece
em cada frame de saída.

    python -m velocity.evaluate.engine_t1 [--engine compositor] [--out pasta]

O mesmo script corre sobre o MLT/GES quando houver adaptador (Fase 1), com os mesmos critérios.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile

from velocity.engine import get_engine
from velocity.evaluate import frame_probe as fp
from velocity.plan.schema import Output, Plan, Source, TimeMap, Track, Transform, VideoItem
from velocity.schemas.edit_dna import Curve

SIZE = (320, 568)


def _item(i, src, at, dur, start_frame, src_fps, speed=1.0, frames=None):
    tm = TimeMap(src_start_s=start_frame / src_fps, speed=Curve(t=[0, 1], v=[speed, speed]),
                 src_fps=src_fps if frames else None, src_frames=frames)
    tr = Transform(warps=[(1, 0, 0, 0, 1, 0)] * dur) if frames else Transform()
    return VideoItem(id=f"v{i}", source=src, at=at, duration=dur, time=tm, transform=tr)


def _plan(fps_out, items, sources):
    total = sum(it.duration for it in items)
    return Plan(output=Output(width=SIZE[0], height=SIZE[1], fps=fps_out, duration_frames=total),
                sources={k: Source(uri=v) for k, v in sources.items()}, tracks=[Track(id="V1", kind="video", items=items)])


def cases(d: str) -> list[tuple[str, Plan, list[tuple[int, int]], str]]:
    """(nome, plano, frames esperados [(fonte, frame)], regra usada para o esperado)."""
    a = fp.write_source(os.path.join(d, "a30.mp4"), 1, 240, 30, SIZE)
    b = fp.write_source(os.path.join(d, "b30.mp4"), 2, 240, 30, SIZE)
    c = fp.write_source(os.path.join(d, "c24.mp4"), 3, 192, 24, SIZE)
    v = fp.write_source(os.path.join(d, "v30.mp4"), 4, 240, 30, SIZE, drop=4)  # VFR (falta 1 em cada 4)
    out = []
    # 1) cortes e trims à mesma taxa
    its = [_item(0, "a", 0, 20, 10, 30), _item(1, "b", 20, 15, 40, 30), _item(2, "a", 35, 25, 70, 30)]
    exp = [(1, 10 + k) for k in range(20)] + [(2, 40 + k) for k in range(15)] + [(1, 70 + k) for k in range(25)]
    out.append(("cortes_30fps", _plan(30, its, {"a": a, "b": b}), exp, "frame_inicial + k"))
    # 2) velocidade 2x linear
    its = [_item(0, "a", 0, 30, 20, 30, speed=2.0)]
    out.append(("velocidade_2x", _plan(30, its, {"a": a}), [(1, 20 + 2 * k) for k in range(30)], "frame_inicial + 2k"))
    # 3) tempo por frame (rampa, congelamento, reverse) — o caminho da reconstrução do cenário A
    frames = [50, 51, 52, 54, 57, 61, 61, 61, 61, 60, 58, 56, 54, 55, 56, 57, 90, 91, 92, 93]
    its = [_item(0, "b", 0, len(frames), frames[0], 30, frames=frames)]
    out.append(("tempo_por_frame", _plan(30, its, {"b": b}), [(2, f) for f in frames], "src_frames do plano"))
    # 4) fonte a 24 fps numa saída a 30 fps (e o inverso)
    # regra "hold": o frame de saída k mostra o frame da fonte que está no ecrã no instante k/fps_saída
    its = [_item(0, "c", 0, 30, 12, 24)]
    exp = [(3, 12 + math.floor(k * 24 / 30 + 1e-9)) for k in range(30)]
    out.append(("fonte24_saida30", _plan(30, its, {"c": c}), exp, "hold: frame no ecrã no instante k/30"))
    its = [_item(0, "a", 0, 24, 30, 30)]
    exp = [(1, 30 + math.floor(k * 30 / 24 + 1e-9)) for k in range(24)]
    out.append(("fonte30_saida24", _plan(24, its, {"a": a}), exp, "hold: frame no ecrã no instante k/24"))
    # 5) fonte VFR (telemóvel): o índice desenhado é o da grelha de 30 fps original
    its = [_item(0, "v", 0, 30, 40, 30)]
    present = [i for i in range(240) if i % 4 != 3]  # a fonte VFR não tem 1 frame em cada 4
    exp = [(4, max(i for i in present if i <= 40 + k)) for k in range(30)]
    out.append(("fonte_vfr", _plan(30, its, {"v": v}), exp, "hold: um frame em falta mostra o anterior"))
    return out


def run(engine_name: str | None = None, d: str | None = None) -> dict:
    eng = get_engine(engine_name)
    d = d or tempfile.mkdtemp(prefix="t1_")
    os.makedirs(d, exist_ok=True)
    report = {"engine": eng.name, "cases": {}}
    for name, plan, expected, rule in cases(d):
        outp = os.path.join(d, f"out_{name}.mp4")
        eng.render(plan, outp, crf=12, preset="veryfast")
        got = fp.read_video(outp)
        n = min(len(got), len(expected))
        errs = [(got[k][1] - expected[k][1]) if got[k][0] == expected[k][0] else None for k in range(n)]
        wrong_src = sum(e is None for e in errs)
        diffs = [e for e in errs if e is not None]
        report["cases"][name] = {
            "frames_out": len(got), "frames_expected": len(expected), "rule": rule,
            "exact": round(sum(e == 0 for e in diffs) / max(len(expected), 1), 4),
            "max_abs_err_frames": max((abs(e) for e in diffs), default=None), "wrong_source": wrong_src,
            "errors": [(k, expected[k][1], got[k][1]) for k in range(n) if errs[k] not in (0,)][:12],
        }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    rep = run(a.engine, a.out)
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
