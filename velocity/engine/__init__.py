"""Motores de render atrás de uma interface única (ver base.RenderEngine).

`get_engine()` devolve o motor configurado (`VELOCITY_ENGINE`, por defeito o compositor próprio).
Um motor novo (p.ex. MLT) entra aqui sem mudar a inteligência nem a plataforma.
"""
from __future__ import annotations

import os

from velocity.engine.base import Capabilities, PlanRejected, RenderEngine, RenderReport, check_capabilities

_ENGINES = {}


def get_engine(name: str | None = None) -> RenderEngine:
    name = name or os.environ.get("VELOCITY_ENGINE", "compositor")
    if name not in _ENGINES:
        if name == "compositor":
            from velocity.engine.compositor import CompositorEngine
            _ENGINES[name] = CompositorEngine()
        else:
            raise ValueError(f"motor desconhecido: {name}")
    return _ENGINES[name]


__all__ = ["Capabilities", "PlanRejected", "RenderEngine", "RenderReport", "check_capabilities", "get_engine"]
