"""Decode único e normalizado de vídeo -> array numpy, com cache em disco.

Decisão de design: todas as análises (cortes, movimento, velocidade, cor, matching)
trabalham sobre os MESMOS frames, decodificados UMA vez, a FPS constante. Isto
resolve três problemas do MVP: (1) seek impreciso do OpenCV, (2) reabrir o vídeo
a cada frame, (3) vídeos VFR (como os do WhatsApp) com timestamps inconsistentes.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass

import numpy as np

from velocity.io.probe import has_audio_stream, probe

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bench", "cache")


@dataclass
class Frames:
    rgb: np.ndarray  # [N, H, W, 3] uint8
    fps: float

    @property
    def n(self) -> int:
        return self.rgb.shape[0]

    @property
    def duration(self) -> float:
        return self.n / self.fps

    def time_of(self, i: int) -> float:
        return i / self.fps

    def gray(self) -> np.ndarray:
        """Luma [N,H,W] float32 0-255 (BT.601)."""
        r, g, b = self.rgb[..., 0], self.rgb[..., 1], self.rgb[..., 2]
        return (0.299 * r + 0.587 * g + 0.114 * b).astype(np.float32)


def _cache_key(path: str, fps: float, width: int) -> str:
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{fps}|{width}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_frames(path: str, fps: float = 30.0, width: int = 192, use_cache: bool = True) -> Frames:
    """Decodifica `path` a `fps` constantes, largura `width` (altura proporcional, par).

    O filtro `fps` do FFmpeg duplica/descarta frames para normalizar VFR -> CFR.
    """
    key = _cache_key(path, fps, width)
    cache_path = os.path.join(CACHE_DIR, f"frames_{key}.npy")
    if use_cache and os.path.exists(cache_path):
        return Frames(rgb=np.load(cache_path), fps=fps)

    info = probe(path)
    height = int(round(width / info.aspect / 2)) * 2
    cmd = [
        "ffmpeg", "-v", "error", "-i", path,
        "-an", "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=rgb24",
        "-f", "rawvideo", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg decode falhou para {path}: {r.stderr[-500:].decode(errors='replace')}")
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    frame_size = width * height * 3
    n = buf.size // frame_size
    if n == 0:
        raise RuntimeError(f"Nenhum frame decodificado de {path}")
    rgb = buf[: n * frame_size].reshape(n, height, width, 3).copy()
    if use_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        np.save(cache_path, rgb)
    return Frames(rgb=rgb, fps=fps)


def load_audio(path: str, sr: int = 22050) -> np.ndarray | None:
    """Áudio mono float32 [-1,1] via pipe (sem ficheiros temporários). None se não houver áudio."""
    if not has_audio_stream(path):
        return None
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg áudio falhou para {path}: {r.stderr[-500:].decode(errors='replace')}")
    return np.frombuffer(r.stdout, dtype=np.float32).copy()
