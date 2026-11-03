"""Mistura final de áudio: faixa principal (música/referência) + som ORIGINAL de cada plano.

O vídeo vem já codificado (sem áudio) e não é recodificado. Cada plano com `audio_volume > 0`
contribui com o trecho da fonte que está a mostrar, no instante em que aparece, à mesma
velocidade; por isso o som continua alinhado à imagem depois de qualquer corte ou troca de plano.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from velocity.io.probe import probe
from velocity.schemas.timeline import Timeline, TimelineShot

FADE_S = 0.01  # sem clique nos pontos de corte do som


def needs_mix(tl: Timeline) -> bool:
    return any(s.clip.audio_volume > 0 for s in tl.shots)


def _mean_speed(shot: TimelineShot) -> float:
    c = shot.clip.speed
    if len(c.t) < 2:
        return float(c.v[0]) if c.v else 1.0
    area = sum((c.t[k + 1] - c.t[k]) * (c.v[k + 1] + c.v[k]) / 2 for k in range(len(c.t) - 1))
    span = c.t[-1] - c.t[0]
    return area / span if span > 0 else 1.0


def _atempo(speed: float) -> str:
    """Encadeia atempo (cada filtro só aceita 0.5..2)."""
    parts = []
    s = max(speed, 0.0625)
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-3:
        parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def _shot_window(shot: TimelineShot, fps: float) -> tuple[float, float]:
    """(início na fonte, velocidade média) do que o plano mostra."""
    ex = shot.clip.exact
    if ex is not None and len(ex.src_frames) > 1:
        t0 = ex.src_frames[0] / ex.src_fps
        span = (ex.src_frames[-1] - ex.src_frames[0]) / ex.src_fps
        return t0, max(abs(span) / max((shot.n_frames - 1) / fps, 1e-3), 0.05)
    return shot.clip.src_start_s, _mean_speed(shot)


def mix_audio(video: str, tl: Timeline, out_path: str) -> str:
    duration = tl.n_frames / tl.fps
    inputs = ["-i", video]
    n_in = 1
    parts: list[str] = []
    labels: list[str] = []
    if tl.music_path:
        inputs += ["-stream_loop", "-1", "-ss", f"{tl.music_offset_s:.3f}", "-i", tl.music_path]
        parts.append(f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,volume={max(0.0, min(tl.music_volume, 2.0)):.3f},"
                     f"afade=t=out:st={max(duration - 0.4, 0):.3f}:d=0.4[bed]")
        labels.append("[bed]")
        n_in += 1
    idx: dict[str, int] = {}
    for k, sh in enumerate(tl.shots):
        vol = sh.clip.audio_volume
        path = sh.clip.source_path
        if vol <= 0 or not probe(path).has_audio:
            continue
        if path not in idx:
            idx[path] = n_in
            inputs += ["-i", path]
            n_in += 1
        t0, speed = _shot_window(sh, tl.fps)
        d_out = sh.n_frames / tl.fps
        tempo = _atempo(speed)
        chain = [f"atrim=start={t0:.4f}:end={t0 + d_out * speed:.4f}", "asetpts=PTS-STARTPTS", "aresample=48000",
                 "aformat=channel_layouts=stereo"]
        if tempo:
            chain.append(tempo)
        ms = int(round(sh.start_frame / tl.fps * 1000))
        chain += [f"afade=t=in:d={FADE_S}", f"afade=t=out:st={max(d_out - FADE_S, 0):.4f}:d={FADE_S}",
                  f"adelay={ms}|{ms}", f"volume={min(vol, 2.0):.3f}"]
        parts.append(f"[{idx[path]}:a]" + ",".join(chain) + f"[c{k}]")
        labels.append(f"[c{k}]")
    if not labels:  # só clips sem faixa de áudio: o vídeo sai como entrou
        os.replace(video, out_path)
        return out_path
    parts.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                 f"apad=whole_dur={duration:.4f},atrim=0:{duration:.4f}[a]")
    graph = ";".join(parts)
    # o grafo cresce com o nº de planos: num ficheiro, para não bater no limite da linha de comandos
    fd, script = tempfile.mkstemp(suffix=".ffgraph")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(graph)
        cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-/filter_complex", script, "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"mistura de áudio falhou: {r.stderr[-500:].decode(errors='replace')}")
    finally:
        os.unlink(script)
    return out_path
