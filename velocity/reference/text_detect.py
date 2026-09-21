"""Deteção de texto criativo na referência (OCR), sem marcas de água.

Regras anti-marca-de-água: nomes de marcas/IDs conhecidos; texto pequeno junto às margens; texto que está
presente em >85% do vídeo na mesma posição E é pequeno. Devolve eventos {quando, onde, tamanho, entrada}.
O conteúdo do texto NUNCA é usado depois: o que interessa é a linguagem (timing, posição, tamanho, animação).
"""
from __future__ import annotations

import os
import re

import numpy as np

from velocity.io.frames import load_frames
from velocity.schemas.edit_dna import TextEvent

_BRAND = re.compile(r"capcut|tiktok|instagram|reels|youtube|@|\bid\b|kinemaster|inshot|vn\b|\.com|www", re.I)
_reader = None


def available() -> bool:
    if os.environ.get("VELOCITY_NO_TEXT"):
        return False
    try:
        import easyocr  # noqa: F401
        return True
    except Exception:
        return False


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["en", "pt"], gpu=True, verbose=False)
    return _reader


def detect_text(path: str, beats: list[float], stride: int = 4, width: int = 480) -> list[TextEvent]:
    if not available():
        return []
    fr = load_frames(path, fps=30.0, width=width)
    rd = _get_reader()
    H, W = fr.rgb.shape[1], fr.rgb.shape[2]
    obs: dict[int, list[dict]] = {}
    for i in range(0, fr.n, stride):
        for box, txt, conf in rd.readtext(fr.rgb[i], paragraph=False):
            t = txt.strip()
            if conf < 0.45 or len(t) < 3 or _BRAND.search(t):
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            bw, bh = (max(xs) - min(xs)) / W, (max(ys) - min(ys)) / H
            cx, cy = (max(xs) + min(xs)) / 2 / W, (max(ys) + min(ys)) / 2 / H
            small_edge = bh < 0.035 and (cy < 0.09 or cy > 0.92 or cx < 0.12 or cx > 0.88)
            if small_edge or bh < 0.012:
                continue  # marca de água / interface
            obs.setdefault(i, []).append({"cx": cx, "cy": cy, "w": bw, "h": bh, "t": t, "c": float(conf)})
    # agrupa observações consecutivas na mesma zona em eventos
    events: list[dict] = []
    for i in sorted(obs):
        for o in obs[i]:
            for e in events:
                if i - e["last"] <= stride * 3 and abs(e["cx"] - o["cx"]) < 0.25 and abs(e["cy"] - o["cy"]) < 0.15:
                    e["obs"].append((i, o))
                    e["last"] = i
                    e["cx"], e["cy"] = o["cx"], o["cy"]
                    break
            else:
                events.append({"cx": o["cx"], "cy": o["cy"], "last": i, "obs": [(i, o)]})
    out: list[TextEvent] = []
    beat_frames = np.array([b * 30.0 for b in beats]) if beats else np.array([])
    for e in events:
        ob = e["obs"]
        if len(ob) < 2:
            continue
        i0, i1 = ob[0][0], ob[-1][0] + stride
        coverage = (i1 - i0) / max(fr.n, 1)
        hs = np.array([o["h"] for _, o in ob])
        if coverage > 0.85 and float(np.median(hs)) < 0.05:
            continue  # texto pequeno e permanente = marca de água
        # texto SOBREPOSTO fica parado no ecrã; texto da CENA (patrocinadores, letras em roupa) move-se com a câmara/sujeito
        pos_std = float(np.hypot(np.std([o["cx"] for _, o in ob]), np.std([o["cy"] for _, o in ob])))
        dur_s = (i1 - i0) / 30.0
        if pos_std > 0.02 or dur_s < 0.5 or len(ob) < 3:
            continue
        first = hs[: max(2, len(hs) // 4)]
        entry = "pop" if len(hs) >= 4 and first[0] < 0.75 * float(np.median(hs)) else "none"
        cy = float(np.mean([o["cy"] for _, o in ob]))
        out.append(TextEvent(
            start_frame=int(i0), end_frame=int(min(i1, fr.n)), x=round(float(np.mean([o["cx"] for _, o in ob])), 3), y=round(cy, 3),
            width=round(float(np.mean([o["w"] for _, o in ob])), 3), height=round(float(np.median(hs)), 3),
            position="top" if cy < 0.33 else ("bottom" if cy > 0.66 else "center"), entry=entry,
            on_beat=bool(np.min(np.abs(beat_frames - i0)) <= 2.5) if len(beat_frames) else None,
            n_chars=int(np.mean([len(o["t"]) for _, o in ob])), confidence=round(float(np.mean([o["c"] for _, o in ob])), 2)))
    return out
