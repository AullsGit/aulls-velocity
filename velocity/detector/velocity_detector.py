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
    """v1: reconhece Velocity por CORTES+transições OU por EFEITOS densos sobre filmagem contínua (edição 'effect-driven')."""
    g = dna.signature
    vb = dna.vibe
    reasons: list[str] = []
    if dna.duration_s > MAX_DURATION_S:
        return Detection(False, 0.0, {}, [f"vídeo longo demais ({dna.duration_s:.0f}s > {MAX_DURATION_S:.0f}s): não é um Velocity curto"])

    n_fx_trans = sum(v for k, v in g.transition_counts.items() if k != "cut")
    eff_rate = vb.effect_density if vb else (n_fx_trans + len(dna.fx_events)) / max(dna.duration_s, 1e-6)
    if g.n_shots < MIN_SHOTS and eff_rate < 0.6:
        return Detection(False, 0.05, {"effects": round(_ramp(eff_rate, 0.25, 1.0), 3)},
                         [f"só {g.n_shots} plano(s) e poucos efeitos ({eff_rate:.2f}/s): sem ritmo de edição"])

    s_fx = _ramp(eff_rate, 0.25, 1.0)  # eventos de efeito por segundo (transições c/ efeito + fx + punch/shake/rgb + pulsos ancorados)
    s_cuts = 1.0 if 0.25 <= g.cuts_per_second <= 3.5 else (_ramp(g.cuts_per_second, 0.08, 0.25) if g.cuts_per_second < 0.25 else _ramp(6.0 - g.cuts_per_second, 0.0, 2.5))
    s_cadence = max(s_cuts, 0.8 * _ramp(eff_rate, 0.4, 1.2))  # edição por efeitos também tem cadência
    s_dyn = float(np.clip(0.5 * g.zoom_active_ratio + 0.5 * g.speed_dynamic_ratio, 0, 1))
    if vb:
        s_dyn = max(s_dyn, _ramp(vb.aggressiveness, 0.1, 0.5) * 0.9, _ramp(vb.zoom.get("punch_rate", 0), 0.15, 0.8))
    a = dna.audio
    if a.has_audio and a.cut_beat_alignment is not None and a.chance_alignment is not None:
        s_beat = _ramp(a.cut_beat_alignment - a.chance_alignment, 0.0, 0.35)
        if vb and vb.beat.get("impacts_on_beat") is not None:  # impactos da energia presos à batida também contam
            s_beat = max(s_beat, _ramp(vb.beat["impacts_on_beat"] - (a.chance_alignment or 0), 0.0, 0.4))
        beat_known = True
    else:
        s_beat, beat_known = 0.5, False
    w = {"effects": 0.40, "cadence": 0.20, "dynamics": 0.20, "beat": 0.20}
    score = w["effects"] * s_fx + w["cadence"] * s_cadence + w["dynamics"] * s_dyn + w["beat"] * s_beat
    sub = {"effects": round(s_fx, 3), "cadence": round(s_cadence, 3), "dynamics": round(s_dyn, 3),
           "beat_lock": round(s_beat, 3) if beat_known else None}

    if n_fx_trans == 0 and not dna.fx_events and eff_rate < 0.4:
        reasons.append("sem transições nem efeitos suficientes (só cortes secos ou nenhum corte)")
    if g.cuts_per_second < 0.25 and eff_rate < 0.6:
        reasons.append(f"poucos cortes ({g.cuts_per_second:.2f}/s) e poucos efeitos")
    if beat_known and s_beat == 0:
        reasons.append("cortes não presos às batidas (pode ser normal se a música não for detetável)")
    is_v = score >= threshold and s_fx > 0.0
    if is_v:
        reasons.insert(0, "efeitos/transições + cadência + dinâmica compatíveis com Velocity" + (" (guiado por efeitos)" if g.cuts_per_second < 0.4 else ""))
    return Detection(is_v, round(float(score), 3), sub, reasons)
