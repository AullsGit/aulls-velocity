"""T4: um motor externo consegue executar o rasto MEDIDO do cenário A (frame da fonte, geometria
e cor por frame)? Usa um plano real produzido pelo pipeline sobre um caso do banco de fidelidade
e compara, nos frames fora de transições (que cada motor desenha à sua maneira), o vídeo de cada
motor com a referência verdadeira.

    python -m velocity.evaluate.engine_t4 [--case r1]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time

import numpy as np

from velocity.evaluate import fidelity_bench as fb
from velocity.evaluate.engine_poc import DOCKER, IMAGE, OUT, ROOT


def _frames_gray(path: str, size: tuple[int, int], fps: float) -> list[np.ndarray]:
    w, h = size
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", f"fps={fps},scale={w}:{h}:flags=area,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    b = r.stdout
    return [np.frombuffer(b[i * w * h:(i + 1) * w * h], np.uint8).reshape(h, w) for i in range(len(b) // (w * h))]


def run(case: str = "r1") -> dict:
    from velocity.engine import get_engine
    from velocity.pipeline import MakeParams, run_make
    work = os.path.join(OUT, "t4", case)
    os.makedirs(work, exist_ok=True)
    paths = fb.build_case(case)
    truth = paths["truth"]
    res = run_make(MakeParams(ref=paths["ref"], clips=paths["A"], out=os.path.join(work, "pipeline.mp4"), profile="fast",
                              force=True), log=lambda m: None)
    plan = res.plan.model_copy(deep=True)
    # plano "limpo": só o que é medido por frame (tempo, geometria, cor); sem transições nem efeitos
    for it in plan.video_items():
        it.transition_in = None
    plan.effects, plan.fx_events = [], []
    plan.tracks = [t for t in plan.tracks if t.kind == "video"]
    W, H, fps = plan.output.width, plan.output.height, plan.output.fps
    # fontes copiadas para a pasta montada no contentor
    local = {}
    for sid, s in plan.sources.items():
        dst = os.path.join(work, f"{sid}.mp4")
        if not os.path.exists(dst):
            shutil.copyfile(s.uri, dst)
        local[sid] = dst
    report = {"case": case, "output": [W, H, fps]}

    # compositor (referência de controlo)
    p_comp = plan.map_uris(lambda u: next(local[k] for k, v in plan.sources.items() if v.uri == u))
    t0 = time.time()
    get_engine("compositor").render(p_comp, os.path.join(work, "out_compositor.mp4"), crf=12, preset="veryfast")
    report["compositor_s"] = round(time.time() - t0, 2)

    # MLT: timeremap por frame + affine com retângulo por frame + matriz de cor (avfilter)
    items, rot = [], 0.0
    for it in plan.video_items():
        rects = []
        for a, b, c, d, e, f in it.transform.warps:
            rot = max(rot, abs(b), abs(d))
            rects.append((-c / a * W, -f / e * H, W / a, H / e))
        items.append({"src": it.source, "at": it.at, "dur": it.duration, "start": it.time.src_frames[0], "speed": 1.0,
                      "time_frames": it.time.src_frames, "zoom": None, "dissolve_in": 0, "rects": rects,
                      "color": it.color.matrix})
    spec = {"fps": [int(round(fps)), 1], "width": W, "height": H, "frames": plan.output.duration_frames,
            "sources": {sid: {"path": f"/work/{sid}.mp4", "fps": it.time.src_fps} for sid in plan.sources
                        for it in plan.video_items() if it.source == sid},
            "items": items}
    json.dump(spec, open(os.path.join(work, "spec_t4.json"), "w"))
    t0 = time.time()
    r = subprocess.run([DOCKER, "run", "--rm", "-v", f"{work}:/work", "-v", f"{os.path.join(ROOT, 'bench', 'engines')}:/code",
                        IMAGE, "python3", "/code/poc_runner.py", "mlt", "/work/spec_t4.json", "/work/out_mlt.mp4"],
                       capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    report["mlt_run"] = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1]) if r.stdout else {"error": r.stderr[-300:]}
    report["mlt_wall_s"] = round(time.time() - t0, 2)
    report["max_rotation_term"] = round(rot, 5)  # o retângulo do MLT não exprime rotação: quanto se perde

    # comparação: frames fora das transições da verdade, à resolução de saída/2
    trans = set()
    for c, t in zip(truth["cuts"], truth["transitions"]):
        if t["type"] != "cut":
            a0 = c - t["frames"] // 2
            trans.update(range(a0 - 1, a0 + t["frames"] + 1))
    size = (W // 2 // 2 * 2, H // 2 // 2 * 2)
    ref = _frames_gray(paths["ref"], size, truth["fps"])
    for name in ("compositor", "mlt"):
        p = os.path.join(work, f"out_{name}.mp4")
        if not os.path.exists(p):
            report[name] = {"status": "sem saída"}
            continue
        out = _frames_gray(p, size, truth["fps"])
        idx = [k for k in range(min(len(ref), len(out))) if k not in trans]
        ss = [fb.ssim(ref[k], out[k]) for k in idx]
        report[name] = {"frames_compared": len(idx), "ssim_mean": round(float(np.mean(ss)), 4),
                        "ssim_p10": round(float(np.percentile(ss, 10)), 4)}
    a, b = (_frames_gray(os.path.join(work, f"out_{n}.mp4"), size, truth["fps"]) for n in ("compositor", "mlt"))
    report["mlt_vs_compositor_ssim"] = round(float(np.mean([fb.ssim(x, y) for x, y in zip(a, b)])), 4)
    json.dump(report, open(os.path.join(work, "report.json"), "w"), indent=1)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="r1")
    print(json.dumps(run(ap.parse_args().case), indent=1, ensure_ascii=False))
