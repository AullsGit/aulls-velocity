"""Verificação do vídeo renderizado: o agente só diz que está bom depois de isto passar.

Medidas objetivas contra o plano e contra a referência; o que precisa de olhos (a legenda está
legível? o sujeito está enquadrado?) o agente vê com as folhas de frames (sheets.py).
"""
from __future__ import annotations

import re
import subprocess

import numpy as np

from velocity.agent.report import reused_material
from velocity.evaluate.metrics import compare, compare_plan
from velocity.io.frames import load_frames
from velocity.io.probe import probe
from velocity.plan.schema import Plan
from velocity.plan.validate import check_media
from velocity.reference.analyzer import analyze_reference
from velocity.schemas.edit_dna import EditDNA

BLACK_LUMA = 14.0
BLACK_MIN_S = 0.4
FROZEN_D = 0.25
FROZEN_MIN_S = 0.8


def _runs(mask: np.ndarray, min_n: int) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if j - i + 1 >= min_n:
                out.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return out


def loudness(path: str) -> dict | None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, timeout=300)
    txt = r.stderr.decode(errors="replace")
    m, x = re.search(r"mean_volume: (-?[\d.]+) dB", txt), re.search(r"max_volume: (-?[\d.]+) dB", txt)
    if not m or not x:
        return None
    return {"mean_db": float(m.group(1)), "max_db": float(x.group(1))}


def verify_render(out_path: str, plan: Plan, dna: EditDNA | None = None, reference: str | None = None) -> dict:
    """`plan`: com caminhos locais. Devolve {ok, issues:[{severity, code, message, at_s?}], metrics}."""
    issues: list[dict] = []

    def issue(sev, code, msg, at=None):
        d = {"severity": sev, "code": code, "message": msg}
        if at is not None:
            d["at_s"] = round(float(at), 2)
        issues.append(d)

    info = probe(out_path)
    o = plan.output
    want_s = o.duration_frames / o.fps
    metrics: dict = {"duration_s": round(float(info.duration), 3), "expected_s": round(want_s, 3), "size": [info.width, info.height],
                     "expected_size": [o.width, o.height], "has_audio": info.has_audio}
    if abs(float(info.duration) - want_s) > 2.0 / o.fps:
        issue("error", "duration", f"o vídeo tem {info.duration:.2f}s e o plano pede {want_s:.2f}s")
    if (info.width, info.height) != (o.width, o.height):
        issue("error", "size", f"resolução {info.width}x{info.height} em vez de {o.width}x{o.height}")
    wants_audio = bool(plan.track("audio") and any(t.items for t in plan.track("audio"))) or any(it.audio_volume > 0 for it in plan.video_items())
    if wants_audio and not info.has_audio:
        issue("warning", "audio_missing", "o plano tem som mas o vídeo saiu mudo (os clips podem não ter faixa de áudio)")

    fr = load_frames(out_path, fps=o.fps, width=160, use_cache=False)
    gray = fr.rgb.astype(np.float32).mean(axis=3)
    luma = gray.mean(axis=(1, 2))
    d = np.zeros(fr.n)
    d[1:] = np.abs(gray[1:] - gray[:-1]).mean(axis=(1, 2))
    dips = [(t.start_frame, t.end_frame) for t in (it.transition_in for it in plan.video_items()) if t is not None and t.type == "dip_black"]
    for a, b in _runs(luma < BLACK_LUMA, int(BLACK_MIN_S * o.fps)):
        if not any(a < e and s < b for s, e in dips):
            issue("warning", "black_frames", f"imagem preta durante {(b - a) / o.fps:.2f}s", a / o.fps)
    for a, b in _runs(d < FROZEN_D, int(FROZEN_MIN_S * o.fps)):
        if luma[a:b].mean() >= BLACK_LUMA:
            issue("warning", "frozen", f"imagem parada durante {(b - a) / o.fps:.2f}s (fonte esgotada, ou trecho estático?)", a / o.fps)
    for i in check_media(plan):
        if i.level == "warning" and i.code == "source_overrun":
            issue("warning", "source_overrun", i.message)
    for r in reused_material(plan):
        issue("warning", "repeated_material", f"{r['source']}: {r['overlap_s']}s repetidos, aos {r['shown_at_s'][0]}s e aos {r['shown_at_s'][1]}s")
    metrics["luma_mean"] = round(float(luma.mean()), 1)

    out_dna = analyze_reference(out_path, with_audio=info.has_audio and dna is not None, with_text=False)
    planned = [it.at for it in sorted(plan.video_items(), key=lambda v: v.at)][1:]
    # um "corte" entre trechos parecidos ou numa dissolução não é visível para o medidor: não é erro, só um número a ler
    metrics["cuts_seen_in_output"] = compare_plan(planned, out_dna)
    if dna is not None:
        rep = compare(dna, out_dna)
        metrics["vs_reference"] = {"fidelity": rep["fidelity"], "components": rep["components"], "cuts": rep["cuts"]}
        if dna.look is not None and out_dna.look is not None:
            metrics["look"] = {"luma": [round(dna.look.luma), round(out_dna.look.luma)],
                               "saturation": [round(dna.look.saturation), round(out_dna.look.saturation)],
                               "contrast": [round(dna.look.contrast), round(out_dna.look.contrast)]}
    if info.has_audio:
        lo = loudness(out_path)
        metrics["loudness"] = lo
        if lo and lo["max_db"] > -0.1:
            issue("warning", "clipping", f"o som satura (pico {lo['max_db']} dB): baixa os volumes")
        if lo and lo["mean_db"] < -35:
            issue("warning", "too_quiet", f"o som está muito baixo ({lo['mean_db']} dB)")
    if reference and probe(reference).has_audio:
        metrics["reference_loudness"] = loudness(reference)
    return {"ok": not any(i["severity"] == "error" for i in issues), "issues": issues[:24], "metrics": metrics}
