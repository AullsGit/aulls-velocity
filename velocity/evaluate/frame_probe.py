"""Fontes com o NÚMERO DO FRAME desenhado na imagem, para medir a precisão temporal de qualquer
motor sem depender de OCR nem de outro analisador.

Cada frame tem uma grelha de 4x6 blocos brancos/pretos = 24 bits: 4 bits de id da fonte + 20 bits
do índice do frame. Blocos grandes (sobrevivem a compressão, reamostragem e zoom moderado) e um
fundo com textura (para o encoder se comportar como em vídeo real). É a base dos testes T1/T2 da
prova técnica de motores (docs/AUDITORIA, secção 10) e do teste de regressão do compositor.
"""
from __future__ import annotations

import subprocess

import cv2
import numpy as np

COLS, ROWS = 4, 6
SRC_BITS, IDX_BITS = 4, 20
GRID = (0.15, 0.12, 0.85, 0.88)  # caixa da grelha (fração x0, y0, x1, y1): margem para recortes ligeiros


def _cells(w: int, h: int):
    x0, y0, x1, y1 = GRID[0] * w, GRID[1] * h, GRID[2] * w, GRID[3] * h
    cw, ch = (x1 - x0) / COLS, (y1 - y0) / ROWS
    for r in range(ROWS):
        for c in range(COLS):
            yield r * COLS + c, (x0 + c * cw, y0 + r * ch, cw, ch)


def draw(src_id: int, idx: int, w: int, h: int, seed: int = 0) -> np.ndarray:
    code = (src_id << IDX_BITS) | idx
    rng = np.random.default_rng(seed + idx)
    img = np.full((h, w, 3), 96, np.uint8)
    img[:] += rng.integers(0, 40, (h, w, 1), dtype=np.uint8)  # textura
    for bit, (x, y, cw, ch) in _cells(w, h):
        on = (code >> bit) & 1
        a, b = (int(x + cw * 0.12), int(y + ch * 0.12)), (int(x + cw * 0.88), int(y + ch * 0.88))
        cv2.rectangle(img, a, b, (250, 250, 250) if on else (8, 8, 8), -1)
    return img


def read(img: np.ndarray) -> tuple[int, int]:
    """(id da fonte, índice do frame) lidos da imagem (o centro de cada bloco)."""
    h, w = img.shape[:2]
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    code = 0
    for bit, (x, y, cw, ch) in _cells(w, h):
        patch = g[int(y + ch * 0.35):int(y + ch * 0.65), int(x + cw * 0.35):int(x + cw * 0.65)]
        if patch.size and float(patch.mean()) > 128:
            code |= 1 << bit
    return code >> IDX_BITS, code & ((1 << IDX_BITS) - 1)


def write_source(path: str, src_id: int, n: int, fps: float, size=(320, 568), drop: int = 0) -> str:
    """Fonte CFR a `fps`. Com `drop`=k>1, remove um frame em cada k (mantendo os instantes dos
    restantes): um ficheiro VFR como os de telemóvel, em que o índice desenhado continua a ser o
    da grelha CFR original."""
    w, h = size
    tmp = path if drop <= 1 else path + ".cfr.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
           "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "30", tmp]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(draw(src_id, i, w, h, seed=src_id * 7919).tobytes())
    p.stdin.close()
    err = p.stderr.read()
    if p.wait() != 0:
        raise RuntimeError(err.decode(errors="replace")[-300:])
    if drop > 1:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tmp, "-vf", f"select='not(eq(mod(n,{drop}),{drop - 1}))'",
                            "-fps_mode", "passthrough", "-c:v", "libx264", "-crf", "12", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", path], capture_output=True)
        import os
        os.remove(tmp)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    return path


def read_video(path: str) -> list[tuple[int, int]]:
    """(fonte, frame) de cada frame do vídeo, na ordem do ficheiro (sem reamostragem)."""
    from velocity.io.probe import probe
    info = probe(path)
    w, h = info.width, info.height
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-an", "-fps_mode", "passthrough", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    try:
        while True:
            buf = p.stdout.read(w * h)
            if len(buf) < w * h:
                break
            out.append(read(np.frombuffer(buf, np.uint8).reshape(h, w)))
    finally:
        p.stdout.close()
        p.wait()
    return out
