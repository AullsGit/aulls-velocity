"""
FASE 5 — EDL Builder
Junta tudo (shots classificados + slots preenchidos) num único objeto
EDL (Edit Decision List) — a "receita" final que o renderer vai seguir.

Formato do EDL (lista de dicts, um por clip na timeline final):
{
    "index": int,
    "source_path": str,
    "source_type": "video" | "photo",
    "in_point": float,
    "duration": float,
    "has_zoom": bool,
    "zoom_direction": "in" | "out" | None,
    "transition_to_next": "cut" | "fade",
    "motion_effect": None | "boomerang" | "bounce" | "time_warp",
}

"motion_effect" é decidido por REGRA, distribuído por config.EFFECT_POOL
respeitando uma cobertura mínima de config.EFFECT_COVERAGE_RATIO (ex:
0.7 = pelo menos 70% dos planos de VÍDEO recebem um efeito). Nunca
aplicado a fotos (não fazem sentido tecnicamente num plano estático).

Se config.USE_MUSIC_ENERGY_ASSIGNMENT estiver ativo e houver uma fonte
de áudio disponível, os planos são escolhidos por ORDEM DE ENERGIA da
música nesse intervalo de tempo (os momentos mais "intensos"/picos
recebem efeito primeiro) em vez de por posição — ver src/audio_analysis.py.
Isto NÃO é reconhecimento de música (não identifica a faixa/género), é
análise de energia (RMS) do áudio. Cai automaticamente para a
distribuição por posição se a análise falhar ou não houver áudio.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

try:
    import audio_analysis
except Exception:
    audio_analysis = None


def _assign_by_position(video_indices: list, pool: list, target_count: int) -> dict:
    """Distribuição uniforme por posição (fallback / comportamento original)."""
    total = len(video_indices)
    assignments = {}
    for k in range(target_count):
        pos = min(int(round(k * total / target_count)), total - 1)
        assignments[video_indices[pos]] = pool[k % len(pool)]
    return assignments


def _assign_by_energy(slots: list, video_indices: list, pool: list, target_count: int, beat_source: str) -> dict:
    """
    Distribuição por energia musical: calcula a energia média da música
    durante cada plano e dá efeito aos planos MAIS intensos primeiro
    (pool[0] vai para o pico mais alto, pool[1] para o 2º mais alto,
    etc., ciclando se houver mais planos escolhidos que itens no pool).
    """
    times, energies = audio_analysis.compute_energy_curve(beat_source)
    scored = []
    for idx in video_indices:
        slot = slots[idx]
        start = slot.get("start", 0.0)
        end = start + slot.get("target_duration", 0.0)
        score = audio_analysis.energy_in_window(times, energies, start, end)
        scored.append((score, idx))

    scored.sort(key=lambda x: x[0], reverse=True)  # mais energia primeiro
    selected = scored[:target_count]

    assignments = {}
    for rank, (score, idx) in enumerate(selected):
        assignments[idx] = pool[rank % len(pool)]
    return assignments


def _assign_motion_effects(slots: list, beat_source: str = None) -> dict:
    """
    Devolve {índice_global: nome_do_efeito} para os slots de vídeo,
    garantindo PELO MENOS EFFECT_COVERAGE_RATIO deles com um efeito
    (arredondado para CIMA quando a divisão não é exata).
    """
    coverage = getattr(config, "EFFECT_COVERAGE_RATIO", 0.0)
    pool = getattr(config, "EFFECT_POOL", [])
    if coverage <= 0 or not pool:
        return {}

    # Planos com uma curva de Speed Ramp REAL detectada (ver
    # config.USE_SPEED_RAMP, src/speed_ramp.py) já recebem um efeito de
    # velocidade específico no renderer — não entram no pool genérico,
    # para não competir/sobrepor dois efeitos dinâmicos no mesmo clip.
    use_speed_ramp = getattr(config, "USE_SPEED_RAMP", False)
    video_indices = [
        i for i, s in enumerate(slots)
        if s["source_type"] == "video"
        and not (use_speed_ramp and s.get("speed_curve_shape") not in (None, "flat"))
    ]
    total = len(video_indices)
    if total == 0:
        return {}

    target_count = min(total, math.ceil(coverage * total - 1e-9))
    if target_count <= 0:
        return {}

    use_energy = getattr(config, "USE_MUSIC_ENERGY_ASSIGNMENT", False) and audio_analysis and beat_source
    if use_energy:
        try:
            return _assign_by_energy(slots, video_indices, pool, target_count, beat_source)
        except Exception as e:
            print(f"  [Aviso] Análise de energia da música falhou ({e}), a usar distribuição por posição.")

    return _assign_by_position(video_indices, pool, target_count)


def build_edl(filled_slots: list, beat_source: str = None) -> list:
    effect_assignments = _assign_motion_effects(filled_slots, beat_source)
    edl = []
    for i, slot in enumerate(filled_slots):
        edl.append({
            "index": i,
            "source_path": slot["source_path"],
            "source_type": slot["source_type"],
            "in_point": slot["in_point"],
            "duration": slot["target_duration"],
            "has_zoom": slot["has_zoom"],
            "zoom_direction": slot["zoom_direction"],
            "transition_to_next": slot["transition_to_next"],
            "motion_effect": effect_assignments.get(i),
            "speed_curve_shape": slot.get("speed_curve_shape"),
            "shot_start_for_beatsnap": slot.get("start"),
        })
    return edl


def save_edl(edl: list, filename: str = "edl.json"):
    path = os.path.join(config.DIR_TEMP, filename)
    with open(path, "w") as f:
        json.dump(edl, f, indent=2)
    return path


def load_edl(filename: str = "edl.json") -> list:
    path = os.path.join(config.DIR_TEMP, filename)
    with open(path) as f:
        return json.load(f)
