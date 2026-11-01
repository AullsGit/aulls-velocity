"""Fase 1 — prova técnica de motores (T1-T7 da auditoria), medida e não declarada.

Os mesmos testes correm no compositor próprio (via velocity/engine) e, no contentor Docker
`aulls-engines` (bench/engines), no MLT, GES e libopenshot. As fontes têm o número do frame
desenhado (frame_probe), por isso cada frame de saída diz que frame da fonte mostra; as imagens
esperadas são calculadas a partir das fontes PERFEITAS (sem compressão).

    python -m velocity.evaluate.engine_poc [--engines compositor,mlt,ges,openshot] [--tag poc]

Escreve bench/out/engines/<tag>/report.json e report.md.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import time

import cv2
import numpy as np

from velocity.evaluate import frame_probe as fp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "bench", "out", "engines")
DOCKER = os.environ.get("DOCKER", r"C:\Users\Aulls\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe")
IMAGE = "aulls-engines"
SIZE = (320, 568)
SRC = {"a": (1, 30.0, 240, 0), "b": (2, 30.0, 240, 0), "c": (3, 24.0, 192, 0), "v": (4, 30.0, 240, 4)}  # id, fps, n, drop


# --------------------------------------------------------------------------- casos
def _it(src, at, dur, start, speed=1.0, frames=None, zoom=None, dissolve=0):
    return {"src": src, "at": at, "dur": dur, "start": start, "speed": speed, "time_frames": frames, "zoom": zoom,
            "dissolve_in": dissolve}


def specs(size=SIZE) -> dict[str, dict]:
    W, H = size
    srcs = {k: {"path": f"/work/{k}.mp4", "fps": v[1]} for k, v in SRC.items()}

    def spec(fps, items):
        end = max(i["at"] + i["dur"] for i in items)
        return {"fps": [fps, 1], "width": W, "height": H, "frames": end, "sources": srcs, "items": items}

    ramp = [50, 51, 52, 54, 57, 61, 61, 61, 61, 60, 58, 56, 54, 55, 56, 57, 90, 91, 92, 93]
    return {
        "T1_cortes": spec(30, [_it("a", 0, 20, 10), _it("b", 20, 15, 40), _it("a", 35, 25, 70)]),
        "T1_fonte24_saida30": spec(30, [_it("c", 0, 30, 12)]),
        "T1_fonte30_saida24": spec(24, [_it("a", 0, 24, 30)]),
        "T1_fonte_vfr": spec(30, [_it("v", 0, 30, 40)]),
        "T2_velocidade_2x": spec(30, [_it("a", 0, 30, 20, speed=2.0)]),
        "T2_velocidade_0.5x": spec(30, [_it("a", 0, 30, 20, speed=0.5)]),
        "T2_mapa_por_frame": spec(30, [_it("b", 0, len(ramp), ramp[0], frames=ramp)]),
        "T3_zoom_keyframes": spec(30, [_it("a", 0, 30, 30, zoom={"z0": 1.0, "z1": 1.5})]),
        "T5_dissolve": spec(30, [_it("a", 0, 40, 10), _it("b", 24, 40, 60, dissolve=16)]),
    }


def expected(spec) -> list[list[tuple[str, int, float]]]:
    """Por frame de saída: [(fonte, frame, peso)] (2 entradas nas dissoluções)."""
    fps = spec["fps"][0] / spec["fps"][1]
    out: list[list] = [[] for _ in range(spec["frames"])]
    for it in spec["items"]:
        src = spec["sources"][it["src"]]
        drop = SRC[it["src"]][3]
        for k in range(it["dur"]):
            if it["time_frames"]:
                f = it["time_frames"][k]
            else:
                f = it["start"] + math.floor(k * it["speed"] * src["fps"] / fps + 1e-9)
            if drop:  # fonte VFR: o frame em falta mostra o anterior
                while f % drop == drop - 1:
                    f -= 1
            out[it["at"] + k].append((it["src"], f, k))
    return out


def _zoom_at(it, k):
    z = it["zoom"]
    return z["z0"] + (z["z1"] - z["z0"]) * k / max(it["dur"] - 1, 1)


def expected_image(spec, it, src_key, f, k) -> np.ndarray:
    W, H = spec["width"], spec["height"]
    img = fp.draw(SRC[src_key][0], f, W, H, seed=SRC[src_key][0] * 7919)
    if it and it.get("zoom"):
        z = _zoom_at(it, k)
        M = np.array([[z, 0, -(z - 1) * W / 2], [0, z, -(z - 1) * H / 2]], np.float32)
        img = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR)
    return img


# ----------------------------------------------------------------------- execução
def _sources(work: str):
    for k, (sid, fps, n, drop) in SRC.items():
        p = os.path.join(work, f"{k}.mp4")
        if not os.path.exists(p):
            fp.write_source(p, sid, n, fps, SIZE, drop=drop)


def _plan_for(spec, work):
    """A mesma especificação como Plano v1, para o compositor (via velocity/engine)."""
    from velocity.plan.schema import Output, Plan, Source, TimeMap, Track, Transform, VideoItem
    from velocity.schemas.edit_dna import Curve
    W, H = spec["width"], spec["height"]
    fps = spec["fps"][0] / spec["fps"][1]
    items = []
    for i, it in enumerate(spec["items"]):
        src = spec["sources"][it["src"]]
        frames = it["time_frames"]
        warps = None
        if it.get("zoom"):  # zoom por frame, como afim normalizada saída->fonte
            warps = []
            for k in range(it["dur"]):
                z = _zoom_at(it, k)
                warps.append((1 / z, 0.0, 0.5 - 0.5 / z, 0.0, 1 / z, 0.5 - 0.5 / z))
            frames = frames or [it["start"] + math.floor(k * it["speed"] * src["fps"] / fps + 1e-9) for k in range(it["dur"])]
        items.append(VideoItem(
            id=f"v{i}", source=it["src"], at=it["at"], duration=it["dur"],
            time=TimeMap(src_start_s=it["start"] / src["fps"], speed=Curve(t=[0, 1], v=[it["speed"]] * 2), slowmo="hold",
                         src_fps=src["fps"] if frames else None, src_frames=frames),
            transform=Transform(warps=warps or ([(1, 0, 0, 0, 1, 0)] * it["dur"] if frames else None))))
    return Plan(output=Output(width=W, height=H, fps=fps, duration_frames=spec["frames"]),
                sources={k: Source(uri=os.path.join(work, f"{k}.mp4")) for k in spec["sources"]},
                tracks=[Track(id="V1", kind="video", items=items)])


def run_engine(engine: str, name: str, spec: dict, work: str) -> dict:
    out = os.path.join(work, f"out_{engine}_{name}.mp4")
    if os.path.exists(out):
        os.remove(out)
    t0 = time.time()
    if engine == "compositor":
        from velocity.engine import PlanRejected, get_engine
        try:
            get_engine("compositor").render(_plan_for(spec, work), out, crf=12, preset="veryfast")
            res = {"ok": True, "unsupported": [], "engine_version": "compositor (velocity/render)"}
        except PlanRejected as e:
            res = {"ok": False, "unsupported": [i.message for i in e.issues], "engine_version": "compositor"}
        res["seconds"] = round(time.time() - t0, 2)
    else:
        runner = engine
        if engine == "mlt_strict":
            # adaptador "estrito": TODOS os planos via timeremap, com o frame de cada instante
            # calculado pela nossa regra (hold) — o MLT só executa, não decide o tempo
            spec = json.loads(json.dumps(spec))
            fps = spec["fps"][0] / spec["fps"][1]
            for it in spec["items"]:
                if not it["time_frames"]:
                    sf = spec["sources"][it["src"]]["fps"]
                    it["time_frames"] = [it["start"] + math.floor(k * it["speed"] * sf / fps + 1e-9) for k in range(it["dur"])]
            runner = "mlt"
        json.dump(spec, open(os.path.join(work, f"spec_{engine}_{name}.json"), "w"))
        cmd = [DOCKER, "run", "--rm", "-v", f"{work}:/work", "-v", f"{os.path.join(ROOT, 'bench', 'engines')}:/code", IMAGE,
               "python3", "/code/poc_runner.py", runner, f"/work/spec_{engine}_{name}.json", f"/work/out_{engine}_{name}.mp4"]
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        last = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        res = json.loads(last[-1]) if last else {"ok": False, "error": (r.stderr or r.stdout)[-400:]}
        res["wall_seconds"] = round(time.time() - t0, 2)
    res["out"] = out if os.path.exists(out) else None
    return res


def _read_rgb(path):
    from velocity.io.probe import probe
    info = probe(path)
    w, h = info.width, info.height
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-fps_mode", "passthrough", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = []
    while True:
        b = p.stdout.read(w * h * 3)
        if len(b) < w * h * 3:
            break
        frames.append(np.frombuffer(b, np.uint8).reshape(h, w, 3))
    p.wait()
    return frames


def _psnr(a, b):
    mse = float(np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2))
    return 99.0 if mse < 1e-6 else 10 * math.log10(255 ** 2 / mse)


def measure(name: str, spec: dict, res: dict) -> dict:
    if not res.get("ok") or not res.get("out"):
        return {"status": "não executou", **{k: res.get(k) for k in ("unsupported", "error", "stderr")}}
    exp = expected(spec)
    frames = _read_rgb(res["out"])
    m = {"frames_out": len(frames), "frames_expected": len(exp)}
    n = min(len(frames), len(exp))
    if name.startswith(("T1", "T2")):
        got = [fp.read(f) for f in frames[:n]]
        ok = [g == (SRC[e[0][0]][0], e[0][1]) for g, e in zip(got, exp)]
        m["exact"] = round(sum(ok) / max(len(exp), 1), 4)
        m["errors"] = [(k, exp[k][0][1], got[k][1]) for k in range(n) if not ok[k]][:8]
    elif name.startswith("T3"):
        it = spec["items"][0]
        ps = [_psnr(frames[k], expected_image(spec, it, *exp[k][0][:2], k)) for k in range(n)]
        m["psnr_mean_db"] = round(float(np.mean(ps)), 2)
        m["psnr_min_db"] = round(float(np.min(ps)), 2)
    elif name.startswith("T5"):
        alphas, resid = [], []
        for k in range(n):
            if len(exp[k]) == 2:
                (sa, fa, _), (sb, fb, _) = exp[k]
                A = expected_image(spec, None, sa, fa, 0).astype(np.float32)
                B = expected_image(spec, None, sb, fb, 0).astype(np.float32)
                O = frames[k].astype(np.float32)
                d = (B - A).reshape(-1)
                a = float(np.clip(np.dot((O - A).reshape(-1), d) / max(np.dot(d, d), 1e-6), 0, 1))
                alphas.append(round(a, 3))
                resid.append(_psnr(O, (1 - a) * A + a * B))
        m["alpha_curve"] = alphas
        m["blend_psnr_db"] = round(float(np.mean(resid)), 2) if resid else None
        m["monotonic"] = all(b >= a - 0.02 for a, b in zip(alphas, alphas[1:])) if alphas else None
        outside = [k for k in range(n) if len(exp[k]) == 1]
        got = {k: fp.read(frames[k]) for k in outside}
        m["exact_outside"] = round(sum(got[k] == (SRC[exp[k][0][0]][0], exp[k][0][1]) for k in outside) / max(len(outside), 1), 4)
    return m


def perf_spec(size=(1080, 1920), seconds=30, fps=30):
    """T6: 30 s a 1080x1920, 10 cortes e zoom por keyframes num terço dos planos."""
    n, shot = seconds * fps, seconds * fps // 10
    items = [_it("ab"[i % 2], i * shot, shot, 20 + 3 * i, zoom={"z0": 1.0, "z1": 1.3} if i % 3 == 0 else None) for i in range(10)]
    srcs = {k: {"path": f"/work/{k}_big.mp4", "fps": 30.0} for k in "ab"}
    return {"fps": [fps, 1], "width": size[0], "height": size[1], "frames": n, "sources": srcs, "items": items}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="compositor,mlt,ges,openshot")
    ap.add_argument("--tag", default="poc")
    ap.add_argument("--perf", action="store_true", help="inclui T6 (30 s a 1080x1920)")
    a = ap.parse_args(argv)
    work = os.path.join(OUT, a.tag, "work")
    os.makedirs(work, exist_ok=True)
    _sources(work)
    report = {}
    for name, spec in specs().items():
        for eng in a.engines.split(","):
            res = run_engine(eng, name, spec, work)
            try:
                met = measure(name, spec, res)
            except Exception as e:  # noqa: BLE001 — um motor que gera um ficheiro ilegível fica registado, não para o resto
                met = {"status": "saída ilegível", "error": f"{type(e).__name__}: {e}"[:300]}
            report.setdefault(name, {})[eng] = {"run": {k: v for k, v in res.items() if k != "out"}, "metrics": met}
            print(name, eng, json.dumps(report[name][eng]["metrics"], ensure_ascii=False)[:220], flush=True)
    if a.perf:
        for k, sid in (("a", 1), ("b", 2)):
            p = os.path.join(work, f"{k}_big.mp4")
            if not os.path.exists(p):
                fp.write_source(p, sid, 450, 30, (1080, 1920))
        ps = perf_spec()
        for eng in a.engines.split(","):
            if eng == "compositor":
                ps_local = json.loads(json.dumps(ps))
                ps_local["sources"] = {k: {"path": f"/work/{k}_big.mp4", "fps": 30.0} for k in "ab"}
                from velocity.engine import get_engine
                pl = _plan_for(ps_local, work)
                for s_id in pl.sources:
                    pl.sources[s_id].uri = os.path.join(work, f"{s_id}_big.mp4")
                t0 = time.time()
                get_engine("compositor").render(pl, os.path.join(work, "out_compositor_T6.mp4"), crf=18, preset="veryfast")
                res = {"ok": True, "seconds": round(time.time() - t0, 2)}
            else:
                res = run_engine(eng, "T6", ps, work)
            report.setdefault("T6_desempenho_30s_1080x1920", {})[eng] = {"run": {k: v for k, v in res.items() if k != "out"}}
            print("T6", eng, res.get("seconds"), res.get("ok"), flush=True)
    os.makedirs(os.path.join(OUT, a.tag), exist_ok=True)
    json.dump(report, open(os.path.join(OUT, a.tag, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(os.path.join(work, "__tmp"), ignore_errors=True)


if __name__ == "__main__":
    main()
