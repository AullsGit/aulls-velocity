"""Adaptador do compositor próprio (velocity/render/compositor.py) para a interface de motor.

É hoje o único motor que executa o rasto medido do cenário A (frame da fonte + afim + matriz de
cor por frame) e os efeitos medidos da referência. Limites: uma pista de vídeo sequencial, sem
sobreposição (sem cross-fade nem PiP) e uma faixa de áudio.
"""
from __future__ import annotations

import time

from velocity.engine.base import Capabilities, PlanRejected, RenderReport, check_capabilities
from velocity.plan.convert import plan_to_timeline
from velocity.plan.schema import Plan
from velocity.plan.validate import Issue, errors, validate_plan

CAPS = Capabilities(
    name="compositor",
    max_video_tracks=1,
    gaps_allowed=False,
    overlapping_items=False,
    max_audio_items=1,
    transitions=frozenset({"cut", "flash", "dip_black", "blur", "glitch"}),
    effects=frozenset({"zoom_punch", "shake", "rgb_split", "exposure_pulse", "contrast_pulse", "sat_pulse"}),
    per_frame_time=True,
    per_frame_transform=True,
    color_matrix=True,
    subject_masks=True,
    text=True,
)


class CompositorEngine:
    name = "compositor"

    def capabilities(self) -> Capabilities:
        return CAPS

    def validate(self, plan: Plan) -> list[Issue]:
        return check_capabilities(plan, CAPS)

    def render(self, plan: Plan, out_path: str, *, crf: int = 18, preset: str = "medium", progress=None,
               masks: dict | None = None) -> RenderReport:
        issues = errors(validate_plan(plan) + self.validate(plan))
        if issues:
            raise PlanRejected(issues)
        from velocity.render.compositor import render
        t0 = time.time()
        tl = plan_to_timeline(plan)
        render(tl, out_path, crf=crf, masks=masks, preset=preset, progress=progress)
        return RenderReport(out_path=out_path, frames=tl.n_frames, seconds=round(time.time() - t0, 2), engine=self.name)
