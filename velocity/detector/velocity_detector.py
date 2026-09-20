"""Velocity Detector v0 — decide se uma referência é o estilo suportado.

REGRAS interpretáveis sobre a assinatura do Edit DNA (não é um modelo treinado). Cada
sub-pontuação é 0..1 e explicável ao utilizador. AVISO: os limiares foram escolhidos por
raciocínio + 7 positivos reais; SEM negativos reais ainda não há medida de falsos positivos.
Ver bench/detector_eval.py e docs/ para o protocolo de calibração.

Definição operacional (Velocity suportado): edição curta e dinâmica em que os planos são
ligados por transições com efeito (blur/whip, flash, dip, glitch), com movimento/zoom/velocidade
variáveis e cortes frequentemente presos às batidas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from velocity.schemas.edit_dna import EditDNA

MAX_DURATION_S = 60.0
MIN_SHOTS = 3


@dataclass
class Detection:
    is_velocity: bool
    score: float
    subscores: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def _ramp(x: float, lo: float, hi: float) -> float:
    return float(np.clip((x - lo) / (hi - lo), 0.0, 1.0))


def detect(dna: EditDNA, threshold: float = 0.55) -> Detection:
    g = dna.signature
    reasons: list[str] = []
    if dna.duration_s > MAX_DURATION_S:
        return Detection(False, 0.0, {}, [f"vídeo longo demais ({dna.duration_s:.0f}s > {MAX_DURATION_S:.0f}s): não é um Velocity curto"])
    if g.n_shots < MIN_SHOTS:
        return Detection(False, 0.05, {}, [f"só {g.n_shots} plano(s) detetado(s): sem ritmo de edição"])

    n_fx_trans = sum(v for k, v in g.transition_counts.items() if k != "cut")
    fx_rate = (n_fx_trans + len(dna.fx_events)) / max(dna.duration_s, 1e-6)
    s_fx = _ramp(fx_rate, 0.10, 0.55)  # transições/efeitos por segundo
    s_cadence = 1.0 if 0.25 <= g.cuts_per_second <= 3.5 else (_ramp(g.cuts_per_second, 0.08, 0.25) if g.cuts_per_second < 0.25 else _ramp(6.0 - g.cuts_per_second, 0.0, 2.5))
    s_dyn = float(np.clip(0.5 * g.zoom_active_ratio + 0.5 * g.speed_dynamic_ratio, 0, 1))
    a = dna.audio
    if a.has_audio and a.cut_beat_alignment is not None and a.chance_alignment is not None:
        s_beat = _ramp(a.cut_beat_alignment - a.chance_alignment, 0.0, 0.35)
        beat_known = True
    else:
        s_beat, beat_known = 0.5, False
    w = {"effects": 0.40, "cadence": 0.20, "dynamics": 0.20, "beat": 0.20}
    score = w["effects"] * s_fx + w["cadence"] * s_cadence + w["dynamics"] * s_dyn + w["beat"] * s_beat
    sub = {"effects": round(s_fx, 3), "cadence": round(s_cadence, 3), "dynamics": round(s_dyn, 3),
           "beat_lock": round(s_beat, 3) if beat_known else None}

    if n_fx_trans == 0 and not dna.fx_events:
        reasons.append("sem transições com efeito (só cortes secos ou nenhum corte)")
    if g.cuts_per_second < 0.25:
        reasons.append(f"poucos cortes ({g.cuts_per_second:.2f}/s)")
    if beat_known and s_beat == 0:
        reasons.append("cortes não presos às batidas (pode ser normal se a música não for detetável)")
    is_v = score >= threshold and s_fx > 0.0
    if is_v:
        reasons.insert(0, "transições com efeito + cadência + dinâmica compatíveis com Velocity")
    return Detection(is_v, round(float(score), 3), sub, reasons)
