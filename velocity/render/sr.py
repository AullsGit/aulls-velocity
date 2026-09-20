"""Super-resolução (Real-ESRGAN general x4 v3, via spandrel) para planos com muita ampliação.

Só se usa onde o zoom amplia muito a imagem (ver compositor). Não inventa detalhe real de um vídeo
comprimido: limpa e reforça o que existe. A saída é reduzida para `factor`x a resolução nativa
(o compositor faz o resto do recorte/zoom), o que limita memória e custo.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bench", "models", "realesr-general-x4v3.pth")
_model = None
_device = None
_cache: dict = {}


def available() -> bool:
    return os.path.exists(MODEL_PATH)


def _load():
    global _model, _device
    if _model is None:
        import torch
        from spandrel import ModelLoader

        _device = "cuda" if torch.cuda.is_available() else "cpu"
        m = ModelLoader().load_from_file(MODEL_PATH)
        _model = m.model.to(_device).eval()
        if _device == "cuda":
            _model = _model.half()
    return _model


def upscale(frames: np.ndarray, factor: float = 1.5, batch: int = 2) -> np.ndarray:
    """frames [n,h,w,3] uint8 -> [n,round(h*factor),round(w*factor),3] uint8."""
    import torch

    model = _load()
    dtype = torch.float16 if _device == "cuda" else torch.float32
    n, h, w, _ = frames.shape
    out_h, out_w = int(round(h * factor)), int(round(w * factor))
    out = np.zeros((n, out_h, out_w, 3), np.uint8)
    with torch.no_grad():
        for i in range(0, n, batch):
            x = torch.from_numpy(frames[i : i + batch]).to(_device).permute(0, 3, 1, 2).to(dtype) / 255.0
            y = model(x).clamp(0, 1).mul(255).byte().permute(0, 2, 3, 1).cpu().numpy()
            for j in range(len(y)):
                out[i + j] = cv2.resize(y[j], (out_w, out_h), interpolation=cv2.INTER_AREA)
    return out


def cached_upscale(key: tuple, frames: np.ndarray, factor: float) -> np.ndarray:
    k = key + (factor,)
    if k not in _cache:
        _cache[k] = upscale(frames, factor)
    return _cache[k]


def enhance_native(native: np.ndarray, factor: float = 1.5, mix: float = 0.75, key: tuple | None = None) -> np.ndarray:
    """SR com anti-halo: o resultado é limitado ao mínimo/máximo local da ampliação bicúbica (sem ringing) e
    misturado `mix` com SR / (1-mix) com a base, o que evita o aspeto 'plástico'. Receita validada em A/B visual."""
    sr_out = cached_upscale(key, native, factor) if key is not None else upscale(native, factor)
    n, h, w, _ = sr_out.shape
    k = np.ones((5, 5), np.uint8)
    out = np.empty_like(sr_out)
    for i in range(n):
        base = cv2.resize(native[i], (w, h), interpolation=cv2.INTER_CUBIC)
        c = np.minimum(np.maximum(sr_out[i], cv2.erode(base, k)), cv2.dilate(base, k))
        out[i] = cv2.addWeighted(c, mix, base, 1.0 - mix, 0)
    return out
