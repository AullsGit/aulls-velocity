"""ffprobe -> VideoInfo. Único sítio do projeto que fala com o ffprobe."""
from __future__ import annotations

import json
import subprocess

from velocity.schemas.media import VideoInfo


def _frac(s: str | None) -> float:
    if not s or s == "0/0":
        return 0.0
    if "/" in s:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else 0.0
    return float(s)


def probe(path: str) -> VideoInfo:
    cmd = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe falhou para {path}: {r.stderr[-500:]}")
    data = json.loads(r.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError(f"Sem stream de vídeo em {path}")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    w, h = int(v["width"]), int(v["height"])
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = int(v.get("tags", {}).get("rotate", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        w, h = h, w

    fps_nominal = _frac(v.get("r_frame_rate"))
    fps_avg = _frac(v.get("avg_frame_rate")) or fps_nominal
    # VFR: r_frame_rate (máximo) e avg_frame_rate divergem, ou não são inteiros "redondos".
    is_vfr = fps_nominal > 0 and abs(fps_nominal - fps_avg) / fps_nominal > 0.01

    duration = float(v.get("duration") or data.get("format", {}).get("duration") or 0.0)
    nb = v.get("nb_frames")
    return VideoInfo(
        path=path, width=w, height=h, duration=duration,
        fps_avg=fps_avg, fps_nominal=fps_nominal, is_vfr=is_vfr,
        n_frames_declared=int(nb) if nb and str(nb).isdigit() else None,
        codec=v.get("codec_name"),
        has_audio=a is not None,
        audio_sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
    )


def has_audio_stream(path: str) -> bool:
    """True se o ficheiro (vídeo OU só-áudio) tem uma faixa de áudio."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
                        "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
    return r.returncode == 0 and "audio" in r.stdout
