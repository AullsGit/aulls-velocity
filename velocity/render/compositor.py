"""Timeline -> vídeo. Compositor frame a frame + UMA codificação FFmpeg (sem xfade, sem drift).

Por plano: decodifica a janela fonte necessária (cover-crop ao tamanho de saída), mapeia
tempo de saída -> tempo fonte pela curva de velocidade (com mistura de frames em slow-mo),
aplica zoom/pan por keyframes e, no fim, o efeito da transição centrado no corte.
"""
from __future__ import annotations

import math
import subprocess

import cv2
import numpy as np

from velocity.io.probe import probe
from velocity.render import effects, fxstack
from velocity.schemas.edit_dna import Curve
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot


def _interp(curve: Curve, x: np.ndarray) -> np.ndarray:
    return np.interp(x, curve.t, curve.v)


def source_frames_needed(shot_frames: int, speed: Curve) -> float:
    """Frames fonte (a 30 fps fonte) consumidos por um plano de `shot_frames` frames de saída."""
    tau = (np.arange(shot_frames) + 0.5) / shot_frames
    return float(np.sum(_interp(speed, tau)))


def decode_window(path: str, t0: float, n_frames: int, w: int, h: int, cover_scale: float, fps: float) -> np.ndarray:
    """Frames [n,H',W',3] a `fps`, escalados para COBRIR (w*cs, h*cs) e cortados ao centro nesse tamanho."""
    info = probe(path)
    tw, th = int(round(w * cover_scale / 2)) * 2, int(round(h * cover_scale / 2)) * 2
    src_ar, dst_ar = info.aspect, tw / th
    if src_ar > dst_ar:
        vf = f"scale=-2:{th}:flags=bicubic,crop={tw}:{th}"
    else:
        vf = f"scale={tw}:-2:flags=bicubic,crop={tw}:{th}"
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(t0, 0):.4f}", "-i", path, "-an",
           "-vf", f"fps={fps},{vf},format=rgb24", "-frames:v", str(n_frames), "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"decode falhou {path}: {r.stderr[-300:].decode(errors='replace')}")
    buf = np.frombuffer(r.stdout, np.uint8)
    n = buf.size // (tw * th * 3)
    if n == 0:
        raise RuntimeError(f"janela vazia em {path} t0={t0}")
    return buf[: n * tw * th * 3].reshape(n, th, tw, 3)


def _render_shot(shot: TimelineShot, tl: Timeline):
    """Gera (frame_global, imagem) para cada frame do plano, ainda SEM efeito de transição."""
    clip: ClipUse = shot.clip
    L = shot.n_frames
    fps = tl.fps
    tau = (np.arange(L) + 0.5) / L
    rate = _interp(clip.speed, tau)
    cum = np.concatenate([[0.0], np.cumsum(rate)[:-1]])  # posição fonte (em frames) do início de cada frame de saída
    need = int(math.ceil(cum[-1] + rate[-1])) + 3

    zk = _interp(clip.zoom, tau) * tl.zoom_gain + fxstack.zoom_extra(shot.start_frame, L, tl.effects, tl.fx_scale)
    zk = np.maximum(zk, 0.0)  # ln(escala) ABSOLUTO >= 0 (1x = cover-fit)
    scale = np.exp(zk)
    max_scale = float(scale.max())
    cover = max(1.0, min(max_scale, 2.0))  # decodifica com margem p/ não desfocar em zoom-in
    frames = decode_window(clip.source_path, clip.src_start_s, need, tl.width, tl.height, cover, fps)
    n_src, H, W = frames.shape[0], frames.shape[1], frames.shape[2]

    for j in range(L):
        pos = min(cum[j], n_src - 1.0)
        i0 = int(math.floor(pos))
        a = pos - i0
        img = frames[i0]
        if rate[j] < 0.95 and a > 0.05 and i0 + 1 < n_src:  # slow-mo: mistura de frames
            img = cv2.addWeighted(frames[i0], 1 - a, frames[i0 + 1], a, 0)
        # região de recorte no frame decodificado, com tamanho (W/scale, H/scale) e centro (cx,cy)+pan
        rw, rh = W / scale[j], H / scale[j]
        cx = (clip.center[0] + clip.pan[0] * tau[j]) * W
        cy = (clip.center[1] + clip.pan[1] * tau[j]) * H
        sdx, sdy = fxstack.shake_offsets(shot.start_frame + j, tl.effects, W, tl.fx_scale)
        x0 = float(np.clip(cx - rw / 2 + sdx, 0, W - rw))
        y0 = float(np.clip(cy - rh / 2 + sdy, 0, H - rh))
        m = np.array([[tl.width / rw, 0, -x0 * tl.width / rw], [0, tl.height / rh, -y0 * tl.height / rh]], np.float32)
        out = cv2.warpAffine(img, m, (tl.width, tl.height), flags=cv2.INTER_LINEAR)
        if clip.flip:
            out = out[:, ::-1]
        yield shot.start_frame + j, np.ascontiguousarray(out)


class _Writer:
    def __init__(self, path: str, w: int, h: int, fps: float, music: str | None, music_offset: float, duration: float, crf: int):
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
        if music:
            cmd += ["-ss", f"{music_offset:.3f}", "-i", music]
        cmd += ["-map", "0:v"]
        if music:
            fade_start = max(duration - 0.4, 0)
            cmd += ["-map", "1:a", "-af", f"afade=t=out:st={fade_start:.3f}:d=0.4", "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.4f}"]
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, img: np.ndarray):
        self.p.stdin.write(img.tobytes())

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="replace")
        if self.p.wait() != 0:
            raise RuntimeError(f"ffmpeg encode falhou: {err[-600:]}")


def render(tl: Timeline, out_path: str, crf: int = 18, progress=None) -> str:
    duration = tl.n_frames / tl.fps
    w = _Writer(out_path, tl.width, tl.height, tl.fps, tl.music_path, tl.music_offset_s, duration, crf)
    all_trans = [s.transition_in for s in tl.shots if s.transition_in is not None]
    written = 0
    try:
        for si, shot in enumerate(tl.shots):
            for f, img in _render_shot(shot, tl):
                if tl.effects:
                    img = fxstack.post_fx(img, f, tl.effects, tl.fx_scale)
                # efeitos cuja janela contém f (transições e eventos de fx)
                for t in all_trans:
                    if t.start_frame <= f < t.end_frame:
                        img = effects.apply_transition(img, t, f)
                for ev in tl.fx_events:
                    if ev.start_frame <= f < ev.end_frame:
                        mid = (ev.start_frame + ev.end_frame) // 2
                        amt = effects.envelope(f, ev.start_frame, mid, ev.end_frame)
                        img = effects.apply(img, ev.type, amt, ev.intensity, "radial", f)
                w.write(img)
                written += 1
            if progress:
                progress(si + 1, len(tl.shots))
    finally:
        w.close()
    if written != tl.n_frames:
        raise RuntimeError(f"timeline incoerente: escreveu {written} frames, esperava {tl.n_frames}")
    return out_path
