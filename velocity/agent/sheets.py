"""Imagens para o agente VER o vídeo: folhas de frames com o instante em cada um, comparações lado a lado.

É assim que o modelo (que não abre vídeos) inspeciona a referência, os clips e o resultado. O lado
maior fica em 1568 px: acima disso a API reduz a imagem e cobra o mesmo.
"""
from __future__ import annotations

import math
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from velocity.io.probe import probe

MAX_SIDE = 1568
MAX_CELLS = 60
_FONT_PATHS = [r"C:\Windows\Fonts\arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _font(size: int):
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def frame_at(path: str, t: float, width: int) -> Image.Image | None:
    info = probe(path)
    w = max(32, min(width, info.width)) // 2 * 2
    h = max(2, int(round(w * info.height / max(info.width, 1) / 2)) * 2)
    t = max(0.0, min(t, max(float(info.duration or 0.0) - 0.04, 0.0)))
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0 or len(r.stdout) < w * h * 3:
        return None
    return Image.frombuffer("RGB", (w, h), r.stdout[:w * h * 3], "raw", "RGB", 0, 1)


def _label(img: Image.Image, text: str, size: int | None = None) -> None:
    d = ImageDraw.Draw(img)
    size = size or max(11, img.width // 12)
    f = _font(size)
    x0, y0, x1, y1 = d.textbbox((4, 3), text, font=f)
    d.rectangle([x0 - 3, y0 - 2, x1 + 3, y1 + 2], fill=(0, 0, 0))
    d.text((4, 3), text, font=f, fill=(255, 255, 255))


def _fmt(t: float) -> str:
    m = int(t // 60)
    return f"{m}:{t - 60 * m:05.2f}" if m else f"{t:.2f}s"


def _grab(path: str, times: list[float], width: int) -> list[tuple[float, Image.Image]]:
    with ThreadPoolExecutor(max_workers=4) as ex:
        got = list(ex.map(lambda t: frame_at(path, t, width), times))
    return [(t, g) for t, g in zip(times, got) if g is not None]


def _tile(cells: list[Image.Image], cols: int, gap: int = 2) -> Image.Image:
    w, h = cells[0].size
    rows = math.ceil(len(cells) / cols)
    sheet = Image.new("RGB", (cols * w + (cols - 1) * gap, rows * h + (rows - 1) * gap), (24, 24, 28))
    for k, c in enumerate(cells):
        sheet.paste(c, ((k % cols) * (w + gap), (k // cols) * (h + gap)))
    return sheet


def _fit(img: Image.Image) -> Image.Image:
    s = max(img.size)
    return img if s <= MAX_SIDE else img.resize((img.width * MAX_SIDE // s, img.height * MAX_SIDE // s), Image.LANCZOS)


def uniform_times(duration: float, n: int) -> list[float]:
    n = max(1, n)
    return [round((k + 0.5) * duration / n, 3) for k in range(n)]


def contact_sheet(path: str, times: list[float], out_path: str, cell_w: int | None = None) -> dict:
    """Folha de frames legendados com o instante. Devolve {path, times, size}."""
    times = [float(t) for t in times][:MAX_CELLS]
    if not times:
        raise ValueError("sem instantes")
    info = probe(path)
    portrait = info.height > info.width
    n = len(times)
    cols = max(1, min(n, round(math.sqrt(n * (1.8 if portrait else 1.0)))))
    w = cell_w or max(96, min(MAX_SIDE // cols, 420))
    cells = _grab(path, times, w)
    if not cells:
        raise RuntimeError("não foi possível extrair frames")
    for t, c in cells:
        _label(c, _fmt(t))
    sheet = _fit(_tile([c for _, c in cells], cols))
    sheet.save(out_path, "JPEG", quality=82)
    return {"path": out_path, "times": [t for t, _ in cells], "size": list(sheet.size)}


def side_by_side(path_a: str, path_b: str, pairs: list[tuple[float, float]], out_path: str, labels: tuple[str, str] = ("A", "B"),
                 cell_w: int = 300) -> dict:
    """Colunas alternadas A | B para os mesmos instantes (ou instantes correspondentes)."""
    pairs = pairs[:12]
    a = _grab(path_a, [p[0] for p in pairs], cell_w)
    b = _grab(path_b, [p[1] for p in pairs], cell_w)
    n = min(len(a), len(b))
    if n == 0:
        raise RuntimeError("não foi possível extrair frames")
    # alturas iguais em cada par (a referência pode ter outro aspeto)
    H = max(a[0][1].height, b[0][1].height)
    cells = []
    for (ta, ia), (tb, ib) in zip(a[:n], b[:n]):
        for im, t, lab in ((ia, ta, labels[0]), (ib, tb, labels[1])):
            c = Image.new("RGB", (cell_w, H), (0, 0, 0))
            c.paste(im.resize((cell_w, im.height * cell_w // im.width)), (0, 0))
            _label(c, f"{lab} {_fmt(t)}", 13)
            cells.append(c)
    cols = 2 if n <= 2 else 4 if n <= 8 else 6
    sheet = _fit(_tile(cells, cols))
    sheet.save(out_path, "JPEG", quality=84)
    return {"path": out_path, "pairs": [[pairs[k][0], pairs[k][1]] for k in range(n)], "size": list(sheet.size)}


def frames_array(path: str, times: list[float], width: int = 160) -> np.ndarray:
    """Frames [n,H,W,3] em instantes dados: amostra para medir cor sem decodificar o vídeo inteiro."""
    got = _grab(path, times, width)
    if not got:
        raise RuntimeError(f"sem frames de {path}")
    h = got[0][1].height
    return np.stack([np.asarray(im.resize((width, h))) for _, im in got])
