"""
FASE 8 (opcional) — Legendas automáticas via Whisper

Usa o filtro `whisper` nativo do FFmpeg (precisa de --enable-whisper na
build, que já confirmaste que a tua build 'gyan.dev' tem) para transcrever
a fala do vídeo final e queimar legendas sincronizadas.

*** AVISO IMPORTANTE ***
Este módulo NÃO foi testado com o filtro whisper real (o FFmpeg usado
para desenvolver o resto do projeto não tem esse filtro compilado). O
que FOI corrigido e confirmado: um bug conhecido do FFmpeg no Windows
em que o ':' da letra de drive (ex: 'C:/...') quebra o parser de
filtros — resolvido usando sempre caminhos relativos ao cwd do processo
(ver `_to_relative`). Os nomes dos parâmetros do filtro (`model`,
`language`, `queue`, `destination`, `format`) foram confirmados como
corretos via `ffmpeg -h filter=whisper` na tua build.

Requer também um modelo Whisper no formato ggml (do projeto whisper.cpp).
Download (escolhe um tamanho — maior = mais preciso, mais lento):
    https://huggingface.co/ggerganov/whisper.cpp/tree/main
Para português, recomenda-se pelo menos "small" (ggml-small.bin, ~460MB)
para uma transcrição minimamente confiável. "base" (~140MB) é mais rápido
mas menos preciso.
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


def _to_relative(path: str, base_dir: str) -> str:
    """
    Converte um caminho absoluto para relativo a base_dir, com barras
    normais. Isto evita completamente o problema do ':' da letra de
    drive do Windows dentro de filtros do FFmpeg — nem escapar com '\\:'
    nem envolver em aspas simples funcionou de forma fiável nesta build,
    então a solução é não ter ':' nenhum no valor.
    """
    rel = os.path.relpath(path, start=base_dir)
    return rel.replace("\\", "/")


def transcribe_to_srt(source_path: str, srt_out_path: str) -> bool:
    """
    Transcreve a fala de source_path (vídeo ou áudio) para um ficheiro
    .srt usando o filtro whisper do FFmpeg. Retorna True se conseguiu
    gerar um .srt, False se falhou (ex: filtro não disponível).

    Roda o FFmpeg com cwd=config.BASE_DIR e usa caminhos relativos a essa
    pasta nos parâmetros do filtro, para evitar o ':' da letra de drive.
    """
    model_path = getattr(config, "WHISPER_MODEL_PATH", None)
    if not model_path or not os.path.exists(model_path):
        print(f"  [Legendas] Modelo Whisper não encontrado em '{model_path}'. Pulando.")
        return False

    language = getattr(config, "WHISPER_LANGUAGE", "pt")
    base_dir = config.BASE_DIR

    model_rel = _to_relative(model_path, base_dir)
    dest_rel = _to_relative(srt_out_path, base_dir)
    source_rel = _to_relative(source_path, base_dir)

    cmd = [
        "ffmpeg", "-y",
        "-i", source_rel,
        "-af", (
            f"whisper=model={model_rel}:language={language}:"
            f"queue=3:destination={dest_rel}:format=srt"
        ),
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=base_dir)
    if result.returncode != 0:
        print(f"  [Legendas] Filtro whisper falhou (verifica 'ffmpeg -h filter=whisper'):")
        print(f"  {result.stderr[-1500:]}")
        return False

    return os.path.exists(srt_out_path) and os.path.getsize(srt_out_path) > 0


def is_srt_meaningful(srt_path: str, min_segments: int = 2) -> bool:
    """
    Verificação de segurança: evita queimar 'legendas' vazias ou lixo
    quando o áudio era só música instrumental sem fala.
    """
    if not os.path.exists(srt_path):
        return False
    with open(srt_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Conta blocos de legenda (separados por linha em branco no formato SRT)
    segments = [s for s in content.strip().split("\n\n") if s.strip()]
    if len(segments) < min_segments:
        return False

    # Verifica se há texto real (não só timestamps/números)
    text_lines = [
        line for line in content.splitlines()
        if line.strip() and "-->" not in line and not line.strip().isdigit()
    ]
    total_chars = sum(len(line.strip()) for line in text_lines)
    return total_chars > 10  # limiar mínimo arbitrário para "há texto de verdade"


def burn_captions(video_path: str, srt_path: str, output_path: str, caption_style: dict = None) -> str:
    """
    Queima as legendas do .srt no vídeo. O estilo (fonte, cor, tamanho,
    posição) vem de caption_style — normalmente config.STYLE_PRESETS[x]["captions"]
    — permitindo "legendas criativas" diferentes por estilo de edição.
    Se não for passado, usa um estilo neutro por defeito.
    """
    default_style = {
        "FontName": "Arial", "FontSize": 14, "PrimaryColour": "&H00FFFFFF",
        "OutlineColour": "&H00000000", "Bold": 0, "Alignment": 2, "MarginV": 60,
    }
    s = {**default_style, **(caption_style or {})}
    style_str = (
        f"FontName={s['FontName']},FontSize={s['FontSize']},"
        f"PrimaryColour={s['PrimaryColour']},OutlineColour={s['OutlineColour']},"
        f"Bold={s['Bold']},BorderStyle=1,Outline=2,Alignment={s['Alignment']},"
        f"MarginV={s['MarginV']}"
    )

    base_dir = config.BASE_DIR
    video_rel = _to_relative(video_path, base_dir)
    srt_rel = _to_relative(srt_path, base_dir)
    output_rel = _to_relative(output_path, base_dir)

    cmd = [
        "ffmpeg", "-y",
        "-i", video_rel,
        "-vf", f"subtitles={srt_rel}:force_style='{style_str}'",
        "-c:a", "copy",
        output_rel,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=base_dir)
    if result.returncode != 0:
        raise RuntimeError(
            f"Comando FFmpeg (burn_captions) falhou:\n{' '.join(cmd)}\n\nSTDERR:\n{result.stderr[-3000:]}"
        )
    return output_path


def add_captions_if_speech_present(video_path: str, output_path: str, temp_dir: str, caption_style: dict = None) -> str:
    """
    Orquestra o processo completo: transcreve, verifica se há fala real,
    e só queima legendas se fizer sentido. Se não houver fala (ex: música
    instrumental) ou o Whisper falhar, devolve o vídeo original sem alterar.
    """
    srt_path = os.path.join(temp_dir, "captions.srt")

    print("  [Legendas] Transcrevendo áudio...")
    ok = transcribe_to_srt(video_path, srt_path)
    if not ok:
        print("  [Legendas] Transcrição indisponível — vídeo final sem legendas.")
        return video_path

    if not is_srt_meaningful(srt_path):
        print("  [Legendas] Sem fala detectada (provavelmente só música) — pulando legendas.")
        return video_path

    print("  [Legendas] Fala detectada, queimando legendas no vídeo...")
    return burn_captions(video_path, srt_path, output_path, caption_style=caption_style)
