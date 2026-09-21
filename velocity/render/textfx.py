"""Texto sobreposto (PIL): entra com 'pop' (escala) ou 'fade', com contorno e sombra. Só é chamado quando há TextOverlay."""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from velocity.schemas.timeline import TextOverlay

_FONTS = [r"C:\Windows\Fonts\ariblk.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_cache: dict = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    key = size
    if key not in _cache:
        for f in _FONTS:
            if os.path.exists(f):
                _cache[key] = ImageFont.truetype(f, size)
                break
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def draw(img: np.ndarray, t: TextOverlay, frame: int) -> np.ndarray:
    if not (t.start_frame <= frame < t.end_frame):
        return img
    h, w = img.shape[:2]
    k = frame - t.start_frame
    dur = t.end_frame - t.start_frame
    alpha = 1.0
    scale = 1.0
    if t.entry == "pop":
        scale = 0.7 + 0.3 * min(1.0, k / 4.0) ** 0.6
        alpha = min(1.0, (k + 1) / 3.0)
    elif t.entry == "fade":
        alpha = min(1.0, (k + 1) / 6.0)
    alpha *= min(1.0, (dur - k) / 3.0)  # saída rápida
    size = max(12, int(t.height * h * 1.6 * scale))
    font = _font(size)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    text = t.text
    # encolhe a fonte até caber na largura pedida
    while size > 12 and d.textlength(text, font=font) > t.width * w * 1.15:
        size -= 2
        font = _font(size)
    tw = d.textlength(text, font=font)
    x, y = t.x * w - tw / 2, t.y * h - size / 2
    d.text((x + 2, y + 3), text, font=font, fill=(0, 0, 0, int(140 * alpha)))  # sombra
    d.text((x, y), text, font=font, fill=(255, 255, 255, int(255 * alpha)), stroke_width=max(1, size // 14), stroke_fill=(0, 0, 0, int(230 * alpha)))
    out = Image.fromarray(img).convert("RGBA")
    return np.array(Image.alpha_composite(out, layer).convert("RGB"))
