"""Timeline -> vídeo. Compositor frame a frame + UMA codificação FFmpeg (sem xfade, sem drift).

Por plano: decodifica a janela fonte necessária (cover-crop ao tamanho de saída), mapeia
tempo de saída -> tempo fonte pela curva de velocidade (com mistura de frames em slow-mo),
aplica zoom/pan por keyframes e, no fim, o efeito da transição centrado no corte.

Planos com `clip.exact` (reconstrução do cenário A, ver matching/source_align.py) seguem outro
caminho: frame da fonte e transformação afim MEDIDOS para cada frame de saída, frame inteiro da
fonte decodificado à taxa nativa, e matriz de cor 3x4 medida.
"""
from __future__ import annotations

import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from velocity.io.frames import iter_range
from velocity.io.probe import probe
from velocity.render import effects, fxstack, sr as srmod, textfx
from velocity.schemas.edit_dna import Curve
from velocity.schemas.timeline import ClipUse, Timeline, TimelineShot


def _interp(curve: Curve, x: np.ndarray) -> np.ndarray:
    return np.interp(x, curve.t, curve.v)


def source_frames_needed(shot_frames: int, speed: Curve) -> float:
    """Frames fonte (a 30 fps fonte) consumidos por um plano de `shot_frames` frames de saída."""
    tau = (np.arange(shot_frames) + 0.5) / shot_frames
    return float(np.sum(_interp(speed, tau)))


def _even_floor(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def _window_size(info, w: int, h: int, cover_scale: float) -> tuple[int, int]:
    """(largura, altura) a que `decode_window` decodifica: cover de (w*cs, h*cs), sem ampliar a fonte."""
    tw, th = int(round(w * cover_scale / 2)) * 2, int(round(h * cover_scale / 2)) * 2
    dst_ar = tw / th
    if info.aspect > dst_ar:
        if th > info.height:
            th = _even_floor(info.height)
            tw = _even_floor(th * dst_ar)
    elif tw > info.width:
        tw = _even_floor(info.width)
        th = _even_floor(tw / dst_ar)
    return tw, th


def decode_window(path: str, t0: float, n_frames: int, w: int, h: int, cover_scale: float, fps: float, denoise: float = 0.0) -> np.ndarray:
    """Frames [n,H',W',3] a `fps`, com o MESMO enquadramento de um "cover" de (w*cs, h*cs)
    cortado ao centro, mas nunca maiores que a resolução nativa da fonte.

    Ampliar aqui não acrescenta detalhe nenhum (a fonte não o tem) e custava caro: com clips de
    telemóvel a 480x864 e saída 720x1280 com margem de zoom 2x, cada frame era ampliado para
    1440x2560 no ffmpeg só para o warpAffine o voltar a reduzir — ~9x mais píxeis descodificados
    e enviados pelo pipe. O campo de visão é o mesmo; quem amplia agora (uma única vez, bicúbico)
    é o warpAffine em `_render_shot`.
    """
    info = probe(path)
    tw, th = _window_size(info, w, h, cover_scale)
    if info.aspect > tw / th:
        vf = f"scale=-2:{th}:flags=bicubic,crop={tw}:{th}"
    else:
        vf = f"scale={tw}:-2:flags=bicubic,crop={tw}:{th}"
    dn = ""
    if denoise > 0.01:  # hqdn3d (espacial+temporal) à resolução NATIVA, antes de ampliar
        d = min(denoise, 1.0)
        dn = f"hqdn3d={1.6 * d:.2f}:{1.3 * d:.2f}:{3.0 * d:.2f}:{2.5 * d:.2f},"
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(t0, 0):.4f}", "-i", path, "-an",
           "-vf", f"fps={fps},{dn}{vf},format=rgb24", "-frames:v", str(n_frames), "-f", "rawvideo", "-"]
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
    out = y + amount * detail * w
    k = np.ones((5, 5), np.uint8)  # anti-halo: o resultado não pode exceder o mínimo/máximo local (sem overshoot nas arestas)
    out = np.minimum(np.maximum(out, cv2.erode(y, k)), cv2.dilate(y, k))
    ycc[..., 0] = np.clip(out, 0, 255).astype(np.uint8)
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


class _ShotPlan:
    """Tudo o que se calcula de um plano ANTES de decodificar (barato, sem I/O)."""

    __slots__ = ("L", "tau", "rate", "cum", "need", "scale", "max_scale", "cover", "ex_lo", "ex_size")

    def __init__(self, shot: TimelineShot, tl: Timeline):
        clip = shot.clip
        self.L = L = shot.n_frames
        self.ex_lo, self.ex_size = 0, None
        if clip.exact is not None:
            self._plan_exact(clip, tl)
            return
        self.tau = tau = (np.arange(L) + 0.5) / L
        self.rate = rate = _interp(clip.speed, tau)
        self.cum = cum = np.concatenate([[0.0], np.cumsum(rate)[:-1]])  # posição fonte (em frames) do início de cada frame de saída
        self.need = int(math.ceil(cum[-1] + rate[-1])) + 3
        zk = _interp(clip.zoom, tau) * tl.zoom_gain + fxstack.zoom_extra(shot.start_frame, L, tl.effects, tl.fx_scale)
        zk = np.maximum(zk, 0.0)  # ln(escala) ABSOLUTO >= 0 (1x = cover-fit)
        self.scale = np.exp(zk)
        self.max_scale = float(self.scale.max())
        self.cover = max(1.0, min(self.max_scale, 2.0))  # margem p/ não desfocar em zoom-in (limitada à resolução nativa)

    def _plan_exact(self, clip: ClipUse, tl: Timeline):
        ex = clip.exact
        if len(ex.src_frames) != self.L or len(ex.warps) != self.L:
            raise ValueError("exact: src_frames/warps têm de ter um valor por frame do plano")
        self.ex_lo = min(ex.src_frames)
        self.need = max(ex.src_frames) - self.ex_lo + 1
        info = probe(clip.source_path)
        # resolução de decode: a que dá >= 1 píxel de fonte por píxel de saída no recorte mais
        # apertado do plano, nunca acima da nativa (ampliar no decode não acrescenta detalhe)
        A = np.array(ex.warps, np.float64)
        per_out_px = np.hypot(A[:, 0], A[:, 3] / info.aspect)  # largura da saída em larguras de fonte
        wd = min(info.width, tl.width / max(float(per_out_px.min()), 1e-3))
        wd = max(2, int(math.ceil(wd / 2)) * 2)
        hd = max(2, int(round(wd / info.aspect / 2)) * 2)
        self.ex_size = (wd, hd)
        self.tau = self.rate = self.cum = None
        self.scale = np.ones(self.L)
        self.max_scale, self.cover = 1.0, 1.0


def _decode_shot(shot: TimelineShot, tl: Timeline, plan: _ShotPlan) -> np.ndarray:
    clip: ClipUse = shot.clip
    if clip.exact is not None:
        # só os frames que o plano mostra (com speed 2x ou ramps, metade ou menos da janela)
        wd, hd = plan.ex_size
        want = set(clip.exact.src_frames)
        return {j: f.copy() for j, f in enumerate(iter_range(clip.source_path, wd, clip.exact.src_fps, plan.ex_lo, plan.need,
                                                                 rgb=True, height=hd), start=plan.ex_lo) if j in want}
    if tl.sr and srmod.available():
        info = probe(clip.source_path)
        a = tl.width / tl.height
        nw, nh = (int(info.height * a) // 2 * 2, info.height // 2 * 2) if info.aspect > a else (info.width // 2 * 2, int(info.width / a) // 2 * 2)
        if tl.width * plan.max_scale / nw >= 1.5:  # só onde a ampliação é grande
            native = decode_window(clip.source_path, clip.src_start_s, plan.need, nw, nh, 1.0, tl.fps, tl.denoise)
            return srmod.enhance_native(native, 1.5, key=(clip.source_path, round(clip.src_start_s, 3), plan.need, tl.denoise))
    return decode_window(clip.source_path, clip.src_start_s, plan.need, tl.width, tl.height, plan.cover, tl.fps, tl.denoise)


def _render_shot(shot: TimelineShot, tl: Timeline, masks: dict | None = None,
                 plan: _ShotPlan | None = None, frames: np.ndarray | None = None):
    """Gera (frame_global, imagem) para cada frame do plano, ainda SEM efeito de transição.

    `frames` pode vir já decodificado (pré-busca paralela em `render`); senão decodifica aqui.
    """
    clip: ClipUse = shot.clip
    plan = plan or _ShotPlan(shot, tl)
    if clip.exact is not None:
        yield from _render_exact(shot, tl, plan, frames if frames is not None else _decode_shot(shot, tl, plan))
        return
    L, tau, rate, cum, scale = plan.L, plan.tau, plan.rate, plan.cum, plan.scale
    fps = tl.fps
    if frames is None:
        frames = _decode_shot(shot, tl, plan)
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
        sx, sy = tl.width / rw, tl.height / rh
        # +0.5*(s-1): mapeia CENTROS de pixel (o warpAffine mapeia índices); sem isto a imagem
        # desloca meio pixel da fonte, o que se nota mais agora que a fonte vem à resolução nativa.
        m = np.array([[sx, 0, -x0 * sx + 0.5 * (sx - 1)], [0, sy, -y0 * sy + 0.5 * (sy - 1)]], np.float32)
        # a ampliar (fonte decodificada abaixo da saída): bicúbico, a mesma qualidade que o
        # ffmpeg dava antes ao ampliar no decode; a reduzir, linear como sempre foi.
        interp = cv2.INTER_CUBIC if tl.width / rw > 1.001 else cv2.INTER_LINEAR
        out = cv2.warpAffine(img, m, (tl.width, tl.height), flags=interp)
        if clip.flip:
            out = out[:, ::-1]
        if use_mask:
            k = min(i_src0 + i0, len(mask_arr) - 1)
            if k not in mask_win:
                mask_win[k] = _fit_mask(mask_arr[k], W, H)
            al = cv2.warpAffine(mask_win[k], m, (tl.width, tl.height), flags=cv2.INTER_LINEAR)
            er = max(3, int(0.008 * tl.width) | 1)  # erosão leve: a máscara do modelo é generosa e apanha céu
            al = cv2.erode(al, np.ones((er, er), np.uint8))
            # filtro guiado: ajusta o alfa aos contornos REAIS da imagem (elimina o contorno claro/halo)
            guide = cv2.cvtColor(np.ascontiguousarray(out), cv2.COLOR_RGB2GRAY)
            try:
                al = cv2.ximgproc.guidedFilter(guide, np.ascontiguousarray(al, dtype=np.float32), max(4, int(0.012 * tl.width)), 60.0)
            except (AttributeError, cv2.error):  # OpenCV sem módulos contrib: degrada para suavização simples (sem crash)
                al = cv2.GaussianBlur(np.ascontiguousarray(al, dtype=np.float32), (0, 0), max(1.0, 0.008 * tl.width))
            al = np.clip(al, 0.0, 1.0)
            if clip.flip:
                al = al[:, ::-1]
            out = _enhance(np.ascontiguousarray(out), np.ascontiguousarray(al), clip)
        if lut is not None:
            out = cv2.LUT(np.ascontiguousarray(out), lut)
        if abs(clip.tone_sat - 1.0) > 0.02:
            gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
            out = cv2.addWeighted(cv2.merge([gray, gray, gray]), 1.0 - clip.tone_sat, out, clip.tone_sat, 0)
        yield shot.start_frame + j, np.ascontiguousarray(out)


def _render_exact(shot: TimelineShot, tl: Timeline, plan: _ShotPlan, frames: dict):
    """Cada frame de saída = frame MEDIDO da fonte, pela afim MEDIDA, com a cor MEDIDA.
    `frames`: frame da fonte -> imagem (só os usados)."""
    ex = shot.clip.exact
    if not frames:
        raise RuntimeError(f"fonte sem frames na janela pedida: {shot.clip.source_path}")
    H, W = plan.ex_size[1], plan.ex_size[0]
    have = sorted(frames)

    def norm(w, h):  # índice de píxel -> [0,1] (centro do píxel em (i+0.5)/w)
        return np.array([[1.0 / w, 0, 0.5 / w], [0, 1.0 / h, 0.5 / h], [0, 0, 1]], np.float64)

    n_out, n_dec_inv = norm(tl.width, tl.height), np.linalg.inv(norm(W, H))
    cm = np.array(ex.color, np.float32) if ex.color else None
    for j in range(plan.L):
        jj = ex.src_frames[j]
        img = frames.get(jj)
        if img is None:  # ficheiro mais curto do que o medido: o frame disponível mais próximo
            img = frames[min(have, key=lambda x: abs(x - jj))]
        A = np.vstack([np.array(ex.warps[j], np.float64).reshape(2, 3), [0, 0, 1]])
        M = (n_dec_inv @ A @ n_out)[:2].astype(np.float32)  # píxel de saída -> píxel decodificado
        up = 1.0 / max(float(np.hypot(M[0, 0], M[1, 0])), 1e-6) > 1.001
        out = cv2.warpAffine(img, M, (tl.width, tl.height), flags=(cv2.INTER_CUBIC if up else cv2.INTER_LINEAR) | cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_REFLECT101)
        if cm is not None:
            out = cv2.transform(out, cm)
        if shot.clip.flip:
            out = out[:, ::-1]
        yield shot.start_frame + j, np.ascontiguousarray(out)


class _Writer:
    def __init__(self, path: str, w: int, h: int, fps: float, music: str | None, music_offset: float, duration: float, crf: int,
                 preset: str = "medium", volume: float = 1.0):
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
        if music:
            # -stream_loop -1: cobre o caso de o vídeo (agora até ~30s, ver matching/extend.py) ser
            # mais longo que a faixa de áudio disponível — sem isto a 2ª metade saía muda.
            cmd += ["-stream_loop", "-1", "-ss", f"{music_offset:.3f}", "-i", music]
        cmd += ["-map", "0:v"]
        if music:
            fade_start = max(duration - 0.4, 0)
            af = f"afade=t=out:st={fade_start:.3f}:d=0.4"
            if abs(volume - 1.0) > 1e-3:
                af = f"volume={max(0.0, min(volume, 2.0)):.3f}," + af
            cmd += ["-map", "1:a", "-af", af, "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.4f}"]
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p", "-movflags", "+faststart", path]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, img: np.ndarray):
        self.p.stdin.write(img.tobytes())

    def close(self):
        self.p.stdin.close()
        err = self.p.stderr.read().decode(errors="replace")
        if self.p.wait() != 0:
            raise RuntimeError(f"ffmpeg encode falhou: {err[-600:]}")


DECODE_WORKERS = int(os.environ.get("VELOCITY_DECODE_WORKERS", str(min(4, os.cpu_count() or 2))))
DECODE_MEM_MB = int(os.environ.get("VELOCITY_DECODE_MEM_MB", "512"))


def _est_bytes(shot: TimelineShot, tl: Timeline, plan: _ShotPlan) -> int:
    if plan.ex_size is not None:
        return len(set(shot.clip.exact.src_frames)) * plan.ex_size[0] * plan.ex_size[1] * 3
    tw, th = _window_size(probe(shot.clip.source_path), tl.width, tl.height, plan.cover)
    return plan.need * tw * th * 3


def _prefetched_frames(tl: Timeline, plans: list[_ShotPlan]):
    """Itera os frames decodificados de cada plano, por ordem, decodificando os SEGUINTES em
    paralelo enquanto o atual é composto (a decodificação ffmpeg corre noutro processo e
    liberta o GIL, por isso há ganho real). Nunca tem mais de DECODE_MEM_MB em memória
    pendente — com clips 4K a pré-busca encolhe sozinha até 1 plano de cada vez.
    """
    n = len(tl.shots)
    if DECODE_WORKERS <= 1 or tl.sr:  # super-resolução usa um modelo partilhado: sem threads
        for si in range(n):
            yield _decode_shot(tl.shots[si], tl, plans[si])
        return
    budget = DECODE_MEM_MB * 1024 * 1024
    sizes = [_est_bytes(tl.shots[i], tl, plans[i]) for i in range(n)]
    ex = ThreadPoolExecutor(max_workers=DECODE_WORKERS)
    pending: dict[int, object] = {}
    nxt, inflight = 0, 0
    try:
        for si in range(n):
            while nxt < n and (nxt == si or (len(pending) < DECODE_WORKERS + 1 and inflight + sizes[nxt] <= budget)):
                pending[nxt] = ex.submit(_decode_shot, tl.shots[nxt], tl, plans[nxt])
                inflight += sizes[nxt]
                nxt += 1
            frames = pending.pop(si).result()
            inflight -= sizes[si]
            yield frames
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


def render(tl: Timeline, out_path: str, crf: int = 18, progress=None, masks: dict | None = None, preset: str = "medium") -> str:
    duration = tl.n_frames / tl.fps
    w = _Writer(out_path, tl.width, tl.height, tl.fps, tl.music_path, tl.music_offset_s, duration, crf, preset,
                volume=tl.music_volume)
    all_trans = [s.transition_in for s in tl.shots if s.transition_in is not None]
    post_lut = None
    if abs(tl.post_gamma - 1.0) > 0.01 or abs(tl.post_contrast - 1.0) > 0.01:
        x = 255.0 * (np.arange(256) / 255.0) ** tl.post_gamma
        post_lut = (tl.post_pivot + (x - tl.post_pivot) * tl.post_contrast).clip(0, 255).astype(np.uint8)
    written = 0
    plans = [_ShotPlan(s, tl) for s in tl.shots]
    decoded = _prefetched_frames(tl, plans)
    try:
        for si, (shot, frames) in enumerate(zip(tl.shots, decoded)):
            for f, img in _render_shot(shot, tl, masks, plans[si], frames):
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
                for tx_ in tl.texts:
                    img = textfx.draw(img, tx_, f)
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
        decoded.close()
        w.close()
    if written != tl.n_frames:
        raise RuntimeError(f"timeline incoerente: escreveu {written} frames, esperava {tl.n_frames}")
    return out_path
