"""Rotas do agente (/api/agent/*). Mesmas regras do resto da API: tudo verifica o dono do projeto
e os erros saem como {"error": {code, message}}."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from studio import config
from studio.api import _limit, _own_project, jobs, project_view
from studio.auth import User, require_user
from studio.errors import ApiError
from studio.storage import get_storage
from studio.store import get_store

log = logging.getLogger("awls.agent.api")
router = APIRouter(prefix="/api/agent")
MAX_MESSAGE_CHARS = 8000

_service = None


def service():
    global _service
    if _service is None:
        from studio.agent.service import AgentService
        _service = AgentService(get_store(), get_storage(), jobs)
    return _service


def reset_service() -> None:
    """Só para testes."""
    global _service
    _service = None


def _thread_row(p: dict, t: dict | None) -> dict:
    return {"project_id": p["id"], "name": p["name"], "status": (t or {}).get("status", "idle"), "updated_at": (t or p)["updated_at"],
            "thumbnail_url": project_view(p)["thumbnail_url"], "has_output": bool(p.get("output_path"))}


@router.get("/config")
def agent_config(user: User = Depends(require_user)):
    return {"enabled": config.AGENT_ENABLED, "model": config.AGENT_MODEL if config.AGENT_ENABLED else None,
            "memory": (get_store().get_profile(user.id) or {}).get("agent_memory") or []}


@router.delete("/memory")
async def forget_memory(request: Request, user: User = Depends(require_user)):
    text = str((await request.json()).get("text") or "")
    mem = [m for m in (get_store().get_profile(user.id) or {}).get("agent_memory") or [] if m != text]
    get_store().update_profile(user.id, {"agent_memory": mem})
    return {"memory": mem}


@router.get("/threads")
def list_threads(user: User = Depends(require_user)):
    store = get_store()
    out = []
    for t in store.list_threads(user.id, 100):
        p = store.get_project(t["project_id"])
        if p:
            out.append(_thread_row(p, t))
    return {"threads": out}


@router.post("/threads")
async def create_thread(request: Request, user: User = Depends(require_user)):
    _limit(f"proj:{user.id}", 60, 3600)
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    name = str(body.get("name") or "").strip()[:60] or time.strftime("Edit %b %d")
    store = get_store()
    p = store.create_project(user.id, name)
    store.update_project(p["id"], {"settings": {"agent": True}})
    store.track("project_created", user.id, None, {"via": "agent"})
    service().get_or_create(user.id, p["id"])
    return {"project": project_view(store.get_project(p["id"]), full=True)}


@router.get("/threads/{project_id}")
def get_thread(project_id: str, after: int = 0, light: int = 0, user: User = Depends(require_user)):
    svc = service()
    if light:  # a app a acompanhar um turno: nada de ir à base de dados várias vezes por segundo
        v = svc.fast_view(project_id, user.id, after)
        if v is not None:
            return {"thread": v}
    p = _own_project(project_id, user)
    t = svc.get_or_create(user.id, p["id"])
    return {"thread": svc.view(t, after), "project": project_view(p, full=True), "credits": get_store().balance(user.id)}


@router.post("/threads/{project_id}/messages")
async def post_message(project_id: str, request: Request, user: User = Depends(require_user)):
    if not config.AGENT_ENABLED:
        raise ApiError(503, "agent_disabled", "The AI agent isn't switched on yet on this server.")
    p = _own_project(project_id, user)
    text = str((await request.json()).get("text") or "").strip()
    if not text:
        raise ApiError(400, "empty_message", "Tell me what you want to do.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ApiError(400, "message_too_long", f"That message is too long (maximum {MAX_MESSAGE_CHARS} characters).")
    _limit(f"agent:{user.id}", config.RATE_AGENT_TURNS_PER_HOUR, 3600)
    store = get_store()
    if store.balance(user.id) <= 0:  # quem ainda não comprou também pode experimentar, com um limite (cada turno custa dinheiro)
        _limit(f"agentfree:{user.id}", config.AGENT_FREE_TURNS_PER_DAY, 86400)
    svc = service()
    t = svc.post_message(user.id, p, text)
    store.track("agent_message", user.id, None, {})
    return {"thread": svc.view(t, int(request.query_params.get("after") or 0)), "credits": store.balance(user.id)}


@router.post("/threads/{project_id}/cancel")
def cancel(project_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    service().cancel(p["id"])
    return {"ok": True}


@router.get("/threads/{project_id}/versions/{version_id}/download")
def download_version(project_id: str, version_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    t = get_store().get_thread(p["id"]) or {}
    v = next((x for x in (t.get("state") or {}).get("versions") or [] if x["id"] == version_id), None)
    if not v or not v.get("output_path") or not v.get("presented"):
        raise ApiError(404, "not_found", "This version isn't available anymore.")
    suffix = "-sem-musica" if v.get("variant") == "no_music" else ""
    url = get_storage().signed_url("outputs", v["output_path"], 600, f"{p['name'] or 'awlsvelocity'}{suffix}.mp4")
    get_store().track("download", user.id, None, {"via": "agent"})
    return RedirectResponse(url, status_code=302)
