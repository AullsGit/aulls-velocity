"""Conversas do agente: uma por projeto. Cada mensagem do utilizador abre um turno em segundo
plano; a app acompanha-o pedindo os eventos novos. Tudo o que importa é guardado a cada passo, por isso
um reinício do servidor (o Render adormece, há deploys) retoma o turno onde ia.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid

import anthropic
import requests

from studio import config
from studio.agent import loop as agent_loop
from studio.agent.tools import TOOL_LABELS, QUALITY, ToolFailure, TurnCancelled
from studio.errors import ApiError
from studio.store import JOB_TERMINAL

log = logging.getLogger("awls.agent")

MAX_EVENTS_RETURNED = 400
MEMORY_MAX = 40
STAGES = {"queued": "na fila", "analyzing_reference": "a analisar", "preparing_edit": "a preparar", "rendering": "a renderizar",
          "applying_effects": "a aplicar efeitos", "finalizing": "a terminar", "complete": "feito"}
# preços por milhão de tokens, só para o painel de administração saber quanto custa um projeto
PRICE = {"claude-opus-5-5": (4.0, 20.0, 0.2, 5.0)}


def _now() -> float:
    return time.time()


class TurnContext:
    """Tudo o que o ciclo e as ferramentas precisam de um turno em curso."""

    def __init__(self, service: "AgentService", thread: dict, user_id: str):
        self.svc, self.user_id = service, user_id
        self.project_id = thread["project_id"]
        self.messages: list[dict] = thread.get("messages") or []
        self.state: dict = thread.get("state") or {}
        self.events: list[dict] = thread.get("events") or []
        self.usage: dict = thread.get("usage") or {}
        self.turn_counters: dict = {}
        self._turn_input = 0
        self.cancel_flag = service._cancel.setdefault(self.project_id, threading.Event())
        self._active_job: str | None = None

    # ---- persistência
    def _store(self):
        return self.svc.store

    def project(self) -> dict:
        p = self._store().get_project(self.project_id)
        if p is None:
            raise TurnCancelled()  # o projeto foi apagado a meio
        return p

    def save_messages(self) -> None:
        self._store().update_thread(self.project_id, {"messages": self.messages, "usage": self.usage})

    def save_state(self) -> None:
        self._store().update_thread(self.project_id, {"state": self.state})

    def dna_doc(self) -> dict | None:
        return self.project().get("reference_dna")

    def save_analysis(self, dna: dict, summary: dict | None) -> None:
        self._store().update_project(self.project_id, {"reference_dna": dna, "reference_summary": summary})

    # ---- eventos para a interface
    def emit(self, ev: dict) -> None:
        ev = {"seq": len(self.events) + 1, "ts": round(_now(), 2), **ev}
        self.events.append(ev)
        self._store().update_thread(self.project_id, {"events": self.events[-2000:]})

    def live_text(self, delta: str) -> None:
        live = self.svc._live.setdefault(self.project_id, {})
        live["text"] = (live.get("text") or "") + delta

    def live_status(self, delta: str) -> None:
        live = self.svc._live.setdefault(self.project_id, {})
        live["status"] = (live.get("status") or "") + delta

    def clear_live(self) -> None:
        live = self.svc._live.setdefault(self.project_id, {})
        live.pop("text", None)
        live.pop("status", None)

    def tool_started(self, tool_id: str, name: str, args: dict) -> None:
        self.svc._live.setdefault(self.project_id, {})["tool"] = {"id": tool_id, "name": name, "label": TOOL_LABELS.get(name, name), "stage": None, "progress": None}
        self.emit({"type": "tool", "id": tool_id, "name": name, "label": TOOL_LABELS.get(name, name), "status": "running"})

    def tool_finished(self, tool_id: str, ok: bool, seconds: float, error: str | None = None, cancelled: bool = False, images: list | None = None) -> None:
        self.svc._live.setdefault(self.project_id, {}).pop("tool", None)
        for ev in reversed(self.events):
            if ev.get("type") == "tool" and ev.get("id") == tool_id:
                ev.update(status="done" if ok else ("cancelled" if cancelled else "error"), seconds=round(seconds, 1))
                if images:
                    ev["images"] = [{"path": i["path"], "label": i.get("label")} for i in images]
                if error:
                    ev["error"] = error[:300]
                break
        self._store().update_thread(self.project_id, {"events": self.events[-2000:]})

    def check_cancel(self) -> None:
        if self.cancel_flag.is_set():
            raise TurnCancelled()

    # ---- computação
    def run_job(self, kind: str, tool_use_id: str, *, keys=None, params=None, plan=None, reference_dna=None, n_images: int = 0, credits: int = 0, profile=None) -> dict:
        """Cria o job (idempotente pelo id da chamada: retomar um turno não o repete) e espera por ele."""
        jobs = self.svc.jobs()
        project = self.project()
        job = jobs.create_agent_job(self.user_id, project, kind, keys=keys, params=params, plan=plan, reference_dna=reference_dna, n_images=n_images,
                                    credits=credits, profile=profile, idem_key=f"agent:{self.project_id}:{tool_use_id}")
        job_id = job["id"]
        self._active_job = job_id
        t0 = _now()
        try:
            while True:
                j = self._store().get_job(job_id)
                if j is None:
                    raise ToolFailure("o trabalho desapareceu")
                if j["status"] in JOB_TERMINAL:
                    break
                try:
                    self.check_cancel()
                except TurnCancelled:
                    jobs.cancel(self.user_id, job_id)
                    raise
                if not jobs.provider.push_updates:
                    jobs.refresh(job_id)
                tool = self.svc._live.get(self.project_id, {}).get("tool")
                if tool is not None:
                    tool["stage"], tool["progress"] = STAGES.get(j.get("stage"), None), j.get("progress")
                if _now() - t0 > config.AGENT_TOOL_TIMEOUT_S:
                    jobs.cancel(self.user_id, job_id)
                    raise ToolFailure("este passo demorou demasiado e foi cancelado")
                time.sleep(0.5)
        finally:
            self._active_job = None
        if j["status"] != "COMPLETED":
            raise ToolFailure(f"o trabalho falhou: {j.get('error_detail') or j.get('error_message') or j['status']}"[:600])
        return j

    def output_paths(self, job: dict) -> tuple[str, str]:
        return self.svc.jobs().output_paths(job)

    def balance(self) -> int:
        return self._store().balance(self.user_id)

    def render_cost(self, quality: str) -> int:
        """A pré-visualização é grátis; a 1.ª final custa uma geração, as correções têm preço próprio."""
        if quality == "preview":
            return 0
        cfg = self._store().get_config()
        first = not any(v.get("quality") != "preview" for v in self.state.get("versions") or [])
        if first:
            return int((cfg.get("credits_per_generation") or {}).get(QUALITY[quality]["profile"], 1))
        return int(cfg.get("credits_per_agent_revision", 1))

    # ---- imagens para o modelo
    def image_block(self, img: dict) -> dict:
        """Sobe a folha de frames para a Files API: o histórico guarda só o id, nunca a imagem,
        e reenviar a conversa fica leve e igual byte a byte."""
        storage = self.svc.storage
        if getattr(storage, "is_local", False):
            data = open(storage.local_path("outputs", img["path"]), "rb").read()
        else:
            r = requests.get(storage.signed_url("outputs", img["path"], 600), timeout=(10, 120))
            r.raise_for_status()
            data = r.content
        try:
            f = agent_loop.get_client().files.upload(file=(img["path"].rsplit("/", 1)[-1], data, "image/jpeg"))
            self.state.setdefault("files_api", []).append(f.id)
            self.save_state()
            return {"type": "image", "source": {"type": "file", "file_id": f.id}}
        except anthropic.APIError as e:  # sem Files API (plataforma/plano): a imagem vai no próprio pedido
            log.warning("Files API indisponível, imagem em base64: %s", e)
            return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(data).decode()}}

    # ---- apresentar, memória, ficheiros
    def present(self, version: dict) -> None:
        plan = version.get("plan")
        vs = self.state.get("versions") or []
        keep_paths = {version["output_path"]}
        self._store().update_project(self.project_id, {
            "status": "completed", "output_path": version["output_path"], "thumbnail_path": version["thumbnail_path"], "duration_s": version.get("duration_s"),
            "resolution": version.get("resolution"), "timeline": plan, "last_job_id": version.get("job_id"),
            "settings": {**(self.project().get("settings") or {}), "ai_timeline": plan, "profile": QUALITY[version["quality"]]["profile"]},
        })
        self.save_state()
        # só as últimas versões ficam guardadas (o plano mantém-se, o vídeo pode ser refeito)
        rendered = [v for v in vs if v.get("output_path")]
        for old in rendered[:-config.AGENT_KEEP_VERSIONS]:
            if old["output_path"] in keep_paths:
                continue
            try:
                self.svc.storage.remove("outputs", [old["output_path"]])
                self.svc.storage.remove("thumbs", [old["thumbnail_path"]])
            except Exception:
                log.exception("limpeza de versão antiga falhou")
            old["output_path"] = None
        self.save_state()

    def _memory(self) -> list[str]:
        return list((self._store().get_profile(self.user_id) or {}).get("agent_memory") or [])

    def remember(self, text: str) -> None:
        mem = [m for m in self._memory() if m != text]
        mem.append(text)
        self._store().update_profile(self.user_id, {"agent_memory": mem[-MEMORY_MAX:]})

    def forget(self, text: str) -> bool:
        mem = self._memory()
        if text not in mem:
            return False
        mem.remove(text)
        self._store().update_profile(self.user_id, {"agent_memory": mem})
        return True

    def set_file_role(self, file_id: str, role: str) -> None:
        p = self.project()
        files = list(p.get("files") or [])
        f = next((x for x in files if x["id"] == file_id), None)
        if f is None:
            raise ToolFailure("esse ficheiro não existe neste projeto")
        if role in ("reference", "music"):
            for x in files:
                if x["kind"] == role and x["id"] != file_id:
                    x["kind"] = "source" if role == "reference" else x["kind"]
        fields = {"files": files}
        if role == "reference" or f["kind"] == "reference":
            fields.update(reference_dna=None, reference_summary=None)
        f["kind"] = role
        self._store().update_project(self.project_id, fields)

    # ---- custo
    def add_usage(self, final) -> None:
        u = getattr(final, "usage", None)
        if u is None:
            return
        inp = int(getattr(u, "input_tokens", 0) or 0)
        out = int(getattr(u, "output_tokens", 0) or 0)
        read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        self._turn_input += inp + write
        a = self.usage
        a["input"], a["output"] = a.get("input", 0) + inp, a.get("output", 0) + out
        a["cache_read"], a["cache_write"] = a.get("cache_read", 0) + read, a.get("cache_write", 0) + write
        a["requests"] = a.get("requests", 0) + 1
        p = PRICE.get(config.AGENT_MODEL)
        if p:
            a["est_usd"] = round(a.get("est_usd", 0.0) + (inp * p[0] + out * p[1] + read * p[2] + write * p[3]) / 1e6, 4)

    def turn_input_tokens(self) -> int:
        return self._turn_input


class AgentService:
    def __init__(self, store, storage, jobs_factory):
        self.store, self.storage, self.jobs = store, storage, jobs_factory
        self._cancel: dict[str, threading.Event] = {}
        self._live: dict[str, dict] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, config.AGENT_MAX_CONCURRENT_TURNS))

    # ------------------------------------------------------------------ conversas
    def get_or_create(self, user_id: str, project_id: str) -> dict:
        return self.store.get_thread(project_id) or self.store.create_thread(user_id, project_id)

    def _project_block(self, user_id: str, project: dict, thread: dict) -> str:
        """Contexto da app para o modelo: vai dentro da mensagem do utilizador, não no sistema,
        para o início da conversa continuar igual byte a byte."""
        state = thread.get("state") or {}
        files = [{"id": f["id"], "role": f["kind"], "name": f["name"], "duration_s": f.get("duration_s"), "size": [f.get("width"), f.get("height")]}
                 for f in project.get("files") or [] if f.get("uploaded")]
        versions = [{"id": v["id"], "quality": v["quality"], "variant": v.get("variant"), "presented": v.get("presented"), "note": v.get("note")}
                    for v in (state.get("versions") or [])[-6:]]
        ctx = {"files": files, "has_plan": bool(state.get("plan")), "versions": versions, "reference_analyzed": bool(project.get("reference_dna")),
               "captions_read": len((state.get("captions") or {}).get("lines") or []), "credits": self.store.balance(user_id),
               "saved_preferences": (self.store.get_profile(user_id) or {}).get("agent_memory") or []}
        return "<project>\n" + json.dumps(ctx, ensure_ascii=False) + "\n</project>"

    def post_message(self, user_id: str, project: dict, text: str) -> dict:
        pid = project["id"]
        thread = self.get_or_create(user_id, pid)
        with self._lock:
            if pid in self._running or thread["status"] == "running":
                raise ApiError(409, "agent_busy", "Ainda estou a trabalhar no pedido anterior. Espera um momento ou carrega em parar.")
            self._running.add(pid)
        try:
            events = thread.get("events") or []
            messages = thread.get("messages") or []
            events.append({"seq": len(events) + 1, "ts": round(_now(), 2), "type": "user", "text": text})
            messages.append({"role": "user", "content": [{"type": "text", "text": self._project_block(user_id, project, thread)}, {"type": "text", "text": text}]})
            self.store.update_thread(pid, {"status": "running", "messages": messages, "events": events})
        except Exception:
            with self._lock:
                self._running.discard(pid)
            raise
        self._start(user_id, pid)
        return self.store.get_thread(pid)

    def _start(self, user_id: str, project_id: str) -> None:
        self._cancel[project_id] = threading.Event()
        self._live[project_id] = {}
        threading.Thread(target=self._run, args=(user_id, project_id), daemon=True, name=f"agent-{project_id[:8]}").start()

    def _run(self, user_id: str, project_id: str) -> None:
        with self._sem:
            ctx = None
            try:
                ctx = TurnContext(self, self.store.get_thread(project_id), user_id)
                agent_loop.run_turn(ctx)
            except TurnCancelled:
                if ctx is not None:
                    ctx.emit({"type": "cancelled"})
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                log.exception("credenciais da Anthropic recusadas")
                self._fail(ctx, "agent_unavailable", "O agente está indisponível de momento. Tenta mais tarde.")
            except anthropic.RateLimitError:
                self._fail(ctx, "rate_limited", "Estou com muitos pedidos agora. Tenta de novo daqui a um minuto.")
            except anthropic.APIConnectionError:
                self._fail(ctx, "connection", "Perdi a ligação ao modelo. Tenta de novo.")
            except anthropic.APIStatusError as e:
                log.error("erro da API do modelo (%s): %s", e.status_code, e.message)
                self._fail(ctx, "model_error", "O modelo respondeu com um erro. Tenta de novo.")
            except Exception:
                log.exception("turno do agente falhou (%s)", project_id)
                self._fail(ctx, "internal", "Algo correu mal do meu lado. Tenta de novo.")
            finally:
                self._finish(project_id)

    def _fail(self, ctx, code: str, msg: str) -> None:
        if ctx is not None:
            ctx.emit({"type": "error", "code": code, "message": msg})

    def _finish(self, project_id: str) -> None:
        with self._lock:
            self._running.discard(project_id)
        self._live.pop(project_id, None)
        try:
            if self.store.get_thread(project_id):
                self.store.update_thread(project_id, {"status": "idle"})
        except Exception:
            log.exception("não consegui fechar o turno %s", project_id)

    def cancel(self, project_id: str) -> None:
        ev = self._cancel.get(project_id)
        if ev is not None:
            ev.set()

    def recover(self) -> int:
        """Ao arrancar: turnos que estavam a correr quando o processo morreu continuam."""
        n = 0
        for t in self.store.list_running_threads():
            if t["project_id"] in self._running:
                continue
            with self._lock:
                self._running.add(t["project_id"])
            self._start(t["user_id"], t["project_id"])
            n += 1
        return n

    # ---------------------------------------------------------------- para a app
    def view(self, thread: dict, after: int = 0) -> dict:
        pid = thread["project_id"]
        events = [e for e in thread.get("events") or [] if e.get("seq", 0) > after][:MAX_EVENTS_RETURNED]
        out_events = [self._event_view(e) for e in events]
        versions = {}
        for v in (thread.get("state") or {}).get("versions") or []:
            if v.get("presented") and v.get("output_path"):
                versions[v["id"]] = self._version_view(pid, v)
        running = pid in self._running or thread["status"] == "running"
        live = self._live.get(pid) or {}
        return {"status": "running" if running else "idle", "events": out_events, "versions": versions,
                "live": {k: live.get(k) for k in ("text", "status", "tool")} if running else None, "last_seq": (thread.get("events") or [{}])[-1].get("seq", 0)}

    def _signed(self, bucket: str, path: str | None, expires: int = 3 * 3600) -> str | None:
        if not path:
            return None
        try:
            return self.storage.signed_url(bucket, path, expires)
        except Exception:
            log.exception("signed_url falhou: %s/%s", bucket, path)
            return None

    def _event_view(self, ev: dict) -> dict:
        if ev.get("images"):
            ev = {**ev, "images": [{"url": self._signed("outputs", i["path"]), "label": i.get("label")} for i in ev["images"]]}
        return ev

    def _version_view(self, project_id: str, v: dict) -> dict:
        return {"id": v["id"], "url": self._signed("outputs", v.get("output_path")), "thumbnail_url": self._signed("thumbs", v.get("thumbnail_path")),
                "duration_s": v.get("duration_s"), "resolution": v.get("resolution"), "quality": v.get("quality"), "variant": v.get("variant"),
                "note": v.get("note"), "download": f"/api/agent/threads/{project_id}/versions/{v['id']}/download"}

    def delete_remote_files(self, thread: dict | None) -> None:
        for fid in ((thread or {}).get("state") or {}).get("files_api") or []:
            try:
                agent_loop.get_client().files.delete(fid)
            except Exception:
                log.info("não consegui apagar %s da Files API", fid)


def new_id() -> str:
    return uuid.uuid4().hex
