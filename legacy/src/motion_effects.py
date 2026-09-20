"""
Motion Effects — mecanismos de pós-processamento aplicados a um clip já
normalizado (já com zoom/cor/vfx do estilo aplicados).

Apenas 3 efeitos ativos no sistema (por pedido explícito):
  - boomerang: UMA ida e volta (toca para a frente, depois para trás)
  - bounce: boomerang REPETIDO várias vezes (oscila para trás e para a
    frente repetidamente) — tecnicamente é um boomerang encadeado com
    um loop; é o que os distingue de verdade um do outro.
  - time_warp: rampa de velocidade (começa devagar, acelera)

Todos preservam a duração total do plano (entry["duration"]) — importante
para não desalinhar o beat sync e as transições da timeline.
"""
import subprocess


def _run(cmd: list):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando FFmpeg falhou (motion_effects):\n{' '.join(cmd)}\n\nSTDERR:\n{result.stderr[-3000:]}"
        )
    return result


def apply_boomerang(input_path: str, output_path: str):
    """Ida e volta: o clip de entrada já deve ter metade da duração final."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", "[0:v]split[a][b];[b]reverse[b2];[a][b2]concat=n=2:v=1:a=0[out]",
        "-map", "[out]",
        output_path,
    ]
    _run(cmd)


def _apply_loop(input_path: str, output_path: str, reps: int, fps: int,
                 segment_duration: float, total_duration: float):
    """
    Repete o conteúdo do clip 'reps' vezes seguidas. Usado internamente
    pelo 'bounce' (repete um ciclo boomerang já feito), não é um efeito
    exposto ao utilizador por si só.
    """
    n_frames = max(int(round(segment_duration * fps)), 1)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"loop=loop={reps - 1}:size={n_frames}:start=0",
        "-t", str(total_duration),  # salvaguarda contra pequenos desvios de arredondamento
        output_path,
    ]
    _run(cmd)


def apply_bounce(base_clip_path: str, cycle_tmp_path: str, output_path: str,
                  reps: int, fps: int, total_duration: float):
    """
    Bounce = boomerang (uma ida-e-volta) repetido 'reps' vezes, para dar
    uma oscilação contínua em vez de um único movimento. 'base_clip_path'
    já deve ter duration = total_duration / (2*reps) (a "perna" de ida).
    'cycle_tmp_path' é um ficheiro intermédio (1 ciclo completo
    ida+volta) que é apagado pelo chamador depois.
    """
    cycle_duration = total_duration / reps
    apply_boomerang(base_clip_path, cycle_tmp_path)
    _apply_loop(cycle_tmp_path, output_path, reps=reps, fps=fps,
                segment_duration=cycle_duration, total_duration=total_duration)


def compute_timewarp_split(total_duration: float, slow_speed: float = 0.6, fast_speed: float = 1.8):
    """
    Calcula como dividir 'total_duration' de conteúdo já existente em
    duas partes — uma tocada mais devagar (slow_speed) e outra mais
    rápida (fast_speed) — de forma a que a soma das duas partes, já
    retimed, resulte EXATAMENTE em total_duration de novo (não precisa
    de imagem extra da fonte, ao contrário do boomerang/bounce).
    Retorna (s1, s2, slow_speed, fast_speed): s1/s2 são as porções (em
    segundos) do clip ORIGINAL a atribuir a cada velocidade.
    """
    inv_slow, inv_fast = 1.0 / slow_speed, 1.0 / fast_speed
    denom = inv_slow - inv_fast
    if abs(denom) < 1e-6:
        # Velocidades iguais não fazem sentido para um time warp -> sem split real
        return total_duration / 2, total_duration / 2, slow_speed, fast_speed
    s1 = total_duration * (1.0 - inv_fast) / denom
    s1 = max(0.05, min(total_duration - 0.05, s1))
    s2 = total_duration - s1
    return s1, s2, slow_speed, fast_speed


def apply_time_warp(input_path: str, output_path: str, s1: float, s2: float,
                     slow_speed: float, fast_speed: float, total_duration: float = None):
    """
    Aplica a rampa de velocidade: primeiros s1 segundos do clip tocam a
    slow_speed (mais devagar), os s2 segundos seguintes tocam a
    fast_speed (mais rápido) — sensação de "câmara lenta que acelera
    para o impacto". A duração total do output mantém-se igual à do
    clip de entrada (ver compute_timewarp_split). 'total_duration', se
    passado, força um corte final exato (salvaguarda contra pequenos
    desvios de arredondamento entre frames).
    """
    setpts_slow = 1.0 / slow_speed
    setpts_fast = 1.0 / fast_speed
    filter_complex = (
        f"[0:v]trim=start=0:end={s1:.4f},setpts=PTS-STARTPTS,setpts={setpts_slow:.6f}*PTS[a];"
        f"[0:v]trim=start={s1:.4f}:end={s1 + s2:.4f},setpts=PTS-STARTPTS,setpts={setpts_fast:.6f}*PTS[b];"
        f"[a][b]concat=n=2:v=1:a=0[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
    ]
    if total_duration:
        cmd += ["-t", str(total_duration)]
    cmd.append(output_path)
    _run(cmd)
