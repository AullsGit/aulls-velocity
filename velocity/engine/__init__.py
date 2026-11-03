"""Motores de render atrás de uma interface única (ver base.RenderEngine).

`get_engine()` devolve o motor configurado (`VELOCITY_ENGINE`, por defeito o compositor próprio);
`choose_engine(plan)` escolhe o primeiro motor disponível que executa o plano sem erros
(compositor primeiro: é o mais exato e o único com os efeitos medidos; MLT quando o plano pede
o que só ele faz, como dissoluções entre itens sobrepostos).
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
        elif name == "mlt":
            from velocity.engine.mlt import MltEngine
            _ENGINES[name] = MltEngine()
        else:
            raise ValueError(f"motor desconhecido: {name}")
    return _ENGINES[name]


def choose_engine(plan, order: tuple[str, ...] | None = None) -> RenderEngine:
    """Primeiro motor disponível (por ordem de preferência) cujos limites aceitam o plano.
    Preferência: VELOCITY_ENGINE (se definido), depois compositor, depois MLT."""
    from velocity.plan.validate import errors
    pref = os.environ.get("VELOCITY_ENGINE")
    order = order or tuple(dict.fromkeys([n for n in (pref, "compositor", "mlt") if n]))
    for name in order:
        try:
            eng = get_engine(name)
        except ImportError:  # imagem leve da API: valida contra o compositor, o worker tem o resto
            continue
        if getattr(eng, "available", lambda: True)() and not errors(eng.validate(plan)):
            return eng
    return get_engine(order[0])  # nenhum aceita: o preferido devolve os erros ao renderizar


__all__ = ["choose_engine", "Capabilities", "PlanRejected", "RenderEngine", "RenderReport", "check_capabilities", "get_engine"]
