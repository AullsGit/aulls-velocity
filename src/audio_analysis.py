"""
Análise da "vibe" da música — energia ao longo do tempo (RMS), usada
para decidir ONDE colocar os motion effects mais dramáticos (nos picos
de energia/"drops"), em vez de distribuir cegamente por posição.

IMPORTANTE (honestidade): isto NÃO é reconhecimento de música (não
identifica o nome da faixa, artista, género musical, nem usa nenhuma
base de dados tipo Shazam — isso exigiria um serviço pago de
fingerprinting de áudio). É análise de sinal: mede o quão "intensa"
(alta energia/volume RMS) está a música em cada momento, o que é uma
aproximação razoável e gratuita de onde estão os pontos altos da faixa.
"""
import os
import numpy as np
import librosa

from beat_sync import _extract_audio_to_wav


def compute_energy_curve(audio_path: str, hop_length: int = 512):
    """
    Retorna (times, energies): times em segundos, energies normalizado
    0-1 (0 = ponto mais "morto" da faixa, 1 = ponto mais intenso).
    """
    wav_path = _extract_audio_to_wav(audio_path)
    try:
        y, sr = librosa.load(wav_path, sr=None, mono=True)
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

        rms_min, rms_max = float(rms.min()), float(rms.max())
        if rms_max - rms_min < 1e-9:
            energies = np.zeros_like(rms)
        else:
            energies = (rms - rms_min) / (rms_max - rms_min)
        return times, energies
    finally:
        os.remove(wav_path)


def energy_in_window(times: np.ndarray, energies: np.ndarray, start: float, end: float) -> float:
    """Energia média (0-1) no intervalo [start, end) segundos."""
    mask = (times >= start) & (times < end)
    if not np.any(mask):
        idx = int(np.argmin(np.abs(times - (start + end) / 2)))
        return float(energies[idx])
    return float(np.mean(energies[mask]))
