"""
Color Matching — aproxima o tom/luz/saturação dos teus clips ao vídeo de
referência.

Abordagem: mede o brilho médio, contraste (desvio padrão do brilho) e
saturação média da referência (amostrando alguns frames), depois calcula
os parâmetros do filtro `eq` do FFmpeg necessários para empurrar cada
clip do utilizador nessa direção.

SIMPLIFICAÇÃO ASSUMIDA: isto aproxima o "look" geral (tom/luz/saturação),
não é uma cópia exata de LUT/curvas de cor — grading profissional usa
curvas por canal e por zona tonal (sombras/meios-tons/realces), o que
exigiria extração de LUT 3D, fora do escopo deste MVP.
"""
import cv2
import numpy as np


def _sample_frames(video_path: str, n_samples: int = 5):
    """Extrai n_samples frames distribuídos uniformemente ao longo do vídeo."""
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        return []

    indices = np.linspace(0, max(total_frames - 1, 0), num=min(n_samples, total_frames), dtype=int)
    frames = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()
    return frames


def _profile_from_frames(frames: list) -> dict:
    """Núcleo do cálculo de perfil de cor, partilhado por analyze_color_profile
    (amostra o vídeo inteiro) e analyze_color_profile_range (amostra só um
    intervalo — usado pelo src/edit_dna.py para um perfil POR PLANO)."""
    if not frames:
        return {"mean_v": 128.0, "std_v": 50.0, "mean_s": 100.0}

    v_values = []
    s_values = []
    for frame in frames:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        s_values.append(float(np.mean(hsv[:, :, 1])))
        v_values.append(hsv[:, :, 2].astype(np.float64))

    all_v = np.concatenate([v.flatten() for v in v_values])
    return {
        "mean_v": float(np.mean(all_v)),
        "std_v": float(np.std(all_v)),
        "mean_s": float(np.mean(s_values)),
    }


def analyze_color_profile(video_path: str, n_samples: int = 5) -> dict:
    """
    Retorna {"mean_v": float, "std_v": float, "mean_s": float} — brilho
    médio, contraste (desvio padrão do brilho) e saturação média, medidos
    em espaço HSV sobre frames amostrados de TODO o vídeo.
    """
    return _profile_from_frames(_sample_frames(video_path, n_samples))


def analyze_color_profile_range(video_path: str, start: float, end: float, n_samples: int = 3) -> dict:
    """
    Mesmo cálculo que analyze_color_profile, mas amostrando só o
    intervalo [start, end] segundos — usado para obter um perfil de cor
    POR PLANO (ver src/edit_dna.py) em vez de um perfil global do vídeo.
    """
    n_samples = max(1, n_samples)
    end_safe = max(start, end - 0.02)
    timestamps = np.linspace(start, end_safe, num=n_samples)

    cap = cv2.VideoCapture(video_path)
    frames = []
    for t in timestamps:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
        ok, frame = cap.read()
        if ok:
            frames.append(frame)
    cap.release()

    return _profile_from_frames(frames)


def compute_eq_filter(clip_profile: dict, target_profile: dict, intensity: float = 1.0) -> str:
    """
    Calcula os parâmetros do filtro `eq` do FFmpeg para empurrar um clip
    (com clip_profile) na direção do target_profile (perfil da referência).

    'intensity' (0.0-1.0) controla o quanto do ajuste é realmente aplicado:
    1.0 = ajuste total (pode saturar se os cenários forem muito diferentes
    da referência); 0.0 = nenhum ajuste (mantém o clip como está). Valores
    baixos (ex: 0.4-0.6) dão um "toque" de cor sem forçar demasiado quando
    o conteúdo do utilizador é visualmente muito diferente da referência.
    """
    intensity = float(np.clip(intensity, 0.0, 1.0))

    # Brilho: eq 'brightness' é aditivo, escala aproximada -1..1 sobre 0..255
    brightness_full = (target_profile["mean_v"] - clip_profile["mean_v"]) / 255.0
    brightness = float(np.clip(brightness_full * intensity, -0.25, 0.25))

    # Contraste: eq 'contrast' é multiplicativo em torno de 1.0
    clip_std = max(clip_profile["std_v"], 1e-3)
    contrast_full = target_profile["std_v"] / clip_std
    # Interpola entre 1.0 (sem alteração) e o valor total, pela intensidade
    contrast = 1.0 + (contrast_full - 1.0) * intensity
    contrast = float(np.clip(contrast, 0.6, 1.6))

    # Saturação: eq 'saturation' é multiplicativo em torno de 1.0
    clip_s = max(clip_profile["mean_s"], 1e-3)
    saturation_full = target_profile["mean_s"] / clip_s
    saturation = 1.0 + (saturation_full - 1.0) * intensity
    saturation = float(np.clip(saturation, 0.5, 1.8))

    return f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:saturation={saturation:.4f}"


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python color_match.py <video>")
        sys.exit(1)
    profile = analyze_color_profile(path)
    print(profile)
