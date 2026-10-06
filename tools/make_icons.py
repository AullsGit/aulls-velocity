"""Gera os ícones PNG da PWA (Android/iOS) a partir das mesmas formas do logo SVG.

    python tools/make_icons.py
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "studio", "static", "brand")
INK, LIME = (11, 11, 15, 255), (198, 255, 61, 255)
LIME_SOFT = (198, 255, 61, 140)
# mesmas coordenadas do logo.svg (viewBox 64x64)
BAR1 = [(17, 47), (31.5, 15), (41, 15), (26.5, 47)]
BAR2 = [(33, 47), (42.5, 26), (50, 26), (40.5, 47)]


def icon(size: int, maskable: bool = False, rounded: bool = True) -> Image.Image:
    ss = 4  # super-amostragem para bordas suaves
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable or not rounded:
        d.rectangle([0, 0, S, S], fill=INK)  # o sistema aplica a máscara (círculo, squircle...)
    else:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 16 / 64), fill=INK)
    # maskable: o desenho tem de caber na "zona segura" (80% central)
    scale, off = (S * 0.72 / 64, S * 0.14) if maskable else (S / 64, 0)
    overlay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.polygon([(off + x * scale, off + y * scale) for x, y in BAR2], fill=LIME_SOFT)
    od.polygon([(off + x * scale, off + y * scale) for x, y in BAR1], fill=LIME)
    img = Image.alpha_composite(img, overlay)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    icon(192).save(os.path.join(OUT, "icon-192.png"))
    icon(512).save(os.path.join(OUT, "icon-512.png"))
    icon(512, maskable=True).save(os.path.join(OUT, "icon-maskable-512.png"))
    icon(180, rounded=False).save(os.path.join(OUT, "apple-touch-icon.png"))  # o iOS arredonda sozinho
    icon(32).save(os.path.join(OUT, "favicon-32.png"))
    print("ícones gerados em", OUT)


if __name__ == "__main__":
    main()
