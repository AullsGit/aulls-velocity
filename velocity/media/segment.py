"""Segmentação de pessoas em GPU (torchvision DeepLabV3), atrás da mesma interface do localizador.

Devolve máscaras SUAVES (probabilidade 0..255) em vez de binárias: o compositor usa-as como alfa,
o que reduz halos. Corre em resolução maior do que a análise (mais pixels na pessoa distante).
"""
from __future__ import annotations

import cv2
import numpy as np

PERSON_CLASS = 15  # Pascal VOC (pessoa)


class PersonSegmenter:
    def __init__(self, device: str | None = None, width: int = 384, batch: int = 8):
        import torch
        import torchvision
        from torchvision.models.segmentation import DeepLabV3_ResNet50_Weights, deeplabv3_resnet50

        self.torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.width, self.batch = width, batch
        w = DeepLabV3_ResNet50_Weights.DEFAULT
        self.model = deeplabv3_resnet50(weights=w).to(self.device).eval().half() if self.device == "cuda" else deeplabv3_resnet50(weights=w).eval()
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.device, dtype=self.dtype).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.device, dtype=self.dtype).view(1, 3, 1, 1)
        self.last_masks: np.ndarray | None = None

    def person_prob(self, rgb: np.ndarray) -> np.ndarray:
        """rgb [N,H,W,3] uint8 -> [N,H,W] uint8 probabilidade de pessoa (0..255)."""
        torch = self.torch
        n, h, w, _ = rgb.shape
        out = np.zeros((n, h, w), np.uint8)
        with torch.no_grad():
            for i in range(0, n, self.batch):
                x = torch.from_numpy(rgb[i : i + self.batch]).to(self.device).permute(0, 3, 1, 2).to(self.dtype) / 255.0
                x = (x - self.mean) / self.std
                logits = self.model(x)["out"]
                p = torch.softmax(logits.float(), dim=1)[:, PERSON_CLASS]
                out[i : i + self.batch] = (p * 255).clamp(0, 255).byte().cpu().numpy()
        return out

    def locate_video(self, path: str, n: int, h: int, w: int, stride: int = 2) -> tuple[np.ndarray, np.ndarray]:
        """Segmenta o vídeo a `self.width` px de largura, a cada `stride` frames (o resto repete a máscara mais próxima),
        com cache em disco. Devolve (loc [n,4], máscaras uint8 [n,h,w] ao tamanho da análise)."""
        import hashlib, os
        from velocity.io.frames import CACHE_DIR, load_frames
        st = os.stat(path)
        key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{self.width}|{stride}|seg1".encode()).hexdigest()[:16]
        cp = os.path.join(CACHE_DIR, f"segmask_{key}.npy")
        if os.path.exists(cp):
            masks = np.load(cp)
        else:
            fr = load_frames(path, fps=30.0, width=self.width)
            idx = np.arange(0, fr.n, stride)
            prob = self.person_prob(fr.rgb[idx])
            full = prob[np.minimum(np.arange(fr.n) // stride, len(prob) - 1)]
            masks = np.stack([cv2.resize(m, (w, h), interpolation=cv2.INTER_AREA) for m in full])
            os.makedirs(CACHE_DIR, exist_ok=True)
            np.save(cp, masks)
        if len(masks) != n:  # alinha ao nº de frames da análise
            masks = masks[np.minimum(np.arange(n), len(masks) - 1)]
        self.last_masks = masks
        return self._loc_from_masks(masks), masks

    def _loc_from_masks(self, prob: np.ndarray) -> np.ndarray:
        n, h, w = prob.shape
        out = np.tile(np.array([0.5, 0.5, 0.0, 0.0], np.float32), (n, 1))
        for i in range(n):
            m = prob[i] > 128
            if m.sum() < 0.002 * h * w:
                continue
            ys, xs = np.nonzero(m)
            out[i] = (xs.mean() / w, (ys.min() + ys.max()) / 2 / h, (ys.max() - ys.min()) / h, 1.0)
        for c in range(3):
            pad = np.pad(out[:, c], (3, 3), mode="edge")
            out[:, c] = np.array([np.median(pad[i : i + 7]) for i in range(n)])
        return out

    def locate(self, rgb: np.ndarray, cam_motion: np.ndarray) -> np.ndarray:
        """Interface SubjectLocator: [N,4] (cx, cy, altura_rel, presença) e self.last_masks [N,H,W]."""
        n, h, w, _ = rgb.shape
        prob = self.person_prob(rgb)
        self.last_masks = prob
        out = np.tile(np.array([0.5, 0.5, 0.0, 0.0], np.float32), (n, 1))
        for i in range(n):
            m = prob[i] > 128
            if m.sum() < 0.002 * h * w:
                continue
            ys, xs = np.nonzero(m)
            out[i] = (xs.mean() / w, (ys.min() + ys.max()) / 2 / h, (ys.max() - ys.min()) / h, 1.0)
        for c in range(3):  # suavização temporal
            pad = np.pad(out[:, c], (3, 3), mode="edge")
            out[:, c] = np.array([np.median(pad[i : i + 7]) for i in range(n)])
        return out
