"""O trabalho pesado de cada ferramenta do agente. Corre no worker; não conhece a API.

Cada função recebe caminhos locais e devolve (resultado JSON, [imagens]); `worker/runner.py` trata
de descarregar as entradas e de enviar as saídas. Imagem = {"path": local, "label": texto}.
"""
from __future__ import annotations

import json
import os

from velocity.agent import sheets
from velocity.agent.builder import BuildOptions, build_plan
from velocity.agent.report import dna_report
from velocity.agent.verify import verify_render
from velocity.io.probe import probe
from velocity.plan.schema import Plan
from velocity.schemas.edit_dna import EditDNA

VIDEO_SHEET_CELLS = 40


def _video_duration(path: str) -> float:
    return float(probe(path).duration or 0.0)


def analyze_reference_job(ref: str, tmp: str, progress, summarize_fn, with_captions: bool = False, dna_doc: dict | None = None) -> tuple[dict, list]:
    """Análise profunda. Reaproveita o DNA já guardado no projeto (a análise grátis do upload)."""
    from velocity.detector.velocity_detector import detect
    from velocity.reference.analyzer import analyze_reference
    if dna_doc:
        dna = EditDNA.model_validate(dna_doc)
    else:
        progress("analyzing_reference", 0.2)
        dna = analyze_reference(ref, with_text=False)
    det = detect(dna)
    summary = summarize_fn(dna, det)
    progress("analyzing_reference", 0.7)
    sheet = os.path.join(tmp, "ref_sheet.jpg")
    info = sheets.contact_sheet(ref, sheets.uniform_times(dna.duration_s, VIDEO_SHEET_CELLS), sheet)
    images = [{"path": sheet, "label": f"Referência: {len(info['times'])} frames ao longo do vídeo, com o instante em cada um"}]
    out = {"report": dna_report(dna, summary), "summary": summary, "reference_dna": json.loads(dna.model_dump_json())}
    if with_captions:
        progress("analyzing_reference", 0.8)
        out["captions"] = captions_job(ref, progress=progress)
    return out, images


CAPTION_BANDS = {"bottom": (0.70, 0.995), "middle": (0.30, 0.70), "top": (0.005, 0.30), "full": (0.0, 1.0)}


def captions_job(ref: str, log=lambda m: None, progress=None, region: str = "bottom", any_text: bool = False) -> dict:
    from velocity.reference.captions import caption_style, clean_captions, extract_captions
    raw, fps = extract_captions(ref, band=CAPTION_BANDS.get(region, CAPTION_BANDS["bottom"]), log=log, progress=progress, require_box=not any_text)
    caps = clean_captions(raw, max_gap=8)
    if not caps:
        return {"lines": [], "style": None, "fps": fps}
    info = probe(ref)
    st = caption_style(caps, info.width, has_box=not any_text)
    lines = [{"text": c.text, "start_s": round(c.start_frame / fps, 3), "end_s": round(c.end_frame / fps, 3)} for c in caps]
    return {"lines": lines, "fps": fps, "style": {"size": round(st["font_w"], 5), "x": round(st["cx"], 4), "y": round(st["cy"], 4),
                                                  "color": list(st["text_rgb"]), "box_alpha": st["box_alpha"]}}


def analyze_clips_job(clips: list[tuple[str, str]], tmp: str, progress) -> tuple[dict, list]:
    """Para cada clip (id, caminho): o que tem, quando mexe mais, onde tem cortes próprios + folha de frames."""
    from velocity.media.analyzer import analyze_clip
    import numpy as np
    reports, images = [], []
    for k, (fid, path) in enumerate(clips):
        progress("preparing_edit", 0.05 + 0.85 * k / max(len(clips), 1))
        c = analyze_clip(path)
        fps = c.fps
        step = int(fps)
        sec = [float(c.energy[i:i + step].mean()) for i in range(0, c.n, step)]
        top = float(max(sec) or 1.0)
        spans, i = [], 0
        while i < c.n:
            if c.bad[i]:
                j = i
                while j + 1 < c.n and c.bad[j + 1]:
                    j += 1
                spans.append(round(((i + j) / 2) / fps, 2))
                i = j + 1
            else:
                i += 1
        pres = c.subj[:, 3] > 0.4
        info = probe(path)
        reports.append({"file": fid, "duration_s": round(c.duration_s, 2), "size": [c.width, c.height], "fps": round(float(info.fps_nominal or info.fps_avg or 0), 2),
                        "has_audio": bool(c.has_audio), "energy_1hz": [round(v / top, 2) for v in sec][:120],
                        "peak_energy": round(top, 5), "luma_1hz": [int(c.lum[i:i + step].mean()) for i in range(0, c.n, step)][:120],
                        "internal_cuts_s": spans[:40], "subject_seen": round(float(pres.mean()), 2),
                        "subject_center": [round(float(np.median(c.subj[pres, 0])), 2), round(float(np.median(c.subj[pres, 1])), 2)] if pres.any() else None})
        n_cells = int(min(24, max(6, c.duration_s)))
        sheet = os.path.join(tmp, f"clip_{k}.jpg")
        sheets.contact_sheet(path, sheets.uniform_times(c.duration_s, n_cells), sheet)
        images.append({"path": sheet, "label": f"Clip {fid}: {n_cells} frames com o instante em cada um"})
    return {"clips": reports}, images


def frames_job(path: str, times: list[float], tmp: str, label: str, cell_w: int | None = None) -> tuple[dict, list]:
    if not times:
        raise ValueError("sem instantes")
    d = _video_duration(path)
    times = [min(max(0.0, float(t)), max(d - 0.04, 0.0)) for t in times]
    sheet = os.path.join(tmp, "frames.jpg")
    info = sheets.contact_sheet(path, times, sheet, cell_w=cell_w)
    return {"times": info["times"], "duration_s": round(d, 3)}, [{"path": sheet, "label": label}]


def compare_job(a: str, b: str, pairs: list[tuple[float, float]], tmp: str, labels: tuple[str, str]) -> tuple[dict, list]:
    sheet = os.path.join(tmp, "compare.jpg")
    info = sheets.side_by_side(a, b, pairs, sheet, labels=labels)
    return {"pairs": info["pairs"]}, [{"path": sheet, "label": f"Lado a lado: {labels[0]} | {labels[1]}"}]


def measure_grade(ref: str, clips: list[str], duration_s: float) -> list[list[float]]:
    """Matriz 3x4 (força total) que leva a paleta dos clips para a da referência."""
    from velocity.reference.grade import grade_matrix
    import numpy as np
    r = sheets.frames_array(ref, sheets.uniform_times(duration_s, 24))
    ss = [sheets.frames_array(c, sheets.uniform_times(_video_duration(c), 8)) for c in clips[:8]]
    return grade_matrix(r, np.concatenate([s.reshape(-1, 3) for s in ss]).reshape(-1, 1, 3), strength=1.0)


def audio_job(path: str) -> dict:
    """A música como o modelo a pode conhecer: tempo, batidas e energia ao longo do tempo (ele não ouve)."""
    import subprocess
    import numpy as np
    from velocity.reference.audio import analyze_audio
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
    d = float(r.stdout.strip() or 0.0)  # `probe` é só para vídeo; a música não tem faixa de imagem
    a = analyze_audio(path, [], 30.0, d)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"], capture_output=True, timeout=300)
    x = np.frombuffer(r.stdout, np.float32)
    step = 4000  # meio segundo
    rms = [float(np.sqrt((x[i:i + step] ** 2).mean())) for i in range(0, max(len(x) - step, 1), step)] if len(x) else []
    top = max(rms) if rms else 1.0
    loud = [round(v / max(top, 1e-6), 2) for v in rms][:400]
    # partes da música: onde a energia salta (mudanças que costumam pedir uma mudança na edição)
    drops = [round((k + 1) * 0.5, 1) for k in range(len(loud) - 2) if abs(np.mean(loud[k + 1:k + 3]) - np.mean(loud[max(0, k - 1):k + 1])) > 0.35]
    return {"duration_s": round(d, 2), "bpm": a.bpm, "beats_s": [round(b, 3) for b in a.beats], "onsets_count": len(a.onsets),
            "loudness_2hz": loud, "energy_changes_s": drops[:40], "has_audio": a.has_audio}


def build_job(ref: str, clips: list[str], dna_doc: dict, opts: dict, log, progress, music: str | None = None) -> dict:
    dna = EditDNA.model_validate(dna_doc)
    options = BuildOptions(**opts)
    if music:  # cortes presos à batida da música escolhida
        options.extra = {**options.extra, "music": music}
    plan, info = build_plan(ref, clips, dna, options, log=log, progress=progress)
    used_dna: EditDNA = info.pop("dna")
    grade = None
    try:
        grade = measure_grade(ref, clips, dna.duration_s)
    except Exception as e:  # noqa: BLE001 — a cor é um ganho opcional; sem ela o plano continua válido
        log(f"medição de cor falhou: {e!r}")
    return {"plan": plan, "info": info, "grade": grade, "beats": used_dna.audio.beats if used_dna.audio.has_audio else [],
            "cuts_s": [round(c / used_dna.fps, 3) for c in used_dna.cut_frames()]}


def verify_job(out_path: str, plan: Plan, dna_doc: dict | None, reference: str | None) -> dict:
    dna = EditDNA.model_validate(dna_doc) if dna_doc else None
    return verify_render(out_path, plan, dna=dna, reference=reference)
