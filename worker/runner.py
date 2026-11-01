"""Executa UM job de computação. O mesmo código corre:
  * localmente (fornecedor `local`, numa thread da API — desenvolvimento / operador);
  * no RunPod Serverless (`worker/handler.py` chama `run`).

Contrato (dict JSON, sem dependências do backend):
  payload = {
    "kind": "analyze" | "generate" | "render_edit",
    "job_id": str,
    "inputs": {"reference": Item, "sources": [Item], "music": Item | None}   # analyze/generate
            | {"files": {key: Item}}                                         # render_edit
    "params": {...}, "limits": {...},
    "reference_dna": dict | None,   # análise já feita (generate)
    "plan": dict | None,            # render_edit: Plano v1 (velocity/plan; uris = chaves de `files`)
    "timeline": dict | None,        # render_edit: formato antigo (API anterior ao Plano) — aceite
    "outputs": {"video": Target, "thumbnail": Target},
  }
  Item   = {"key": str, "name": str, "url": str, "local_path": str | None}
  Target = {"url": str, "local_path": str | None}   # url = upload assinado (PUT multipart "file")

Devolve sempre um dict (nunca levanta): {"ok": True, ...} ou
{"ok": False, "error_code": ..., "detail": ...}. O backend traduz error_code para uma mensagem
compreensível e decide o reembolso.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback

import requests

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
STILL_SECONDS = 3.0


class UserError(Exception):
    """Problema com o que o utilizador enviou (não é falha do sistema)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def run(payload: dict, progress=None) -> dict:
    progress = progress or (lambda stage, frac: None)
    t0 = time.time()
    kind = payload.get("kind")
    tmp = tempfile.mkdtemp(prefix="awls_")
    try:
        if kind == "analyze":
            out = _analyze(payload, tmp, progress)
        elif kind == "generate":
            out = _generate(payload, tmp, progress)
        elif kind == "render_edit":
            out = _render_edit(payload, tmp, progress)
        else:
            return {"ok": False, "error_code": "bad_request", "detail": f"kind desconhecido: {kind}"}
        out.setdefault("metrics", {})["worker_wall_s"] = round(time.time() - t0, 2)
        out["ok"] = True
        return out
    except UserError as e:
        return {"ok": False, "error_code": e.code, "detail": e.detail, "metrics": {"worker_wall_s": round(time.time() - t0, 2)}}
    except Exception:
        return {"ok": False, "error_code": "internal", "detail": traceback.format_exc(limit=10),
                "metrics": {"worker_wall_s": round(time.time() - t0, 2)}}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # nada de ficheiros temporários acumulados


# --------------------------------------------------------------------------- I/O
def _fetch(item: dict, tmp: str, max_bytes: int) -> str:
    lp = item.get("local_path")
    if lp and os.path.isfile(lp):
        return lp
    ext = os.path.splitext(item.get("name") or item["key"])[1].lower() or ".bin"
    dest = os.path.join(tmp, "in", hashlib.sha1(item["key"].encode()).hexdigest()[:16] + ext)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with requests.get(item["url"], stream=True, timeout=(15, 120)) as r:
        r.raise_for_status()
        got = 0
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                got += len(chunk)
                if got > max_bytes:
                    raise UserError("file_too_large", f"{item.get('name')}: maior que o limite")
                fh.write(chunk)
    return dest


def _put(target: dict, src: str, content_type: str) -> None:
    lp = target.get("local_path")
    if lp:
        os.makedirs(os.path.dirname(lp), exist_ok=True)
        shutil.copyfile(src, lp)
        return
    with open(src, "rb") as fh:
        r = requests.put(target["url"], files={"file": (os.path.basename(src), fh, content_type)}, timeout=(15, 600))
    r.raise_for_status()


def _duration(path: str) -> tuple[float, int, int]:
    from velocity.io.probe import probe
    try:
        info = probe(path)
    except Exception:
        raise UserError("invalid_media", f"{os.path.basename(path)}: não é um vídeo legível")
    return float(info.duration or 0.0), int(info.width), int(info.height)


def _still_to_clip(img: str, tmp: str) -> str:
    """Fotos entram no motor como clips de 3s (o zoom/efeitos da referência dão-lhes movimento)."""
    out = os.path.join(tmp, "stills", os.path.basename(img) + ".mp4")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", img, "-t", f"{STILL_SECONDS}", "-r", "30",
           "-vf", "scale='min(1080,iw)':-2,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", out]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise UserError("invalid_media", f"{os.path.basename(img)}: imagem ilegível")
    return out


def _thumbnail(video: str, tmp: str, at: float = 1.0) -> str:
    out = os.path.join(tmp, "thumb.jpg")
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", video, "-frames:v", "1",
           "-vf", "scale=360:-2", "-q:v", "4", out]
    subprocess.run(cmd, capture_output=True, timeout=60)
    if not os.path.isfile(out):  # vídeo mais curto que `at`
        subprocess.run(cmd[:5] + ["0"] + cmd[6:], capture_output=True, timeout=60)
    return out


def _fetch_inputs(payload: dict, tmp: str, progress, with_sources: bool = True) -> tuple[str, list[str], str | None, dict]:
    lim = payload.get("limits", {})
    max_bytes = int(lim.get("max_upload_mb", 50)) * 1024 * 1024
    inp = payload["inputs"]
    progress("analyzing_reference", 0.01)
    ref = _fetch(inp["reference"], tmp, max_bytes)
    ref_d, _, _ = _duration(ref)
    if ref_d > float(lim.get("max_reference_s", 90)) + 0.5:
        raise UserError("reference_too_long", f"referência com {ref_d:.0f}s")
    key_of = {ref: inp["reference"]["key"]}
    sources: list[str] = []
    if with_sources:
        total = 0.0
        for item in inp.get("sources", []):
            p = _fetch(item, tmp, max_bytes)
            if os.path.splitext(p)[1].lower() in IMAGE_EXT:
                clip = _still_to_clip(p, tmp)
                key_of[clip] = item["key"]  # nota: no editor, uma foto volta a ser convertida no worker
                sources.append(clip)
                total += STILL_SECONDS
                continue
            d, _, _ = _duration(p)
            if d > float(lim.get("max_source_s", 180)) + 0.5:
                raise UserError("source_too_long", f"{item.get('name')}: {d:.0f}s")
            total += d
            key_of[p] = item["key"]
            sources.append(p)
        if not sources:
            raise UserError("no_sources", "nenhum vídeo do utilizador")
        if total > float(lim.get("max_sources_total_s", 600)) + 1:
            raise UserError("sources_total_too_long", f"{total:.0f}s no total")
    music = None
    if inp.get("music"):
        music = _fetch(inp["music"], tmp, max_bytes)
        key_of[music] = inp["music"]["key"]
    return ref, sources, music, key_of


# ------------------------------------------------------------------ análise (passo 2)
def summarize(dna, det) -> dict:
    """Resumo legível do estilo da referência (o que a app mostra no passo de análise)."""
    sig, vibe, audio = dna.signature, dna.vibe, dna.audio
    tags = []
    cps = sig.cuts_per_second
    tags.append("Fast cuts" if cps >= 1.2 else "Slow, cinematic cuts" if cps <= 0.45 else "Medium pace")
    zoom = (vibe.zoom or {}) if vibe else {}
    if zoom.get("punch_rate", 0) > 0.2 or sig.zoom_active_ratio > 0.35:
        tags.append("Zoom punches")
    if dna.look and dna.look.shake_fraction > 0.04:
        tags.append("Camera shake")
    if audio.has_audio and audio.cut_beat_alignment is not None and audio.chance_alignment is not None \
            and audio.cut_beat_alignment >= audio.chance_alignment + 0.15:
        tags.append("Beat-synced")
    names = {"flash": "Flash transitions", "glitch": "Glitch transitions", "blur": "Motion-blur transitions",
             "dip_black": "Dip to black"}
    for t, c in sorted(sig.transition_counts.items(), key=lambda kv: -kv[1]):
        if t in names and c >= 2:
            tags.append(names[t])
    if dna.look and dna.look.rgb_split_fraction > 0.02:
        tags.append("RGB split")
    if sig.speed_dynamic_ratio > 0.3:
        tags.append("Speed ramps")
    if vibe and vibe.smoothness > 0.7:
        tags.append("Smooth motion")
    return {
        "duration_s": round(dna.duration_s, 2),
        "n_shots": sig.n_shots,
        "cuts_per_second": round(cps, 2),
        "bpm": round(audio.bpm, 0) if audio.bpm else None,
        "has_audio": audio.has_audio,
        "aggressiveness": round(vibe.aggressiveness, 2) if vibe else None,
        "descriptors": (vibe.descriptors if vibe else [])[:6],
        "transitions": sig.transition_counts,
        "tags": tags[:7],
        "is_velocity": bool(det.is_velocity),
        "score": round(float(det.score), 3),
        "reasons": list(det.reasons)[:3],
    }


def _analyze(payload: dict, tmp: str, progress) -> dict:
    from velocity.detector.velocity_detector import detect
    from velocity.reference.analyzer import analyze_reference
    t = time.time()
    ref, _, _, _ = _fetch_inputs(payload, tmp, progress, with_sources=False)
    progress("analyzing_reference", 0.3)
    dna = analyze_reference(ref, with_text=False)  # o OCR corre na geração, só se houver texto pedido
    det = detect(dna)
    progress("finalizing", 0.95)
    return {
        "reference_summary": summarize(dna, det),
        "reference_dna": json.loads(dna.model_dump_json()),
        "metrics": {"analysis_s": round(time.time() - t, 2)},
    }


# ------------------------------------------------------------------------- geração
def _generate(payload: dict, tmp: str, progress) -> dict:
    from velocity.pipeline import MakeParams, run_make
    from velocity.schemas.edit_dna import EditDNA

    ref, sources, music, key_of = _fetch_inputs(payload, tmp, progress)
    params = payload.get("params", {})
    dna = EditDNA.model_validate(payload["reference_dna"]) if payload.get("reference_dna") else None
    out = os.path.join(tmp, "out.mp4")
    log_lines: list[str] = []
    res = run_make(MakeParams(
        ref=ref, clips=sources, out=out, music=music, profile=params.get("profile", "standard"),
        force=bool(params.get("force")), subject_enhance=bool(params.get("subject_enhance")),
        text=params.get("text") or None, dna=dna,
    ), log=log_lines.append, progress=progress)
    if res.rejected:
        d = res.detector
        raise UserError("reference_not_velocity", json.dumps({"score": d.score if d else None,
                                                              "reasons": (d.reasons if d else [])[:3]}))
    if not res.ok:
        raise RuntimeError(res.error or "falha na geração")

    tl = res.timeline
    for s in tl.shots:  # caminhos locais do worker -> chaves de armazenamento (para o editor)
        s.clip.source_path = key_of.get(s.clip.source_path, s.clip.source_path)
    if tl.music_path:
        tl.music_path = key_of.get(tl.music_path, tl.music_path)
    plan = res.plan.map_uris(lambda u: key_of.get(u, u))

    progress("finalizing", 0.96)
    thumb = _thumbnail(out, tmp)
    _put(payload["outputs"]["video"], out, "video/mp4")
    _put(payload["outputs"]["thumbnail"], thumb, "image/jpeg")
    progress("finalizing", 1.0)
    return {
        "plan": json.loads(plan.model_dump_json()),
        "timeline": json.loads(tl.model_dump_json()),  # para uma API anterior ao Plano (deploys desfasados)
        "duration_s": round(res.duration_s or 0.0, 2),
        "resolution": f"{tl.width}x{tl.height}",
        "fidelity": res.fidelity,
        "detector": {"score": res.detector.score, "is_velocity": res.detector.is_velocity} if res.detector else None,
        "metrics": {"timings_s": res.timings, "resources": res.resources, "fidelity": res.fidelity, "vibe": res.vibe_score,
                    "sources": len(sources), "log_tail": log_lines[-8:]},
    }


# ------------------------------------------------------------- re-render do editor
def _render_edit(payload: dict, tmp: str, progress) -> dict:
    from velocity.engine import PlanRejected, choose_engine
    from velocity.pipeline import PROFILES
    from velocity.plan import check_media, errors, load_plan, plan_to_timeline, validate_plan

    lim = payload.get("limits", {})
    max_bytes = int(lim.get("max_upload_mb", 50)) * 1024 * 1024
    doc = payload.get("plan") or payload["timeline"]
    plan = load_plan(doc)
    engine = choose_engine(plan)
    issues = errors(validate_plan(plan) + engine.validate(plan))
    if issues:
        raise UserError("invalid_plan", "; ".join(str(i) for i in issues[:3]))
    files = payload["inputs"]["files"]
    progress("preparing_edit", 0.05)
    local: dict[str, str] = {}
    for key in {s.uri for s in plan.sources.values()}:
        if key not in files:
            raise UserError("missing_source", "um ficheiro-fonte do projeto já não está disponível")
        p = _fetch(files[key], tmp, max_bytes)
        if os.path.splitext(p)[1].lower() in IMAGE_EXT:
            p = _still_to_clip(p, tmp)
        local[key] = p
    edited = plan.map_uris(lambda u: local[u])
    media = errors(check_media(edited))
    if media:
        raise UserError("invalid_media", "; ".join(str(i) for i in media[:3]))

    prof = PROFILES.get(payload.get("params", {}).get("profile", "standard"), PROFILES["standard"])
    out = os.path.join(tmp, "out.mp4")
    t = time.time()
    progress("rendering", 0.1)
    try:
        engine.render(edited, out, crf=prof["crf"], preset=prof["preset"],
                      progress=lambda i, n: progress("rendering", 0.1 + 0.8 * i / n))
    except PlanRejected as e:
        raise UserError("invalid_plan", str(e)[:300])
    render_s = round(time.time() - t, 2)
    progress("finalizing", 0.92)
    thumb = _thumbnail(out, tmp)
    _put(payload["outputs"]["video"], out, "video/mp4")
    _put(payload["outputs"]["thumbnail"], thumb, "image/jpeg")
    o = plan.output
    return {
        "plan": json.loads(plan.model_dump_json()),
        "timeline": json.loads(plan_to_timeline(plan).model_dump_json()),
        "duration_s": round(o.duration_frames / o.fps, 2),
        "resolution": f"{o.width}x{o.height}",
        "metrics": {"timings_s": {"render": render_s}, "engine": engine.name},
    }