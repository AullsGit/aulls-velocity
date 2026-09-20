"""Planeador de efeitos: EffectTracks MEDIDAS na referência -> efeitos da timeline.

Hierarquia (ordem de prioridade): 1) fidelidade à referência, 2) qualidade técnica, 3) efeitos
complementares. Este módulo é a camada 1 para efeitos:
  * REGRA DA EVIDÊNCIA: só se transfere o que foi medido. Nada é adicionado por 'ficar mais bonito'.
  * Filtros de precisão: pulsos só se estiverem ancorados a um corte/batida (pulso 'livre' é quase
    sempre conteúdo da cena); rgb_split só com intensidade mínima (deteção de baixa confiança).
  * Os efeitos seguem os cortes: mapeamento linear por partes dos frames antigos para os novos
    (relevante quando os cortes são re-cronometrados à música).
"""
from __future__ import annotations

import numpy as np

from velocity.schemas.edit_dna import EditDNA, EffectTrack

MIN_PUNCH = 0.045
MIN_RGB_INTENSITY = 0.25
MIN_PULSE_INTENSITY = 0.30


def _accept(t: EffectTrack) -> bool:
    if t.type == "zoom_punch":
        return t.confidence >= 0.5 and abs(t.magnitude) >= MIN_PUNCH
    if t.type == "shake":
        return t.confidence >= 0.5 and t.intensity > 0.0
    if t.type == "rgb_split":
        return t.intensity >= MIN_RGB_INTENSITY
    if t.type.endswith("_pulse"):
        return t.anchor in ("cut", "beat") and t.intensity >= MIN_PULSE_INTENSITY
    return False


def plan_effects(dna: EditDNA, new_cuts: list[int]) -> tuple[list[EffectTrack], dict]:
    old = [0] + dna.cut_frames() + [dna.n_frames]
    new = [0] + list(new_cuts) + [dna.n_frames]
    fmap = lambda f: float(np.interp(f, old, new))  # noqa: E731
    out: list[EffectTrack] = []
    dropped = {"rejeitado_por_evidencia": 0}
    for t in dna.effects:
        if not _accept(t):
            dropped["rejeitado_por_evidencia"] += 1
            continue
        a, b, p = int(round(fmap(t.start_frame))), int(round(fmap(t.end_frame))), int(round(fmap(t.peak_frame)))
        a, b = max(0, a), min(dna.n_frames, max(b, a + 2))
        if b <= a:
            continue
        out.append(t.model_copy(update={"start_frame": a, "end_frame": b, "peak_frame": min(max(p, a), b - 1)}))
    kinds: dict[str, int] = {}
    for t in out:
        kinds[t.type] = kinds.get(t.type, 0) + 1
    return out, {"transferidos": kinds, **dropped}
