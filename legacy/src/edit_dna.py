"""
Edit DNA — representação estruturada e unificada da "receita" de edição
de um vídeo (a referência, OU um vídeo já gerado pelo pipeline, para
comparação — ver src/compare_refine.py).

Junta numa única estrutura JSON tudo o que o pipeline já sabe analisar
sobre um vídeo: cortes e duração de cada plano (Fase 1), zoom e tipo de
transição (Fase 2), movimento de câmera — pan/rotação/zoom
(camera_motion.py), cor por plano (color_match.py), presença de
sujeito/pessoa por plano (subject_detect.py, se o modelo estiver
disponível) e overlays de texto por plano (overlay_detect.py, se o
Tesseract estiver disponível/ativado). Quando há uma fonte de áudio
associada, inclui também tempo/beats/energia (beat_sync.py,
audio_analysis.py).

É a peça central pedida no objetivo do projeto: transformar a análise da
referência numa "receita digital de edição" explícita — um objeto que
pode ser guardado, inspecionado e comparado — em vez de ficar implícita
e espalhada pelas várias fases do pipeline.

HONESTIDADE (como no resto do projeto): isto é uma AGREGAÇÃO das
análises heurísticas já existentes (cada uma com as suas limitações
documentadas no seu próprio módulo), não um modelo novo e não é
compreensão semântica ao nível de um LLM multimodal. Os campos
"camera_motion", "subject" e "overlays" só são preenchidos com dados
reais quando a respetiva análise está disponível/ativada — nunca são
inventados; ficam None/[] caso contrário, e cada bloco diz de onde vem.
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import cv2

import config
from scene_detect import detect_scenes
from classify_cuts import classify_shots
import color_match
import camera_motion

try:
    import subject_detect
except Exception:
    subject_detect = None

try:
    import overlay_detect
except Exception:
    overlay_detect = None

try:
    import speed_ramp
except Exception:
    speed_ramp = None

try:
    import beat_sync
    import audio_analysis
except Exception:
    beat_sync = None
    audio_analysis = None


def _video_meta(video_path: str) -> dict:
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    duration = (n_frames / fps) if fps else 0.0
    return {"fps": float(fps), "resolution": [w, h], "duration": float(duration)}


def _build_audio_summary(beat_source: str) -> dict:
    summary = {"beat_source": beat_source, "tempo_bpm": None, "n_beats": None, "mean_energy": None}
    if not (beat_source and beat_sync):
        return summary
    try:
        beats, tempo = beat_sync.detect_beats(beat_source)
        summary["tempo_bpm"] = round(float(tempo), 1)
        summary["n_beats"] = len(beats)
        summary["beat_times"] = [round(b, 3) for b in beats]
    except Exception as e:
        summary["error"] = str(e)
    if audio_analysis:
        try:
            _, energies = audio_analysis.compute_energy_curve(beat_source)
            summary["mean_energy"] = round(float(np.mean(energies)), 3) if len(energies) else None
        except Exception:
            pass
    return summary


def build_edit_dna(
    video_path: str,
    shots: list = None,
    beat_source: str = None,
    analyze_camera_motion: bool = True,
    analyze_color: bool = True,
    analyze_subject: bool = None,
    analyze_overlays: bool = None,
    analyze_speed: bool = None,
    label: str = "reference",
) -> dict:
    """
    Constrói a Edit DNA de video_path.

    Se 'shots' não for passado, deteta e classifica automaticamente
    (Fases 1+2: scene_detect + classify_cuts). Passar 'shots' já
    classificados evita reanálise quando o chamador já os tem (ex:
    main.py, que já correu essas fases para construir o EDL) — IMPORTANTE
    para a Edit DNA da REFERÊNCIA bater certo com o que realmente guiou a
    edição, incluindo ajustes como FORCE_ZOOM ou beat-snap.

    analyze_subject/analyze_overlays, se None, usam
    config.USE_SUBJECT_BOOST / config.USE_OVERLAY_DETECTION como
    default — mas o resultado real depende sempre de is_available() dos
    respetivos módulos, nunca falha o pipeline se não estiverem
    disponíveis.
    """
    if analyze_subject is None:
        analyze_subject = getattr(config, "USE_SUBJECT_BOOST", False)
    if analyze_overlays is None:
        analyze_overlays = getattr(config, "USE_OVERLAY_DETECTION", False)
    if analyze_speed is None:
        analyze_speed = getattr(config, "USE_SPEED_RAMP", False)

    if shots is None:
        raw_shots = detect_scenes(video_path)
        shots = classify_shots(
            video_path, raw_shots,
            zoom_threshold=config.ZOOM_SCALE_THRESHOLD,
            fade_similarity_threshold=config.FADE_SIMILARITY_THRESHOLD,
        )

    meta = _video_meta(video_path)

    shot_entries = []
    for i, shot in enumerate(shots):
        entry = {
            "index": i,
            "start": round(shot["start"], 3),
            "end": round(shot["end"], 3),
            "duration": round(shot["duration"], 3),
            "transition_to_next": shot.get("transition_to_next", "cut"),
            "zoom": {
                "has_zoom": bool(shot.get("has_zoom", False)),
                "direction": shot.get("zoom_direction"),
            },
            "camera_motion": None,
            "color": None,
            "subject": None,
            "overlays": [],
            "speed_curve": None,
        }

        if analyze_camera_motion:
            try:
                entry["camera_motion"] = camera_motion.estimate_shot_motion(video_path, shot["start"], shot["end"])
            except Exception as e:
                entry["camera_motion"] = {"error": str(e)}

        if analyze_color:
            try:
                entry["color"] = color_match.analyze_color_profile_range(video_path, shot["start"], shot["end"])
            except Exception as e:
                entry["color"] = {"error": str(e)}

        if analyze_subject and subject_detect and subject_detect.is_available():
            try:
                entry["subject"] = subject_detect.estimate_subject_presence(video_path, shot["start"], shot["end"])
            except Exception as e:
                entry["subject"] = {"error": str(e)}

        if analyze_overlays and overlay_detect and overlay_detect.is_available():
            try:
                entry["overlays"] = overlay_detect.detect_overlays_for_shot(video_path, shot["start"], shot["end"])
            except Exception:
                entry["overlays"] = []

        if analyze_speed and speed_ramp:
            try:
                entry["speed_curve"] = speed_ramp.analyze_shot_speed(video_path, shot["start"], shot["end"])
            except Exception as e:
                entry["speed_curve"] = {"error": str(e)}

        shot_entries.append(entry)

    audio_summary = _build_audio_summary(beat_source) if beat_source else None

    n_shots = len(shot_entries)
    total_duration = meta["duration"] or sum(s["duration"] for s in shot_entries)
    n_zoom = sum(1 for s in shot_entries if s["zoom"]["has_zoom"])
    n_fade = sum(1 for s in shot_entries if s["transition_to_next"] == "fade")
    avg_shot_duration = (sum(s["duration"] for s in shot_entries) / n_shots) if n_shots else 0.0

    style_signature = {
        "n_shots": n_shots,
        "avg_shot_duration": round(avg_shot_duration, 3),
        "cuts_per_minute": round(n_shots / (total_duration / 60), 2) if total_duration > 0 else 0.0,
        "zoom_ratio": round(n_zoom / n_shots, 3) if n_shots else 0.0,
        "fade_ratio": round(n_fade / n_shots, 3) if n_shots else 0.0,
    }

    return {
        "label": label,
        "source_path": video_path,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "video": meta,
        "shots": shot_entries,
        "audio": audio_summary,
        "style_signature": style_signature,
    }


def save_edit_dna(dna: dict, path: str) -> str:
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dna, f, indent=2, ensure_ascii=False)
    return path


def load_edit_dna(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python edit_dna.py <video> [audio_ou_video_para_beats]")
        sys.exit(1)
    beat_src = sys.argv[2] if len(sys.argv) > 2 else None

    dna = build_edit_dna(path, beat_source=beat_src)
    print(json.dumps(dna, indent=2, ensure_ascii=False))
