"""
Camera Motion — extensão da Fase 2 (classify_cuts.py)

Estima o movimento de câmera de um plano (pan horizontal/vertical, rotação
e escala/zoom) comparando frames amostrados ao longo do plano via feature
matching (ORB) + estimativa de transformação afim parcial
(cv2.estimateAffinePartial2D), que devolve escala+rotação+translação de
forma consistente entre si — mais rico do que a heurística de zoom
baseada só na distância média dos pontos ao centro usada em
classify_cuts.py (essa mantém-se intacta, para não quebrar o resto do
pipeline; este módulo é ADITIVO e alimenta o src/edit_dna.py).

O QUE ISTO NÃO É (honestidade, como no resto do projeto):
- Não é um estimador de pose de câmera 3D (SfM/SLAM/gyro data) — é uma
  estimativa 2D global do deslocamento entre frames.
- Não distingue "a câmera moveu-se" de "o assunto principal moveu-se
  muito e ocupa a maior parte do frame" — feature matching global mistura
  os dois. Para planos com um sujeito dominante e fundo pouco texturado,
  a estimativa fica menos confiável (ver campo "confidence").
- Rotação estimada é só rotação NO PLANO da imagem (roll), não
  tilt/pan 3D verdadeiros — suficiente para o que interessa numa edição
  2D (vídeos verticais tipo Reels/TikTok raramente têm rotação 3D real).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np


def _get_frame_at(video_path: str, timestamp_sec: float):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _sample_timestamps(start: float, end: float, n_samples: int):
    n_samples = max(2, n_samples)
    end_safe = max(start, end - 0.02)
    return list(np.linspace(start, end_safe, num=n_samples))


def _estimate_transform(frame_a, frame_b):
    """
    Devolve dict {"dx_norm","dy_norm","scale","rotation_deg","n_matches",
    "n_inliers"} descrevendo a transformação afim parcial (translação +
    rotação + escala uniforme) de frame_a para frame_b, ou None se não
    houver matches suficientes para uma estimativa confiável.
    """
    if frame_a is None or frame_b is None:
        return None

    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)

    orb = cv2.ORB_create(800)
    kp1, des1 = orb.detectAndCompute(gray_a, None)
    kp2, des2 = orb.detectAndCompute(gray_b, None)
    if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw_matches = bf.knnMatch(des1, des2, k=2)
    good = []
    for pair in raw_matches:
        if len(pair) != 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    if len(good) < 8:
        return None

    pts1 = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    pts2 = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    M, inliers_mask = cv2.estimateAffinePartial2D(
        pts1, pts2, method=cv2.RANSAC, ransacReprojThreshold=3.0
    )
    if M is None:
        return None

    a, b = M[0, 0], M[0, 1]
    scale = float(np.hypot(a, b))
    rotation_rad = float(np.arctan2(b, a))
    dx, dy = float(M[0, 2]), float(M[1, 2])
    n_inliers = int(inliers_mask.sum()) if inliers_mask is not None else len(good)

    h, w = gray_a.shape
    return {
        "dx_norm": dx / w,
        "dy_norm": dy / h,
        "scale": scale,
        "rotation_deg": float(np.degrees(rotation_rad)),
        "n_matches": len(good),
        "n_inliers": n_inliers,
    }


def estimate_shot_motion(video_path: str, start: float, end: float, n_samples: int = 5) -> dict:
    """
    Amostra n_samples frames ao longo de [start, end] e estima a
    transformação entre frames consecutivos, devolvendo um resumo do
    movimento de câmera do plano inteiro:

    {
      "pan_x": float,          # translação horizontal acumulada, normalizada pela largura do frame (+ = direita)
      "pan_y": float,          # idem vertical (+ = baixo)
      "rotation_deg": float,   # rotação acumulada estimada (graus, + = sentido horário)
      "zoom_scale": float,     # >1 = zoom in acumulado, <1 = zoom out, ~1 = sem zoom
      "motion_type": "static" | "pan" | "zoom" | "rotation" | "handheld" | "unknown",
      "confidence": float,     # 0-1, fração de pares de frames consecutivos com estimativa válida
      "n_pairs_estimated": int,
      "n_pairs_total": int,
    }

    "handheld" é um sinal heurístico de tremor/instabilidade (direção do
    movimento inconsistente entre pares consecutivos), não deteção de
    estabilização óptica nem de tipo de suporte usado.
    """
    timestamps = _sample_timestamps(start, end, n_samples)
    frames = [_get_frame_at(video_path, t) for t in timestamps]

    transforms = []
    for i in range(len(frames) - 1):
        t = _estimate_transform(frames[i], frames[i + 1])
        if t is None:
            continue
        # Rejeição de estimativas implausíveis: quando o conteúdo entre os
        # dois frames é demasiado diferente (corte escondido dentro do
        # plano, movimento de sujeito muito rápido, baixa textura), o
        # RANSAC por vezes "encontra" uma transformação com poucos
        # inliers ou valores absurdos (ex: escala 0.06x) — sem este
        # filtro, esses outliers dominam o produto acumulado de zoom_scale
        # e distorcem o resultado do plano inteiro.
        inlier_ratio = t["n_inliers"] / t["n_matches"] if t["n_matches"] else 0.0
        plausible = (
            inlier_ratio >= 0.35
            and t["n_inliers"] >= 6
            and 0.3 <= t["scale"] <= 3.0
            and abs(t["dx_norm"]) <= 0.5
            and abs(t["dy_norm"]) <= 0.5
        )
        if plausible:
            transforms.append(t)

    n_pairs_total = max(len(frames) - 1, 0)
    if not transforms:
        return {
            "pan_x": 0.0, "pan_y": 0.0, "rotation_deg": 0.0, "zoom_scale": 1.0,
            "motion_type": "unknown", "confidence": 0.0,
            "n_pairs_estimated": 0, "n_pairs_total": n_pairs_total,
        }

    dxs = [t["dx_norm"] for t in transforms]
    dys = [t["dy_norm"] for t in transforms]
    pan_x = float(sum(dxs))
    pan_y = float(sum(dys))
    rotation_deg = float(sum(t["rotation_deg"] for t in transforms))
    zoom_scale = 1.0
    for t in transforms:
        zoom_scale *= t["scale"]

    confidence = len(transforms) / n_pairs_total if n_pairs_total else 0.0
    jitter = float(np.std(dxs) + np.std(dys)) if len(transforms) > 1 else 0.0

    abs_pan = max(abs(pan_x), abs(pan_y))
    abs_rotation = abs(rotation_deg)
    abs_zoom = abs(zoom_scale - 1.0)

    # Limiares heurísticos calibrados para movimento "percetível" numa
    # edição típica de vídeo curto — não são medidas físicas.
    if abs_pan < 0.01 and abs_rotation < 1.0 and abs_zoom < 0.02:
        motion_type = "static"
    elif jitter > 0.04 and abs_pan < 0.08 and abs_zoom < 0.05:
        motion_type = "handheld"
    elif abs_rotation >= 3.0 and abs_rotation > abs_pan * 50:
        motion_type = "rotation"
    elif abs_zoom >= 0.05 and abs_zoom * 5 > abs_pan:
        motion_type = "zoom"
    elif abs_pan >= 0.02:
        motion_type = "pan"
    else:
        motion_type = "static"

    return {
        "pan_x": round(pan_x, 4),
        "pan_y": round(pan_y, 4),
        "rotation_deg": round(rotation_deg, 2),
        "zoom_scale": round(zoom_scale, 4),
        "motion_type": motion_type,
        "confidence": round(confidence, 2),
        "n_pairs_estimated": len(transforms),
        "n_pairs_total": n_pairs_total,
    }


if __name__ == "__main__":
    import json
    from scene_detect import detect_scenes

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python camera_motion.py <video>")
        sys.exit(1)

    shots = detect_scenes(path)
    for i, shot in enumerate(shots):
        motion = estimate_shot_motion(path, shot["start"], shot["end"])
        print(f"Plano {i} [{shot['start']:.2f}s-{shot['end']:.2f}s]: {json.dumps(motion)}")
