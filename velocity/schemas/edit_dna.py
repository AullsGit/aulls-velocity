"""Edit DNA v1 — representação estruturada e EXECUTÁVEL da linguagem de edição de uma referência.

Princípios:
  * É PURO: só descreve o que foi medido na referência. Nunca é alterado por presets
    (erro do MVP, onde FORCE_ZOOM contaminava o DNA).
  * Tempo em FRAMES inteiros sobre uma grelha de `fps` constantes (sem drift), com
    segundos derivados. O renderer executa exatamente esta grelha.
  * É ANÓNIMO em relação ao conteúdo: guarda movimento, ritmo e efeitos, não pixels.
  * Cada medida tem `confidence` quando é uma estimativa; os consumidores decidem
    o que fazer com baixa confiança.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.3"

TransitionType = Literal["cut", "flash", "dip_black", "blur", "glitch", "dissolve"]
BlurKind = Literal["none", "horizontal", "vertical", "radial"]
SpeedShape = Literal["flat", "ramp_up", "ramp_down", "peak_middle", "valley_middle", "unknown"]


class Transition(BaseModel):
    """Transição centrada num ponto de corte. O conteúdo troca em `cut_frame`;
    o efeito ocupa [start_frame, end_frame) e é simétrico/assimétrico à volta dele."""

    type: TransitionType
    start_frame: int
    end_frame: int  # exclusivo
    cut_frame: int  # primeiro frame do plano novo
    intensity: float = Field(0.0, ge=0.0, le=1.0)  # 0..1 força relativa do efeito
    blur_kind: BlurKind = "none"
    peak_luma: float | None = None  # luma máx. na transição (flash) / mín. (dip_black)
    beat_offset_ms: float | None = None  # (cut_time - beat mais próximo) em ms; None sem áudio
    beat_index: float | None = None  # posição do corte em batidas (fracionária), p/ retime a outra música
    direction: str = "none"  # slide_left|slide_right|slide_up|slide_down (movimento do CONTEÚDO) | zoom_in|zoom_out | none
    motion: dict[str, float] = {}  # dx, dy (fração da largura), zoom (ln escala) acumulados na transição
    envelope: list[float] = []  # perfil MEDIDO da intensidade ao longo de [start,end), normalizado (pico=1)

    @property
    def span(self) -> int:
        return self.end_frame - self.start_frame


class Curve(BaseModel):
    """Curva normalizada em keyframes: t em [0,1] (fração do plano), v = valor."""

    t: list[float]
    v: list[float]


class MotionProfile(BaseModel):
    """Movimento de câmera/conteúdo de um plano (fluxo óptico denso + ajuste afim global)."""

    energy_mean: float  # movimento total médio (fração da diagonal por frame)
    energy_curve: Curve  # energia ao longo do plano (normalizada pelo máx. global do vídeo)
    zoom_total: float  # ln(escala final/escala inicial); >0 zoom in
    zoom_curve: Curve  # ln(escala) acumulado, começa em 0
    pan_x: float  # deslocamento acumulado / largura (+ = conteúdo move para a direita)
    pan_y: float
    rotation_deg: float
    shake: float  # jitter de alta frequência da translação (mão tremida / shake effect)
    motion_type: Literal["static", "zoom_in", "zoom_out", "pan", "handheld", "mixed", "unknown"]
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class SpeedProfile(BaseModel):
    """Estimativa INDIRETA da curva de velocidade (energia de movimento + frames repetidos).
    Não distingue movimento do conteúdo de retiming de pós-produção — ver `confidence`."""

    shape: SpeedShape
    curve: Curve  # velocidade relativa (1.0 = média do plano)
    frozen_ratio: float  # fração de frames ~idênticos ao anterior (slow-mo por repetição / freeze)
    variation: float  # coeficiente de variação da energia
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class Appearance(BaseModel):
    mean_luma: float
    contrast: float
    saturation: float
    sharpness: float


class Shot(BaseModel):
    index: int
    start_frame: int
    end_frame: int  # exclusivo
    duration_s: float
    transition_in: Transition | None  # None só no primeiro plano
    motion: MotionProfile
    speed: SpeedProfile
    appearance: Appearance
    reuses_shot: int | None = None  # se o mesmo conteúdo já apareceu antes na referência (índice)


class FxEvent(BaseModel):
    """Efeito visual dentro de um plano SEM troca de conteúdo (punch, flash de glow, glitch)."""

    type: TransitionType
    start_frame: int
    end_frame: int
    intensity: float


EffectType = Literal["zoom_punch", "shake", "rgb_split", "exposure_pulse", "contrast_pulse", "sat_pulse"]
AnchorKind = Literal["cut", "beat", "free"]


class EffectTrack(BaseModel):
    """Efeito MEDIDO na referência (nunca inventado). `magnitude` é a grandeza física; `intensity`
    é essa grandeza normalizada 0..1 para comparar entre efeitos. A âncora diz a que evento o
    efeito está preso, para o planeador o transferir para os cortes/batidas do resultado."""

    type: EffectType
    start_frame: int
    end_frame: int  # exclusivo
    peak_frame: int
    magnitude: float  # zoom_punch: ln(escala) (+ = in); shake: amplitude/largura; rgb_split: desvio/largura; pulses: variação relativa com sinal
    intensity: float = Field(0.0, ge=0.0, le=1.0)
    anchor: AnchorKind = "free"
    anchor_ref: int | None = None  # índice da transição (cut) ou da batida (beat)
    anchor_offset: int = 0  # peak_frame - frame da âncora
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    envelope: list[float] = []  # perfil MEDIDO da intensidade ao longo de [start,end), normalizado (pico=1)


class LookProfile(BaseModel):
    """O 'look' global da referência: o que o melhoramento visual TEM de respeitar."""

    sharpness: float  # variância do Laplaciano (192 px) mediana nos frames estáveis
    contrast: float
    saturation: float
    luma: float
    motion_energy_mean: float
    motion_energy_p90: float
    cuts_per_second: float
    effect_transition_rate: float  # transições não-cut por segundo
    punch_rate: float  # zoom-punches por segundo
    shake_fraction: float  # fração de frames em shake
    rgb_split_fraction: float
    aggressiveness: float = Field(0.0, ge=0.0, le=1.0)
    descriptors: list[str] = []  # ex.: ["aggressive", "fast"]


class Impact(BaseModel):
    """Momento de impacto da edição (pico da curva de energia)."""

    t: float  # segundos
    strength: float  # 0..1
    on_beat: bool | None = None


class VibeProfile(BaseModel):
    """A 'vibe' da referência: como a edição SE SENTE ao longo do tempo. Todos os valores 0..1, exceto onde indicado.
    É o alvo que o motor de decisão tenta preservar (e que o avaliador mede no output)."""

    aggressiveness: float
    smoothness: float  # 1 = movimentos e cortes fluidos; 0 = abrupto/tremido
    calm: float  # 1 = calmo/contemplativo
    intensity: float  # média da curva de energia
    speed: float  # ritmo percebido (cortes + movimento)
    energy_scale: float  # movimento médio ABSOLUTO (fração da diagonal por frame), para comparar entre vídeos
    energy_hz: float = 4.0
    energy_curve: list[float] = []  # energia 0..1 amostrada a energy_hz
    arc: str = "plateau"  # build | decay | peak_early | peak_middle | peak_late | plateau | wave
    arc_levels: list[float] = []  # energia média do início / meio / fim
    impacts: list[Impact] = []
    rests: list[tuple[float, float]] = []  # janelas de descanso (s)
    transition_density: float = 0.0  # transições/s
    transition_style: dict[str, float] = {}  # distribuição por tipo (soma 1)
    effect_density: float = 0.0  # efeitos (não-cut) por segundo
    effect_intensity_mean: float = 0.0
    effect_intensity_p90: float = 0.0
    vocabulary: dict[str, float] = {}  # tipo de efeito/transição -> ocorrências por segundo
    zoom: dict[str, float] = {}  # punch_rate, punch_mean, punch_in_ratio, slow_zoom_ratio
    beat: dict[str, float] = {}  # lock (alinhamento-acaso), energy_on_beat, impacts_on_beat
    descriptors: list[str] = []


class TextEvent(BaseModel):
    """Texto criativo na referência (marcas de água excluídas). O CONTEÚDO não se copia: só o quando/onde/como."""

    start_frame: int
    end_frame: int
    x: float  # centro (fração da largura)
    y: float  # centro (fração da altura)
    width: float  # fração da largura
    height: float  # fração da altura (tamanho da fonte)
    position: str  # top | center | bottom
    entry: str = "none"  # pop (cresce ao entrar) | fade | none
    on_beat: bool | None = None
    n_chars: int = 0
    confidence: float = 0.5


class AudioDNA(BaseModel):
    has_audio: bool
    bpm: float | None = None
    beats: list[float] = []  # segundos
    downbeats_hint: list[float] = []
    onsets: list[float] = []
    cut_beat_alignment: float | None = None  # fração de cortes a <= 70 ms de uma batida
    chance_alignment: float | None = None  # alinhamento esperado ao ACASO (baseline p/ interpretar o anterior)
    cut_interval_beats: float | None = None  # intervalo mediano entre cortes, em batidas


class Letterbox(BaseModel):
    """Barras pretas fixas (fração da altura/largura), medidas e removidas antes da análise."""

    top_frac: float = 0.0
    bottom_frac: float = 0.0
    left_frac: float = 0.0
    right_frac: float = 0.0


class Signature(BaseModel):
    """Resumo global, usado pelo Velocity Detector e pelo avaliador."""

    n_shots: int
    cuts_per_second: float
    median_shot_s: float
    shot_s_p10: float
    shot_s_p90: float
    transition_counts: dict[str, int]
    transition_ratio: float  # transições com efeito (não-cut) / total
    motion_energy_mean: float
    motion_energy_cv: float
    zoom_active_ratio: float  # planos com |zoom_total| relevante
    speed_dynamic_ratio: float  # planos com curva de velocidade não-flat
    frozen_ratio_mean: float
    fx_events_per_second: float


class EditDNA(BaseModel):
    schema_version: str = SCHEMA_VERSION
    source_path: str
    width: int
    height: int
    fps: float
    n_frames: int
    duration_s: float
    letterbox: Letterbox
    shots: list[Shot]
    fx_events: list[FxEvent] = []
    effects: list[EffectTrack] = []
    look: LookProfile | None = None
    vibe: VibeProfile | None = None
    text_events: list[TextEvent] = []
    audio: AudioDNA
    signature: Signature
    warnings: list[str] = []

    def transitions(self) -> list[Transition]:
        return [s.transition_in for s in self.shots if s.transition_in is not None]

    def cut_frames(self) -> list[int]:
        return [t.cut_frame for t in self.transitions()]
