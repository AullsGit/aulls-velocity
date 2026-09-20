"""
FASE 3 — Beat Sync (opcional)
Detecta os beats de uma faixa de áudio e ajusta ("encaixa") os timestamps
de corte para o beat mais próximo, dando sensação de sincronização musical.
"""
import os
import subprocess
import tempfile
import librosa
import numpy as np


def _extract_audio_to_wav(source_path: str) -> str:
    """
    Extrai a faixa de áudio de qualquer ficheiro (vídeo ou áudio) para um
    .wav temporário, que o librosa consegue ler de forma confiável.
    O ficheiro .wav é criado em pasta temporária do sistema; o chamador
    é responsável por apagar depois de usar (ver detect_beats).
    """
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)

    cmd = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-vn",                # descarta vídeo, se houver
        "-ac", "1",            # mono
        "-ar", "22050",        # sample rate reduzido, suficiente para beat tracking
        wav_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        os.remove(wav_path)
        raise RuntimeError(
            f"Falha ao extrair áudio de '{source_path}' com FFmpeg:\n{result.stderr[-1500:]}"
        )
    return wav_path


def detect_beats(audio_path: str):
    """
    Retorna uma lista de timestamps (segundos) onde há beats.
    Funciona tanto com ficheiros de áudio puro (mp3/wav/...) quanto com
    vídeos (mp4/mov/...) — nesse caso extrai o áudio primeiro via FFmpeg,
    porque o librosa não lê containers de vídeo diretamente.
    """
    wav_path = _extract_audio_to_wav(audio_path)
    try:
        y, sr = librosa.load(wav_path, sr=None, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        # Versões recentes do librosa devolvem 'tempo' como array em vez de escalar
        tempo_value = float(np.atleast_1d(tempo)[0])
        return beat_times.tolist(), tempo_value
    finally:
        os.remove(wav_path)


def snap_shots_to_beat(shots: list, beat_times: list, max_snap_distance: float = 0.15):
    """
    Ajusta o 'end' de cada shot para o beat mais próximo, dentro de uma
    janela de tolerância (max_snap_distance segundos). Se não houver beat
    próximo o suficiente, mantém o corte original.
    """
    if not beat_times:
        return shots

    beat_arr = np.array(beat_times)
    snapped = []
    for shot in shots:
        end = shot["end"]
        idx = int(np.argmin(np.abs(beat_arr - end)))
        closest_beat = beat_arr[idx]
        if abs(closest_beat - end) <= max_snap_distance:
            new_end = float(closest_beat)
        else:
            new_end = end
        new_shot = {**shot, "end": new_end, "duration": new_end - shot["start"]}
        snapped.append(new_shot)

    # Corrige o 'start' de cada shot para bater com o 'end' do anterior (mantém contiguidade)
    for i in range(1, len(snapped)):
        snapped[i]["start"] = snapped[i - 1]["end"]
        snapped[i]["duration"] = snapped[i]["end"] - snapped[i]["start"]

    return snapped


if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Uso: python beat_sync.py <caminho_do_audio_ou_video>")
        sys.exit(1)

    beats, tempo = detect_beats(path)
    print(f"Tempo estimado: {tempo:.1f} BPM")
    print(f"{len(beats)} beats detectados")
    print(json.dumps(beats[:20], indent=2), "...")
