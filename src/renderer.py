"""
FASE 6/7 — Renderer
Pega no EDL (Fase 5) e produz o vídeo final via FFmpeg, em 3 etapas:

  A) Normaliza cada clip do EDL individualmente (trim, resolução, fps,
     zoom direcional, reverse opcional, color matching, VFX) ->
     clip_000.mp4, clip_001.mp4, ...
  B) Concatena/funde os clips normalizados numa única timeline,
     aplicando xfade nos pontos marcados como "fade" (e um xfade muito
     curto nos marcados como "cut", que funciona como corte quase-seco).
  C) Sobrepõe a música (ou o áudio da referência, se configurado) por
     cima do vídeo final.

ATUALIZAÇÃO: zoom in/out agora é direcionalmente correto (bug anterior
corrigido: a taxa de zoom usava sempre a duração fixa das fotos, mesmo
para vídeos). Reverse é uma opção estilística baseada em regra (não
detecção automática de reverse na referência — ver README para porquê).
"""
import os
import sys
import subprocess
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

try:
    import color_match
except Exception:
    color_match = None

try:
    import vfx
except Exception:
    vfx = None

try:
    import subject_detect
    import subject_boost
except Exception:
    subject_detect = None
    subject_boost = None

try:
    import speed_ramp
except Exception:
    speed_ramp = None

import zoom as zoom_module
import motion_effects


def _run(cmd: list):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando FFmpeg falhou:\n{' '.join(cmd)}\n\nSTDERR:\n{result.stderr[-3000:]}"
        )
    return result


def normalize_clip(entry: dict, index: int, temp_dir: str, target_color_profile: dict = None) -> str:
    """Gera um clip normalizado (sem áudio) a partir de uma entrada do EDL."""
    w, h = config.OUTPUT_RESOLUTION
    fps = config.OUTPUT_FPS
    out_path = os.path.join(temp_dir, f"clip_{index:03d}.mp4")
    total_duration = entry["duration"]

    effect = entry.get("motion_effect") if entry["source_type"] == "video" else None
    bounce_reps = getattr(config, "BOUNCE_REPS", 2)

    # --- Speed Ramp real (src/speed_ramp.py) — tem prioridade sobre
    # qualquer efeito do pool (edl_builder já evita atribuir um efeito do
    # pool a planos com curva real detectada, mas isto protege mesmo
    # assim). Ver config.USE_SPEED_RAMP para o porquê/âmbito/limitações. ---
    use_speed_ramp = False
    speed_segment_plan = None
    speed_required_src = None
    speed_shape = entry.get("speed_curve_shape") if entry["source_type"] == "video" else None
    if (
        speed_ramp is not None
        and getattr(config, "USE_SPEED_RAMP", False)
        and speed_shape and speed_shape != "flat"
    ):
        try:
            intensity = getattr(config, "SPEED_RAMP_INTENSITY", 1.0)
            beat_times = getattr(config, "ACTIVE_BEAT_TIMES", None)
            shot_start = entry.get("shot_start_for_beatsnap")
            speed_segment_plan = speed_ramp.build_segment_plan(
                speed_shape, total_duration, intensity=intensity,
                beat_times=beat_times, shot_start=shot_start,
            )
            speed_required_src = sum(
                d for d, _ in speed_ramp.compute_segment_source_durations(speed_segment_plan, total_duration)
            )
            source_total = _get_duration(entry["source_path"])
            available = source_total - entry["in_point"]
            if available + 1e-3 >= speed_required_src:
                use_speed_ramp = True
                effect = None  # o speed ramp real substitui qualquer efeito do pool para este clip
            else:
                print(
                    f"  [Speed Ramp] Plano {index}: footage insuficiente para a curva '{speed_shape}' "
                    f"(precisa {speed_required_src:.2f}s, disponível {available:.2f}s) — a saltar (sem ramp)."
                )
        except Exception as e:
            print(f"  [Aviso] Speed Ramp falhou no plano {index}, seguindo sem ramp: {e}")

    # Cada efeito precisa que o CLIP BASE (antes do pós-processamento)
    # tenha uma duração diferente da final — calculado aqui, usado tanto
    # para construir o clip base como depois para o pós-processamento.
    if effect == "boomerang":
        effective_duration = total_duration / 2
    elif effect == "bounce":
        effective_duration = total_duration / (2 * bounce_reps)  # 1 "perna" de 1 dos N ciclos
    elif use_speed_ramp:
        effective_duration = speed_required_src  # precisa de mais conteúdo fonte que a duração final
    else:
        # None e "time_warp" (precisa da duração TOTAL como base, ver abaixo)
        effective_duration = total_duration

    needs_post_process = effect in ("boomerang", "bounce", "time_warp") or use_speed_ramp
    base_out_path = os.path.join(temp_dir, f"clip_{index:03d}_base.mp4") if needs_post_process else out_path
    bounce_cycle_path = os.path.join(temp_dir, f"clip_{index:03d}_cycle.mp4") if effect == "bounce" else None

    base_scale_crop = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"

    def _color_filter_suffix(source_path: str) -> str:
        """Retorna ',eq=...' se color matching estiver ativo, senão string vazia."""
        if not (target_color_profile and color_match and getattr(config, "USE_COLOR_MATCHING", False)):
            return ""
        try:
            clip_profile = color_match.analyze_color_profile(source_path)
            intensity = getattr(config, "COLOR_MATCH_INTENSITY", 1.0)
            eq = color_match.compute_eq_filter(clip_profile, target_color_profile, intensity=intensity)
            return f",{eq}"
        except Exception as e:
            print(f"  [Aviso] Color matching falhou para {source_path}: {e}")
            return ""

    def _vfx_suffix() -> str:
        """Retorna ',<filtros vfx>' se VFX estiver ativo, senão string vazia."""
        if not (vfx and getattr(config, "USE_VFX", False)):
            return ""
        vfx_names = getattr(config, "VFX_LIST", [])
        strengths = getattr(config, "ACTIVE_VFX_STRENGTH", {})
        chain = vfx.build_vfx_chain(vfx_names, strengths=strengths)
        return f",{chain}" if chain else ""

    if entry["source_type"] == "photo":
        zoom_dir = entry.get("zoom_direction") or "in"
        zc = zoom_module.build_zoom_chain(w, h, fps, effective_duration, config.PHOTO_ZOOM_FACTOR, zoom_dir)
        vf = (
            f"{zc['prep_scale_crop']}{_color_filter_suffix(entry['source_path'])}"
            f",{zc['zoompan']}{_vfx_suffix()},format=yuv420p"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", entry["source_path"],
            "-t", str(effective_duration),
            "-vf", vf,
            "-r", str(fps),
            "-an",
            base_out_path,
        ]
    else:
        if entry["has_zoom"] and not use_speed_ramp:
            zoom_dir = entry.get("zoom_direction") or "in"

            center_track = None
            if getattr(config, "USE_SUBJECT_LOCKED_ZOOM", False) and subject_detect and subject_detect.is_available():
                try:
                    center_track = subject_detect.estimate_subject_centroid_track(
                        entry["source_path"], entry["in_point"], entry["in_point"] + effective_duration,
                    )
                except Exception as e:
                    print(f"  [Aviso] Zoom subject-locked falhou no clip {index}: {e}")
                    center_track = None

            zc = zoom_module.build_zoom_chain(
                w, h, fps, effective_duration,
                getattr(config, "VIDEO_ZOOM_FACTOR", 1.2), zoom_dir,
                center_track=center_track,
            )
            # Quando há zoom, o scale+crop tem de ser para a resolução
            # SUPERAMOSTRADA (não a final) — ver src/zoom.py para o motivo.
            vf = f"{zc['prep_scale_crop']}{_color_filter_suffix(entry['source_path'])},{zc['zoompan']}"
        else:
            # use_speed_ramp desativa o zoom deliberadamente neste clip
            # (ver docstring de config.USE_SUBJECT_LOCKED_ZOOM/SPEED_RAMP):
            # não combinamos dois efeitos dinâmicos no mesmo clip nesta versão.
            vf = base_scale_crop + _color_filter_suffix(entry["source_path"])
        vf += _vfx_suffix()
        vf += ",format=yuv420p"

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(entry["in_point"]),
            "-i", entry["source_path"],
            "-t", str(effective_duration),
            "-vf", vf,
            "-r", str(fps),
            "-an",
            base_out_path,
        ]

    _run(cmd)

    if effect == "boomerang":
        motion_effects.apply_boomerang(base_out_path, out_path)
    elif effect == "bounce":
        motion_effects.apply_bounce(base_out_path, bounce_cycle_path, out_path,
                                     reps=bounce_reps, fps=fps, total_duration=total_duration)
        os.remove(bounce_cycle_path)
    elif effect == "time_warp":
        slow_speed = getattr(config, "TIMEWARP_SLOW_SPEED", 0.6)
        fast_speed = getattr(config, "TIMEWARP_FAST_SPEED", 1.8)
        s1, s2, m1, m2 = motion_effects.compute_timewarp_split(total_duration, slow_speed, fast_speed)
        motion_effects.apply_time_warp(base_out_path, out_path, s1, s2, m1, m2, total_duration=total_duration)
    elif use_speed_ramp:
        speed_ramp.apply_speed_curve(base_out_path, out_path, speed_segment_plan, total_output_duration=total_duration)

    if needs_post_process:
        os.remove(base_out_path)

    if getattr(config, "USE_SUBJECT_BOOST", False) and entry["source_type"] == "video":
        if subject_detect and subject_boost and subject_detect.is_available():
            mask_path = os.path.join(temp_dir, f"clip_{index:03d}_mask.mp4")
            boosted_path = os.path.join(temp_dir, f"clip_{index:03d}_boosted.mp4")
            try:
                if subject_detect.generate_subject_mask(out_path, mask_path):
                    subject_boost.apply_subject_boost(out_path, mask_path, boosted_path)
                    os.replace(boosted_path, out_path)
                else:
                    print(f"  [AI Subject Boost] Nenhuma pessoa detectada no clip {index} — sem alteração.")
            except Exception as e:
                print(f"  [Aviso] AI Subject Boost falhou no clip {index}: {e}")
            finally:
                if os.path.exists(mask_path):
                    os.remove(mask_path)
        elif index == 0:
            if subject_detect is None:
                print("  [AI Subject Boost] Falha ao importar módulo — verifica a instalação.")
            else:
                try:
                    import mediapipe  # noqa: F401
                    print("  [AI Subject Boost] Modelo .tflite não encontrado (config.SUBJECT_SEGMENTER_MODEL_PATH) — a saltar.")
                except Exception:
                    print("  [AI Subject Boost] MediaPipe não instalado (pip install mediapipe) — a saltar.")

    return out_path


def _get_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def concat_with_transitions(clip_paths: list, edl: list, temp_dir: str) -> str:
    """
    Concatena os clips normalizados usando xfade em cadeia.
    Cortes ("cut") usam uma duração de transição mínima (quase-corte-seco).
    Fades ("fade") usam config.TRANSITION_DURATION.
    """
    out_path = os.path.join(temp_dir, "final_no_audio.mp4")

    if len(clip_paths) == 1:
        shutil.copy(clip_paths[0], out_path)
        return out_path

    durations = [_get_duration(p) for p in clip_paths]
    min_cut_duration = round(2 / config.OUTPUT_FPS, 4)  # ~2 frames, aparenta corte seco

    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    # Tipo de transição xfade usado nos cortes "fade" do EDL. 'fadewhite'
    # funde através de branco — dá o efeito de "flash de luz" rápido e
    # suave na transição. Outras opções válidas do FFmpeg: 'fade' (dissolve
    # normal), 'fadeblack' (funde a preto). Configurável por estilo.
    fade_xfade_type = getattr(config, "TRANSITION_XFADE_TYPE", "fade")

    filter_parts = []
    cumulative = durations[0]
    prev_label = "0:v"

    for i in range(1, len(clip_paths)):
        transition_type = edl[i - 1].get("transition_to_next", "cut")
        d = config.TRANSITION_DURATION if transition_type == "fade" else min_cut_duration
        d = min(d, durations[i - 1], durations[i])  # nunca maior que os clips envolvidos
        offset = max(0.0, cumulative - d)
        out_label = f"vx{i}"

        # Cortes "cut" mantêm-se um dissolve mínimo (aparenta corte seco);
        # cortes "fade" usam o tipo de transição do estilo ativo (ex: o
        # flash branco do 'fadewhite').
        xfade_type = fade_xfade_type if transition_type == "fade" else "fade"

        filter_parts.append(
            f"[{prev_label}][{i}:v]xfade=transition={xfade_type}:duration={d:.4f}:offset={offset:.4f}[{out_label}]"
        )
        cumulative = cumulative + durations[i] - d
        prev_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{prev_label}]",
        "-c:v", "libx264",
        "-crf", str(config.CRF),
        "-preset", getattr(config, "FFMPEG_PRESET", "medium"),
        "-r", str(config.OUTPUT_FPS),
        out_path,
    ]
    _run(cmd)
    return out_path


def add_audio(video_path: str, output_path: str, music_path: str = None, reference_path: str = None):
    """
    Sobrepõe áudio ao vídeo final:
      - usa music_path se fornecido
      - senão, se config.USE_REFERENCE_AUDIO_IF_NO_MUSIC, usa o áudio da referência
      - senão, gera vídeo final sem áudio (silencioso)
    """
    audio_source = None
    if music_path and os.path.exists(music_path):
        audio_source = music_path
    elif config.USE_REFERENCE_AUDIO_IF_NO_MUSIC and reference_path:
        audio_source = reference_path

    if not audio_source:
        shutil.copy(video_path, output_path)
        return output_path

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1",
        "-i", audio_source,
        "-shortest",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    _run(cmd)
    return output_path


def render(edl: list, output_filename: str, music_path: str = None, reference_path: str = None):
    """Orquestra as etapas A, B e C do render."""
    temp_dir = config.DIR_TEMP
    os.makedirs(temp_dir, exist_ok=True)

    target_color_profile = None
    if getattr(config, "USE_COLOR_MATCHING", False) and color_match and reference_path:
        print("[Render] Analisando perfil de cor da referência...")
        try:
            target_color_profile = color_match.analyze_color_profile(reference_path)
            print(f"  Perfil alvo: {target_color_profile}")
        except Exception as e:
            print(f"  [Aviso] Análise de cor da referência falhou, seguindo sem color matching: {e}")

    print(f"[Render] Normalizando {len(edl)} clips...")
    clip_paths = []
    for i, entry in enumerate(edl):
        print(f"  -> clip {i+1}/{len(edl)} ({entry['source_type']}, {entry['duration']:.2f}s)")
        clip_paths.append(normalize_clip(entry, i, temp_dir, target_color_profile=target_color_profile))

    print("[Render] Concatenando com transições...")
    no_audio_path = concat_with_transitions(clip_paths, edl, temp_dir)

    print("[Render] Adicionando áudio...")
    output_path = os.path.join(config.DIR_OUTPUT, output_filename)
    os.makedirs(config.DIR_OUTPUT, exist_ok=True)
    add_audio(no_audio_path, output_path, music_path=music_path, reference_path=reference_path)

    print(f"[Render] Concluído -> {output_path}")
    return output_path
