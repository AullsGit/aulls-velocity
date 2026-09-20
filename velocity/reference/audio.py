"""Batidas, onsets e alinhamento dos cortes com a música."""
from __future__ import annotations

import numpy as np

from velocity.io.frames import load_audio
from velocity.schemas.edit_dna import AudioDNA, Transition

SR = 22050
ALIGN_TOL_S = 0.070  # ±70 ms ≈ ±2 frames a 30 fps


def _beat_analysis(y: np.ndarray):
    import librosa

    onset_env = librosa.onset.onset_strength(y=y, sr=SR)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=SR, units="frames")
    beats = librosa.frames_to_time(beat_frames, sr=SR)
    on_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=SR, units="frames", backtrack=False)
    onsets = librosa.frames_to_time(on_frames, sr=SR)
    return float(np.atleast_1d(tempo)[0]), np.asarray(beats), np.asarray(onsets)


def analyze_audio(path: str, transitions: list[Transition], fps: float, duration_s: float) -> AudioDNA:
    y = load_audio(path, SR)
    if y is None or len(y) < SR * 2 or float(np.abs(y).max()) < 1e-4:
        return AudioDNA(has_audio=False)
    bpm, beats, onsets = _beat_analysis(y)
    if len(beats) < 3:
        return AudioDNA(has_audio=True, bpm=round(bpm, 1), beats=[round(float(b), 3) for b in beats],
                        onsets=[round(float(o), 3) for o in onsets])

    period = float(np.median(np.diff(beats)))
    cut_times = np.array([t.cut_frame / fps for t in transitions])
    align = None
    interval_beats = None
    if len(cut_times):
        for t in transitions:
            tt = t.cut_frame / fps
            k = int(np.argmin(np.abs(beats - tt)))
            t.beat_offset_ms = round(float((tt - beats[k]) * 1000), 1)
            # posição fracionária em batidas (interpolação linear entre batidas vizinhas)
            t.beat_index = round(float(np.interp(tt, beats, np.arange(len(beats)))), 3)
        dists = np.array([np.min(np.abs(beats - c)) for c in cut_times])
        align = float((dists <= ALIGN_TOL_S).mean())
    if len(cut_times) >= 2:
        interval_beats = round(float(np.median(np.diff(cut_times)) / period), 3)
    return AudioDNA(
        has_audio=True, bpm=round(bpm, 1),
        beats=[round(float(b), 3) for b in beats],
        onsets=[round(float(o), 3) for o in onsets],
        cut_beat_alignment=round(align, 3) if align is not None else None,
        chance_alignment=round(chance_alignment([float(b) for b in beats], duration_s), 3),
        cut_interval_beats=interval_beats,
    )


def chance_alignment(beats: list[float], duration_s: float) -> float:
    """Fração da timeline a <=70ms de uma batida: alinhamento esperado de cortes ALEATÓRIOS.
    Sem isto, 'alinhamento 60%' não diz nada (a 120 BPM o acaso já dá ~28%)."""
    if len(beats) < 2 or duration_s <= 0:
        return 0.0
    return float(min(1.0, len(beats) * 2 * ALIGN_TOL_S / duration_s))
