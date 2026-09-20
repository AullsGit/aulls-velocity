"""
Speed Ramp — análise e reconstrução das curvas de velocidade que
caracterizam uma "velocity edit": planos que aceleram/desaceleram ao
longo da sua própria duração, tipicamente sincronizados com a música
(build lento -> impacto rápido, ou o inverso).

Isto é DIFERENTE do "time_warp" em src/motion_effects.py: aquele é um
efeito genérico de 2 velocidades fixas (lenta depois rápida), aplicado
por regra/pool a uma fração dos planos, sem olhar para o que a
referência realmente fez. Este módulo faz duas coisas que faltavam:

1. ANÁLISE: mede a "energia de movimento" (quanto muda visualmente de
   frame para frame, via optical flow denso) ao longo de CADA plano da
   referência e classifica a FORMA dessa curva — é o sinal mais próximo
   que conseguimos medir de "este plano foi acelerado/desacelerado na
   edição", sem teres a gravação original em bruto para comparar.
2. RECONSTRUÇÃO: aplica um perfil de velocidade por segmentos (N
   segmentos, não só 2) ao clip do utilizador, aproximando a forma
   detectada — generalização de motion_effects.compute_timewarp_split /
   apply_time_warp para mais de 2 segmentos.

HONESTIDADE (importante, como no resto do projeto):
- A análise NÃO distingue com certeza "este plano foi filmado assim"
  de "este plano foi acelerado em pós-produção" — medimos energia de
  movimento visual, não metadata de velocidade de captura (que não
  existe no ficheiro final). Um plano com muito movimento de conteúdo
  (ex: alguém a correr) pode parecer uma "curva de velocidade" mesmo
  sem ter sido reeditado. Isto é uma APROXIMAÇÃO por sinal indireto,
  não deteção certa — por isso só é aplicada quando a variação de
  energia dentro do plano ultrapassa um limiar mínimo
  (config.SPEED_RAMP_MIN_ENERGY_VARIATION), para evitar "inventar"
  rampas em planos que na verdade são uniformes.
- A reconstrução é uma aproximação por DEGRAUS (piecewise-constant) de
  uma curva contínua, não uma curva suave verdadeira — mais segmentos
  (config.SPEED_RAMP_N_SEGMENTS) aproximam melhor à custa de mais
  cortes internos ao ffmpeg (mais lento a renderizar, mais chance de
  artefactos nas fronteiras dos segmentos).
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np

import config


# --- ANÁLISE ---------------------------------------------------------

def _get_frame_at(video_path: str, timestamp_sec: float):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _flow_magnitude(frame_a, frame_b) -> float:
    """
    Energia de movimento entre dois frames: magnitude média do optical
    flow denso (Farneback), normalizada pela diagonal do frame (para não
    depender da resolução). Optical flow denso captura TODO o movimento
    visual (câmera + conteúdo), ao contrário do ORB usado em
    camera_motion.py (que isola só a transformação global) — aqui
    queremos exatamente o total, porque "quanto está a acontecer
    visualmente por segundo" é o que percebemos como "rápido"/"lento".
    """
    if frame_a is None or frame_b is None:
        return 0.0
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
    # Reduz para acelerar o cálculo — não precisamos de precisão pixel a
    # pixel, só da magnitude agregada.
    h, w = gray_a.shape
    scale = min(1.0, 240.0 / max(h, w))
    if scale < 1.0:
        gray_a = cv2.resize(gray_a, (int(w * scale), int(h * scale)))
        gray_b = cv2.resize(gray_b, (int(w * scale), int(h * scale)))

    flow = cv2.calcOpticalFlowFarneback(gray_a, gray_b, None, 0.5, 2, 15, 3, 5, 1.2, 0)
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    diag = np.hypot(*gray_a.shape)
    return float(np.mean(mag) / diag)


def compute_motion_energy_curve(video_path: str, start: float, end: float, n_samples: int = None) -> dict:
    """
    Amostra n_samples frames dentro de [start, end] e devolve a curva de
    energia de movimento entre amostras consecutivas:
    {"energies": [float,...] (len = n_samples-1, normalizado 0-1 dentro
    do próprio plano), "times": [float,...] (timestamp de CADA par,
    ponto médio), "raw_mean": float (energia bruta média, antes de
    normalizar — útil para comparar planos entre si)}.
    """
    n_samples = n_samples or getattr(config, "SPEED_RAMP_N_SAMPLES", 8)
    n_samples = max(3, n_samples)
    end_safe = max(start, end - 0.02)
    timestamps = np.linspace(start, end_safe, num=n_samples)
    frames = [_get_frame_at(video_path, float(t)) for t in timestamps]

    raw = []
    mid_times = []
    for i in range(len(frames) - 1):
        raw.append(_flow_magnitude(frames[i], frames[i + 1]))
        mid_times.append(float((timestamps[i] + timestamps[i + 1]) / 2))

    raw_arr = np.array(raw)
    raw_mean = float(np.mean(raw_arr)) if len(raw_arr) else 0.0
    raw_std = float(np.std(raw_arr)) if len(raw_arr) else 0.0
    # Coeficiente de variação (desvio padrão / média) da energia BRUTA —
    # ao contrário de uma normalização min-max por plano (que dá sempre
    # 1.0 de "variação" por construção, exceto quando literalmente
    # plano), isto mede se o movimento REALMENTE varia dentro do plano,
    # numa escala comparável entre planos.
    relative_variation = (raw_std / raw_mean) if raw_mean > 1e-6 else 0.0

    rmin, rmax = float(raw_arr.min()) if len(raw_arr) else 0.0, float(raw_arr.max()) if len(raw_arr) else 0.0
    if rmax - rmin < 1e-9:
        energies = [0.5] * len(raw)
    else:
        energies = [(r - rmin) / (rmax - rmin) for r in raw]

    return {
        "energies": energies, "times": mid_times,
        "raw_mean": raw_mean, "relative_variation": round(relative_variation, 4),
    }


def _smooth(arr: np.ndarray, window: int = 2) -> np.ndarray:
    """Média móvel simples — reduz o peso de um único par de frames
    ruidoso antes de classificar a FORMA da curva (o gate de 'vale a
    pena aplicar' usa relative_variation, calculado à parte, sobre o
    sinal bruto não suavizado)."""
    if len(arr) < 2 * window + 1:
        return arr
    kernel = np.ones(window * 2 + 1) / (window * 2 + 1)
    return np.convolve(arr, kernel, mode="same")


def classify_curve_shape(energies: list, relative_variation: float = None, min_variation: float = None) -> dict:
    """
    Classifica a FORMA da curva de energia normalizada (0-1) em:
      "flat"          — sem variação relevante (nada a reconstruir)
      "ramp_up"       — começa lento, acaba rápido (build -> impacto)
      "ramp_down"     — começa rápido, acaba lento (impacto -> respiro)
      "peak_middle"   — lento-rápido-lento (o padrão mais clássico de
                         velocity edit: acelera para o meio do plano e
                         volta a abrandar)
      "valley_middle" — rápido-lento-rápido (menos comum)

    O GATE "vale a pena aplicar" usa relative_variation (coeficiente de
    variação da energia BRUTA — ver compute_motion_energy_curve), não a
    amplitude da curva normalizada (essa é ~1.0 por construção em quase
    todos os planos, por isso sozinha não serve para decidir "isto é
    plano ou dinâmico"). Só entra em SPEED_RAMP_MIN_ENERGY_VARIATION.

    Retorna {"shape": str, "variation": float (coeficiente de variação
    bruto, para inspeção), "peak_position": float (0-1, onde ocorre o
    pico de energia dentro do plano — útil para alinhar com a batida)}.
    """
    min_variation = min_variation if min_variation is not None else getattr(config, "SPEED_RAMP_MIN_ENERGY_VARIATION", 0.35)
    relative_variation = relative_variation if relative_variation is not None else 0.0

    if not energies or len(energies) < 3:
        return {"shape": "flat", "variation": round(relative_variation, 3), "peak_position": 0.5}

    raw_shape_arr = np.array(energies)
    peak_idx = int(np.argmax(raw_shape_arr))
    peak_position = peak_idx / max(len(raw_shape_arr) - 1, 1)

    if relative_variation < min_variation:
        return {"shape": "flat", "variation": round(relative_variation, 3), "peak_position": round(peak_position, 3)}

    arr = _smooth(raw_shape_arr, window=1)
    third = max(1, len(arr) // 3)
    start_mean = float(np.mean(arr[:third]))
    mid_mean = float(np.mean(arr[third:-third])) if len(arr) > 2 * third else float(np.mean(arr))
    end_mean = float(np.mean(arr[-third:]))

    # Compara os 3 segmentos entre si para decidir a forma — limiares
    # relativos (não absolutos), porque já trabalhamos com energia
    # normalizada 0-1 dentro do próprio plano.
    if mid_mean > start_mean + 0.15 and mid_mean > end_mean + 0.15:
        shape = "peak_middle"
    elif mid_mean < start_mean - 0.15 and mid_mean < end_mean - 0.15:
        shape = "valley_middle"
    elif end_mean > start_mean + 0.15:
        shape = "ramp_up"
    elif start_mean > end_mean + 0.15:
        shape = "ramp_down"
    else:
        shape = "flat"

    return {"shape": shape, "variation": round(relative_variation, 3), "peak_position": round(peak_position, 3)}


def analyze_shot_speed(video_path: str, start: float, end: float) -> dict:
    """Combina compute_motion_energy_curve + classify_curve_shape num único resultado por plano."""
    curve = compute_motion_energy_curve(video_path, start, end)
    classification = classify_curve_shape(curve["energies"], relative_variation=curve["relative_variation"])
    return {**classification, "energies": [round(e, 3) for e in curve["energies"]], "raw_mean": round(curve["raw_mean"], 5)}


# --- PERFIS DE SEGMENTOS (reconstrução) -------------------------------

# Cada forma mapeia para uma sequência de multiplicadores de velocidade
# por segmento (aplicados em partes IGUAIS da duração do plano por
# defeito — ver build_segment_plan para o alinhamento a batidas). Valores
# escaláveis por config.SPEED_RAMP_INTENSITY (1.0 = como está aqui).
_SHAPE_SEGMENT_SPEEDS = {
    "ramp_up": [0.65, 0.85, 1.15, 1.6],
    "ramp_down": [1.6, 1.15, 0.85, 0.65],
    "peak_middle": [0.7, 1.0, 1.7, 1.0, 0.7],
    "valley_middle": [1.5, 1.0, 0.6, 1.0, 1.5],
    "flat": [1.0],
}


def _scale_speeds(speeds: list, intensity: float) -> list:
    """
    Escala os multiplicadores de velocidade em torno de 1.0 pela
    intensidade (0 = sem efeito/tudo a 1x, 1 = efeito completo como
    definido em _SHAPE_SEGMENT_SPEEDS, >1 = exagera ainda mais).
    """
    return [1.0 + (s - 1.0) * intensity for s in speeds]


def build_segment_plan(shape: str, total_duration: float, intensity: float = None, beat_times: list = None, shot_start: float = None) -> list:
    """
    Constrói o plano de segmentos para um plano com curva 'shape':
    devolve uma lista de (duration_fraction_of_clip_source, speed_multiplier)
    — frações da duração FONTE (antes do retiming) que, aplicadas às
    velocidades dadas, resultam em aproximadamente total_duration no
    output (ver compute_segment_source_durations).

    Se beat_times e shot_start forem passados, tenta ajustar as
    fronteiras dos segmentos para caírem perto de um beat (dentro de
    uma janela de tolerância) — é o mecanismo de "sincronizar a mudança
    de velocidade com a música" pedido na ideia original. Best-effort:
    se não houver beat próximo o suficiente, mantém a fronteira original
    (divisão igual).
    """
    intensity = intensity if intensity is not None else getattr(config, "SPEED_RAMP_INTENSITY", 1.0)
    speeds = _scale_speeds(_SHAPE_SEGMENT_SPEEDS.get(shape, _SHAPE_SEGMENT_SPEEDS["flat"]), intensity)
    n = len(speeds)

    # Fronteiras iguais por defeito (fração 0, 1/n, 2/n, ..., 1 da duração OUTPUT)
    boundaries = [i / n for i in range(n + 1)]

    if beat_times and shot_start is not None and n > 1:
        max_snap = getattr(config, "SPEED_RAMP_BEAT_SNAP_WINDOW", 0.2)
        beat_arr = np.array(beat_times)
        new_boundaries = [0.0]
        for b in boundaries[1:-1]:
            target_time = shot_start + b * total_duration
            idx = int(np.argmin(np.abs(beat_arr - target_time)))
            closest = float(beat_arr[idx])
            if abs(closest - target_time) <= max_snap:
                snapped_fraction = (closest - shot_start) / total_duration
                snapped_fraction = min(max(snapped_fraction, 0.02), 0.98)
                new_boundaries.append(snapped_fraction)
            else:
                new_boundaries.append(b)
        new_boundaries.append(1.0)
        # Garante ordem crescente (o snap pode, em teoria, inverter fronteiras vizinhas)
        boundaries = sorted(set(new_boundaries)) if len(set(new_boundaries)) == len(new_boundaries) else boundaries

    fractions = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
    return list(zip(fractions, speeds[:len(fractions)]))


def compute_segment_source_durations(segment_plan: list, total_output_duration: float) -> list:
    """
    Dado um plano [(fração_output, velocidade), ...] onde as frações
    somam 1.0 e descrevem quanto do OUTPUT cada segmento ocupa, calcula
    quanto de conteúdo FONTE (antes do retime) cada segmento precisa:
    source_duration = output_duration_do_segmento * velocidade (porque
    reproduzir a `velocidade`x mais rápido consome `velocidade`x mais
    conteúdo fonte por segundo de output). Devolve lista de
    (source_duration, speed) na mesma ordem — usado para saber quanto
    "roubar" da fonte e onde cortar.
    """
    result = []
    for frac, speed in segment_plan:
        out_dur = frac * total_output_duration
        src_dur = out_dur * speed
        result.append((src_dur, speed))
    return result


# --- RENDER (ffmpeg) ---------------------------------------------------

def _run(cmd: list):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Comando FFmpeg falhou (speed_ramp):\n{' '.join(cmd)}\n\nSTDERR:\n{result.stderr[-3000:]}")
    return result


def apply_speed_curve(input_path: str, output_path: str, segment_plan: list, total_output_duration: float):
    """
    Aplica um perfil de velocidade por segmentos a input_path (que deve
    ter pelo menos sum(source_durations) de conteúdo disponível — ver
    normalize_clip no renderer.py para como isso é garantido no in_point
    escolhido). Generaliza motion_effects.apply_time_warp de 2 para N
    segmentos, usando trim+setpts por segmento e concat no fim.

    segment_plan: lista de (fração_output, velocidade) — ver
    build_segment_plan. A soma das frações deve ser ~1.0.
    """
    source_segments = compute_segment_source_durations(segment_plan, total_output_duration)

    filter_parts = []
    labels = []
    cursor = 0.0
    for i, (src_dur, speed) in enumerate(source_segments):
        label = f"s{i}"
        setpts_factor = 1.0 / speed
        filter_parts.append(
            f"[0:v]trim=start={cursor:.4f}:end={cursor + src_dur:.4f},"
            f"setpts=PTS-STARTPTS,setpts={setpts_factor:.6f}*PTS[{label}]"
        )
        labels.append(f"[{label}]")
        cursor += src_dur

    concat_inputs = "".join(labels)
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={len(labels)}:v=1:a=0[out]"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-t", str(total_output_duration),  # salvaguarda contra desvios de arredondamento
        output_path,
    ]
    _run(cmd)


def required_source_duration(shape: str, total_output_duration: float, intensity: float = None) -> float:
    """
    Quanto de conteúdo FONTE (antes do retime) um plano com esta forma
    de curva precisa disponível, no total, para conseguir produzir
    total_output_duration de output. Usado por normalize_clip/EDL para
    verificar se o in_point escolhido tem material suficiente antes de
    tentar aplicar a curva — se não tiver, cai para "flat" (sem ramp)
    em vez de falhar ou cortar o clip a meio.
    """
    plan = build_segment_plan(shape, total_output_duration, intensity=intensity)
    segments = compute_segment_source_durations(plan, total_output_duration)
    return sum(src for src, _ in segments)


if __name__ == "__main__":
    import json
    from scene_detect import detect_scenes

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python speed_ramp.py <video>")
        sys.exit(1)

    shots = detect_scenes(path)
    for i, shot in enumerate(shots):
        analysis = analyze_shot_speed(path, shot["start"], shot["end"])
        print(f"Plano {i} [{shot['start']:.2f}s-{shot['end']:.2f}s]: {json.dumps(analysis)}")
