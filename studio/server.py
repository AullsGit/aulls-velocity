"""Studio — interface web local para operar o Velocity Engine sem usar a linha de comandos.

Desenhado para UM operador (o utilizador) processar encomendas: recebe referência + vídeos do
cliente, gera o vídeo, mostra o resultado e o relatório, disponibiliza para download. Corre
localmente (não está exposto à Internet). Os jobs correm em SÉRIE (um de cada vez) numa thread
de fundo, para não competir por CPU/GPU consigo próprio — e para os modelos (OCR, segmentador de
pessoas) ficarem quentes em memória entre encomendas, em vez de recarregar a cada geração
(o maior custo fixo por vídeo nesta máquina).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from queue import Queue

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, File, Form, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from velocity.detector.velocity_detector import detect  # noqa: E402
from velocity.pipeline import PROFILES, MakeParams, run_make  # noqa: E402
from velocity.reference.analyzer import analyze_reference  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_UPLOAD_MB = 300
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

app = FastAPI(title="Velocity Studio")


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued | running | done | rejected | error
    log: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    dir: str = ""
    out_path: str | None = None
    report: dict | None = None
    error: str | None = None

    def to_json(self):
        return {"id": self.id, "status": self.status, "log": self.log, "report": self.report, "error": self.error,
                "has_video": bool(self.out_path and os.path.exists(self.out_path))}


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
QUEUE: "Queue[str]" = Queue()


def _worker():
    """Processa um job de cada vez, para nunca competir por CPU/GPU com uma geração em curso."""
    while True:
        job_id = QUEUE.get()
        job = JOBS.get(job_id)
        if job is None:
            continue
        job.status = "running"
        try:
            _run_job(job)
        except Exception as e:  # nunca deixa o worker morrer por causa de um job
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            job.log.append(f"[ERRO] {job.error}")
            job.log.append(traceback.format_exc(limit=6))


def _ext_ok(name: str, allowed: set) -> bool:
    return os.path.splitext(name)[1].lower() in allowed


def _save_upload(f, dest_dir: str) -> str:
    safe_name = os.path.basename(f.filename or "ficheiro")
    path = os.path.join(dest_dir, safe_name)
    i = 1
    base, ext = os.path.splitext(path)
    while os.path.exists(path):
        path = f"{base}_{i}{ext}"
        i += 1
    with open(path, "wb") as out:
        shutil.copyfileobj(f.file, out)
    return path


def _run_job(job: Job):
    def log(m: str):
        job.log.append(m)

    meta = json.load(open(os.path.join(job.dir, "params.json"), encoding="utf-8"))
    ref = meta["ref"]
    clips = meta["clips"]
    music = meta.get("music")
    out_path = os.path.join(job.dir, "resultado.mp4")
    report_path = os.path.join(job.dir, "relatorio.json")

    text_lines = [t.strip() for t in (meta.get("text") or "").splitlines() if t.strip()] or None

    p = MakeParams(
        ref=ref, clips=clips, out=out_path, music=music, profile=meta.get("profile", "standard"),
        subject_enhance=bool(meta.get("subject_enhance")), force=bool(meta.get("force")), text=text_lines,
        report=report_path,
    )
    res = run_make(p, log=log)
    if res.rejected:
        job.status = "rejected"
        job.error = res.error
        job.report = {"detector": res.detector.__dict__ if res.detector else None}
        return
    if not res.ok:
        job.status = "error"
        job.error = res.error or "falha desconhecida"
        return
    job.out_path = res.out_path
    job.report = res.report
    job.status = "done"


threading.Thread(target=_worker, daemon=True).start()


@app.post("/api/jobs")
async def create_job(
    reference: UploadFile = File(...),
    clips: list[UploadFile] = File(...),
    music: UploadFile | None = File(None),
    profile: str = Form("standard"),
    subject_enhance: bool = Form(False),
    force: bool = Form(False),
    text: str = Form(""),
):
    if not _ext_ok(reference.filename, VIDEO_EXT):
        return JSONResponse({"error": f"referência: formato não suportado ({reference.filename})"}, status_code=400)
    if not clips:
        return JSONResponse({"error": "envie pelo menos 1 vídeo seu"}, status_code=400)
    for c in clips:
        if not _ext_ok(c.filename, VIDEO_EXT):
            return JSONResponse({"error": f"vídeo do utilizador: formato não suportado ({c.filename})"}, status_code=400)
    if music is not None and music.filename and not _ext_ok(music.filename, AUDIO_EXT):
        return JSONResponse({"error": f"música: formato não suportado ({music.filename})"}, status_code=400)
    if profile not in PROFILES:
        profile = "standard"

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(DATA_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    ref_path = _save_upload(reference, job_dir)
    clip_paths = [_save_upload(c, job_dir) for c in clips]
    music_path = _save_upload(music, job_dir) if (music is not None and music.filename) else None

    json.dump({"ref": ref_path, "clips": clip_paths, "music": music_path, "profile": profile,
               "subject_enhance": subject_enhance, "force": force, "text": text},
              open(os.path.join(job_dir, "params.json"), "w", encoding="utf-8"))

    job = Job(id=job_id, dir=job_dir)
    with JOBS_LOCK:
        JOBS[job_id] = job
    QUEUE.put(job_id)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "job desconhecido"}, status_code=404)
    return job.to_json()


@app.get("/api/jobs/{job_id}/video")
def get_video(job_id: str):
    job = JOBS.get(job_id)
    if job is None or not job.out_path or not os.path.exists(job.out_path):
        return JSONResponse({"error": "vídeo indisponível"}, status_code=404)
    return FileResponse(job.out_path, media_type="video/mp4", filename=f"velocity_{job_id}.mp4")


@app.get("/api/jobs/{job_id}/report")
def get_report(job_id: str):
    job = JOBS.get(job_id)
    if job is None or not job.report:
        return JSONResponse({"error": "relatório indisponível"}, status_code=404)
    return job.report


@app.post("/api/check-reference")
async def check_reference(reference: UploadFile = File(...)):
    """Verificação rápida (sem gerar nada): esta referência parece um Velocity suportado?"""
    if not _ext_ok(reference.filename, VIDEO_EXT):
        return JSONResponse({"error": "formato não suportado"}, status_code=400)
    tmp_dir = os.path.join(DATA_DIR, "_check", uuid.uuid4().hex[:8])
    os.makedirs(tmp_dir, exist_ok=True)
    path = _save_upload(reference, tmp_dir)
    try:
        dna = analyze_reference(path, with_text=False)
        d = detect(dna)
        return {"is_velocity": d.is_velocity, "score": d.score, "reasons": d.reasons,
                "duration_s": dna.duration_s, "n_shots": dna.signature.n_shots}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def index():
    return HTMLResponse(open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8").read())


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
