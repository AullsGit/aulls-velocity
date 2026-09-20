"""
FASE 2 — Classificação de cortes
Para cada plano detectado na Fase 1, analisa:
  - se há zoom (comparando escala do frame inicial vs final do plano)
  - o tipo de transição para o próximo plano (corte seco vs fade/blend)
"""
import cv2
import numpy as np


def _get_frame_at(video_path: str, timestamp_sec: float):
    """Extrai um frame específico do vídeo no timestamp dado."""
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    return frame


def _estimate_zoom(frame_start, frame_end) -> float:
    """
    Estima a variação de escala entre dois frames usando ORB feature matching.
    Retorna um valor aproximado: >0 = zoom in, <0 = zoom out, ~0 = sem zoom.
    """
    if frame_start is None or frame_end is None:
        return 0.0

    gray1 = cv2.cvtColor(frame_start, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(frame_end, cv2.COLOR_BGR2GRAY)

    orb = cv2.ORB_create(500)
    kp1, des1 = orb.detectAndCompute(gray1, None)
    kp2, des2 = orb.detectAndCompute(gray2, None)

    if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
        return 0.0

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    if len(matches) < 10:
        return 0.0

    matches = sorted(matches, key=lambda m: m.distance)[:50]

    # Distância média entre pontos correspondentes ao centro, comparada entre os dois frames
    h, w = gray1.shape
    cx, cy = w / 2, h / 2

    dists1, dists2 = [], []
    for m in matches:
        x1, y1 = kp1[m.queryIdx].pt
        x2, y2 = kp2[m.trainIdx].pt
        dists1.append(np.hypot(x1 - cx, y1 - cy))
        dists2.append(np.hypot(x2 - cx, y2 - cy))

    mean1, mean2 = np.mean(dists1), np.mean(dists2)
    if mean1 == 0:
        return 0.0

    # Se os pontos ficaram mais próximos do centro proporcionalmente -> zoom in
    ratio = (mean1 - mean2) / mean1
    return float(ratio)


def _frame_similarity(frame_a, frame_b) -> float:
    """Similaridade simples via histograma de cor (0 a 1, 1 = idênticos)."""
    if frame_a is None or frame_b is None:
        return 0.0
    hist_a = cv2.calcHist([frame_a], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    hist_b = cv2.calcHist([frame_b], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))


def classify_shots(video_path: str, shots: list, zoom_threshold: float, fade_similarity_threshold: float):
    """
    Recebe a lista de shots da Fase 1 e devolve a mesma lista enriquecida com:
      - has_zoom: bool
      - zoom_direction: "in" | "out" | None
      - transition_to_next: "cut" | "fade"
    """
    enriched = []
    for i, shot in enumerate(shots):
        frame_start = _get_frame_at(video_path, shot["start"] + 0.05)
        frame_end = _get_frame_at(video_path, max(shot["start"] + 0.05, shot["end"] - 0.05))

        zoom_ratio = _estimate_zoom(frame_start, frame_end)
        has_zoom = abs(zoom_ratio) >= zoom_threshold
        zoom_direction = None
        if has_zoom:
            zoom_direction = "in" if zoom_ratio > 0 else "out"

        transition_to_next = "cut"
        if i < len(shots) - 1:
            next_shot = shots[i + 1]
            last_frame_this = _get_frame_at(video_path, max(shot["start"], shot["end"] - 0.05))
            first_frame_next = _get_frame_at(video_path, next_shot["start"] + 0.02)
            similarity = _frame_similarity(last_frame_this, first_frame_next)
            # Alta similaridade entre o fim de um plano e o início do próximo E
            # o scene detector não pegou como corte abrupto de conteúdo -> provável fade
            if similarity >= fade_similarity_threshold:
                transition_to_next = "fade"

        enriched.append({
            **shot,
            "has_zoom": has_zoom,
            "zoom_direction": zoom_direction,
            "transition_to_next": transition_to_next,
        })

    return enriched


if __name__ == "__main__":
    import sys
    import json
    from scene_detect import detect_scenes

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python classify_cuts.py <caminho_do_video>")
        sys.exit(1)

    shots = detect_scenes(path)
    classified = classify_shots(path, shots, zoom_threshold=0.08, fade_similarity_threshold=0.75)
    print(json.dumps(classified, indent=2))
