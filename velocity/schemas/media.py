"""Contratos de dados de baixo nível partilhados por todo o sistema."""
from __future__ import annotations

from pydantic import BaseModel


class VideoInfo(BaseModel):
    """Metadados de um vídeo, já em coordenadas de *apresentação* (rotação aplicada)."""

    path: str
    width: int
    height: int
    duration: float
    fps_avg: float
    fps_nominal: float
    is_vfr: bool  # frame rate variável — precisa de normalização antes de qualquer análise
    n_frames_declared: int | None = None
    codec: str | None = None
    has_audio: bool = False
    audio_sample_rate: int | None = None

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def orientation(self) -> str:
        if abs(self.aspect - 1.0) < 0.05:
            return "square"
        return "vertical" if self.aspect < 1 else "horizontal"
