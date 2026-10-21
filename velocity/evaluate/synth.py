"""Referências sintéticas com VERDADE CONHECIDA, para medir fidelidade de forma objetiva.

Porquê: comparar o DNA da referência com o DNA do resultado (ambos medidos pelo mesmo
analisador) esconde os erros do analisador dos dois lados. Aqui sabemos exatamente o que cada
frame da referência é: de que fonte veio, em que instante, com que zoom/centro, velocidade,
cor e transição. Assim medimos separadamente:
  1. o ANALISADOR (o que ele percebe da referência vs a verdade);
  2. a REPRODUÇÃO (o vídeo gerado vs a referência — no cenário A com as mesmas fontes,
     comparação pixel a pixel; no cenário B, estrutura).

Implementação deliberadamente independente do renderer do motor (velocity/render): as
transformações são feitas aqui com uma única warpAffine a partir de uma "tela" procedural, e
as transições com fórmulas próprias (incluindo cross-fade, que o motor ainda não suporta).

As fontes são procedurais (tela grande colorida + textura + "sujeito" em movimento, com a
câmara a deslizar a velocidade constante). A referência usa sempre frames INTEIROS da fonte
(como um editor real a trabalhar sobre o ficheiro), por isso o cenário A é reproduzível ao
pixel por quem acertar fonte, instante, enquadramento, cor e transição.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import wave
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np


# --------------------------------------------------------------------------- fontes
@dataclass
class SourceSpec:
    seed: int
    size: tuple[int, int] = (540, 960)
    drift: tuple[float, float] = (1.2, 0.5)  # deslocamento da câmara (px/frame, à resolução da fonte)
    seconds: float = 8.0
    fps: float = 30.0


class Source:
    """Fonte procedural: frame(i) exato para qualquer frame inteiro i."""

    def __init__(self, spec: SourceSpec):
        self.spec = spec
        self.w, self.h = spec.size
        self.fps = spec.fps
        rng = np.random.default_rng(spec.seed)
        cw, ch = int(self.w * 2.4), int(self.h * 2.0)
        yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
        hsv = np.zeros((ch, cw, 3), np.float32)
        hsv[..., 0] = (rng.uniform(0, 180) + 25 * np.sin(xx / cw * 6.28) + 20 * np.cos(yy / ch * 6.28)) % 180
        hsv[..., 1] = 140 + 60 * np.sin(yy / 97.0)
        hsv[..., 2] = 120 + 50 * np.cos(xx / 131.0)
        base = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
        for _ in range(int(160 * cw * ch / 2.0e6)):  # formas com bordas nítidas (textura para o fluxo óptico)
            c = tuple(int(v) for v in rng.integers(20, 235, 3))
            x, y = int(rng.uniform(0, cw)), int(rng.uniform(0, ch))
            r = int(rng.uniform(8, 70))
            if rng.random() < 0.5:
                cv2.circle(base, (x, y), r, c, -1, cv2.LINE_AA)
            else:
                cv2.rectangle(base, (x, y), (x + r, y + int(r * rng.uniform(0.5, 2))), c, -1)
        noise = rng.normal(0, 7, (ch, cw, 1)).astype(np.float32)
        self.canvas = np.clip(base.astype(np.float32) + noise, 0, 255).astype(np.uint8)
        self.ox0, self.oy0 = cw * 0.08, ch * 0.08
        self.subj_r = int(min(self.w, self.h) * 0.11)
        self.subj_col = tuple(int(v) for v in rng.integers(180, 255, 3))
        self.subj_phase = float(rng.uniform(0, 6.28))
        n = self.n_frames
        if self.ox0 + abs(spec.drift[0]) * n + self.w > cw or self.oy0 + abs(spec.drift[1]) * n + self.h > ch:
            raise ValueError("drift demasiado grande para a tela")

    @property
    def n_frames(self) -> int:
        return int(round(self.spec.seconds * self.fps))

    def offset(self, i: int) -> tuple[float, float]:
        i = int(min(max(i, 0), self.n_frames - 1))
        return self.spec.drift[0] * i + self.ox0, self.spec.drift[1] * i + self.oy0

    def frame(self, i: int, M: np.ndarray | None = None, out_size: tuple[int, int] | None = None) -> np.ndarray:
        """Frame i da fonte; com M (2x3, fonte->saída) devolve-o já transformado para out_size."""
        i = int(min(max(i, 0), self.n_frames - 1))
        ox, oy = self.offset(i)
        T = np.array([[1, 0, -ox], [0, 1, -oy], [0, 0, 1]], np.float64)  # tela -> fonte
        Ms = M if M is not None else np.array([[1, 0, 0], [0, 1, 0]], np.float64)
        size = out_size or (self.w, self.h)
        A = np.vstack([Ms, [0, 0, 1]]) @ T
        img = cv2.warpAffine(self.canvas, A[:2].astype(np.float32), size, flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REFLECT)
        t = i / self.fps
        sx = (0.5 + 0.18 * math.sin(t * 2.1 + self.subj_phase)) * self.w
        sy = (0.55 + 0.08 * math.cos(t * 1.7 + self.subj_phase)) * self.h
        p = Ms @ np.array([sx, sy, 1.0])
        sc = math.sqrt(abs(np.linalg.det(Ms[:, :2])))
        cv2.ellipse(img, (int(round(p[0])), int(round(p[1]))), (max(1, int(self.subj_r * sc)), max(1, int(self.subj_r * 1.6 * sc))),
                    0, 0, 360, self.subj_col, -1, cv2.LINE_AA)
        return img

    def write(self, path: str, crf: int = 14) -> str:
        _encode(path, (self.frame(i) for i in range(self.n_frames)), self.w, self.h, self.fps, crf)
        return path


def _encode(path, frames, w, h, fps, crf, audio: str | None = None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-i", audio, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "fast", "-pix_fmt", "yuv420p", path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for f in frames:
            p.stdin.write(np.ascontiguousarray(f).tobytes())
    finally:
        p.stdin.close()
    err = p.stderr.read().decode(errors="replace")
    if p.wait() != 0:
        raise RuntimeError(err[-400:])


# ------------------------------------------------------------------------- edição
EASES = ("linear", "in", "out", "inout")


@dataclass
class GTShot:
    source: int
    src_frame: int  # frame da fonte no início do plano
    frames: int  # duração no vídeo final (frames da referência)
    speed: float = 1.0
    zoom: tuple[float, float] = (1.0, 1.0)  # escala no início/fim do plano, sobre o enquadramento "cover"
    center: tuple[tuple[float, float], tuple[float, float]] = ((0.5, 0.5), (0.5, 0.5))  # centro do recorte (fração da fonte)
    ease: str = "linear"
    color: tuple[float, float, float] = (0.0, 1.0, 1.0)  # brilho (+-), contraste (x), saturação (x)


@dataclass
class GTTransition:
    type: str = "cut"  # cut | flash | dip_black | crossfade | zoom_blur
    frames: int = 0  # duração total, centrada no corte


@dataclass
class GTEdit:
    name: str
    style: str
    fps: float
    size: tuple[int, int]
    shots: list[GTShot]
    transitions: list[GTTransition]  # len(shots)-1: transição de ENTRADA de cada plano a partir do 2º
    bpm: float | None = None
    grade: tuple[float, float, float] = (1.0, 1.0, 1.0)  # ganho R,G,B global (look)
    notes: str = ""
    cut_frames: list[int] = field(default_factory=list)

    def __post_init__(self):
        assert len(self.transitions) == len(self.shots) - 1
        acc, cuts = 0, []
        for s in self.shots[:-1]:
            acc += s.frames
            cuts.append(acc)
        self.cut_frames = cuts

    @property
    def n_frames(self) -> int:
        return sum(s.frames for s in self.shots)

    @property
    def duration_s(self) -> float:
        return self.n_frames / self.fps


def ease(u: float, kind: str) -> float:
    u = min(max(u, 0.0), 1.0)
    return {"in": u * u, "out": 1 - (1 - u) ** 2, "inout": u * u * (3 - 2 * u)}.get(kind, u)


def shot_params(s: GTShot, k: int) -> tuple[float, float, float]:
    """(zoom, cx, cy) no frame local k (k pode sair do plano: cross-fade)."""
    u = ease(k / max(s.frames - 1, 1), s.ease)
    z = s.zoom[0] + (s.zoom[1] - s.zoom[0]) * u
    cx = s.center[0][0] + (s.center[1][0] - s.center[0][0]) * u
    cy = s.center[0][1] + (s.center[1][1] - s.center[0][1]) * u
    return z, cx, cy


def src_index(s: GTShot, k: int, src_fps: float, out_fps: float) -> int:
    """Frame inteiro da fonte mostrado no frame local k (hold, como um editor sem interpolação)."""
    return int(math.floor(s.src_frame + k * s.speed * src_fps / out_fps + 1e-6))


def shot_matrix(s: GTShot, k: int, src: Source, out_size: tuple[int, int]) -> np.ndarray:
    """Matriz fonte->saída (2x3): enquadramento 'cover' * zoom, centrado em (cx, cy) da fonte."""
    w, h = out_size
    z, cx, cy = shot_params(s, k)
    k_ = max(w / src.w, h / src.h) * z
    return np.array([[k_, 0, w / 2 - k_ * cx * src.w], [0, k_, h / 2 - k_ * cy * src.h]], np.float64)


def validate(edit: GTEdit, sources: list[Source]) -> list[str]:
    """Erros de construção: recorte fora da fonte, instantes fora do ficheiro."""
    errs = []
    w, h = edit.size
    for i, s in enumerate(edit.shots):
        src = sources[s.source]
        for k in (-8, 0, s.frames - 1, s.frames + 8):
            z, cx, cy = shot_params(s, k)
            k_ = max(w / src.w, h / src.h) * z
            hw, hh = w / (2 * k_), h / (2 * k_)
            if cx * src.w - hw < -0.5 or cx * src.w + hw > src.w + 0.5 or cy * src.h - hh < -0.5 or cy * src.h + hh > src.h + 0.5:
                errs.append(f"plano {i}: recorte sai da fonte (k={k}, z={z:.2f}, c=({cx:.2f},{cy:.2f}))")
                break
        lo, hi = src_index(s, -8, src.fps, edit.fps), src_index(s, s.frames + 8, src.fps, edit.fps)
        if lo < 0 or hi >= src.n_frames:
            errs.append(f"plano {i}: frames da fonte fora do ficheiro ({lo}..{hi} de {src.n_frames})")
    return errs


def apply_color(img: np.ndarray, c: tuple[float, float, float], grade=(1.0, 1.0, 1.0)) -> np.ndarray:
    b, ct, sat = c
    x = img.astype(np.float32) / 255.0
    g = x.mean(axis=2, keepdims=True)
    x = g + (x - g) * sat
    x = (x - 0.5) * ct + 0.5 + b
    x = x * np.asarray(grade, np.float32)
    return np.clip(x * 255.0 + 0.5, 0, 255).astype(np.uint8)


def radial_blur(img: np.ndarray, amt: float) -> np.ndarray:
    if amt <= 0.01:
        return img
    h, w = img.shape[:2]
    acc = img.astype(np.float32)
    for i in range(1, 6):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), 0, 1.0 + 0.05 * amt * i)
        acc += cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REFLECT).astype(np.float32)
    return (acc / 6).astype(np.uint8)


def transition_weight(tr: GTTransition, cut: int, f: int) -> float | None:
    """Envelope 0..1 (pico no corte) da transição no frame f, ou None fora dela."""
    if tr.type == "cut" or tr.frames <= 0:
        return None
    a = cut - tr.frames // 2
    if not (a <= f < a + tr.frames):
        return None
    return max(0.0, 1.0 - abs((f + 0.5) - cut) / max(tr.frames / 2, 1))


def frame_at(edit: GTEdit, sources: list[Source], f: int) -> np.ndarray:
    starts = [0] + edit.cut_frames
    w, h = edit.size

    def shot_img(si: int, k: int) -> np.ndarray:
        s = edit.shots[si]
        src = sources[s.source]
        return apply_color(src.frame(src_index(s, k, src.fps, edit.fps), shot_matrix(s, k, src, (w, h)), (w, h)), s.color, edit.grade)

    si = max(i for i, st in enumerate(starts) if st <= f)
    img = shot_img(si, f - starts[si])
    for j, tr in enumerate(edit.transitions):
        cut = edit.cut_frames[j]
        env = transition_weight(tr, cut, f)
        if env is None:
            continue
        if tr.type == "flash":
            img = cv2.addWeighted(img, 1 - env, np.full_like(img, 255), env, 0)
        elif tr.type == "dip_black":
            img = (img.astype(np.float32) * (1 - env)).astype(np.uint8)
        elif tr.type == "zoom_blur":
            img = radial_blur(img, env * 3)
        elif tr.type == "crossfade":
            a = cut - tr.frames // 2
            alpha = (f + 0.5 - a) / tr.frames
            A = shot_img(j, f - starts[j])
            B = shot_img(j + 1, f - starts[j + 1])
            img = cv2.addWeighted(A, 1 - alpha, B, alpha, 0)
    return img


def ground_truth(edit: GTEdit, sources: list[Source]) -> dict:
    """Verdade por plano, nas convenções do analisador: zoom em ln(escala) (>0 = aproxima),
    pan em fração da LARGURA nos dois eixos, como signals._fit_affine_flow (+ = o CONTEÚDO move
    para a direita/baixo)."""
    w, h = edit.size
    shots = []
    starts = [0] + edit.cut_frames
    for i, s in enumerate(edit.shots):
        src = sources[s.source]
        per = []
        for k in range(s.frames):
            M = shot_matrix(s, k, src, (w, h))
            ox, oy = src.offset(src_index(s, k, src.fps, edit.fps))
            per.append((M[0, 0], M[0, 2] - M[0, 0] * ox, M[1, 2] - M[1, 1] * oy))  # tela -> saída: escala, tx, ty
        zs = np.array([p[0] for p in per])
        # trajetória do ponto da tela que está no centro da saída no frame 0 do plano
        cx0 = ((w / 2) - per[0][1]) / per[0][0]
        cy0 = ((h / 2) - per[0][2]) / per[0][0]
        xs = np.array([p[0] * cx0 + p[1] for p in per]) / w
        ys = np.array([p[0] * cy0 + p[2] for p in per]) / w
        shots.append({
            "index": i, "start_frame": starts[i], "end_frame": starts[i] + s.frames, "source": s.source,
            "src_frame": s.src_frame, "speed": s.speed, "zoom": list(s.zoom), "center": [list(c) for c in s.center],
            "ease": s.ease, "color": list(s.color),
            "zoom_ln_total": float(math.log(zs[-1] / zs[0])),
            "pan_x_total": float(xs[-1] - xs[0]), "pan_y_total": float(ys[-1] - ys[0]),
            "zoom_ln_curve": [float(v) for v in np.log(zs / zs[0])],
            "traj": [[float(p[0]), float(p[1]), float(p[2])] for p in per],  # tela->saída por frame: escala, tx, ty (px)
        })
    return {
        "name": edit.name, "style": edit.style, "fps": edit.fps, "size": list(edit.size), "n_frames": edit.n_frames,
        "duration_s": edit.duration_s, "cuts": edit.cut_frames, "cut_times_s": [c / edit.fps for c in edit.cut_frames],
        "shots": shots, "transitions": [asdict(t) for t in edit.transitions], "bpm": edit.bpm,
        "beats_s": ([k * 60.0 / edit.bpm for k in range(int(edit.duration_s * edit.bpm / 60.0) + 1)] if edit.bpm else []),
        "grade": list(edit.grade), "notes": edit.notes,
        "sources": [{"seed": s.spec.seed, "size": list(s.spec.size), "drift": list(s.spec.drift), "fps": s.fps,
                     "seconds": s.spec.seconds} for s in sources],
    }


def render_reference(edit: GTEdit, sources: list[Source], path: str, audio_path: str | None = None) -> dict:
    errs = validate(edit, sources)
    if errs:
        raise ValueError("; ".join(errs))
    w, h = edit.size
    _encode(path, (frame_at(edit, sources, f) for f in range(edit.n_frames)), w, h, edit.fps, crf=14, audio=audio_path)
    gt = ground_truth(edit, sources)
    with open(os.path.splitext(path)[0] + ".gt.json", "w", encoding="utf-8") as fh:
        json.dump(gt, fh, indent=1)
    return gt


def click_track(path: str, seconds: float, bpm: float, sr: int = 44100) -> str:
    """Áudio com batidas (cliques graves) em 0, 60/bpm, ... — os cortes da referência caem nelas."""
    t = np.arange(int(seconds * sr)) / sr
    y = 0.06 * np.sin(2 * np.pi * 220 * t)
    period = 60.0 / bpm
    for k in range(int(seconds / period) + 1):
        t0 = k * period
        m = (t >= t0) & (t < t0 + 0.15)
        tt = t[m] - t0
        y[m] += 0.85 * np.sin(2 * np.pi * (70 + 260 * np.exp(-tt * 40)) * tt) * np.exp(-tt * 22)
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path
