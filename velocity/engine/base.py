"""Interface entre o plano e quem o executa. A inteligência só conhece isto; o motor concreto
(compositor próprio hoje; MLT/GES se a prova técnica justificar) fica atrás do adaptador."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from velocity.plan.schema import Plan
from velocity.plan.validate import Issue


@dataclass(frozen=True)
class Capabilities:
    """O que um motor sabe executar. Tudo o que o plano pedir fora disto é "unsupported" e é
    recusado (ou avisado) ANTES do render — nunca descoberto a meio."""

    name: str
    max_video_tracks: int = 1
    gaps_allowed: bool = False  # buracos na pista de vídeo (frames sem item)
    overlapping_items: bool = False  # sobreposição de itens (cross-fade, picture-in-picture)
    max_audio_items: int = 1
    transitions: frozenset[str] = frozenset()
    effects: frozenset[str] = frozenset()
    per_frame_time: bool = False
    per_frame_transform: bool = False
    color_matrix: bool = False
    subject_masks: bool = False
    text: bool = False


@dataclass
class RenderReport:
    out_path: str
    frames: int
    seconds: float
    engine: str
    warnings: list[str] = field(default_factory=list)


class PlanRejected(ValueError):
    """O plano tem erros (do próprio plano ou não suportados pelo motor): nada foi renderizado."""

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues[:5]))


class RenderEngine(Protocol):
    name: str

    def capabilities(self) -> Capabilities: ...

    def validate(self, plan: Plan) -> list[Issue]:
        """Só os limites do motor (a coerência do plano é validate_plan)."""
        ...

    def render(self, plan: Plan, out_path: str, *, crf: int = 18, preset: str = "medium",
               progress: Callable[[int, int], None] | None = None, masks: dict | None = None) -> RenderReport: ...


def check_capabilities(plan: Plan, cap: Capabilities) -> list[Issue]:
    """Verificação genérica de um plano contra as capacidades declaradas de um motor."""
    out: list[Issue] = []

    def unsup(where, msg, level="error"):
        out.append(Issue(level, "unsupported", where, f"{cap.name}: {msg}"))

    vtracks = plan.track("video")
    if len(vtracks) > cap.max_video_tracks:
        unsup("tracks", f"{len(vtracks)} pistas de vídeo (máx. {cap.max_video_tracks})")
    for t in vtracks:
        items = sorted(t.items, key=lambda i: i.at)
        cursor = 0
        for it in items:
            if it.at < cursor and not cap.overlapping_items:
                unsup(f"{t.id}/{it.id}", "itens sobrepostos (cross-fade/PiP) não suportados")
            if it.at > cursor and not cap.gaps_allowed:
                unsup(f"{t.id}/{it.id}", f"buraco na pista entre os frames {cursor} e {it.at}")
            cursor = max(cursor, it.at + it.duration)
            tr = it.transition_in
            if tr is not None and tr.type not in cap.transitions:
                unsup(f"{t.id}/{it.id}", f"transição '{tr.type}' não suportada")
            if it.time.src_frames is not None and not cap.per_frame_time:
                unsup(f"{t.id}/{it.id}", "tempo por frame (rasto medido) não suportado", "warning")
            if it.transform.warps is not None and not cap.per_frame_transform:
                unsup(f"{t.id}/{it.id}", "transformação por frame não suportada", "warning")
            if it.color.matrix is not None and not cap.color_matrix:
                unsup(f"{t.id}/{it.id}", "matriz de cor não suportada", "warning")
        if items and cursor < plan.output.duration_frames and not cap.gaps_allowed:
            unsup(t.id, f"a pista de vídeo acaba no frame {cursor} e o vídeo tem {plan.output.duration_frames}")
    if not vtracks:
        unsup("tracks", "sem pista de vídeo")
    n_audio = sum(len(t.items) for t in plan.track("audio"))
    if n_audio > cap.max_audio_items:
        unsup("tracks", f"{n_audio} itens de áudio (máx. {cap.max_audio_items})")
    if plan.track("text") and any(t.items for t in plan.track("text")) and not cap.text:
        unsup("tracks", "texto não suportado")
    for i, e in enumerate(plan.effects):
        if e.type not in cap.effects:
            unsup(f"effects[{i}]", f"efeito '{e.type}' não suportado")
    for i, ev in enumerate(plan.fx_events):
        if ev.type not in cap.transitions:
            unsup(f"fx_events[{i}]", f"evento '{ev.type}' não suportado")
    return out
