"""
FASE 4 — Content Mapper
Recebe os "slots" extraídos do padrão da referência (Fases 1-3) e os
ficheiros do utilizador (vídeos + fotos), e decide qual ficheiro do
utilizador preenche cada slot.

Duas estratégias disponíveis (controladas por config.USE_SEMANTIC_MATCHING):

  1. SEMÂNTICA (CLIP, local, grátis) — para cada plano da referência,
     escolhe o clip do utilizador visualmente mais parecido, usando
     embeddings CLIP. Requer `pip install torch open_clip_torch`.
     Se as dependências não estiverem instaladas, cai automaticamente
     para a estratégia sequencial abaixo (sem travar o pipeline).

  2. SEQUENCIAL (fallback, sempre disponível) — round-robin simples
     pelos vídeos primeiro; quando os vídeos acabam, usa fotos com
     Ken Burns.
"""
import os
import sys
import random
import subprocess
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

try:
    import semantic_match
except Exception:
    # Cobre tanto "não instalado" quanto instalações quebradas de torch/CLIP
    semantic_match = None


def _get_video_duration(path: str) -> float:
    """Usa ffprobe para obter a duração de um ficheiro de vídeo."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def list_user_media():
    """Lista vídeos e fotos do utilizador nas pastas de input."""
    video_exts = (".mp4", ".mov", ".mkv", ".avi", ".webm")
    photo_exts = (".jpg", ".jpeg", ".png", ".webp")

    videos = sorted([
        os.path.join(config.DIR_USER_VIDEOS, f)
        for f in os.listdir(config.DIR_USER_VIDEOS)
        if f.lower().endswith(video_exts)
    ])
    photos = sorted([
        os.path.join(config.DIR_USER_PHOTOS, f)
        for f in os.listdir(config.DIR_USER_PHOTOS)
        if f.lower().endswith(photo_exts)
    ])
    return videos, photos


def _pick_in_point(source_path: str, target_duration: float, video_durations: dict, reused: bool) -> float:
    src_duration = video_durations[source_path]
    if src_duration <= target_duration:
        return 0.0
    max_start = src_duration - target_duration
    return 0.0 if not reused else round(random.uniform(0, max_start), 2)


def _map_semantic(shots: list, reference_path: str, videos: list, photos: list, video_durations: dict):
    """Estratégia 1: escolhe o clip mais parecido visualmente para cada plano."""
    print("  [Content Mapper] Usando matching semântico (CLIP)...")
    shot_embeddings = semantic_match.embed_reference_shots(reference_path, shots)
    media_embeddings = semantic_match.embed_user_media(videos, photos)
    matches = semantic_match.match_shots_to_media(shot_embeddings, media_embeddings)

    seen_count = {}
    filled_slots = []
    for shot, match in zip(shots, matches):
        source_path = match["source_path"]
        seen_count[source_path] = seen_count.get(source_path, 0) + 1
        reused = seen_count[source_path] > 1
        target_duration = shot["duration"]
        is_photo = source_path in photos

        if is_photo:
            source_type = "photo"
            in_point = 0.0
            target_duration = max(target_duration, config.PHOTO_DURATION_SEC)
        else:
            source_type = "video"
            in_point = _pick_in_point(source_path, target_duration, video_durations, reused)

        filled_slots.append({
            "source_path": source_path,
            "source_type": source_type,
            "in_point": in_point,
            "target_duration": round(target_duration, 3),
            "start": shot.get("start", 0.0),
            "has_zoom": shot.get("has_zoom", False),
            "zoom_direction": shot.get("zoom_direction"),
            "transition_to_next": shot.get("transition_to_next", "cut"),
            "speed_curve_shape": shot.get("speed_curve_shape"),
            "match_similarity": round(match["similarity"], 3),
        })
    return filled_slots


def _map_sequential(shots: list, videos: list, photos: list, video_durations: dict):
    """Estratégia 2 (fallback): round-robin simples, sem entender conteúdo."""
    filled_slots = []
    video_idx = 0
    photo_idx = 0
    used_videos_once = False

    for shot in shots:
        target_duration = shot["duration"]

        if video_idx < len(videos):
            source_path = videos[video_idx]
            source_type = "video"
            in_point = _pick_in_point(source_path, target_duration, video_durations, used_videos_once)
            video_idx += 1

        elif photo_idx < len(photos):
            source_path = photos[photo_idx % len(photos)]
            source_type = "photo"
            in_point = 0.0
            target_duration = max(target_duration, config.PHOTO_DURATION_SEC)
            photo_idx += 1

        else:
            used_videos_once = True
            video_idx = 0
            if not videos:
                photo_idx = 0
                source_path = photos[photo_idx % len(photos)]
                source_type = "photo"
                in_point = 0.0
                target_duration = max(target_duration, config.PHOTO_DURATION_SEC)
                photo_idx += 1
            else:
                source_path = videos[video_idx]
                source_type = "video"
                in_point = _pick_in_point(source_path, target_duration, video_durations, True)
                video_idx += 1

        filled_slots.append({
            "source_path": source_path,
            "source_type": source_type,
            "in_point": in_point,
            "target_duration": round(target_duration, 3),
            "start": shot.get("start", 0.0),
            "has_zoom": shot.get("has_zoom", False),
            "zoom_direction": shot.get("zoom_direction"),
            "transition_to_next": shot.get("transition_to_next", "cut"),
            "speed_curve_shape": shot.get("speed_curve_shape"),
        })

    return filled_slots


def map_content_to_slots(shots: list, reference_path: str = None):
    """
    Recebe a lista de shots classificados (Fase 2/3) e devolve uma lista
    de "slots" preenchidos. Ver estratégias no docstring do módulo.

    reference_path é necessário apenas para a estratégia semântica —
    se não for fornecido, ou se as dependências CLIP não estiverem
    instaladas, cai automaticamente para a estratégia sequencial.
    """
    videos, photos = list_user_media()
    if not videos and not photos:
        raise RuntimeError(
            "Nenhum vídeo ou foto encontrado em input/user_videos ou input/user_photos."
        )

    video_durations = {v: _get_video_duration(v) for v in videos}

    use_semantic = (
        getattr(config, "USE_SEMANTIC_MATCHING", False)
        and semantic_match is not None
        and semantic_match.is_available()
        and reference_path is not None
    )

    if use_semantic:
        try:
            return _map_semantic(shots, reference_path, videos, photos, video_durations)
        except Exception as e:
            print(f"  [Aviso] Matching semântico falhou ({e}), a usar fallback sequencial.")

    return _map_sequential(shots, videos, photos, video_durations)


if __name__ == "__main__":
    videos, photos = list_user_media()
    print(f"{len(videos)} vídeos encontrados: {videos}")
    print(f"{len(photos)} fotos encontradas: {photos}")
    print(f"Matching semântico disponível: {semantic_match.is_available() if semantic_match else False}")

