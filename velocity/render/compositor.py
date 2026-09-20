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


def _fit_mask(mask: np.ndarray, W: int, H: int) -> np.ndarray:
    """Máscara (192 px, aspeto da fonte) -> mesmo cover-crop central que o decode: [H,W] float 0..1."""
    mh, mw = mask.shape
    src_ar = mw / mh
    if src_ar > W / H:
        nh, nw = H, int(round(H * src_ar))
        r = cv2.resize(mask, (nw, nh), interpolation=cv2.INTER_LINEAR)
        x0 = (nw - W) // 2
        r = r[:, x0:x0 + W]
    else:
        nw, nh = W, int(round(W / src_ar))
        r = cv2.resize(mask, (nw, nh), interpolation=cv2.INTER_LINEAR)
        y0 = (nh - H) // 2
        r = r[y0:y0 + H, :]
    return r.astype(np.float32) / 255.0


def _sharpen_luma(img: np.ndarray, amount: float) -> np.ndarray:
    """Unsharp só na luminância e só onde há contorno (o ruído em zonas planas não é amplificado)."""
    ycc = cv2.cvtColor(img, cv2.COLOR_RGB2YCrCb)
    y = ycc[..., 0].astype(np.float32)
    sigma = max(0.8, 1.1 * img.shape[1] / 540.0)
    detail = y - cv2.GaussianBlur(y, (0, 0), sigma)
    w = np.clip(np.abs(detail) / 8.0, 0.0, 1.0)  # suprime micro-variações (ruído/blocos)
    ycc[..., 0] = np.clip(y + amount * detail * w, 0, 255).astype(np.uint8)
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)


def _enhance(out: np.ndarray, alpha: np.ndarray, clip: ClipUse) -> np.ndarray:
    """Melhoria LOCAL: sujeito e fundo recebem tratamentos diferentes, misturados pela máscara suave."""
    x = np.arange(256) / 255.0
    lut_s = (255.0 * x ** clip.subj_gamma).clip(0, 255).astype(np.uint8)
    lut_b = (255.0 * x ** clip.bg_gamma).clip(0, 255).astype(np.uint8)
    subj = cv2.LUT(out, lut_s)
    if clip.subj_sharp > 0:
        blur = cv2.GaussianBlur(subj, (0, 0), 1.3)
        subj = cv2.addWeighted(subj, 1.0 + clip.subj_sharp, blur, -clip.subj_sharp, 0)
    a = alpha[..., None]
    return (a * subj.astype(np.float32) + (1.0 - a) * cv2.LUT(out, lut_b).astype(np.float32)).clip(0, 255).astype(np.uint8)


def _render_shot(shot: TimelineShot, tl: Timeline, masks: dict | None = None):
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

    mask_arr = (masks or {}).get(clip.source_path)
    use_mask = mask_arr is not None and (abs(clip.subj_gamma - 1.0) > 0.02 or abs(clip.bg_gamma - 1.0) > 0.02 or clip.subj_sharp > 0)
    i_src0 = int(round(clip.src_start_s * fps))
    mask_win = {}
    lut = None
    if abs(clip.tone_gamma - 1.0) > 0.02:
        lut = (255.0 * (np.arange(256) / 255.0) ** clip.tone_gamma).clip(0, 255).astype(np.uint8)

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
        if use_mask:
            k = min(i_src0 + i0, len(mask_arr) - 1)
            if k not in mask_win:
                mask_win[k] = _fit_mask(mask_arr[k], W, H)
            al = cv2.warpAffine(mask_win[k], m, (tl.width, tl.height), flags=cv2.INTER_LINEAR)
            er = max(3, int(0.012 * tl.width) | 1)  # erosão: a máscara do modelo é generosa e apanha céu (contorno claro)
            al = cv2.erode(al, np.ones((er, er), np.uint8))
            al = cv2.GaussianBlur(al, (0, 0), max(1.0, 0.008 * tl.width))
            if clip.flip:
                al = al[:, ::-1]
            out = _enhance(np.ascontiguousarray(out), np.ascontiguousarray(al), clip)
        if lut is not None:
            out = cv2.LUT(np.ascontiguousarray(out), lut)
        if abs(clip.tone_sat - 1.0) > 0.02:
            gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
            out = cv2.addWeighted(cv2.merge([gray, gray, gray]), 1.0 - clip.tone_sat, out, clip.tone_sat, 0)
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


def render(tl: Timeline, out_path: str, crf: int = 18, progress=None, masks: dict | None = None) -> str:
    duration = tl.n_frames / tl.fps
    w = _Writer(out_path, tl.width, tl.height, tl.fps, tl.music_path, tl.music_offset_s, duration, crf)
    all_trans = [s.transition_in for s in tl.shots if s.transition_in is not None]
    post_lut = None
    if abs(tl.post_gamma - 1.0) > 0.01 or abs(tl.post_contrast - 1.0) > 0.01:
        x = 255.0 * (np.arange(256) / 255.0) ** tl.post_gamma
        post_lut = (tl.post_pivot + (x - tl.post_pivot) * tl.post_contrast).clip(0, 255).astype(np.uint8)
    written = 0
    try:
        for si, shot in enumerate(tl.shots):
            for f, img in _render_shot(shot, tl, masks):
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
                if post_lut is not None:
                    img = cv2.LUT(img, post_lut)
                if tl.post_sharp > 0.02:
                    img = _sharpen_luma(img, tl.post_sharp)
                if abs(tl.post_sat - 1.0) > 0.02:
                    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                    img = cv2.addWeighted(cv2.merge([g, g, g]), 1.0 - tl.post_sat, img, tl.post_sat, 0)
                w.write(img)
                written += 1
            if progress:
                progress(si + 1, len(tl.shots))
    finally:
        w.close()
    if written != tl.n_frames:
        raise RuntimeError(f"timeline incoerente: escreveu {written} frames, esperava {tl.n_frames}")
    return out_path
