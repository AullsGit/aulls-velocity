"""Timeline executável: o que o renderer recebe. Não decide nada, só executa."""
from __future__ import annotations

from pydantic import BaseModel, Field

from velocity.schemas.edit_dna import Curve, EffectTrack, FxEvent, Transition


class ClipUse(BaseModel):
    """Como um trecho de um vídeo do utilizador preenche um plano."""

    source_path: str
    src_start_s: float
    speed: Curve = Field(default_factory=lambda: Curve(t=[0.0, 1.0], v=[1.0, 1.0]))  # multiplicador de velocidade (1 = normal)
    zoom: Curve = Field(default_factory=lambda: Curve(t=[0.0, 1.0], v=[0.0, 0.0]))  # ln(escala) relativo
    center: tuple[float, float] = (0.5, 0.5)  # ponto de interesse (fração do frame fonte)
    pan: tuple[float, float] = (0.0, 0.0)  # deslocamento total da janela de corte (fração da largura/altura)
    flip: bool = False
    tone_gamma: float = 1.0  # <1 clareia, >1 escurece (aproxima o brilho do plano da referência)
    tone_sat: float = 1.0  # multiplicador de saturação
    subj_gamma: float = 1.0  # <1 levanta a luz SÓ do sujeito (máscara)
    bg_gamma: float = 1.0  # >1 escurece SÓ o fundo (clima da referência)
    subj_sharp: float = 0.0  # 0..1 nitidez local no sujeito (unsharp em máscara)


class TimelineShot(BaseModel):
    index: int
    start_frame: int
    end_frame: int  # exclusivo
    clip: ClipUse
    transition_in: Transition | None = None

    @property
    def n_frames(self) -> int:
        return self.end_frame - self.start_frame


class Timeline(BaseModel):
    fps: float = 30.0
    width: int = 1080
    height: int = 1920
    n_frames: int
    shots: list[TimelineShot]
    fx_events: list[FxEvent] = []
    effects: list[EffectTrack] = []  # efeitos paramétricos planeados (ver render/fxstack.py)
    fx_scale: float = 1.0  # alavanca global de intensidade (protetor de fidelidade)
    post_gamma: float = 1.0  # correção global de look (ciclo fechado), aplicada a todos os frames
    post_sat: float = 1.0
    post_contrast: float = 1.0
    post_sharp: float = 0.0  # 0..0.9 nitidez global só nos contornos (ciclo fechado; só sobe se o output for mais mole que a referência)
    post_pivot: float = 110.0  # luma-alvo à volta da qual o contraste é escalado
    music_path: str | None = None
    music_offset_s: float = 0.0
    zoom_gain: float = 1.0  # amplifica o zoom do DNA (1 = fiel)
