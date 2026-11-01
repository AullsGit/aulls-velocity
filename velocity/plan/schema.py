"""Plano de edição v1: o contrato entre a INTELIGÊNCIA (que analisa e decide) e o MOTOR (que executa).

Regras deste formato:
  * independente do motor: nenhum campo descreve como um motor específico funciona por dentro
    (as preferências de execução vão em `render_hints`, que um motor pode ignorar);
  * versionado (`plan_version`) e validável antes do render (velocity/plan/validate.py);
  * tempo em FRAMES inteiros à taxa de saída `output.fps` (sem somas de segundos em vírgula
    flutuante); só os instantes dentro das fontes usam segundos ou frames da própria fonte;
  * cada decisão diz de onde veio (`provenance`): matcher, alinhamento medido, editor, utilizador.

Pistas: `video` (planos sequenciais com transições de entrada), `audio` e `text`. O esquema
admite várias pistas e itens em qualquer posição; o que cada motor consegue executar é declarado
pelo próprio motor (velocity/engine) e verificado antes do render.

JSON Schema publicado em docs/plan.schema.json (gerado: `python -m velocity.plan --schema`).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from velocity.schemas.edit_dna import Curve, EffectTrack, FxEvent, Transition

PLAN_VERSION = "1.0"
PLAN_MAJOR = 1

Warp = tuple[float, float, float, float, float, float]


class Output(BaseModel):
    width: int
    height: int
    fps: float
    duration_frames: int
    color_space: Literal["bt709"] = "bt709"


class Source(BaseModel):
    """Ficheiro de media. `uri`: caminho local no worker ou chave de armazenamento na plataforma."""

    uri: str
    kind: Literal["video", "image", "audio"] = "video"


class Provenance(BaseModel):
    """Origem de uma decisão e quanta confiança merece. `confidence` None = não medida."""

    by: Literal["matcher", "source_align", "reference", "editor", "user", "system"] = "matcher"
    confidence: float | None = None
    method: str | None = None
    notes: list[str] = []


class TimeMap(BaseModel):
    """Que instante da fonte aparece em cada frame do item.

    linear: começa em `src_start_s` e avança pela curva `speed` (1 = tempo real; t em 0..1 ao longo
    do item). per_frame: `src_frames[k]` (à taxa `src_fps`) para cada frame k do item — speed
    ramps, freezes e reverse medidos. Quando per_frame existe, os campos lineares ficam como
    aproximação editável."""

    src_start_s: float
    speed: Curve = Field(default_factory=lambda: Curve(t=[0.0, 1.0], v=[1.0, 1.0]))
    src_fps: float | None = None
    src_frames: list[int] | None = None
    # abaixo de 1x: "blend" mistura os 2 frames vizinhos (suave); "hold" repete frames reais da fonte
    slowmo: Literal["blend", "hold"] = "blend"

    @property
    def per_frame(self) -> bool:
        return self.src_frames is not None


class Transform(BaseModel):
    """Enquadramento da fonte no frame de saída.

    Paramétrico: `zoom` (ln da escala sobre o enquadramento "cover", curva em t 0..1), `center`
    (ponto de interesse, fração da fonte), `pan` (deslocamento total do recorte), `flip`.
    Por frame: `warps[k]` é uma afim 2x3 de coordenadas normalizadas da saída para coordenadas
    normalizadas da fonte — zoom, pan, rotação e shake medidos."""

    zoom: Curve = Field(default_factory=lambda: Curve(t=[0.0, 1.0], v=[0.0, 0.0]))
    center: tuple[float, float] = (0.5, 0.5)
    pan: tuple[float, float] = (0.0, 0.0)
    flip: bool = False
    warps: list[Warp] | None = None


class ColorOps(BaseModel):
    """Cor do item. `matrix` (3x4, valores 0..255) é uma transformação MEDIDA fonte -> referência;
    os restantes são ajustes de tom (o sujeito/fundo dependem de uma máscara de pessoa)."""

    gamma: float = 1.0
    saturation: float = 1.0
    subject_gamma: float = 1.0
    background_gamma: float = 1.0
    subject_sharpen: float = 0.0
    matrix: list[list[float]] | None = None


class VideoItem(BaseModel):
    type: Literal["video"] = "video"
    id: str
    source: str  # chave em Plan.sources
    at: int  # primeiro frame na saída
    duration: int  # em frames de saída
    time: TimeMap
    transform: Transform = Field(default_factory=Transform)
    color: ColorOps = Field(default_factory=ColorOps)
    transition_in: Transition | None = None  # frames absolutos na saída
    provenance: Provenance = Field(default_factory=Provenance)
    measured: list[bool] | None = None  # por frame: medido (True) ou extrapolado (False) — só em per_frame


class AudioItem(BaseModel):
    type: Literal["audio"] = "audio"
    id: str
    source: str
    at: int = 0
    duration: int
    offset_s: float = 0.0  # instante da fonte que toca no frame `at`
    volume: float = 1.0
    loop: bool = True
    fade_out_s: float = 0.4


class TextItem(BaseModel):
    type: Literal["text"] = "text"
    id: str
    text: str
    at: int
    duration: int
    x: float
    y: float
    width: float
    height: float
    entry: Literal["pop", "fade", "none"] = "none"
    provenance: Provenance = Field(default_factory=lambda: Provenance(by="reference"))


class Track(BaseModel):
    id: str
    kind: Literal["video", "audio", "text"]
    items: list[Annotated[VideoItem | AudioItem | TextItem, Field(discriminator="type")]] = []


class Look(BaseModel):
    """Correção global aplicada depois da composição (ciclo de fidelidade ou editor)."""

    gamma: float = 1.0
    saturation: float = 1.0
    contrast: float = 1.0
    sharpen: float = 0.0
    pivot: float = 110.0


class RenderHints(BaseModel):
    """Preferências de execução que um motor PODE ignorar (sem mudar o significado do plano)."""

    denoise: float = 0.0
    super_resolution: bool = False


class Plan(BaseModel):
    plan_version: str = PLAN_VERSION
    output: Output
    sources: dict[str, Source]
    tracks: list[Track]
    effects: list[EffectTrack] = []  # efeitos paramétricos medidos (frames absolutos)
    fx_events: list[FxEvent] = []
    look: Look = Field(default_factory=Look)
    zoom_gain: float = 1.0  # multiplica as curvas de zoom paramétricas (1 = fiel ao medido)
    effects_scale: float = 1.0  # multiplica as magnitudes de `effects` (protetor de fidelidade)
    render_hints: RenderHints = Field(default_factory=RenderHints)
    meta: dict = {}

    def track(self, kind: str) -> list[Track]:
        return [t for t in self.tracks if t.kind == kind]

    def video_items(self) -> list[VideoItem]:
        return [it for t in self.track("video") for it in t.items]  # type: ignore[misc]

    def map_uris(self, fn) -> "Plan":
        """Cópia com cada `uri` substituído por fn(uri) (caminho local <-> chave de armazenamento)."""
        p = self.model_copy(deep=True)
        for s in p.sources.values():
            s.uri = fn(s.uri)
        return p
