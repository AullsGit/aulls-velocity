"""Ciclo de vida dos jobs: criar -> (créditos reservados) -> fornecedor -> progresso -> fim.

Regras de negócio que vivem AQUI (e só aqui):
  * os créditos são reservados ANTES de qualquer processamento (`spend_credits`, atómico e
    idempotente por job) — nunca se gera sem pagar;
  * se o job não entregar vídeo (erro do sistema, ficheiro inválido, referência recusada,
    cancelamento), os créditos voltam automaticamente (`refund_job`, idempotente);
  * a API nunca espera pelo processamento: cria o job e devolve logo o id.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from studio import config
from studio.errors import ApiError, job_error_message
from studio.store import JOB_ACTIVE, JOB_TERMINAL

log = logging.getLogger("awls.jobs")

GENERATION_PROFILES = ("fast", "standard", "hd")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secs_since(iso: str | None) -> float | None:
    if not iso:
        return None
    return (_now() - datetime.fromisoformat(iso)).total_seconds()


class JobService:
    def __init__(self, store, storage, provider):
        self.store, self.storage, self.provider = store, storage, provider
        self._progress_written: dict[str, tuple[float, str]] = {}
        self._monitor_started = False
        if provider.push_updates:
            provider.bind(self.on_progress, self.on_result)

    # ------------------------------------------------------------------ helpers
    def _item(self, f: dict) -> dict:
        local = self.storage.local_path("uploads", f["path"]) if getattr(self.storage, "is_local", False) else None
        return {"key": f"uploads/{f['path']}", "name": f.get("name") or f["path"].rsplit("/", 1)[-1],
                "url": self.storage.signed_url("uploads", f["path"], 6 * 3600), "local_path": local}

    def _target(self, bucket: str, path: str) -> dict:
        up = self.storage.create_upload_url(bucket, path)
        local = self.storage.local_path(bucket, path) if getattr(self.storage, "is_local", False) else None
        return {"url": up["url"], "local_path": local}

    @staticmethod
    def _limits() -> dict:
        return {"max_upload_mb": config.MAX_UPLOAD_MB, "max_reference_s": config.MAX_REFERENCE_S,
                "max_source_s": config.MAX_SOURCE_S, "max_sources_total_s": config.MAX_SOURCES_TOTAL_S}

    @staticmethod
    def output_paths(job: dict) -> tuple[str, str]:
        base = f"{job['user_id']}/{job['project_id']}/{job['id']}"
        return base + ".mp4", base + ".jpg"

    def _track(self, name: str, user_id: str | None, props: dict | None = None) -> None:
        try:
            self.store.track(name, user_id, None, props)
        except Exception:  # analytics nunca pode partir um fluxo
            log.exception("track falhou: %s", name)

    def _check_capacity(self) -> None:
        if self.store.count_active_jobs() >= config.MAX_QUEUE:
            raise ApiError(503, "busy", "We're creating a lot of videos right now. Please try again in a few minutes.")

    def _project_files(self, project: dict) -> tuple[dict | None, list[dict], dict | None]:
        files = [f for f in project.get("files") or [] if f.get("uploaded")]
        ref = next((f for f in files if f["kind"] == "reference"), None)
        sources = [f for f in files if f["kind"] == "source"]
        music = next((f for f in files if f["kind"] == "music"), None)
        return ref, sources, music

    def _assert_idle(self, project: dict) -> None:
        if self.store.list_jobs(project_id=project["id"], statuses=JOB_ACTIVE, limit=1):
            raise ApiError(409, "busy_project", "This project is already being processed.")

    def _new_job(self, user_id: str, project: dict, kind: str, credits: int, **extra) -> dict:
        job_id = uuid.uuid4().hex
        if credits > 0 and not self.store.spend_credits(user_id, credits, job_id):
            raise ApiError(402, "insufficient_credits", "You don't have enough credits for this.",
                           balance=self.store.balance(user_id), cost=credits)
        try:
            job = self.store.create_job({"id": job_id, "user_id": user_id, "project_id": project["id"], "kind": kind,
                                         "credits_used": credits, "provider": self.provider.name, **extra})
        except Exception:
            if credits > 0:  # o job não chegou a existir: devolve já o que foi cobrado
                self.store.grant_credits(user_id, credits, "refund", job_id, f"refund:{job_id}")
            raise
        if kind != "analyze":
            out_path, _ = self.output_paths(job)
            self.store.update_job(job_id, {"output_path": out_path})
        return job

    def _submit(self, job: dict, payload: dict) -> dict:
        try:
            pid = self.provider.submit(job["id"], payload)
        except Exception as e:
            log.exception("submit falhou (%s)", job["id"])
            self._fail(job["id"], "internal", f"submit: {e!r}")
            raise ApiError(503, "compute_unavailable",
                           "Our video engine is unavailable right now. Your credits were refunded — please try again in a minute.")
        return self.store.update_job(job["id"], {"provider_job_id": pid})

    # ----------------------------------------------------------------- criação
    def create_analysis(self, user_id: str, project: dict) -> dict:
        ref, _, _ = self._project_files(project)
        if ref is None:
            raise ApiError(400, "no_reference", "Upload a reference video first.")
        self._check_capacity()
        job = self._new_job(user_id, project, "analyze", 0)
        payload = {"kind": "analyze", "job_id": job["id"], "inputs": {"reference": self._item(ref)},
                   "limits": self._limits()}
        self.store.update_project(project["id"], {"reference_summary": None, "reference_dna": None,
                                                  "settings": {**(project.get("settings") or {}), "analysis_job": job["id"],
                                                               "analysis_error": None}})
        return self._submit(job, payload)

    def create_generation(self, user_id: str, project: dict, profile: str = "standard", force: bool = False,
                          text: list[str] | None = None, idem_key: str | None = None) -> dict:
        if idem_key:
            existing = self.store.find_job_by_idempotency(user_id, idem_key)
            if existing:
                return existing  # o mesmo clique repetido (rede lenta, duplo toque) não cobra 2x
        if profile not in GENERATION_PROFILES:
            raise ApiError(400, "bad_profile", "Unknown quality option.")
        ref, sources, music = self._project_files(project)
        if ref is None:
            raise ApiError(400, "no_reference", "Upload a reference video first.")
        if not sources:
            raise ApiError(400, "no_sources", "Add at least one of your own videos.")
        if len(sources) > config.MAX_SOURCE_FILES:
            raise ApiError(400, "too_many_files", f"Use up to {config.MAX_SOURCE_FILES} files.")
        self._assert_idle(project)
        self._check_capacity()
        cost = int((self.store.get_config().get("credits_per_generation") or {}).get(profile, 1))
        job = self._new_job(user_id, project, "generate", cost, profile=profile, idempotency_key=idem_key)
        out_path, thumb_path = self.output_paths(job)
        payload = {
            "kind": "generate", "job_id": job["id"],
            "inputs": {"reference": self._item(ref), "sources": [self._item(f) for f in sources],
                       "music": self._item(music) if music else None},
            "params": {"profile": profile, "force": bool(force), "text": [t for t in (text or []) if t.strip()][:5]},
            "reference_dna": project.get("reference_dna"),
            "limits": self._limits(),
            "outputs": {"video": self._target("outputs", out_path), "thumbnail": self._target("thumbs", thumb_path)},
        }
        self.store.update_project(project["id"], {"status": "processing", "last_job_id": job["id"],
                                                  "settings": {**(project.get("settings") or {}), "profile": profile}})
        self._track("generation_started", user_id, {"profile": profile, "credits": cost})
        return self._submit(job, payload)

    def create_render_edit(self, user_id: str, project: dict, doc: dict, idem_key: str | None = None) -> dict:
        from pydantic import ValidationError
        from velocity.edit.timeline_ops import EditDoc, apply_edit
        from velocity.engine import choose_engine
        from velocity.plan import PlanVersionError, errors, load_plan, plan_to_timeline, timeline_to_plan, validate_plan

        if idem_key:
            existing = self.store.find_job_by_idempotency(user_id, idem_key)
            if existing:
                return existing
        if not project.get("timeline"):
            raise ApiError(400, "nothing_to_edit", "Generate a video before editing it.")
        if project.get("sources_purged"):
            raise ApiError(410, "sources_expired", "The original clips for this project have expired, so it can't be edited.")
        self._assert_idle(project)
        try:
            edit = EditDoc.model_validate(doc)
            files = {f["id"]: f"uploads/{f['path']}" for f in project.get("files") or [] if f.get("uploaded")}
            original = load_plan(project["timeline"])
            edited = apply_edit(plan_to_timeline(original), edit, files)
            plan = timeline_to_plan(edited, meta={**original.meta, "edited": True})
        except PlanVersionError as e:
            raise ApiError(409, "project_too_new", f"This project was saved by a newer version: {e}"[:300])
        except (ValidationError, ValueError) as e:
            raise ApiError(400, "invalid_edit", f"This edit can't be applied: {e}"[:300])
        issues = errors(validate_plan(plan) + choose_engine(plan).validate(plan))
        if issues:  # recusado ANTES de reservar créditos ou de ocupar um worker
            raise ApiError(400, "invalid_edit", "This edit can't be rendered: " + "; ".join(i.message for i in issues[:2]))
        self._check_capacity()
        cost = int(self.store.get_config().get("credits_per_edit_export") or 0)
        profile = (project.get("settings") or {}).get("profile", "standard")
        job = self._new_job(user_id, project, "render_edit", cost, profile=profile, idempotency_key=idem_key)
        out_path, thumb_path = self.output_paths(job)
        keys = {s.clip.source_path for s in edited.shots} | ({edited.music_path} if edited.music_path else set())
        by_key = {f"uploads/{f['path']}": f for f in project.get("files") or [] if f.get("uploaded")}
        missing = [k for k in keys if k not in by_key]
        if missing:
            self._fail(job["id"], "missing_source", f"chaves sem ficheiro: {missing}")
            raise ApiError(410, "sources_expired", "Some original clips for this project are no longer available.")
        payload = {
            "kind": "render_edit", "job_id": job["id"],
            "plan": json.loads(plan.model_dump_json()),
            "timeline": json.loads(edited.model_dump_json()),  # worker anterior ao Plano (deploys desfasados)
            "inputs": {"files": {k: self._item(by_key[k]) for k in keys}},
            "params": {"profile": profile}, "limits": self._limits(),
            "outputs": {"video": self._target("outputs", out_path), "thumbnail": self._target("thumbs", thumb_path)},
        }
        self.store.update_project(project["id"], {"status": "processing", "last_job_id": job["id"],
                                                  "settings": {**(project.get("settings") or {}), "edit_doc": doc}})
        self._track("edit_export_started", user_id, {"credits": cost})
        return self._submit(job, payload)

    # --------------------------------------------------------------- progresso
    def on_progress(self, job_id: str, stage: str, frac: float) -> None:
        now = time.time()
        last_t, last_stage = self._progress_written.get(job_id, (0.0, ""))
        if stage == last_stage and now - last_t < 1.5 and frac < 1.0:
            return  # não escrever na base de dados a cada frame
        self._progress_written[job_id] = (now, stage)
        job = self.store.get_job(job_id)
        if not job or job["status"] in JOB_TERMINAL:
            return
        fields = {"stage": "queued" if stage == "started" else stage, "progress": float(frac)}
        if job["status"] == "QUEUED":
            fields.update(status="PROCESSING", started_at=_now().isoformat(),
                          queue_time_s=round(_secs_since(job["created_at"]) or 0.0, 2))
        self.store.update_job(job_id, fields)

    def on_result(self, job_id: str, output: dict | None, exec_ms=None, queue_ms=None, worker_id=None) -> None:
        job = self.store.get_job(job_id)
        if not job or job["status"] in JOB_TERMINAL:
            return  # idempotente: webhook + polling podem entregar o mesmo resultado
        self._progress_written.pop(job_id, None)
        if output and output.get("ok"):
            self._complete(job, output, exec_ms, queue_ms, worker_id)
        else:
            out = output or {}
            self._fail(job_id, out.get("error_code", "internal"), out.get("detail"), exec_ms=exec_ms,
                       queue_ms=queue_ms, worker_id=worker_id)

    def refresh(self, job_id: str) -> dict | None:
        """Fornecedores sem push (RunPod): consulta o estado e aplica-o."""
        job = self.store.get_job(job_id)
        if not job or job["status"] in JOB_TERMINAL or not job.get("provider_job_id") or self.provider.push_updates:
            return job
        try:
            st = self.provider.poll(job["provider_job_id"])
        except Exception as e:
            log.warning("poll falhou (%s): %r", job_id, e)
            return job
        if st.status in ("QUEUED", "PROCESSING"):
            fields = {}
            if st.status == "PROCESSING" and job["status"] == "QUEUED":
                fields.update(status="PROCESSING", started_at=_now().isoformat(),
                              queue_time_s=round(_secs_since(job["created_at"]) or 0.0, 2), stage="analyzing_reference")
            if st.stage:
                fields.update(stage=st.stage, progress=st.progress or 0.0)
            age = _secs_since(job["created_at"]) or 0
            if age > config.RUNPOD_EXECUTION_TIMEOUT_S + 2 * 3600:
                try:
                    self.provider.cancel(job["provider_job_id"])
                finally:
                    self._fail(job_id, "timeout", f"sem resposta há {age:.0f}s")
                return self.store.get_job(job_id)
            if fields:
                self.store.update_job(job_id, fields)
        elif st.status == "COMPLETED":
            self.on_result(job_id, st.output or {"ok": False, "error_code": "internal", "detail": "sem output"},
                           exec_ms=st.exec_ms, queue_ms=st.queue_ms, worker_id=st.worker_id)
        elif st.status == "CANCELLED":
            self._finish_cancel(job)
        else:
            self._fail(job_id, "internal", st.error, exec_ms=st.exec_ms, queue_ms=st.queue_ms, worker_id=st.worker_id)
        return self.store.get_job(job_id)

    # ------------------------------------------------------------------- fim
    def _cost(self, exec_s: float | None) -> float | None:
        if exec_s is None or self.provider.name == "local":
            return None
        rate = float(self.store.get_config().get("compute_cost_per_second_usd") or 0.0)
        return round(exec_s * rate, 5) if rate > 0 else None

    def _complete(self, job: dict, out: dict, exec_ms, queue_ms, worker_id) -> None:
        metrics = out.get("metrics") or {}
        exec_s = exec_ms / 1000 if exec_ms is not None else metrics.get("worker_wall_s")
        fields = {
            "status": "COMPLETED", "stage": "complete", "progress": 1.0, "completed_at": _now().isoformat(),
            "processing_time_s": round(exec_s, 2) if exec_s is not None else None,
            "gpu_time_s": round(exec_s, 2) if (exec_s is not None and self.provider.name != "local") else None,
            "est_cost_usd": self._cost(exec_s), "worker_id": worker_id, "worker_type": self.provider.name,
            "metrics": metrics,
        }
        if queue_ms is not None:
            fields["queue_time_s"] = round(queue_ms / 1000, 2)
        if not job.get("started_at"):
            fields["started_at"] = fields["completed_at"]
        project = self.store.get_project(job["project_id"]) if job.get("project_id") else None
        if job["kind"] == "analyze":
            self.store.update_job(job["id"], fields)
            if project:
                self.store.update_project(project["id"], {"reference_summary": out.get("reference_summary"),
                                                          "reference_dna": out.get("reference_dna")})
            self._track("analysis_completed", job["user_id"], {"is_velocity": (out.get("reference_summary") or {}).get("is_velocity")})
            return
        out_path, thumb_path = self.output_paths(job)
        fields.update(video_duration_s=out.get("duration_s"), resolution=out.get("resolution"), output_path=out_path)
        self.store.update_job(job["id"], fields)
        if project:
            old = (project.get("output_path"), project.get("thumbnail_path"))
            settings = dict(project.get("settings") or {})
            if job["kind"] == "generate":
                settings["ai_timeline"] = out.get("plan") or out.get("timeline")  # "voltar à versão da IA" no editor
                settings.pop("edit_doc", None)
            self.store.update_project(project["id"], {
                "status": "completed", "output_path": out_path, "thumbnail_path": thumb_path,
                "duration_s": out.get("duration_s"), "resolution": out.get("resolution"),
                # coluna `timeline`: guarda o Plano v1 (ou a Timeline antiga de um worker anterior)
                "timeline": out.get("plan") or out.get("timeline"), "last_job_id": job["id"], "settings": settings,
            })
            try:  # o Supabase Free tem 1 GB: o vídeo anterior do mesmo projeto já não é preciso
                if old[0] and old[0] != out_path:
                    self.storage.remove("outputs", [old[0]])
                if old[1] and old[1] != thumb_path:
                    self.storage.remove("thumbs", [old[1]])
            except Exception:
                log.exception("limpeza do output anterior falhou")
        if job["kind"] == "generate":
            self._track("generation_completed", job["user_id"], {"processing_s": fields["processing_time_s"],
                                                                 "profile": job.get("profile")})
            done = [j for j in self.store.list_jobs(user_id=job["user_id"], statuses=["COMPLETED"], limit=5)
                    if j["kind"] == "generate"]
            if len(done) == 1:
                self._track("first_video", job["user_id"])
        else:
            self._track("edit_export_completed", job["user_id"])

    def _fail(self, job_id: str, code: str, detail: str | None, exec_ms=None, queue_ms=None, worker_id=None) -> None:
        job = self.store.get_job(job_id)
        if not job or job["status"] in JOB_TERMINAL:
            return
        exec_s = exec_ms / 1000 if exec_ms is not None else None
        self.store.update_job(job_id, {
            "status": "FAILED", "stage": "failed", "failed_at": _now().isoformat(),
            "error_message": job_error_message(code), "error_detail": f"{code}: {detail or ''}"[:4000],
            "processing_time_s": round(exec_s, 2) if exec_s is not None else None,
            "est_cost_usd": self._cost(exec_s), "worker_id": worker_id, "worker_type": self.provider.name,
            "metrics": {**(job.get("metrics") or {}), "error_code": code},
            **({"queue_time_s": round(queue_ms / 1000, 2)} if queue_ms is not None else {}),
        })
        refunded = False
        if job.get("credits_used"):
            refunded = self.store.refund_job(job_id)  # sem vídeo, sem cobrança
        project = self.store.get_project(job["project_id"]) if job.get("project_id") else None
        if project:
            if job["kind"] == "analyze":
                self.store.update_project(project["id"], {"settings": {**(project.get("settings") or {}),
                                                                       "analysis_error": code}})
            else:
                self.store.update_project(project["id"], {"status": "completed" if project.get("output_path") else "failed"})
        log.warning("job %s falhou: %s (reembolso=%s) %s", job_id, code, refunded, (detail or "")[:300])
        self._track("generation_failed" if job["kind"] == "generate" else f"{job['kind']}_failed",
                    job["user_id"], {"code": code, "refunded": refunded})

    def _finish_cancel(self, job: dict) -> None:
        self.store.update_job(job["id"], {"status": "CANCELLED", "stage": "cancelled", "cancelled_at": _now().isoformat()})
        if job.get("credits_used"):
            self.store.refund_job(job["id"])
        project = self.store.get_project(job["project_id"]) if job.get("project_id") else None
        if project and job["kind"] != "analyze":
            self.store.update_project(project["id"], {"status": "completed" if project.get("output_path") else "draft"})

    def cancel(self, user_id: str, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job or job["user_id"] != user_id:
            raise ApiError(404, "not_found", "Job not found.")
        if job["status"] in JOB_TERMINAL:
            return job
        try:
            if job.get("provider_job_id"):
                self.provider.cancel(job["provider_job_id"])
        except Exception:
            log.exception("cancel no fornecedor falhou (%s)", job_id)
        self._finish_cancel(job)
        return self.store.get_job(job_id)

    # ----------------------------------------------------------- fundo / limpeza
    def recover_interrupted(self) -> int:
        """A fila do fornecedor local vive em memória: jobs que estavam a correr quando o
        processo reiniciou nunca vão acabar — falham já, com reembolso."""
        if self.provider.name != "local":
            return 0
        n = 0
        for job in self.store.list_jobs(statuses=JOB_ACTIVE, provider="local", limit=200):
            self._fail(job["id"], "interrupted", "processo reiniciado")
            n += 1
        return n

    def cleanup(self) -> dict:
        res = {"drafts": 0, "sources": 0}
        found = self.store.list_projects_for_cleanup(config.DRAFT_RETENTION_DAYS, config.SOURCE_RETENTION_DAYS)
        for p in found["drafts"]:
            try:
                self.storage.remove_prefix("uploads", f"{p['user_id']}/{p['id']}")
                self.store.delete_project(p["id"])
                res["drafts"] += 1
            except Exception:
                log.exception("limpeza de rascunho falhou (%s)", p["id"])
        for p in found["stale_sources"]:
            try:
                self.storage.remove_prefix("uploads", f"{p['user_id']}/{p['id']}")
                self.store.update_project(p["id"], {"sources_purged": True})
                res["sources"] += 1
            except Exception:
                log.exception("limpeza de fontes falhou (%s)", p["id"])
        if res["drafts"] or res["sources"]:
            log.info("limpeza: %s", res)
        return res

    def start_monitor(self) -> None:
        if self._monitor_started:
            return
        self._monitor_started = True

        def loop():
            try:
                n = self.recover_interrupted()
                if n:
                    log.warning("%d job(s) interrompido(s) pelo reinício — reembolsados", n)
            except Exception:
                log.exception("recover_interrupted falhou")
            last_cleanup = 0.0
            while True:
                active = []
                if not self.provider.push_updates:
                    try:
                        active = self.store.list_jobs(statuses=JOB_ACTIVE, provider=self.provider.name, limit=50)
                        for j in active:
                            self.refresh(j["id"])
                    except Exception:
                        log.exception("monitor: refresh falhou")
                if time.time() - last_cleanup > 3600:
                    last_cleanup = time.time()
                    try:
                        self.cleanup()
                    except Exception:
                        log.exception("monitor: cleanup falhou")
                time.sleep(3 if active else 10)

        threading.Thread(target=loop, daemon=True, name="job-monitor").start()
