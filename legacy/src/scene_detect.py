"""
FASE 1 — Scene Detection
Detecta os pontos de corte no vídeo de referência e devolve a lista
de planos (shots) com os seus timestamps.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector
import config


def detect_scenes(video_path: str):
    """
    Retorna uma lista de dicts:
    [{"start": 0.0, "end": 1.24, "duration": 1.24}, ...]
    """
    video = open_video(video_path)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=config.SCENE_DETECT_THRESHOLD))

    scene_manager.detect_scenes(video, show_progress=False)
    scene_list = scene_manager.get_scene_list()

    if not scene_list:
        # Nenhum corte detectado -> trata o vídeo inteiro como um único plano
        duration = video.duration.get_seconds()
        return [{"start": 0.0, "end": duration, "duration": duration}]

    shots = []
    for start_tc, end_tc in scene_list:
        start = start_tc.get_seconds()
        end = end_tc.get_seconds()
        dur = end - start
        if dur < config.MIN_SHOT_DURATION:
            # Funde planos muito curtos ao anterior para evitar ruído
            if shots:
                shots[-1]["end"] = end
                shots[-1]["duration"] = shots[-1]["end"] - shots[-1]["start"]
                continue
        shots.append({"start": start, "end": end, "duration": dur})

    return shots


if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python scene_detect.py <caminho_do_video>")
        sys.exit(1)

    shots = detect_scenes(path)
    print(f"Detectados {len(shots)} planos:")
    print(json.dumps(shots, indent=2))
