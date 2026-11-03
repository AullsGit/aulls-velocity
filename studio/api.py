"""Rotas da API (/api/*). Todas devolvem JSON; erros sempre como {"error": {code, message}}.

Autorização: cada rota que toca num projeto/job verifica que pertence ao utilizador da sessão
(`_own_project`, `_own_job`) — nunca confia num user_id vindo do browser.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from studio import config
from studio.auth import (AuthFailed, User, clear_session, current_user, get_auth, is_owner, require_owner,
                         require_user, set_session)
from studio.compute import get_provider
from studio.errors import ApiError
from studio.jobs import JobService
from studio.payments import get_provider as get_payment_provider
from studio.storage import get_storage, verify_token
from studio.store import JOB_ACTIVE, get_store

log = logging.getLogger("awls.api")
router = APIRouter(prefix="/api")

_jobs: JobService | None = None


def jobs() -> JobService:
    global _jobs
    if _jobs is None:
        _jobs = JobService(get_store(), get_storage(), get_provider())
    return _jobs


def reset_services() -> None:
    """Só para testes."""
    global _jobs
    _jobs = None


# ------------------------------------------------------------------ rate limit
_hits: dict[str, deque] = defaultdict(deque)


def _limit(key: str, n: int, window_s: int) -> None:
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= n:
        raise ApiError(429, "rate_limited", "Too many requests. Please wait a moment and try again.")
    q.append(now)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


# ------------------------------------------------------------------- helpers
def _own_project(project_id: str, user: User) -> dict:
    try:
        uuid.UUID(project_id)
    except ValueError:
        raise ApiError(404, "not_found", "Project not found.")
    p = get_store().get_project(project_id)
    if not p or p["user_id"] != user.id:
        raise ApiError(404, "not_found", "Project not found.")  # 404 (não 403): não revela que existe
    return p


def _own_job(job_id: str, user: User) -> dict:
    j = get_store().get_job(job_id)
    if not j or j["user_id"] != user.id:
        raise ApiError(404, "not_found", "Job not found.")
    return j


def _signed(bucket: str, path: str | None, expires: int = 3600, download: str | None = None) -> str | None:
    if not path:
        return None
    try:
        return get_storage().signed_url(bucket, path, expires, download)
    except Exception:
        log.exception("signed_url falhou: %s/%s", bucket, path)
        return None


def job_view(j: dict | None) -> dict | None:
    if not j:
        return None
    return {k: j.get(k) for k in ("id", "project_id", "kind", "status", "stage", "progress", "created_at",
                                  "started_at", "completed_at", "failed_at", "error_message", "credits_used",
                                  "refunded", "video_duration_s", "resolution", "processing_time_s", "queue_time_s")}


def project_view(p: dict, full: bool = False) -> dict:
    files = [{k: f.get(k) for k in ("id", "kind", "name", "size", "duration_s", "width", "height", "uploaded")}
             for f in p.get("files") or []]
    out = {
        "id": p["id"], "name": p["name"], "status": p["status"], "created_at": p["created_at"],
        "updated_at": p["updated_at"], "duration_s": p.get("duration_s"), "resolution": p.get("resolution"),
        "thumbnail_url": _signed("thumbs", p.get("thumbnail_path")), "is_public": p.get("is_public", False),
        "files": files, "reference_summary": p.get("reference_summary"),
        "has_output": bool(p.get("output_path")), "sources_purged": p.get("sources_purged", False),
        "share_url": f"/s/{p['share_token']}" if p.get("share_token") else None,
        "agent": bool((p.get("settings") or {}).get("agent")),
    }
    ref = next((f for f in p.get("files") or [] if f["kind"] == "reference"), None)
    out["reference_name"] = ref["name"] if ref else None
    if full:
        out["video_url"] = _signed("outputs", p.get("output_path"), 3 * 3600)
        out["file_urls"] = {f["id"]: _signed("uploads", f["path"], 3 * 3600)
                            for f in p.get("files") or [] if f.get("uploaded") and not p.get("sources_purged")}
        out["settings"] = {k: v for k, v in (p.get("settings") or {}).items()
                           if k in ("profile", "analysis_error", "analysis_job")}
        out["can_edit"] = bool(p.get("timeline")) and not p.get("sources_purged")
        out["has_ai_version"] = bool((p.get("settings") or {}).get("ai_timeline"))
        last = get_store().get_job(p["last_job_id"]) if p.get("last_job_id") else None
        out["last_job"] = job_view(last)
        active = get_store().list_jobs(project_id=p["id"], statuses=JOB_ACTIVE, limit=1)
        out["active_job"] = job_view(active[0]) if active else None
    return out


def _pricing() -> dict:
    cfg = get_store().get_config()
    return {"credits_per_generation": cfg.get("credits_per_generation"),
            "credits_per_edit_export": cfg.get("credits_per_edit_export", 0),
            "credit_usd_value": cfg.get("credit_usd_value", 0.5)}


def _limits() -> dict:
    return {"max_upload_mb": config.MAX_UPLOAD_MB, "max_files": config.MAX_SOURCE_FILES,
            "max_reference_s": config.MAX_REFERENCE_S, "max_source_s": config.MAX_SOURCE_S,
            "max_sources_total_s": config.MAX_SOURCES_TOTAL_S,
            "video_ext": sorted(config.VIDEO_EXT), "image_ext": sorted(config.IMAGE_EXT), "audio_ext": sorted(config.AUDIO_EXT)}


# ===================================================================== auth
@router.get("/auth/providers")
def auth_providers(request: Request):
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    url = get_auth().oauth_url("google", f"{base}/auth/callback")
    return {"google": bool(url), "google_url": url}


def _creds(body: dict) -> tuple[str, str]:
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    if "@" not in email or len(email) > 254:
        raise ApiError(400, "invalid_email", "Enter a valid email address.")
    if len(pw) < 8:
        raise ApiError(400, "weak_password", "Use at least 8 characters for your password.")
    return email, pw


@router.post("/auth/signup")
async def signup(request: Request, response: Response):
    _limit(f"auth:{_ip(request)}", config.RATE_AUTH_PER_10MIN, 600)
    email, pw = _creds(await request.json())
    try:
        s = get_auth().signup(email, pw)
    except AuthFailed as e:
        raise ApiError(400, e.code, e.message)
    if s is None:
        return {"ok": True, "confirm_email": True}
    get_store().ensure_profile(s.user.id, s.user.email)
    if config.DEV_SIGNUP_CREDITS and not config.USE_SUPABASE:
        get_store().grant_credits(s.user.id, config.DEV_SIGNUP_CREDITS, "admin", "dev", f"dev_signup:{s.user.id}")
    get_store().track("signup", s.user.id, None, {})
    set_session(request, response, s)
    return {"ok": True}


@router.post("/auth/login")
async def login(request: Request, response: Response):
    _limit(f"auth:{_ip(request)}", config.RATE_AUTH_PER_10MIN, 600)
    body = await request.json()
    email, pw = (body.get("email") or "").strip().lower(), body.get("password") or ""
    try:
        s = get_auth().login(email, pw)
    except AuthFailed as e:
        raise ApiError(401, e.code, e.message)
    get_store().ensure_profile(s.user.id, s.user.email)
    set_session(request, response, s)
    return {"ok": True}


@router.post("/auth/session")
async def oauth_session(request: Request, response: Response):
    """Volta do login Google: valida os tokens no fornecedor de auth antes de criar a sessão."""
    _limit(f"auth:{_ip(request)}", config.RATE_AUTH_PER_10MIN, 600)
    body = await request.json()
    from studio.auth import Session
    access, refresh = body.get("access_token") or "", body.get("refresh_token") or ""
    user = get_auth().get_user(access) if access else None
    if not user or not refresh:
        raise ApiError(401, "invalid_session", "Sign-in failed. Please try again.")
    store = get_store()
    is_new = store.get_profile(user.id) is None
    store.ensure_profile(user.id, user.email)
    if is_new:
        store.track("signup", user.id, None, {"method": "google"})
    set_session(request, response, Session(user, access, refresh))
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/me")
def me(user: User | None = Depends(current_user)):
    store = get_store()
    pp = get_payment_provider("paypal")
    base = {"pricing": _pricing(), "limits": _limits(), "google_auth": config.GOOGLE_AUTH_ENABLED}
    if user is None:
        return {"logged_in": False, **base}
    prof = store.ensure_profile(user.id, user.email)
    return {
        **base, "logged_in": True, "id": user.id, "email": user.email,
        "display_name": prof.get("display_name") or (user.email or "").split("@")[0],
        "credits": int(prof.get("paid_credits") or 0), "plan": prof.get("plan", "pay_per_use"),
        "onboarded": bool(prof.get("onboarded")), "is_admin": is_owner(user),
        "payments": {"paypal": pp.public_config() if pp else None, "emola_number": config.EMOLA_NUMBER,
                     "packs": store.list_packs()},
    }


@router.patch("/me")
async def update_me(request: Request, user: User = Depends(require_user)):
    body = await request.json()
    fields = {}
    if "display_name" in body:
        fields["display_name"] = str(body["display_name"] or "").strip()[:40]
    if "onboarded" in body:
        fields["onboarded"] = bool(body["onboarded"])
    get_store().update_profile(user.id, fields)
    return {"ok": True}


# ================================================================== projetos
@router.get("/projects")
def list_projects(user: User = Depends(require_user)):
    return {"projects": [project_view(p) for p in get_store().list_projects(user.id)]}


@router.post("/projects")
async def create_project(request: Request, user: User = Depends(require_user)):
    _limit(f"proj:{user.id}", 60, 3600)
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    name = str(body.get("name") or "").strip()[:60] or time.strftime("Edit %b %d")
    p = get_store().create_project(user.id, name)
    get_store().track("project_created", user.id, None, {})
    return {"project": project_view(p, full=True)}


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: User = Depends(require_user)):
    return {"project": project_view(_own_project(project_id, user), full=True)}


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: Request, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    body = await request.json()
    fields = {}
    if "name" in body:
        fields["name"] = str(body["name"] or "").strip()[:60] or p["name"]
    if "is_public" in body:
        if body["is_public"] and p["status"] != "completed":
            raise ApiError(400, "not_ready", "Only finished videos can be published.")
        fields["is_public"] = bool(body["is_public"])
    p = get_store().update_project(p["id"], fields)
    return {"project": project_view(p, full=True)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    for j in get_store().list_jobs(project_id=p["id"], statuses=JOB_ACTIVE, limit=5):
        jobs().cancel(user.id, j["id"])
    from studio.agent_api import service as agent_service
    agent_service().cancel(p["id"])
    agent_service().delete_remote_files(get_store().get_thread(p["id"]))
    st = get_storage()
    prefix = f"{user.id}/{p['id']}"
    for bucket in ("uploads", "outputs", "thumbs"):
        try:
            st.remove_prefix(bucket, prefix)
        except Exception:
            log.exception("remover ficheiros do projeto falhou (%s)", bucket)
    get_store().delete_project(p["id"])
    return {"ok": True}


@router.post("/projects/{project_id}/duplicate")
def duplicate_project(project_id: str, user: User = Depends(require_user)):
    """Cria um rascunho novo com a mesma referência e os mesmos vídeos (copiados), para gerar
    outra versão sem voltar a fazer upload."""
    p = _own_project(project_id, user)
    if p.get("sources_purged"):
        raise ApiError(410, "sources_expired", "The original clips for this project have expired.")
    st, store = get_storage(), get_store()
    new = store.create_project(user.id, f"{p['name']} (copy)"[:60])
    files = []
    for f in p.get("files") or []:
        if not f.get("uploaded"):
            continue
        new_path = f"{user.id}/{new['id']}/{f['kind']}/{uuid.uuid4().hex}{os.path.splitext(f['path'])[1]}"
        st.copy("uploads", f["path"], new_path)
        files.append({**f, "id": uuid.uuid4().hex, "path": new_path})
    new = store.update_project(new["id"], {"files": files, "reference_summary": p.get("reference_summary"),
                                           "reference_dna": p.get("reference_dna")})
    return {"project": project_view(new, full=True)}


# ---------------------------------------------------------------- ficheiros
@router.post("/projects/{project_id}/files")
async def add_file(project_id: str, request: Request, user: User = Depends(require_user)):
    """Valida o ficheiro e devolve uma URL de upload DIRETO para o Storage (o vídeo não passa
    pelo servidor da API)."""
    _limit(f"upload:{user.id}", 200, 3600)
    p = _own_project(project_id, user)
    if p["status"] == "processing":
        raise ApiError(409, "busy_project", "Wait for the current video to finish.")
    body = await request.json()
    kind = body.get("kind")
    name = os.path.basename(str(body.get("name") or "file"))[:120]
    ext = os.path.splitext(name)[1].lower()
    size = int(body.get("size") or 0)
    duration = float(body.get("duration_s") or 0.0)
    allowed = {"reference": config.VIDEO_EXT, "source": config.VIDEO_EXT | config.IMAGE_EXT, "music": config.AUDIO_EXT}
    if kind not in allowed:
        raise ApiError(400, "bad_kind", "Unknown file type.")
    if ext not in allowed[kind]:
        raise ApiError(400, "bad_format", f"{name}: this format isn't supported.")
    if size <= 0 or size > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ApiError(400, "too_large", f"{name}: files must be under {config.MAX_UPLOAD_MB} MB.")
    if kind == "reference" and duration > config.MAX_REFERENCE_S:
        raise ApiError(400, "too_long", f"The reference must be under {config.MAX_REFERENCE_S}s.")
    if kind == "source" and ext in config.VIDEO_EXT and duration > config.MAX_SOURCE_S:
        raise ApiError(400, "too_long", f"{name}: each video must be under {config.MAX_SOURCE_S // 60} min.")
    files = list(p.get("files") or [])
    if kind == "source":
        srcs = [f for f in files if f["kind"] == "source"]
        if len(srcs) >= config.MAX_SOURCE_FILES:
            raise ApiError(400, "too_many_files", f"Use up to {config.MAX_SOURCE_FILES} files.")
        total = sum(float(f.get("duration_s") or 0) for f in srcs) + duration
        if total > config.MAX_SOURCES_TOTAL_S:
            raise ApiError(400, "too_long", f"Your videos must add up to under {config.MAX_SOURCES_TOTAL_S // 60} min.")
    else:  # referência / música: só uma — substitui a anterior
        old = [f for f in files if f["kind"] == kind]
        if old:
            get_storage().remove("uploads", [f["path"] for f in old])
            files = [f for f in files if f["kind"] != kind]
    fid = uuid.uuid4().hex
    path = f"{user.id}/{p['id']}/{kind}/{fid}{ext}"
    files.append({"id": fid, "kind": kind, "name": name, "path": path, "size": size, "duration_s": duration or None,
                  "width": body.get("width"), "height": body.get("height"), "uploaded": False})
    fields = {"files": files}
    if kind == "reference":
        fields.update(reference_summary=None, reference_dna=None)
    get_store().update_project(p["id"], fields)
    up = get_storage().create_upload_url("uploads", path)
    return {"file": {"id": fid, "kind": kind, "name": name}, "upload": up}


@router.post("/projects/{project_id}/files/{file_id}/complete")
def complete_file(project_id: str, file_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    files = list(p.get("files") or [])
    f = next((x for x in files if x["id"] == file_id), None)
    if not f:
        raise ApiError(404, "not_found", "File not found.")
    size = get_storage().size("uploads", f["path"])
    if not size:
        raise ApiError(400, "upload_missing", "The upload didn't finish. Please try again.")
    f["uploaded"], f["size"] = True, size
    get_store().update_project(p["id"], {"files": files})
    get_store().track("upload_completed", user.id, None, {"kind": f["kind"]})
    job = None
    if f["kind"] == "reference":  # análise grátis do estilo, logo a seguir ao upload
        _limit(f"analyze:{user.id}", config.RATE_ANALYSES_PER_HOUR, 3600)
        job = jobs().create_analysis(user.id, get_store().get_project(p["id"]))
    return {"project": project_view(get_store().get_project(p["id"]), full=True), "job": job_view(job)}


@router.delete("/projects/{project_id}/files/{file_id}")
def delete_file(project_id: str, file_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    if p["status"] == "processing":
        raise ApiError(409, "busy_project", "Wait for the current video to finish.")
    files = list(p.get("files") or [])
    f = next((x for x in files if x["id"] == file_id), None)
    if not f:
        raise ApiError(404, "not_found", "File not found.")
    get_storage().remove("uploads", [f["path"]])
    fields = {"files": [x for x in files if x["id"] != file_id]}
    if f["kind"] == "reference":
        fields.update(reference_summary=None, reference_dna=None)
    p = get_store().update_project(p["id"], fields)
    return {"project": project_view(p, full=True)}


# ------------------------------------------------------------------- geração
@router.post("/projects/{project_id}/analyze")
def analyze(project_id: str, user: User = Depends(require_user)):
    _limit(f"analyze:{user.id}", config.RATE_ANALYSES_PER_HOUR, 3600)
    job = jobs().create_analysis(user.id, _own_project(project_id, user))
    return {"job": job_view(job)}


@router.post("/projects/{project_id}/generate")
async def generate(project_id: str, request: Request, user: User = Depends(require_user)):
    _limit(f"gen:{user.id}", config.RATE_GENERATIONS_PER_HOUR, 3600)
    p = _own_project(project_id, user)
    body = await request.json()
    text = body.get("text") or []
    if isinstance(text, str):
        text = [t for t in text.splitlines()]
    job = jobs().create_generation(user.id, p, profile=body.get("profile", "standard"), force=bool(body.get("force")),
                                   text=[str(t)[:60] for t in text][:5], idem_key=request.headers.get("idempotency-key"))
    return {"job": job_view(job), "credits": get_store().balance(user.id)}


@router.get("/projects/{project_id}/editor")
def editor_state(project_id: str, user: User = Depends(require_user)):
    from velocity.edit.timeline_ops import doc_from_timeline
    from velocity.plan import load_plan, plan_to_timeline
    p = _own_project(project_id, user)
    if not p.get("timeline"):
        raise ApiError(400, "nothing_to_edit", "Generate a video before editing it.")
    tl = plan_to_timeline(load_plan(p["timeline"]))
    key_to_file = {f"uploads/{f['path']}": f["id"] for f in p.get("files") or []}
    doc = doc_from_timeline(tl)
    shots = [{"file": key_to_file.get(s.clip.source_path), "src_start_s": round(s.clip.src_start_s, 3),
              "transition": s.transition_in.type if s.transition_in else None} for s in tl.shots]
    ai = (p.get("settings") or {}).get("ai_timeline")
    ai_doc = None
    if ai:
        ai_tl = plan_to_timeline(load_plan(ai))
        ai_doc = {"doc": doc_from_timeline(ai_tl).model_dump(),
                  "shots": [{"file": key_to_file.get(s.clip.source_path), "src_start_s": round(s.clip.src_start_s, 3),
                             "transition": s.transition_in.type if s.transition_in else None} for s in ai_tl.shots]}
    return {"doc": doc.model_dump(), "shots": shots, "fps": tl.fps, "ai": ai_doc,
            "music_file": key_to_file.get(tl.music_path) if tl.music_path else None,
            "project": project_view(p, full=True)}


@router.post("/projects/{project_id}/edit")
async def export_edit(project_id: str, request: Request, user: User = Depends(require_user)):
    _limit(f"gen:{user.id}", config.RATE_GENERATIONS_PER_HOUR * 2, 3600)
    p = _own_project(project_id, user)
    body = await request.json()
    job = jobs().create_render_edit(user.id, p, body.get("doc") or {}, idem_key=request.headers.get("idempotency-key"))
    return {"job": job_view(job), "credits": get_store().balance(user.id)}


@router.post("/projects/{project_id}/share")
def share(project_id: str, request: Request, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    if not p.get("output_path"):
        raise ApiError(400, "not_ready", "Create the video first.")
    if not p.get("share_token"):
        p = get_store().update_project(p["id"], {"share_token": secrets.token_urlsafe(9)})
    get_store().track("share_link", user.id, None, {})
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return {"url": f"{base}/s/{p['share_token']}"}


@router.get("/projects/{project_id}/download")
def download(project_id: str, user: User = Depends(require_user)):
    p = _own_project(project_id, user)
    url = _signed("outputs", p.get("output_path"), 600, download=f"{p['name'] or 'awlsvelocity'}.mp4")
    if not url:
        raise ApiError(404, "not_ready", "This video isn't ready yet.")
    get_store().track("download", user.id, None, {})
    return RedirectResponse(url, status_code=302)


# ======================================================================= jobs
@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(require_user)):
    j = _own_job(job_id, user)
    return {"job": job_view(j), "credits": get_store().balance(user.id)}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: User = Depends(require_user)):
    _own_job(job_id, user)
    return {"job": job_view(jobs().cancel(user.id, job_id)), "credits": get_store().balance(user.id)}


@router.post("/compute/runpod/webhook")
async def runpod_webhook(request: Request):
    """O RunPod avisa que um job mudou. Não confiamos no corpo: verificamos o token HMAC do URL
    e vamos buscar o estado oficial à API do RunPod."""
    from studio.compute.runpod import webhook_token
    job_id, tok = request.query_params.get("job", ""), request.query_params.get("t", "")
    if not job_id or not hmac.compare_digest(tok, webhook_token(job_id)):
        raise ApiError(403, "forbidden", "bad token")
    jobs().refresh(job_id)
    return {"ok": True}


# =================================================================== créditos
@router.get("/credits")
def credits(user: User = Depends(require_user)):
    store = get_store()
    return {"balance": store.balance(user.id), "transactions": store.list_transactions(user.id, 50),
            "packs": store.list_packs(), "pricing": _pricing(), "payments": store.list_payments(user.id)}


@router.post("/credits/redeem")
async def redeem(request: Request, user: User = Depends(require_user)):
    _limit(f"promo:{user.id}", 10, 3600)
    code = str((await request.json()).get("code") or "").strip()[:40]
    ok, res = get_store().redeem_promo(user.id, code)
    if not ok:
        msgs = {"invalid": "This code isn't valid.", "expired": "This code has expired.",
                "exhausted": "This code has been fully used.", "already_used": "You've already used this code."}
        raise ApiError(400, f"promo_{res}", msgs.get(res, "This code isn't valid."))
    get_store().track("promo_redeemed", user.id, None, {"code": code.upper()})
    return {"ok": True, "credits_added": res, "balance": get_store().balance(user.id)}


def _credit_capture(cap: dict, expected_user: str | None = None) -> bool:
    """Credita UMA captura PayPal verificada. Recusa se o valor pago não for o preço do pacote
    (ex.: uma order criada no browser com valor alterado) ou se o dono não bater certo."""
    user_id, _, pack_id = (cap.get("custom_id") or "").partition(":")
    pack = next((x for x in get_store().list_packs(active_only=False) if x["id"] == pack_id), None)
    if not user_id or not pack:
        log.warning("captura PayPal sem custom_id válido: %s", cap.get("id"))
        return False
    if expected_user and user_id != expected_user:
        log.warning("captura PayPal de outro utilizador: %s", cap.get("id"))
        return False
    if cap["amount_usd"] + 1e-6 < pack["price_cents"] / 100:
        log.error("captura PayPal %s pagou %.2f mas o pacote %s custa %.2f — NÃO creditado",
                  cap.get("id"), cap["amount_usd"], pack_id, pack["price_cents"] / 100)
        return False
    new = get_store().record_purchase(user_id, "paypal", cap["id"], cap["amount_usd"], pack_id, int(pack["credits"]))
    if new:
        get_store().track("purchase_completed", user_id, None, {"provider": "paypal", "usd": cap["amount_usd"],
                                                                "credits": pack["credits"]})
    return new


@router.post("/payments/paypal/create-order")
async def paypal_create(request: Request, user: User = Depends(require_user)):
    pp = get_payment_provider("paypal")
    if not pp:
        raise ApiError(400, "payments_unavailable", "Card payments aren't available right now.")
    _limit(f"pay:{user.id}", 30, 3600)
    pack_id = (await request.json()).get("pack_id")
    pack = next((x for x in get_store().list_packs() if x["id"] == pack_id), None)
    if not pack:
        raise ApiError(400, "bad_pack", "Choose a credit pack.")
    try:
        return pp.create_checkout(user.id, pack)  # preço da tabela do servidor, nunca do browser
    except Exception:
        log.exception("PayPal create-order falhou")
        raise ApiError(502, "payment_error", "We couldn't start the payment. Please try again.")


@router.post("/payments/paypal/capture/{order_id}")
def paypal_capture(order_id: str, user: User = Depends(require_user)):
    pp = get_payment_provider("paypal")
    if not pp:
        raise ApiError(400, "payments_unavailable", "Card payments aren't available right now.")
    try:
        caps = pp.confirm(order_id)
    except Exception:
        log.exception("PayPal capture falhou")
        raise ApiError(402, "payment_failed", "Payment failed. No credits were added and you were not charged.")
    if not caps:
        raise ApiError(402, "payment_failed", "Payment failed. No credits were added and you were not charged.")
    for c in caps:
        _credit_capture(c, expected_user=user.id)
    return {"ok": True, "balance": get_store().balance(user.id)}


@router.post("/payments/paypal/webhook")
async def paypal_webhook(request: Request):
    pp = get_payment_provider("paypal")
    if not pp:
        return {"ok": True, "ignored": "disabled"}
    body = await request.json()
    try:
        valid = pp.verify_webhook(dict(request.headers), body)
    except Exception:
        log.exception("verificação do webhook PayPal falhou")
        raise ApiError(502, "verify_failed", "verification failed")
    if not valid:
        raise ApiError(400, "bad_signature", "invalid signature")
    for c in pp.webhook_captures(body):
        _credit_capture(c)
    return {"ok": True}


@router.post("/payments/emola/declare")
async def emola_declare(request: Request, user: User = Depends(require_user)):
    _limit(f"emola:{user.id}", 10, 3600)
    body = await request.json()
    try:
        amount = float(body.get("amount_mt"))
    except (TypeError, ValueError):
        raise ApiError(400, "bad_amount", "Enter the amount you paid.")
    if not 1 <= amount <= 100000:
        raise ApiError(400, "bad_amount", "Enter the amount you paid.")
    ref = str(body.get("reference") or "").strip()[:120] or None
    row = get_store().declare_manual_payment(user.id, amount, ref)
    return {"ok": True, "payment": row}


# ============================================================== analytics
_EVENT_NAMES = {"page_view", "landing_cta", "app_open", "install_prompt", "install_accepted", "share_clicked",
                "onboarding_done", "editor_opened"}


@router.post("/events")
async def event(request: Request, user: User | None = Depends(current_user)):
    _limit(f"ev:{_ip(request)}", 120, 3600)
    body = await request.json()
    name = body.get("name")
    if name not in _EVENT_NAMES:
        return {"ok": False}
    props = {k: str(v)[:80] for k, v in (body.get("props") or {}).items()}
    get_store().track(name, user.id if user else None, str(body.get("anon_id") or "")[:40] or None, props)
    return {"ok": True}


# ===================================================================== público
@router.get("/gallery")
def gallery():
    out = []
    for p in get_store().list_public_projects(24):
        out.append({"id": p["id"], "name": p["name"], "duration_s": p.get("duration_s"),
                    "thumbnail_url": _signed("thumbs", p.get("thumbnail_path")),
                    "video_url": _signed("outputs", p.get("output_path"), 3600)})
    return {"videos": out}


@router.get("/share/{token}")
def shared(token: str):
    p = get_store().find_project_by_share(token)
    if not p or not p.get("output_path"):
        raise ApiError(404, "not_found", "This video isn't available.")
    return {"name": p["name"], "duration_s": p.get("duration_s"),
            "video_url": _signed("outputs", p["output_path"], 3600),
            "thumbnail_url": _signed("thumbs", p.get("thumbnail_path"), 3600)}


# =============================================================== storage local
@router.put("/files/upload")
async def local_upload(t: str, file: UploadFile = File(...)):
    """Só existe com armazenamento local (desenvolvimento): imita o PUT multipart do Supabase."""
    st = get_storage()
    claims = verify_token(t)
    if not getattr(st, "is_local", False) or not claims or claims.get("op") != "put":
        raise ApiError(403, "forbidden", "Invalid upload link.")
    dest = st.local_path(claims["b"], claims["p"])
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    written, limit = 0, int(claims.get("max") or config.MAX_UPLOAD_MB * 1024 * 1024)
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            written += len(chunk)
            if written > limit:
                out.close()
                os.remove(dest)
                raise ApiError(413, "too_large", "File too large.")
            out.write(chunk)
    return {"Key": f"{claims['b']}/{claims['p']}"}


@router.get("/files/get")
def local_get(t: str):
    st = get_storage()
    claims = verify_token(t)
    if not getattr(st, "is_local", False) or not claims or claims.get("op") != "get":
        raise ApiError(403, "forbidden", "Invalid link.")
    path = st.local_path(claims["b"], claims["p"])
    if not os.path.isfile(path):
        raise ApiError(404, "not_found", "File not found.")
    return FileResponse(path, filename=claims.get("dl") or None)


# ======================================================================= admin
@router.get("/admin/stats")
def admin_stats(user: User = Depends(require_owner)):
    store = get_store()
    st = store.admin_stats()
    return {"stats": st, "provider": get_provider().name, "pricing": store.get_config(),
            "packs": store.list_packs(active_only=False)}


@router.get("/admin/users")
def admin_users(user: User = Depends(require_owner)):
    return {"users": get_store().admin_list_users(200)}


@router.get("/admin/jobs")
def admin_jobs(status: str | None = None, user: User = Depends(require_owner)):
    rows = get_store().list_jobs(statuses=[status] if status else None, limit=100)
    return {"jobs": [{**job_view(j), "user_id": j["user_id"], "provider": j.get("provider"),
                      "error_detail": j.get("error_detail"), "est_cost_usd": j.get("est_cost_usd"),
                      "gpu_time_s": j.get("gpu_time_s"), "worker_type": j.get("worker_type")} for j in rows]}


@router.get("/admin/payments/pending")
def admin_pending(user: User = Depends(require_owner)):
    return {"payments": get_store().list_pending_manual_payments()}


@router.post("/admin/payments/{payment_id}/confirm")
async def admin_confirm(payment_id: str, request: Request, user: User = Depends(require_owner)):
    credits_ = int((await request.json()).get("credits") or 0)
    if credits_ <= 0:
        raise ApiError(400, "bad_credits", "How many credits does this payment buy?")
    return {"ok": get_store().confirm_manual_payment(payment_id, credits_)}


@router.post("/admin/payments/{payment_id}/reject")
def admin_reject(payment_id: str, user: User = Depends(require_owner)):
    return {"ok": get_store().reject_manual_payment(payment_id)}


@router.post("/admin/credits/grant")
async def admin_grant(request: Request, user: User = Depends(require_owner)):
    body = await request.json()
    target, amount = str(body.get("user_id") or ""), int(body.get("credits") or 0)
    if not target or not amount:
        raise ApiError(400, "bad_request", "user_id and credits are required.")
    ok = get_store().grant_credits(target, amount, "admin", user.email, f"admin:{uuid.uuid4().hex}")
    return {"ok": ok, "balance": get_store().balance(target)}


@router.put("/admin/config/{key}")
async def admin_config(key: str, request: Request, user: User = Depends(require_owner)):
    if key not in config.DEFAULT_PRICING:
        raise ApiError(400, "bad_key", "Unknown setting.")
    get_store().set_config(key, (await request.json()).get("value"))
    return {"ok": True, "config": get_store().get_config()}


@router.put("/admin/packs/{pack_id}")
async def admin_pack(pack_id: str, request: Request, user: User = Depends(require_owner)):
    b = await request.json()
    pack = {"id": pack_id, "credits": int(b["credits"]), "price_cents": int(b["price_cents"]),
            "currency": "USD", "label": str(b.get("label") or f"{b['credits']} generations")[:40],
            "active": bool(b.get("active", True)), "sort": int(b.get("sort", 0))}
    if pack["credits"] <= 0 or pack["price_cents"] <= 0:
        raise ApiError(400, "bad_pack", "Credits and price must be positive.")
    get_store().upsert_pack(pack)
    return {"ok": True, "packs": get_store().list_packs(active_only=False)}


@router.post("/admin/promos")
async def admin_promo(request: Request, user: User = Depends(require_owner)):
    b = await request.json()
    code = str(b.get("code") or "").strip().upper()
    if not code or int(b.get("credits") or 0) <= 0:
        raise ApiError(400, "bad_promo", "Code and credits are required.")
    get_store().create_promo(code, int(b["credits"]), int(b.get("max_uses") or 1), b.get("expires_at"))
    return {"ok": True}
