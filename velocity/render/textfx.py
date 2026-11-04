"""Texto sobreposto (PIL). Estilos: 'outline' (branco com contorno e sombra; entra com 'pop' ou 'fade'),
'box' (legenda: texto numa fonte regular sobre uma caixa semitransparente, cores medidas na
referência) e 'watermark' (conta a seguir, com ícone, discreta). Só é chamado quando há TextOverlay.

'box' e 'watermark' não mudam ao longo do tempo: a camada é desenhada uma vez e só o retângulo que
ocupa é composto em cada frame (compor o frame inteiro por texto custava caro a 1080p)."""
from __future__ import annotations

import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from velocity.schemas.timeline import TextOverlay

_FONTS = [r"C:\Windows\Fonts\ariblk.ttf", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_FONTS_REGULAR = [r"C:\Windows\Fonts\arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_FONTS_SEMI = [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
_cache: dict = {}
_layers: dict = {}
SS = 4  # a marca é desenhada a 4x e reduzida: ícones e letras suaves em tamanhos pequenos


def _font(size: int, regular: bool = False, names: list[str] | None = None) -> ImageFont.FreeTypeFont:
    key = (size, regular, tuple(names or ()))
    if key not in _cache:
        for f in names or ((_FONTS_REGULAR + _FONTS) if regular else _FONTS):
            if os.path.exists(f):
                _cache[key] = ImageFont.truetype(f, size)
                break
        else:
            _cache[key] = ImageFont.load_default()
    return _cache[key]


def _composite(img: np.ndarray, layer: tuple[int, int, np.ndarray], alpha: float = 1.0) -> np.ndarray:
    """Compõe uma camada RGBA recortada (x0, y0, pixels) sobre o frame."""
    x0, y0, px = layer
    h, w = img.shape[:2]
    x1, y1 = min(w, x0 + px.shape[1]), min(h, y0 + px.shape[0])
    xa, ya = max(0, x0), max(0, y0)
    if x1 <= xa or y1 <= ya:
        return img
    src = px[ya - y0:y1 - y0, xa - x0:x1 - x0]
    a = src[..., 3:4].astype(np.float32) / 255.0 * alpha
    out = img.copy()
    out[ya:y1, xa:x1] = (src[..., :3].astype(np.float32) * a + out[ya:y1, xa:x1].astype(np.float32) * (1 - a)).astype(np.uint8)
    return out


def _crop_layer(layer: Image.Image) -> tuple[int, int, np.ndarray]:
    bbox = layer.getbbox() or (0, 0, 1, 1)
    return bbox[0], bbox[1], np.array(layer.crop(bbox))


def _box_layer(t: TextOverlay, w: int, h: int) -> tuple[int, int, np.ndarray]:
    """Legenda: corpo da letra = t.height (fração da altura do frame); caixa centrada em (x, y)
    com a geometria medida numa legenda real (Arial): altura 1,54× o corpo, linha de base a 1,02×
    do topo, margem lateral 0,42×. A caixa não depende das letras (igual em "eu" e em "ligue").
    Encolhe só se não couber em t.width."""
    size = max(10, int(round(t.height * h)))
    font = _font(size, regular=True)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    while size > 10 and d.textlength(t.text, font=font) > t.width * w:
        size -= 1
        font = _font(size, regular=True)
    tw = d.textlength(t.text, font=font)
    cx, cy = t.x * w, t.y * h
    top = cy - 0.77 * size
    d.rectangle([round(cx - tw / 2 - 0.42 * size), round(top), round(cx + tw / 2 + 0.42 * size), round(top + 1.54 * size)],
                fill=(*t.box_color, int(255 * t.box_alpha)))
    d.text((cx - tw / 2, top + 1.02 * size), t.text, font=font, anchor="ls", fill=(*(t.color or (255, 255, 255)), 255))
    return _crop_layer(layer)


def _icon_tiktok(d, x, y, s, lw):
    """Nota estilizada: haste, anel em baixo à esquerda, bandeira curva em cima à direita."""
    sx = x + 0.56 * s
    d.line([(sx, y + 0.04 * s), (sx, y + 0.72 * s)], fill=255, width=lw)
    r = 0.22 * s
    cx, cy = sx - r + lw / 2, y + 0.72 * s
    d.arc([cx - r, cy - r, cx + r, cy + r], start=-80, end=270, fill=255, width=lw)
    d.arc([sx - 0.02 * s, y - 0.30 * s, sx + 0.46 * s, y + 0.30 * s], start=90, end=180, fill=255, width=lw)


def _icon_instagram(d, x, y, s, lw):
    """Quadrado de cantos redondos, lente e ponto."""
    d.rounded_rectangle([x, y, x + s, y + s], radius=0.28 * s, outline=255, width=lw)
    r = 0.22 * s
    d.ellipse([x + s / 2 - r, y + s / 2 - r, x + s / 2 + r, y + s / 2 + r], outline=255, width=lw)
    p = 0.055 * s
    d.ellipse([x + 0.76 * s - p, y + 0.24 * s - p, x + 0.76 * s + p, y + 0.24 * s + p], fill=255)


_ICONS = {"tiktok": _icon_tiktok, "instagram": _icon_instagram}


def _watermark_layer(t: TextOverlay, w: int, h: int) -> tuple[int, int, np.ndarray]:
    """Branca com sombra suave: lê-se no céu claro e na noite, sem caixa nem contorno duro.
    (x, y) = início da linha, ao meio da altura da letra."""
    W, H = w * SS, h * SS
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    size = max(8 * SS, int(t.height * H))
    font = _font(size, names=_FONTS_SEMI)
    x0, cy = int(t.x * W), int(t.y * H)
    tx = x0
    if t.icon in _ICONS:
        _ICONS[t.icon](d, x0, cy - size // 2, size, max(SS, int(size * 0.12)))
        tx = x0 + size + int(0.42 * size)
    d.text((tx, cy), t.text, font=font, fill=255, anchor="lm")
    mask = mask.resize((w, h), Image.LANCZOS)
    shadow = mask.filter(ImageFilter.GaussianBlur(2.2)).point(lambda v: int(v * 0.55))
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.putalpha(ImageChops.offset(shadow, 1, 1))
    white = Image.new("RGBA", (w, h), (*(t.color or (255, 255, 255)), 0))
    white.putalpha(mask)
    layer = Image.alpha_composite(layer, white)
    layer.putalpha(layer.getchannel("A").point(lambda v: int(v * t.opacity)))
    return _crop_layer(layer)


def _static_layer(t: TextOverlay, w: int, h: int):
    key = (t.style, t.text, t.x, t.y, t.width, t.height, t.color, t.box_color, t.box_alpha, t.icon, t.opacity, w, h)
    got = _layers.get(key)
    if got is None:
        if len(_layers) > 256:  # uma letra de música tem dezenas de linhas; não cresce sem limite
            _layers.clear()
        got = _layers[key] = (_box_layer if t.style == "box" else _watermark_layer)(t, w, h)
    return got


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
    if t.style in ("box", "watermark"):  # legenda e marca: troca seca, como a da referência
        return _composite(img, _static_layer(t, w, h), alpha)
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
    d.text((x, y), text, font=font, fill=(*(t.color or (255, 255, 255)), int(255 * alpha)), stroke_width=max(1, size // 14), stroke_fill=(0, 0, 0, int(230 * alpha)))
    out = Image.fromarray(img).convert("RGBA")
    return np.array(Image.alpha_composite(out, layer).convert("RGB"))
