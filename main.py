"""
Ponto de entrada do pipeline.

Uso:
    python main.py [estilo]

    onde [estilo] é opcional: velocity | memory_rewind | clean_minimal
    (default: config.DEFAULT_STYLE). Ver config.STYLE_PRESETS para o que
    cada um ajusta.

Espera encontrar:
    input/reference/   -> exatamente 1 vídeo de referência
    input/user_videos/ -> os teus vídeos (0 ou mais)
    input/user_photos/ -> as tuas fotos (0 ou mais)
    input/music/       -> opcional, 1 ficheiro de áudio

Produz:
    output/video_final.mp4
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import config
from scene_detect import detect_scenes
from classify_cuts import classify_shots
from beat_sync import detect_beats, snap_shots_to_beat
from content_mapper import map_content_to_slots
from edl_builder import build_edl, save_edl
from renderer import render
from captions import add_captions_if_speech_present
import edit_dna
import compare_refine


def apply_style_preset(style_name: str):
    """
    Aplica um preset de config.STYLE_PRESETS sobre o módulo config,
    sobrescrevendo os parâmetros relevantes (zoom, transições, VFX,
    reverse) e guardando o estilo de legendas em config.ACTIVE_CAPTION_STYLE.
    O vídeo de referência do utilizador continua a ser analisado
    normalmente — o preset só muda COMO o resultado dessa análise é
    interpretado/aplicado.
    """
    presets = getattr(config, "STYLE_PRESETS", {})
    if style_name not in presets:
        available = ", ".join(presets.keys()) or "(nenhum definido)"
        raise ValueError(f"Estilo '{style_name}' desconhecido. Disponíveis: {available}")

    preset = presets[style_name]
    # Reset de campos que só existem nalguns presets, para evitar valores
    # "pendurados" de uma chamada anterior (relevante se apply_style_preset
    # for chamado mais que uma vez no mesmo processo, ex: em testes).
    config.ACTIVE_VFX_STRENGTH = {}

    for key, value in preset.items():
        if key == "captions":
            config.ACTIVE_CAPTION_STYLE = value
        elif key == "VFX_STRENGTH":
            config.ACTIVE_VFX_STRENGTH = value
        else:
            setattr(config, key, value)

    print(f"Estilo ativo: {style_name}")


def auto_detect_style(reference_path: str, tempo: float = None) -> str:
    """
    Tenta classificar automaticamente qual preset de config.STYLE_PRESETS
    combina melhor com a referência, usando:
      1. CLIP zero-shot (imagem vs. descrições de mood em config.MOOD_LABEL_DESCRIPTIONS)
      2. Tempo/BPM (se disponível) como sinal de reforço

    Cai para config.DEFAULT_STYLE se as dependências não estiverem
    disponíveis ou a classificação falhar por qualquer motivo — nunca
    trava o pipeline.
    """
    default = getattr(config, "DEFAULT_STYLE", "velocity")

    try:
        import semantic_match
    except Exception:
        print("  [Auto-estilo] CLIP indisponível — usando estilo por defeito.")
        return default

    if not semantic_match.is_available():
        print("  [Auto-estilo] Dependências (torch/open_clip) não instaladas — usando estilo por defeito.")
        return default

    labels = getattr(config, "MOOD_LABEL_DESCRIPTIONS", None)
    if not labels:
        return default

    try:
        result = semantic_match.classify_mood(reference_path, labels)
        scores = dict(result["scores"])
        print(f"  [Auto-estilo] Scores CLIP: {', '.join(f'{k}={v:.3f}' for k, v in scores.items())}")

        # Reforço por BPM: tempo rápido empurra para 'velocity', lento para
        # 'memory_rewind' — soma um pequeno bónus ao score CLIP existente.
        if tempo:
            fast_th = getattr(config, "BPM_FAST_THRESHOLD", 120)
            slow_th = getattr(config, "BPM_SLOW_THRESHOLD", 85)
            if tempo >= fast_th and "velocity" in scores:
                scores["velocity"] += 0.05
            elif tempo <= slow_th and "memory_rewind" in scores:
                scores["memory_rewind"] += 0.05
            print(f"  [Auto-estilo] BPM={tempo:.1f} aplicado como reforço.")

        best = max(scores, key=scores.get)
        if best not in getattr(config, "STYLE_PRESETS", {}):
            return default
        print(f"  [Auto-estilo] Escolhido: {best}")
        return best
    except Exception as e:
        print(f"  [Auto-estilo] Classificação falhou ({e}) — usando estilo por defeito.")
        return default


def find_reference_video():
    exts = (".mp4", ".mov", ".mkv", ".avi", ".webm")
    files = [f for f in os.listdir(config.DIR_REFERENCE) if f.lower().endswith(exts)]
    if not files:
        raise RuntimeError(
            f"Nenhum vídeo de referência encontrado em {config.DIR_REFERENCE}. "
            "Coloca lá o teu vídeo de referência."
        )
    if len(files) > 1:
        print(f"[Aviso] Mais de um vídeo em input/reference/, usando o primeiro: {files[0]}")
    return os.path.join(config.DIR_REFERENCE, files[0])


def find_music_file():
    if not os.path.isdir(config.DIR_MUSIC):
        return None
    exts = (".mp3", ".wav", ".m4a", ".aac", ".flac")
    files = [f for f in os.listdir(config.DIR_MUSIC) if f.lower().endswith(exts)]
    return os.path.join(config.DIR_MUSIC, files[0]) if files else None


def main():
    print("=" * 60)
    print("PIPELINE DE EDIÇÃO POR REFERÊNCIA — MVP")
    print("=" * 60)

    reference_path = find_reference_video()
    music_path = find_music_file()
    print(f"\nVídeo de referência: {reference_path}")
    print(f"Música: {music_path or '(nenhuma encontrada — vídeo final ficará silencioso ou com áudio da referência conforme config)'}")

    # Fonte de áudio para beat sync/tempo: música do utilizador tem
    # prioridade; senão, áudio da própria referência (se configurado).
    beat_source = music_path
    if not beat_source and config.USE_REFERENCE_AUDIO_IF_NO_MUSIC:
        beat_source = reference_path

    # Calcula o tempo cedo (antes de escolher estilo), para poder usá-lo
    # como sinal na deteção automática. Guarda também os beats para não
    # ter de recalcular na Fase 3.
    cached_beats, cached_tempo = None, None
    if beat_source:
        try:
            cached_beats, cached_tempo = detect_beats(beat_source)
        except Exception:
            pass  # Fase 3 tenta de novo e reporta o erro lá, se ainda falhar

    # --- Escolha do estilo: manual (argumento CLI) ou automático ---
    if len(sys.argv) > 1:
        style_name = sys.argv[1]
        print(f"\nEstilo escolhido manualmente: {style_name}")
    elif getattr(config, "USE_AUTO_STYLE_DETECTION", False):
        print("\n[Auto-estilo] Analisando a referência para escolher o estilo...")
        style_name = auto_detect_style(reference_path, tempo=cached_tempo)
    else:
        style_name = getattr(config, "DEFAULT_STYLE", "velocity")

    apply_style_preset(style_name)

    # --- FASE 1 ---
    print("\n[Fase 1] Detectando cenas na referência...")
    shots = detect_scenes(reference_path)
    print(f"  {len(shots)} planos detectados.")

    # --- FASE 2 ---
    print("\n[Fase 2] Classificando zoom e transições...")
    shots = classify_shots(
        reference_path, shots,
        zoom_threshold=config.ZOOM_SCALE_THRESHOLD,
        fade_similarity_threshold=config.FADE_SIMILARITY_THRESHOLD,
    )
    n_zoom = sum(1 for s in shots if s["has_zoom"])
    n_fade = sum(1 for s in shots if s["transition_to_next"] == "fade")
    print(f"  {n_zoom} planos com zoom, {n_fade} transições tipo fade.")

    # Alguns estilos (ex: velocity) querem zoom dramático em quase todos os
    # planos, independentemente de a referência ter zoom detectado nesses
    # pontos exatos — sem isto, o VIDEO_ZOOM_FACTOR do preset fica "à espera"
    # de planos que podem nem existir na referência.
    if getattr(config, "FORCE_ZOOM", False):
        pattern = getattr(config, "FORCE_ZOOM_PATTERN", ["in", "out"])
        for i, shot in enumerate(shots):
            shot["has_zoom"] = True
            shot["zoom_direction"] = pattern[i % len(pattern)]
        print(f"  [FORCE_ZOOM ativo] zoom aplicado a todos os {len(shots)} planos.")

    # --- FASE 3 (opcional) ---
    if beat_source:
        print(f"\n[Fase 3] Sincronizando cortes com o beat de: {os.path.basename(beat_source)}...")
        try:
            beats, tempo = (cached_beats, cached_tempo) if cached_beats is not None else detect_beats(beat_source)
            print(f"  Tempo estimado: {tempo:.1f} BPM, {len(beats)} beats.")
            shots = snap_shots_to_beat(shots, beats)
        except Exception as e:
            print(f"  [Aviso] Beat sync falhou, seguindo sem sincronização: {e}")
    else:
        print("\n[Fase 3] Sem fonte de áudio disponível — pulando beat sync.")

    # --- EDIT DNA — representação estruturada da edição de referência ---
    # Construída aqui (depois das Fases 1-3) para capturar o padrão de
    # cortes/zoom/transição REALMENTE usado no resto do pipeline —
    # incluindo o beat-snap e o FORCE_ZOOM do estilo ativo, se aplicável
    # — mais movimento de câmera (camera_motion.py) e cor por plano
    # (color_match.py). É a "receita digital de edição" pedida no
    # objetivo do projeto: uma representação explícita e guardável, em
    # vez de ficar implícita e espalhada pelo código de cada fase.
    print("\n[Edit DNA] Construindo a representação estruturada da edição de referência...")
    reference_dna = edit_dna.build_edit_dna(reference_path, shots=shots, beat_source=beat_source, label="reference")
    edit_dna.save_edit_dna(reference_dna, os.path.join(config.DIR_TEMP, "edit_dna_reference.json"))
    sig = reference_dna["style_signature"]
    print(
        f"  {sig['n_shots']} planos, {sig['cuts_per_minute']} cortes/min, "
        f"zoom em {sig['zoom_ratio'] * 100:.0f}% dos planos, fade em {sig['fade_ratio'] * 100:.0f}%."
    )

    # Speed Ramp real (config.USE_SPEED_RAMP): a FORMA da curva detectada
    # por plano na referência (Edit DNA acima) é anexada aos próprios
    # 'shots', para fluir por content_mapper -> edl_builder -> renderer
    # (ver src/speed_ramp.py). config.ACTIVE_BEAT_TIMES fica disponível
    # globalmente para o renderer poder alinhar fronteiras de segmento a
    # um beat (mesmo padrão já usado por ACTIVE_VFX_STRENGTH/ACTIVE_CAPTION_STYLE).
    if getattr(config, "USE_SPEED_RAMP", False):
        n_curves = 0
        for i, shot in enumerate(shots):
            sc = reference_dna["shots"][i].get("speed_curve")
            shape = sc.get("shape") if sc and "error" not in sc else None
            shot["speed_curve_shape"] = shape
            if shape and shape != "flat":
                n_curves += 1
        print(f"  [Speed Ramp] {n_curves}/{len(shots)} planos com curva de velocidade real detectada.")
        audio_summary = reference_dna.get("audio") or {}
        config.ACTIVE_BEAT_TIMES = audio_summary.get("beat_times")

    # --- FASE 4 ---
    print("\n[Fase 4] Mapeando conteúdo do utilizador para os slots do padrão...")
    filled_slots = map_content_to_slots(shots, reference_path=reference_path)
    print(f"  {len(filled_slots)} slots preenchidos.")

    # --- FASE 5 ---
    print("\n[Fase 5] Construindo EDL...")
    edl = build_edl(filled_slots, beat_source=beat_source)
    edl_path = save_edl(edl)
    print(f"  EDL guardado em {edl_path}")

    # --- FASE 6/7 (+ Refinamento Iterativo opcional) ---
    # Se ativo (config.ENABLE_ITERATIVE_REFINEMENT), depois de renderizar
    # o sistema analisa o PRÓPRIO resultado com a mesma Edit DNA, compara
    # com reference_dna, ajusta parâmetros de render por regra
    # determinística, e renderiza de novo — o ciclo "gerar -> comparar ->
    # ajustar -> renderizar de novo" pedido no objetivo do projeto. Ver
    # src/compare_refine.py para o âmbito exato dos ajustes automáticos.
    if getattr(config, "ENABLE_ITERATIVE_REFINEMENT", False):
        print("\n[Fase 6/7] Renderizando com refinamento iterativo (gerar -> comparar -> ajustar -> renderizar de novo)...")
        output_path, refine_history = compare_refine.refine_and_render(
            edl, reference_dna, reference_path,
            output_filename_base="video_final", music_path=music_path,
        )
        print(f"  {len(refine_history)} iteração(ões) avaliada(s) — relatórios em temp/refine_iteration_*.json")
    else:
        print("\n[Fase 6/7] Renderizando vídeo final...")
        output_path = render(edl, "video_final.mp4", music_path=music_path, reference_path=reference_path)

    # --- FASE 8 (opcional) ---
    if getattr(config, "USE_AUTO_CAPTIONS", False):
        print("\n[Fase 8] Legendas automáticas...")
        try:
            captioned_path = os.path.join(config.DIR_OUTPUT, "video_final_legendado.mp4")
            final_with_captions = add_captions_if_speech_present(
                output_path, captioned_path, config.DIR_TEMP,
                caption_style=getattr(config, "ACTIVE_CAPTION_STYLE", None),
            )
            if final_with_captions != output_path:
                output_path = final_with_captions
        except Exception as e:
            print(f"  [Aviso] Legendas falharam, seguindo com o vídeo sem legendas: {e}")

    print("\n" + "=" * 60)
    print(f"CONCLUÍDO: {output_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
