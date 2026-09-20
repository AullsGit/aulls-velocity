"""Lançador de testes: lê as pastas teste/1_reference, 2_inputs, 3_music e escreve em 4_output.
Uso:  venv\Scripts\python.exe testar.py [--size 1080x1920] [--force] [--sem-musica]"""
import glob
import os
import sys
import time

from velocity.cli import main

ROOT = os.path.dirname(os.path.abspath(__file__))
T = os.path.join(ROOT, "teste")
VID = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
AUD = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


def files(folder, exts):
    d = os.path.join(T, folder)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(exts))


refs, clips, music = files("1_reference", VID), files("2_inputs", VID), files("3_music", AUD)
if len(refs) != 1:
    sys.exit(f"ERRO: tem de haver exatamente 1 vídeo em teste/1_reference (encontrei {len(refs)}).")
if not clips:
    sys.exit("ERRO: coloque pelo menos 1 vídeo em teste/2_inputs.")
use_music = music and "--sem-musica" not in sys.argv
extra = [a for a in sys.argv[1:] if a != "--sem-musica"]
name = os.path.splitext(os.path.basename(refs[0]))[0]
out = os.path.join(T, "4_output", f"{name}__{time.strftime('%H%M%S')}.mp4")
args = ["make", "--ref", refs[0], "--clips", *clips, "--out", out, "--report", out[:-4] + ".json", *extra]
if use_music:
    args += ["--music", music[0]]
    if len(music) > 1:
        print(f"Aviso: {len(music)} músicas encontradas; a usar {os.path.basename(music[0])}")
if not use_music:
    args.append("--audio-ref")  # sem música do utilizador: usa o áudio da referência
print(f"Referência: {os.path.basename(refs[0])} | {len(clips)} clip(s) | música: {os.path.basename(music[0]) if use_music else 'áudio da referência'}")
sys.exit(main(args))
