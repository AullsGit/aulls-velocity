"""Banco de fidelidade com verdade conhecida (referências sintéticas de velocity/evaluate/synth.py).

Mede duas coisas SEPARADAS, sem nunca usar o DNA do motor como verdade:
  * analisador: o que o motor percebe da referência vs o que a referência realmente é;
  * reprodução: o vídeo gerado vs a referência.
      - estrutura (cenários A e B): geometria, fps, duração, cortes, transições, movimento, batida;
      - semelhança visual (só cenário A, mesmas fontes): SSIM/PSNR por frame, cor (ΔE Lab),
        correspondência fonte/instante.

Uso:
    python -m velocity.evaluate.fidelity_bench --tag baseline            # todos os casos, A e B
    python -m velocity.evaluate.fidelity_bench --cases r1 --scenarios A --tag teste

Escreve bench/out/fidelity/<tag>/<caso>_<cenário>.json e summary.md. O material sintético é
gerado uma vez em bench/synth/ (reprodutível: tudo vem de seeds fixas).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict

import cv2
import numpy as np

from velocity.evaluate import synth
from velocity.evaluate.synth import FileSpec, GTEdit, GTShot, GTTransition, SourceSpec, make_source

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNTH_DIR = os.path.join(ROOT, "bench", "synth")
OUT_DIR = os.path.join(ROOT, "bench", "out", "fidelity")

CUT_TOL_S = 0.067  # 2 frames a 30 fps: um corte conta como encontrado dentro desta janela
BEAT_TOL_S = 0.034  # 1 frame a 30 fps
GT_TO_DNA = {"cut": "cut", "flash": "flash", "dip_black": "dip_black", "zoom_blur": "blur", "crossfade": None}
MOTION_MIN = 0.04  # |zoom ln| ou |pan| (fração da largura) abaixo disto conta como "sem movimento"


# --------------------------------------------------------------------------- casos
def _S(src, f0, n, **kw) -> GTShot:
    return GTShot(source=src, src_frame=f0, frames=n, **kw)


def _T(kind="cut", n=0) -> GTTransition:
    return GTTransition(type=kind, frames=n)


def case_r1() -> tuple[GTEdit, list[SourceSpec], list[SourceSpec]]:
    """Velocity 9:16, 30 fps, 120 BPM (batida = 15 frames): punches, zoom lento, speed 2x/0.5x,
    pan horizontal e vertical, flash, zoom-blur, dip-to-black, cor por plano."""
    drifts = [(1.5, 0.3), (0.4, 1.2), (2.0, 0.0), (0.8, 0.8)]
    A = [SourceSpec(seed=11 + i, size=(540, 960), drift=d) for i, d in enumerate(drifts)]
    B = [SourceSpec(seed=111 + i, size=(540, 960), drift=d) for i, d in enumerate(reversed(drifts))]
    shots = [
        _S(0, 20, 15),
        _S(1, 30, 15, zoom=(1.35, 1.0), ease="out"),
        _S(2, 60, 8),
        _S(3, 40, 7),
        _S(0, 90, 30, zoom=(1.0, 1.3), ease="inout"),
        _S(1, 90, 15, speed=2.0),
        _S(2, 120, 15, zoom=(1.25, 1.25), center=((0.42, 0.5), (0.58, 0.5))),
        _S(3, 100, 15, speed=0.5, zoom=(1.15, 1.15)),
        _S(0, 160, 30, zoom=(1.0, 1.4)),
        _S(2, 20, 15, color=(0.05, 1.15, 1.3)),
        _S(1, 160, 15, zoom=(1.2, 1.2), center=((0.5, 0.44), (0.5, 0.56))),
        _S(3, 160, 15, zoom=(1.5, 1.0), ease="out"),
        _S(2, 180, 30, color=(-0.06, 1.1, 0.6)),
        _S(0, 40, 15, speed=1.5),
        _S(1, 200, 15),
    ]
    trans = [_T("flash", 4), _T(), _T(), _T("zoom_blur", 6), _T(), _T(), _T("flash", 4), _T("dip_black", 6), _T(), _T(),
             _T("flash", 4), _T("zoom_blur", 6), _T(), _T("dip_black", 6)]
    return GTEdit("r1_velocity_916", "velocity", 30.0, (540, 960), shots, trans, bpm=120.0,
                  notes="cortes na batida exceto 1 (meio tempo)"), A, B


def case_r2() -> tuple[GTEdit, list[SourceSpec], list[SourceSpec]]:
    """Cinematográfico 16:9 a 24 fps (fontes a 30 fps), planos longos, zooms lentos com easing,
    pans, cross-fades e dip-to-black, color grade quente global, sem áudio."""
    drifts = [(1.0, 0.2), (0.6, 0.0), (1.4, 0.4), (0.3, 0.6)]
    A = [SourceSpec(seed=21 + i, size=(960, 540), drift=d, seconds=10.0) for i, d in enumerate(drifts)]
    B = [SourceSpec(seed=121 + i, size=(960, 540), drift=d, seconds=10.0) for i, d in enumerate(reversed(drifts))]
    shots = [
        _S(0, 20, 60, zoom=(1.0, 1.15), ease="inout"),
        _S(1, 30, 72, zoom=(1.2, 1.2), center=((0.42, 0.5), (0.58, 0.5)), ease="inout"),
        _S(2, 40, 60, zoom=(1.25, 1.0), ease="out"),
        _S(3, 30, 72, zoom=(1.1, 1.1), center=((0.5, 0.46), (0.5, 0.54))),
        _S(0, 150, 60, speed=0.5),
        _S(1, 160, 48, zoom=(1.0, 1.2)),
    ]
    trans = [_T("crossfade", 12), _T("dip_black", 12), _T("crossfade", 16), _T(), _T("dip_black", 12)]
    return GTEdit("r2_cinematic_169", "cinematic", 24.0, (960, 540), shots, trans, grade=(1.07, 1.0, 0.86),
                  notes="24 fps; fontes a 30 fps; grade quente"), A, B


def case_r3() -> tuple[GTEdit, list[SourceSpec], list[SourceSpec]]:
    """Social 1:1, 30 fps, 100 BPM (batida = 18 frames), fontes verticais recortadas a quadrado:
    tilt, speed ramps, flashes, plano quase P&B."""
    drifts = [(1.0, 0.5), (0.5, 1.0), (1.6, 0.2), (0.2, 1.4)]
    A = [SourceSpec(seed=31 + i, size=(540, 960), drift=d) for i, d in enumerate(drifts)]
    B = [SourceSpec(seed=131 + i, size=(540, 960), drift=d) for i, d in enumerate(reversed(drifts))]
    shots = [
        _S(0, 10, 18, center=((0.5, 0.35), (0.5, 0.35))),
        _S(1, 20, 18, center=((0.5, 0.6), (0.5, 0.6)), zoom=(1.3, 1.0), ease="out"),
        _S(2, 30, 9, speed=2.0),
        _S(3, 40, 9),
        _S(0, 100, 36, center=((0.5, 0.3), (0.5, 0.7))),
        _S(1, 120, 18, speed=0.5, color=(0.0, 1.0, 0.25)),
        _S(2, 150, 18, zoom=(1.0, 1.5), ease="in"),
        _S(3, 140, 36, speed=1.5, center=((0.5, 0.55), (0.5, 0.55))),
        _S(0, 200, 18),
    ]
    trans = [_T("flash", 4), _T(), _T(), _T(), _T("flash", 4), _T(), _T("zoom_blur", 6), _T()]
    return GTEdit("r3_social_11", "social", 30.0, (540, 540), shots, trans, bpm=100.0,
                  notes="fontes 9:16 recortadas a 1:1"), A, B


REAL_SOURCES = os.path.join(ROOT, "bench", "real", "sources.json")


def _real(group: str) -> list[FileSpec]:
    """Clips reais locais (NUNCA versionados: material de terceiros). bench/real/sources.json:
    {"bmw": ["C:/.../clip.mp4", ...], "futebol": [...]}"""
    if not os.path.exists(REAL_SOURCES):
        raise FileNotFoundError(f"casos com filmagem real precisam de {REAL_SOURCES}")
    with open(REAL_SOURCES, encoding="utf-8") as fh:
        return [FileSpec(p) for p in json.load(fh)[group]]


def case_r4() -> tuple[GTEdit, list, list]:
    """Filmagem REAL (3 clips de telemóvel a ~26,08 fps, VFR) -> edição Velocity 9:16 a 30 fps,
    120 BPM: conversão de fps real, speed ramps, punches, zoom lento, pan, flash, zoom-blur, dip.
    Material diferente (cenário B): clips de futebol."""
    A, B = _real("bmw"), _real("futebol")[:3]
    shots = [
        _S(0, 10, 15),
        _S(2, 30, 15, zoom=(1.3, 1.0), ease="out"),
        _S(1, 8, 8),
        _S(2, 110, 7),
        _S(0, 50, 30, zoom=(1.0, 1.35), ease="inout"),
        _S(2, 180, 15, speed=2.0),
        _S(1, 30, 15, zoom=(1.25, 1.25), center=((0.42, 0.5), (0.58, 0.5))),
        _S(0, 110, 15, speed=0.5, zoom=(1.15, 1.15)),
        _S(2, 260, 30, zoom=(1.0, 1.4)),
        _S(0, 140, 15, color=(0.05, 1.15, 1.3)),
        _S(2, 360, 15, speed=1.5),
        _S(1, 45, 15, zoom=(1.2, 1.2), center=((0.5, 0.44), (0.5, 0.56))),
    ]
    trans = [_T("flash", 4), _T(), _T(), _T("zoom_blur", 6), _T(), _T(), _T("flash", 4), _T("dip_black", 6), _T(), _T(),
             _T("flash", 4)]
    return GTEdit("r4_real_bmw_916", "velocity (filmagem real)", 30.0, (540, 960), shots, trans, bpm=120.0,
                  notes="fontes reais de telemóvel (~26,08 fps)"), A, B


def case_r5() -> tuple[GTEdit, list, list]:
    """Filmagem REAL de futebol (30 fps, comprimida para redes sociais) -> edição social 1:1 a
    30 fps, 100 BPM, com plano quase P&B e speed ramps. Material diferente: clips da BMW."""
    A, B = _real("futebol"), _real("bmw")
    shots = [
        _S(2, 30, 18, center=((0.5, 0.4), (0.5, 0.4))),
        _S(3, 40, 18, zoom=(1.3, 1.0), ease="out"),
        _S(2, 400, 9, speed=2.0),
        _S(0, 10, 9),
        _S(3, 200, 36, center=((0.5, 0.32), (0.5, 0.68))),
        _S(2, 700, 18, speed=0.5, color=(0.0, 1.0, 0.25)),
        _S(1, 12, 18, zoom=(1.0, 1.5), ease="in"),
        _S(2, 1000, 36, speed=1.5),
        _S(3, 380, 18),
    ]
    trans = [_T("flash", 4), _T(), _T(), _T(), _T("flash", 4), _T(), _T("zoom_blur", 6), _T()]
    return GTEdit("r5_real_futebol_11", "social (filmagem real)", 30.0, (720, 720), shots, trans, bpm=100.0,
                  notes="fontes reais de futebol (TikTok, 30 fps)"), A, B


CASES = {"r1": case_r1, "r2": case_r2, "r3": case_r3, "r4": case_r4, "r5": case_r5}


# ------------------------------------------------------------------- gerar material
def build_case(key: str) -> dict:
    """Gera (uma vez) referência, verdade e fontes A/B; devolve caminhos + verdade."""
    edit, specs_a, specs_b = CASES[key]()
    d = os.path.join(SYNTH_DIR, edit.name)
    with open(synth.__file__, "rb") as fh:  # mudar o gerador também regenera o material
        gen_hash = hashlib.sha1(fh.read()).hexdigest()
    stamp = hashlib.sha1(json.dumps([asdict(edit), [asdict(s) for s in specs_a], [asdict(s) for s in specs_b], gen_hash],
                                    default=str).encode()).hexdigest()[:12]
    stamp_path = os.path.join(d, "stamp.txt")
    paths = {
        "ref": os.path.join(d, "reference.mp4"), "gt": os.path.join(d, "reference.gt.json"),
        "A": [os.path.join(d, "src_A", f"clip{i}.mp4") for i in range(len(specs_a))],
        "B": [os.path.join(d, "src_B", f"clip{i}.mp4") for i in range(len(specs_b))],
    }
    if not (os.path.exists(stamp_path) and open(stamp_path).read().strip() == stamp and os.path.exists(paths["ref"])):
        os.makedirs(d, exist_ok=True)
        srcs_a = [make_source(s) for s in specs_a]
        for s, p in zip(srcs_a, paths["A"]):
            s.write(p)
        for s, p in zip(specs_b, paths["B"]):
            make_source(s).write(p)
        audio = None
        if edit.bpm:
            audio = synth.click_track(os.path.join(d, "music.wav"), edit.duration_s + 0.5, edit.bpm)
        synth.render_reference(edit, srcs_a, paths["ref"], audio)
        with open(stamp_path, "w") as fh:
            fh.write(stamp)
    with open(paths["gt"], encoding="utf-8") as fh:
        paths["truth"] = json.load(fh)
    return paths


# ---------------------------------------------------------------------- utilidades
def match_times(gt_t: list[float], det_t: list[float], tol: float = CUT_TOL_S) -> dict:
    cand = sorted((abs(g - d_), i, j) for i, g in enumerate(gt_t) for j, d_ in enumerate(det_t) if abs(g - d_) <= tol)
    gi, dj, pairs = set(), set(), []
    for _, i, j in cand:
        if i in gi or j in dj:
            continue
        gi.add(i)
        dj.add(j)
        pairs.append((i, j, det_t[j] - gt_t[i]))
    tp = len(pairs)
    prec = tp / len(det_t) if det_t else (1.0 if not gt_t else 0.0)
    rec = tp / len(gt_t) if gt_t else 1.0
    errs = [abs(e) for _, _, e in pairs]
    return {
        "n_gt": len(gt_t), "n_det": len(det_t), "tp": tp, "precision": round(prec, 3), "recall": round(rec, 3),
        "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0,
        "mean_abs_err_ms": round(1000 * float(np.mean(errs)), 1) if errs else None,
        "max_abs_err_ms": round(1000 * float(np.max(errs)), 1) if errs else None,
        "missed_s": [round(gt_t[i], 3) for i in range(len(gt_t)) if i not in gi],
        "extra_s": [round(det_t[j], 3) for j in range(len(det_t)) if j not in dj],
        "pairs": pairs,
    }


def gt_motion(truth: dict, shot_i: int, t0: float, t1: float) -> tuple[float, float, float, int]:
    """Movimento verdadeiro do conteúdo entre t0 e t1 (s) dentro do plano shot_i, nas convenções
    do analisador: (ln escala, dx/largura, dy/largura, nº de frames)."""
    sh = truth["shots"][shot_i]
    fps, (w, h) = truth["fps"], truth["size"]
    a, n = sh["start_frame"], sh["end_frame"] - sh["start_frame"]
    ka = int(np.clip(round(t0 * fps) - a, 0, n - 1))
    kb = int(np.clip(round(t1 * fps) - a, 0, n - 1))
    s0, x0, y0 = sh["traj"][ka]
    s1, x1, y1 = sh["traj"][kb]
    cx, cy = (w / 2 - x0) / s0, (h / 2 - y0) / s0
    return math.log(s1 / s0), (s1 * cx + x1 - w / 2) / w, (s1 * cy + y1 - h / 2) / w, kb - ka


def _overlap_shot(truth: dict, t0: float, t1: float) -> int | None:
    best, best_ov = None, 0.0
    for i, sh in enumerate(truth["shots"]):
        a, b = sh["start_frame"] / truth["fps"], sh["end_frame"] / truth["fps"]
        ov = min(b, t1) - max(a, t0)
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def motion_vs_truth(truth: dict, dna, window_s: float | None = None) -> dict:
    """Movimento medido em cada plano do DNA (no mesmo intervalo de frames que o analisador usa)
    contra o movimento verdadeiro nesse intervalo."""
    if not truth.get("motion_gt", True):
        return {"n": 0, "note": "fontes reais: o movimento de câmara da própria filmagem não é conhecido"}
    from velocity.reference.shot_features import stable_range
    rows = []
    fps = dna.fps
    for j, sh in enumerate(dna.shots):
        if window_s is not None and sh.start_frame / fps >= window_s:
            break
        t_in = sh.transition_in
        t_next = dna.shots[j + 1].transition_in if j + 1 < len(dna.shots) else None
        a_, b_ = stable_range(sh.start_frame, sh.end_frame, (t_in.start_frame, t_in.end_frame) if t_in else None,
                              t_next.start_frame if t_next else None)
        t0, t1 = (max(a_, 1) - 1) / fps, (b_ - 1) / fps
        gi = _overlap_shot(truth, t0, t1)
        if gi is None:
            continue
        gz, gx, gy, nfr = gt_motion(truth, gi, t0, t1)
        if nfr < 4:
            continue
        m = sh.motion
        rows.append({"dna_shot": j, "gt_shot": gi, "frames": nfr,
                     "gt": [round(gz, 4), round(gx, 4), round(gy, 4)],
                     "det": [round(m.zoom_total, 4), round(m.pan_x, 4), round(m.pan_y, 4)],
                     "motion_type": m.motion_type})
    if not rows:
        return {"n": 0}
    dz = [abs(r["det"][0] - r["gt"][0]) for r in rows]
    dp = [math.hypot(r["det"][1] - r["gt"][1], r["det"][2] - r["gt"][2]) for r in rows]
    zoom_rows = [r for r in rows if abs(r["gt"][0]) >= MOTION_MIN]
    pan_rows = [r for r in rows if math.hypot(r["gt"][1], r["gt"][2]) >= MOTION_MIN]
    sign_z = [np.sign(r["det"][0]) == np.sign(r["gt"][0]) and abs(r["det"][0]) >= MOTION_MIN / 2 for r in zoom_rows]
    ratio_z = [r["det"][0] / r["gt"][0] for r in zoom_rows]
    ratio_p = [(r["det"][1] * r["gt"][1] + r["det"][2] * r["gt"][2]) / (r["gt"][1] ** 2 + r["gt"][2] ** 2) for r in pan_rows]
    still = [r for r in rows if abs(r["gt"][0]) < MOTION_MIN / 2 and math.hypot(r["gt"][1], r["gt"][2]) < MOTION_MIN / 2]
    return {
        "n": len(rows), "zoom_mae": round(float(np.mean(dz)), 4), "pan_mae": round(float(np.mean(dp)), 4),
        "zoom_sign_acc": round(float(np.mean(sign_z)), 3) if sign_z else None,
        "zoom_ratio_median": round(float(np.median(ratio_z)), 3) if ratio_z else None,
        "pan_ratio_median": round(float(np.median(ratio_p)), 3) if ratio_p else None,
        "n_zoom_shots": len(zoom_rows), "n_pan_shots": len(pan_rows),
        "false_motion_in_still_shots": sum(1 for r in still if abs(r["det"][0]) >= MOTION_MIN or math.hypot(r["det"][1], r["det"][2]) >= MOTION_MIN),
        "n_still_shots": len(still), "rows": rows,
    }


def _dna_cut_times(dna) -> list[float]:
    return [t.cut_frame / dna.fps for t in dna.transitions()]


# ---------------------------------------------------------------- analisador vs GT
def analyzer_report(truth: dict, dna) -> dict:
    cuts = match_times(truth["cut_times_s"], _dna_cut_times(dna))
    trans = dna.transitions()
    types = []
    for gi, dj, _ in cuts["pairs"]:
        g = truth["transitions"][gi]["type"]
        types.append({"gt": g, "expected": GT_TO_DNA[g], "det": trans[dj].type})
    sup = [t for t in types if t["expected"] is not None]
    acc = round(float(np.mean([t["det"] == t["expected"] for t in sup])), 3) if sup else None
    beats_gt = truth["beats_s"]
    beats_det = dna.audio.beats if dna.audio.has_audio else []
    beat = match_times(beats_gt, beats_det, tol=0.05) if beats_gt else None
    return {
        "geometry": {"gt_size": truth["size"], "dna_size": [dna.width, dna.height], "gt_fps": truth["fps"], "dna_fps": dna.fps,
                     "gt_duration_s": round(truth["duration_s"], 3), "dna_duration_s": round(dna.duration_s, 3)},
        "cuts": {k: v for k, v in cuts.items() if k != "pairs"},
        "transition_type_acc": acc, "transition_types": types,
        "unsupported_types": sorted({t["gt"] for t in types if t["expected"] is None}),
        "motion": {k: v for k, v in motion_vs_truth(truth, dna).items() if k != "rows"},
        "motion_rows": motion_vs_truth(truth, dna).get("rows", []),
        "speed": {"gt_speeds": [s["speed"] for s in truth["shots"]],
                  "dna_speed_shapes": [s.speed.shape for s in dna.shots],
                  "note": "o DNA só guarda velocidade RELATIVA dentro do plano (curva normalizada); a velocidade absoluta (0.5x, 2x) não é medida"},
        "beats": ({"bpm_gt": truth["bpm"], "bpm_det": dna.audio.bpm, "f1": beat["f1"], "mean_abs_err_ms": beat["mean_abs_err_ms"]}
                  if beat else None),
        "color": {"gt_grade_rgb": truth["grade"], "dna_look": dna.look.model_dump() if dna.look else None,
                  "note": "o LookProfile só tem luma/contraste/saturação/nitidez; balanço de cor (temperatura/tint) não é representado"},
    }


# -------------------------------------------------------------------- reprodução
def _reader(path: str, fps: float, w: int, h: int, crop_aspect: float | None):
    vf = f"fps={fps},"
    if crop_aspect:
        vf += f"crop=w='min(iw,ih*{crop_aspect:.6f})':h='min(ih,iw/{crop_aspect:.6f})',"
    vf += f"scale={w}:{h}:flags=area,format=rgb24"
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", vf, "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.wait()


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a.astype(np.float64), b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = lambda x: cv2.GaussianBlur(x, (11, 11), 1.5)  # noqa: E731
    mu1, mu2 = g(a), g(b)
    s1, s2, s12 = g(a * a) - mu1 ** 2, g(b * b) - mu2 ** 2, g(a * b) - mu1 * mu2
    m = ((2 * mu1 * mu2 + c1) * (2 * s12 + c2)) / ((mu1 ** 2 + mu2 ** 2 + c1) * (s1 + s2 + c2))
    return float(m.mean())


def visual_compare(ref: str, out: str, truth: dict, max_side: int = 480) -> dict:
    """Frame a frame, aos instantes da referência. O resultado é recortado ao aspeto da referência
    antes de comparar (comparação generosa: a diferença de geometria é reportada à parte)."""
    w, h = truth["size"]
    s = max_side / max(w, h)
    cw, ch = int(round(w * s / 2)) * 2, int(round(h * s / 2)) * 2
    ss, ps, de, lab_r, lab_o, grays = [], [], [], [], [], []
    for fa, fb in zip(_reader(ref, truth["fps"], cw, ch, None), _reader(out, truth["fps"], cw, ch, w / h)):
        ga, gb = cv2.cvtColor(fa, cv2.COLOR_RGB2GRAY), cv2.cvtColor(fb, cv2.COLOR_RGB2GRAY)
        grays.append(ga)
        ss.append(ssim(ga, gb))
        mse = float(np.mean((fa.astype(np.float32) - fb.astype(np.float32)) ** 2))
        ps.append(10 * math.log10(255 ** 2 / max(mse, 1e-6)))
        la = cv2.cvtColor(fa.astype(np.float32) / 255, cv2.COLOR_RGB2LAB).reshape(-1, 3).mean(0)
        lb = cv2.cvtColor(fb.astype(np.float32) / 255, cv2.COLOR_RGB2LAB).reshape(-1, 3).mean(0)
        de.append(float(np.linalg.norm(la - lb)))
        lab_r.append(la)
        lab_o.append(lb)
    if not ss:
        return {"frames": 0}
    lr, lo = np.mean(lab_r, 0), np.mean(lab_o, 0)
    ssa = np.array(ss)
    # linha de base "ao acaso": a própria referência contra si mesma desfasada meio vídeo (mesmo
    # tipo de imagem, conteúdo/instante errados). Um SSIM perto disto = nada reproduzido.
    n = len(grays)
    chance = float(np.mean([ssim(grays[i], grays[(i + n // 2) % n]) for i in range(0, n, 3)])) if n > 2 else None
    return {
        "frames": len(ss), "ssim_mean": round(float(ssa.mean()), 4), "ssim_chance": round(chance, 4) if chance else None, "ssim_p10": round(float(np.percentile(ssa, 10)), 4),
        "frames_ssim_ge_0_8": round(float((ssa >= 0.8).mean()), 3), "psnr_mean_db": round(float(np.mean(ps)), 2),
        "deltaE_frame_mean": round(float(np.mean(de)), 2),
        "lab_ref_mean": [round(float(v), 2) for v in lr], "lab_out_mean": [round(float(v), 2) for v in lo],
        "ssim_per_gt_shot": [round(float(ssa[sh["start_frame"]:min(sh["end_frame"], len(ssa))].mean()), 3)
                             for sh in truth["shots"] if sh["start_frame"] < len(ssa)],
    }


def codec_ceiling(ref: str, out_wh: tuple[int, int], fps: float, truth: dict, crf: int = 21, preset: str = "veryfast") -> dict:
    """Teto do que QUALQUER reprodução perfeita consegue neste banco: a própria referência
    reamostrada ao tamanho de saída e recodificada com as definições do perfil — o que sobra de
    diferença é só codec/reamostragem, não edição."""
    d = os.path.dirname(ref)
    path = os.path.join(d, f"ceiling_{out_wh[0]}x{out_wh[1]}_{fps:g}_crf{crf}_{preset}.mp4")
    if not os.path.exists(path):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ref, "-an", "-vf", f"fps={fps},scale={out_wh[0]}:{out_wh[1]}:flags=bicubic",
                        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p", path], check=True)
    v = visual_compare(ref, path, truth)
    return {"ssim_mean": v["ssim_mean"], "psnr_mean_db": v["psnr_mean_db"], "deltaE_frame_mean": v["deltaE_frame_mean"]}


def source_correspondence(truth: dict, tl, src_paths: list[str]) -> dict:
    """Cenário A: para o instante central de cada plano verdadeiro, o resultado mostra a mesma
    fonte, no mesmo instante da fonte?"""
    norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    idx = {norm(p): i for i, p in enumerate(src_paths)}
    rows = []
    for gi, sh in enumerate(truth["shots"]):
        f_mid = (sh["start_frame"] + sh["end_frame"] - 1) / 2
        t_mid = f_mid / truth["fps"]
        src = truth["sources"][sh["source"]]
        k = f_mid - sh["start_frame"]
        gt_src_t = math.floor(sh["src_frame"] + k * sh["speed"] * src["fps"] / truth["fps"] + 1e-6) / src["fps"]
        fo = t_mid * tl.fps
        ts = next((s for s in tl.shots if s.start_frame <= fo < s.end_frame), None)
        if ts is None:
            rows.append({"gt_shot": gi, "same_source": False, "src_err_s": None})
            continue
        sp = float(np.mean(ts.clip.speed.v)) if ts.clip.speed.v else 1.0
        if ts.clip.exact is not None:  # reconstrução: o instante da fonte é o MEDIDO para este frame
            k_ = min(int(fo - ts.start_frame), len(ts.clip.exact.src_frames) - 1)
            out_src_t = ts.clip.exact.src_frames[k_] / ts.clip.exact.src_fps
        else:
            out_src_t = ts.clip.src_start_s + (fo - ts.start_frame) / tl.fps * sp
        same = idx.get(norm(ts.clip.source_path)) == sh["source"]
        rows.append({"gt_shot": gi, "same_source": same, "src_err_s": round(out_src_t - gt_src_t, 3) if same else None,
                     "gt_speed": sh["speed"], "out_speed": round(sp, 3)})
    same = [r for r in rows if r["same_source"]]
    ok = [r for r in same if abs(r["src_err_s"]) <= 2 / 30]
    return {"n": len(rows), "same_source_rate": round(len(same) / len(rows), 3),
            "correct_inpoint_rate": round(len(ok) / len(rows), 3),
            "median_abs_src_err_s": round(float(np.median([abs(r["src_err_s"]) for r in same])), 3) if same else None,
            "speed_abs_err_mean": round(float(np.mean([abs(r["out_speed"] - r["gt_speed"]) for r in rows if "out_speed" in r])), 3),
            "rows": rows}


def reproduction_report(truth: dict, ref: str, out: str, tl, out_dna, scenario: str, src_paths: list[str]) -> dict:
    from velocity.io.probe import probe
    pi = probe(out)
    dur = truth["duration_s"]
    planned = [s.start_frame / tl.fps for s in tl.shots[1:]]
    cuts_full = match_times(truth["cut_times_s"], planned)
    cuts_win = match_times(truth["cut_times_s"], [t for t in planned if t <= dur + CUT_TOL_S])
    observed = match_times(truth["cut_times_s"], [t for t in _dna_cut_times(out_dna) if t <= dur + CUT_TOL_S])
    types = []
    for gi, dj, _ in cuts_full["pairs"]:
        g = truth["transitions"][gi]["type"]
        tin = tl.shots[dj + 1].transition_in
        types.append({"gt": g, "expected": GT_TO_DNA[g], "out": tin.type if tin else "cut"})
    sup = [t for t in types if t["expected"] is not None]
    rep = {
        "scenario": scenario,
        "geometry": {"ref_size": truth["size"], "out_size": [pi.width, pi.height], "ref_fps": truth["fps"], "out_fps": round(pi.fps_nominal, 3),
                     "aspect_match": abs(pi.width / pi.height - truth["size"][0] / truth["size"][1]) < 0.01,
                     "ref_duration_s": round(dur, 3), "out_duration_s": round(pi.duration, 3),
                     "duration_ratio": round(pi.duration / dur, 3)},
        "cuts_planned_full": {k: v for k, v in cuts_full.items() if k not in ("pairs", "missed_s", "extra_s")},
        "cuts_planned_ref_window": {k: v for k, v in cuts_win.items() if k != "pairs"},
        "cuts_observed_ref_window": {k: v for k, v in observed.items() if k != "pairs"},
        "transition_type_acc": round(float(np.mean([t["out"] == t["expected"] for t in sup])), 3) if sup else None,
        "transition_types": types,
        "motion_observed": {k: v for k, v in motion_vs_truth(truth, out_dna, window_s=dur).items() if k != "rows"},
        "audio": {"music_path_is_ref": bool(tl.music_path) and os.path.abspath(tl.music_path) == os.path.abspath(ref),
                  "music_offset_s": tl.music_offset_s},
    }
    if truth["beats_s"]:
        b = np.array(truth["beats_s"])
        on = lambda ts: round(float(np.mean([np.min(np.abs(b - t)) <= BEAT_TOL_S for t in ts])), 3) if ts else None  # noqa: E731
        rep["beat_sync"] = {"gt_on_beat": on(truth["cut_times_s"]), "out_on_beat_ref_window": on([t for t in planned if t <= dur])}
    if scenario == "A":
        rep["source_correspondence"] = source_correspondence(truth, tl, src_paths)
        rep["visual"] = visual_compare(ref, out, truth)
        rep["visual"]["codec_ceiling"] = codec_ceiling(ref, (pi.width, pi.height), truth["fps"], truth)
    return rep


# -------------------------------------------------------------------------- correr
def run_case(key: str, scenario: str, tag: str, make_overrides: dict | None = None, analyzer_cache: dict | None = None) -> dict:
    from velocity.pipeline import MakeParams, run_make
    from velocity.reference.analyzer import analyze_reference
    paths = build_case(key)
    truth = paths["truth"]
    out_dir = os.path.join(OUT_DIR, tag)
    os.makedirs(out_dir, exist_ok=True)
    if analyzer_cache is not None and key in analyzer_cache:
        a_rep = analyzer_cache[key]
    else:
        a_rep = analyzer_report(truth, analyze_reference(paths["ref"], with_text=False))
        if analyzer_cache is not None:
            analyzer_cache[key] = a_rep
    out = os.path.join(out_dir, f"{truth['name']}_{scenario}.mp4")
    logs: list[str] = []
    t0 = time.time()
    params = MakeParams(ref=paths["ref"], clips=paths[scenario], out=out, profile="fast", force=True)
    for k, v in (make_overrides or {}).items():
        setattr(params, k, v)
    res = run_make(params, log=logs.append)
    elapsed = time.time() - t0
    if not res.ok:
        return {"case": truth["name"], "scenario": scenario, "ok": False, "error": res.error, "log": logs}
    out_dna = analyze_reference(out, with_audio=False, with_text=False)
    r_rep = reproduction_report(truth, paths["ref"], out, res.timeline, out_dna, scenario, paths[scenario])
    rep = {"case": truth["name"], "style": truth["style"], "scenario": scenario, "ok": True, "make_s": round(elapsed, 1),
           "engine_fidelity_self_reported": res.fidelity, "analyzer": a_rep, "reproduction": r_rep,
           "overrides": make_overrides or {}, "log_tail": logs[-6:]}
    with open(os.path.join(out_dir, f"{truth['name']}_{scenario}.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=1, default=str)
    return rep


def summarize(reports: list[dict]) -> str:
    lines = ["| caso | cen. | geometria (ref→out) | duração ×ref | cortes F1 total | F1 janela ref | erro corte ms | transições | zoom MAE (obs) | zoom razão (obs) | SSIM (acaso / teto codec) | PSNR (teto) | ΔE | fonte certa | in-point certo | fidelidade auto-reportada | tempo s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        if not r.get("ok"):
            lines.append(f"| {r['case']} | {r['scenario']} | FALHOU: {r.get('error')} |")
            continue
        g = r["reproduction"]["geometry"]
        rp = r["reproduction"]
        v = rp.get("visual", {})
        sc = rp.get("source_correspondence", {})
        mo = rp["motion_observed"]
        cc = v.get("codec_ceiling", {})
        f = lambda x: "—" if x is None else x  # noqa: E731
        lines.append(
            f"| {r['case']} | {r['scenario']} | {g['ref_size'][0]}x{g['ref_size'][1]}@{g['ref_fps']:g} → {g['out_size'][0]}x{g['out_size'][1]}@{g['out_fps']:g} "
            f"| {g['duration_ratio']} | {rp['cuts_planned_full']['f1']} | {rp['cuts_planned_ref_window']['f1']} "
            f"| {f(rp['cuts_planned_ref_window']['mean_abs_err_ms'])} | {f(rp['transition_type_acc'])} | {f(mo.get('zoom_mae'))} "
            f"| {f(mo.get('zoom_ratio_median'))} | {f(v.get('ssim_mean'))} ({f(v.get('ssim_chance'))} / {f(cc.get('ssim_mean'))}) "
            f"| {f(v.get('psnr_mean_db'))} ({f(cc.get('psnr_mean_db'))}) | {f(v.get('deltaE_frame_mean'))} "
            f"| {f(sc.get('same_source_rate'))} | {f(sc.get('correct_inpoint_rate'))} | {f(r['engine_fidelity_self_reported'])} | {r['make_s']} |")
    lines += ["", "**Analisador (referência vs verdade)**", "",
              "| caso | cortes P/R/F1 | erro ms (médio/máx) | tipo transição | não suportados | zoom MAE | zoom sinal | zoom razão | pan MAE | pan razão | movimento falso em planos parados | BPM ref/det |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    seen = set()
    for r in reports:
        if not r.get("ok") or r["case"] in seen:
            continue
        seen.add(r["case"])
        a = r["analyzer"]
        c, m = a["cuts"], a["motion"]
        bp = a["beats"] or {}
        lines.append(f"| {r['case']} | {c['precision']}/{c['recall']}/{c['f1']} | {c['mean_abs_err_ms']}/{c['max_abs_err_ms']} "
                     f"| {a['transition_type_acc']} | {', '.join(a['unsupported_types']) or '—'} | {m.get('zoom_mae')} | {m.get('zoom_sign_acc')} "
                     f"| {m.get('zoom_ratio_median')} | {m.get('pan_mae')} | {m.get('pan_ratio_median')} "
                     f"| {m.get('false_motion_in_still_shots')}/{m.get('n_still_shots')} | {bp.get('bpm_gt', '—')}/{bp.get('bpm_det', '—')} |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", default="r1,r2,r3")
    ap.add_argument("--scenarios", default="A,B")
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--set", action="append", default=[], help="override de MakeParams, ex.: --set no_extend=true")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # consola do Windows (cp1252)
    overrides = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        overrides[k] = json.loads(v) if v not in ("", None) else None
    reports, cache = [], {}
    for key in a.cases.split(","):
        for sc in a.scenarios.split(","):
            t0 = time.time()
            r = run_case(key.strip(), sc.strip(), a.tag, overrides, cache)
            reports.append(r)
            print(f"{r['case']} {sc}: ok={r.get('ok')} ({time.time() - t0:.0f}s)", flush=True)
    md = summarize(reports)
    with open(os.path.join(OUT_DIR, a.tag, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Fidelidade — {a.tag}\n\noverrides: {overrides}\n\n" + md + "\n")
    print(md)


if __name__ == "__main__":
    main()
