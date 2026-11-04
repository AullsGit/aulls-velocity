"""Legendas da referência (letra da música, karaoke): o QUE diz, QUANDO muda e COMO aparece.

Ao contrário de text_detect.py (que só guarda o timing/posição do texto criativo e descarta o
conteúdo), isto existe para copiar a legenda da referência para a edição quando o utilizador o
pede explicitamente: texto lido por OCR, segmentado ao frame, com posição, tamanho, cor do texto e
cor/opacidade da caixa MEDIDAS na referência.

Método:
  1. OCR (EasyOCR, pt+en) em todos os frames de uma faixa horizontal (por defeito o terço de baixo);
  2. só conta texto centrado horizontalmente e sobre uma caixa escura (a legenda), não texto da
     própria cena (patrocinadores, nomes em camisolas);
  3. frames consecutivos com o mesmo texto formam um segmento; leituras isoladas de 1-2 frames
     (OCR instável) são absorvidas pelos vizinhos; o texto de cada segmento é o mais lido.
"""
from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass

import numpy as np

from velocity.io.probe import probe


@dataclass
class Caption:
    start_frame: int  # frames nativos da referência
    end_frame: int  # exclusivo
    text: str
    cx: float  # centro da caixa (fração da largura)
    cy: float  # (fração da altura)
    text_h: float  # altura do texto (fração da LARGURA — o aspeto da edição pode ser outro)
    box_w: float  # largura da caixa (fração da largura)
    text_rgb: tuple[int, int, int]
    box_rgb: tuple[int, int, int]
    box_alpha: float
    confidence: float


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def extract_captions(path: str, band: tuple[float, float] = (0.70, 0.995), min_conf: float = 0.3,
                     log=lambda m: None, progress=None, stride: int = 2, require_box: bool = True) -> tuple[list[Caption], float]:
    """Devolve (legendas, fps nativo da referência). `stride`: lê 1 frame em cada N (a legenda dura
    décimos de segundo; ler todos custa o dobro e não muda os tempos em mais de 1 frame)."""
    import easyocr
    info = probe(path)
    W, H = info.width, info.height
    fps = float(info.fps_nominal or info.fps_avg or 30.0)
    y0 = int(band[0] * H) // 2 * 2
    bh = int((band[1] - band[0]) * H) // 2 * 2
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-fps_mode", "passthrough", "-vf",
                        f"crop={W}:{bh}:0:{y0}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    n = len(r.stdout) // (W * bh * 3)
    frames = np.frombuffer(r.stdout[: n * W * bh * 3], np.uint8).reshape(n, bh, W, 3)
    reader = easyocr.Reader(["pt", "en"], gpu=True, verbose=False)
    per: list[dict | None] = []
    for k in range(n):
        if progress and k % 30 == 0:
            progress("analyzing_reference", 0.05 + 0.9 * k / max(n, 1))
        if stride > 1 and k % stride:
            per.append(per[-1])
            continue
        img = frames[k]
        boxes = []
        for box, txt, conf in reader.readtext(img, paragraph=False):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            x0, x1, ya, yb = max(0, int(min(xs))), int(max(xs)), max(0, int(min(ys))), int(max(ys))
            if conf < min_conf or x1 - x0 < 4 or yb - ya < 4:
                continue
            boxes.append((x0, x1, ya, yb, _norm(txt), float(conf)))
        if not boxes:
            per.append(None)
            continue
        # uma linha de legenda: caixas na mesma altura, juntas da esquerda para a direita
        boxes.sort(key=lambda b: (round((b[2] + b[3]) / 2 / 8), b[0]))
        line_y = np.median([(b[2] + b[3]) / 2 for b in boxes])
        line = [b for b in boxes if abs((b[2] + b[3]) / 2 - line_y) < 0.6 * (b[3] - b[2])]
        if not line:
            line = [min(boxes, key=lambda b: abs((b[2] + b[3]) / 2 - line_y))]
        x0, x1 = min(b[0] for b in line), max(b[1] for b in line)
        ya, yb = min(b[2] for b in line), max(b[3] for b in line)
        cx = (x0 + x1) / 2 / W
        roi = img[ya:yb, x0:x1].reshape(-1, 3).astype(np.int32)
        lum = roi.max(axis=1)
        dark = float((lum < 90).mean())
        if not (0.3 <= cx <= 0.7) or (require_box and dark < 0.3):  # texto da cena, não legenda
            per.append(None)
            continue
        text = _norm(" ".join(b[4] for b in sorted(line, key=lambda b: b[0])))
        bright = roi[lum >= np.percentile(lum, 85)]
        darkpx = roi[lum < 90]
        per.append({"t": text, "x0": x0, "x1": x1, "ya": ya + y0, "yb": yb + y0, "conf": float(np.mean([b[5] for b in line])),
                    "text_rgb": tuple(int(v) for v in np.percentile(bright, 75, axis=0)),  # a mediana apanha o antialias
                    "bg": tuple(int(v) for v in np.median(darkpx, axis=0)) if len(darkpx) else (0, 0, 0)})
        if k % 100 == 0:
            log(f"legendas: frame {k}/{n}")
    # segmentos de texto igual; leituras isoladas (<=2 frames) absorvidas pelos vizinhos
    keys = [p["t"].lower() if p else "" for p in per]
    for k in range(1, n - 1):
        for span in (1, 2):
            if k + span < n and keys[k - 1] == keys[min(k + span, n - 1)] and keys[k] != keys[k - 1]:
                for j in range(k, min(k + span, n)):
                    keys[j] = keys[k - 1]
    caps: list[Caption] = []
    k = 0
    while k < n:
        j = k
        while j < n and keys[j] == keys[k]:
            j += 1
        if keys[k] and j - k >= 3:
            obs = [per[i] for i in range(k, j) if per[i]]
            text = Counter(o["t"] for o in obs).most_common(1)[0][0]
            x0 = np.median([o["x0"] for o in obs])
            x1 = np.median([o["x1"] for o in obs])
            ya = np.median([o["ya"] for o in obs])
            yb = np.median([o["yb"] for o in obs])
            bg = np.median([o["bg"] for o in obs], axis=0)
            # opacidade da caixa: quão escura fica em relação ao vídeo à volta (estimativa)
            caps.append(Caption(start_frame=k, end_frame=j, text=text, cx=float((x0 + x1) / 2 / W), cy=float((ya + yb) / 2 / H),
                                text_h=float((yb - ya) / W), box_w=float((x1 - x0) / W),
                                text_rgb=tuple(int(v) for v in np.median([o["text_rgb"] for o in obs], axis=0)),
                                box_rgb=tuple(int(v) for v in bg), box_alpha=0.65,
                                confidence=round(float(np.mean([o["conf"] for o in obs])), 3)))
        k = j
    return caps, fps


def clean_captions(caps: list[Caption], max_gap: int = 6) -> list[Caption]:
    """Tira o ruído do OCR mantendo a letra:
      * leituras fora da linha da legenda (texto da cena colado à legenda, caixa mal medida): ficam
        com o estilo de um vizinho bom se o texto (sem uma 1.ª palavra espúria) acaba no texto de um
        vizinho, ou é o início do texto do vizinho seguinte (karaoke a crescer); senão saem;
      * falhas curtas (<= max_gap frames) entre leituras: a legenda estava lá — o segmento anterior
        estende-se até ao seguinte, e segmentos iguais contíguos juntam-se."""
    if not caps:
        return []
    cy0 = float(np.median([c.cy for c in caps]))
    good = lambda c: abs(c.cx - 0.5) <= 0.04 and abs(c.cy - cy0) <= 0.03  # noqa: E731
    out: list[Caption] = []
    for i, c in enumerate(caps):
        if not good(c):
            prev = next((n for n in reversed(caps[:i]) if good(n)), None)
            nxt = next((n for n in caps[i + 1:] if good(n)), None)
            words = c.text.split()
            fixed = None
            for cand in (c.text, " ".join(words[1:])):
                if not cand:
                    continue
                low = cand.lower()
                end = next((n for n in (prev, nxt) if n and low.endswith(n.text.lower())), None)
                if end is not None:
                    fixed = (end, end.text)
                elif nxt and len(low.rstrip("-")) >= 3 and nxt.text.lower().startswith(low.rstrip("-")) and len(cand) < len(nxt.text):
                    fixed = (nxt, cand)
                if fixed:
                    break
            if fixed is None:
                continue
            c = Caption(**{**fixed[0].__dict__, "text": fixed[1], "start_frame": c.start_frame, "end_frame": c.end_frame})
        out.append(c)
    merged: list[Caption] = []
    for c in out:
        if merged and c.start_frame - merged[-1].end_frame <= max_gap:
            if c.text == merged[-1].text:
                merged[-1].end_frame = c.end_frame
                continue
            merged[-1].end_frame = c.start_frame
        merged.append(c)
    return merged


def caption_style(caps: list[Caption], ref_w: int, has_box: bool = True) -> dict:
    """Estilo comum medido: cor do texto, corpo da letra (fração da largura) e posição.
    O corpo sai da LARGURA lida de cada frase comparada com a da mesma frase em Arial (mais estável
    que a altura da caixa do OCR); só frases longas (as curtas têm margem do OCR proporcionalmente
    maior). 0,95 desconta a margem das caixas do EasyOCR (medido: 28,5 px lidos vs 27,2 reais)."""
    from velocity.render.textfx import _font
    f = _font(100, regular=True)
    long = [c for c in caps if c.box_w >= 0.2] or caps
    size_w = 0.95 * float(np.median([c.box_w * ref_w / (f.getlength(c.text) / 100) for c in long])) / ref_w
    return {"text_rgb": tuple(int(v) for v in np.median([c.text_rgb for c in caps], axis=0)), "font_w": size_w,
            "cx": float(np.median([c.cx for c in caps])), "cy": float(np.median([c.cy for c in caps])), "box_alpha": 0.8 if has_box else 0.0}


def to_overlays(caps: list[Caption], ref_fps: float, out_fps: float, out_size: tuple[int, int],
                ref_size: tuple[int, int], y: float | None = None, color: tuple[int, int, int] | None = None) -> list:
    """Legendas → TextOverlay(style="box") na edição. Tempo convertido para os frames da saída; a
    letra mantém a MESMA proporção em relação à largura (a edição pode ter outro aspeto); a cor do
    texto é a medida, a da caixa preta com a opacidade estimada."""
    from velocity.schemas.timeline import TextOverlay
    W, H = out_size
    st = caption_style(caps, ref_size[0])
    k = out_fps / ref_fps
    return [TextOverlay(text=c.text, start_frame=int(round(c.start_frame * k)), end_frame=int(round(c.end_frame * k)),
                        x=st["cx"], y=y if y is not None else st["cy"], width=0.92, height=st["font_w"] * W / H,
                        style="box", color=color or st["text_rgb"], box_color=(0, 0, 0), box_alpha=st["box_alpha"])
            for c in caps]
