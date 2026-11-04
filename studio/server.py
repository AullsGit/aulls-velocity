"""AwlsVelocity — servidor web (API + páginas). Orquestra; nunca faz processamento pesado se
COMPUTE_PROVIDER=runpod (aí o vídeo é gerado no worker GPU e esta API só cria/acompanha jobs).

Arrancar localmente:  python run_studio.py   (sem Supabase: dados em memória, ficheiros em disco)
Produção (Render):    uvicorn studio.server:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import html
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from studio import config  # noqa: E402
from studio.agent_api import router as agent_router, service as agent_service  # noqa: E402
from studio.api import jobs, router  # noqa: E402
from studio.errors import ApiError  # noqa: E402
from studio.store import get_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("awls")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
START = time.time()

@asynccontextmanager
async def _lifespan(app_: FastAPI):
    _startup()
    yield


app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.state.maintenance = None


# ----------------------------------------------------------------------- erros
def _err(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


@app.exception_handler(ApiError)
async def _api_error(request: Request, e: ApiError):
    return _err(e.status, e.code, e.message, **e.extra)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, e: RequestValidationError):
    return _err(400, "bad_request", "Something in this request isn't valid.")


@app.exception_handler(Exception)
async def _unhandled(request: Request, e: Exception):
    log.exception("erro não tratado em %s %s", request.method, request.url.path)
    return _err(500, "internal", "Something went wrong. Please try again.")


# ------------------------------------------------------------------ middleware
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_WEBHOOKS = ("/api/payments/paypal/webhook", "/api/compute/runpod/webhook")
_CSP = ("default-src 'self'; "
        "script-src 'self' https://www.paypal.com https://*.paypal.com https://*.paypalobjects.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
        "connect-src 'self' https://*.supabase.co https://*.paypal.com; "
        "frame-src https://*.paypal.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def _guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        # CSRF: um pedido que altera estado tem de vir da nossa própria origem (além de SameSite=Lax)
        if request.method in _UNSAFE and not path.startswith(_WEBHOOKS):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return _err(403, "forbidden", "Cross-site request blocked.")
        if app.state.maintenance and path not in ("/api/me",) and not path.startswith(_WEBHOOKS):
            return _err(503, "maintenance", "We're updating AwlsVelocity. Please try again in a few minutes.")
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", _CSP)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith("/static/"):
        # revalida sempre (304 barato via ETag): um deploy novo chega logo a toda a gente
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(router)
app.include_router(agent_router)


# ----------------------------------------------------------------- arranque
def _startup():
    if config.USE_SUPABASE:
        try:
            v = get_store().schema_version()
        except Exception:
            v = 0
        if v < 4:
            app.state.maintenance = "schema"
            log.critical("Base de dados desatualizada (schema_version=%s). Corre supabase-schema.sql no "
                         "Supabase (SQL Editor) — até lá a API responde 'maintenance'.", v)
            return
    if not config.USE_SUPABASE:
        log.warning("Sem Supabase: dados em MEMÓRIA (perdem-se ao reiniciar) e ficheiros em disco. Só para desenvolvimento.")
    if config.COMPUTE_PROVIDER == "local":
        log.warning("COMPUTE_PROVIDER=local: os vídeos são gerados na CPU deste servidor.")
        try:
            import velocity.pipeline  # noqa: F401
        except ImportError as e:
            app.state.maintenance = "engine_missing"
            log.critical("COMPUTE_PROVIDER=local mas o motor de vídeo não está instalado nesta imagem (%s). "
                         "Usa o Dockerfile completo, ou configura o RunPod.", e)
            return
    try:
        jobs().start_monitor()
    except Exception as e:  # ex.: RUNPOD_API_KEY em falta — falha clara, sem derrubar o site
        app.state.maintenance = "compute_config"
        log.critical("Fornecedor de computação mal configurado: %s", e)
        return
    if config.AGENT_ENABLED:
        try:
            n = agent_service().recover()
            if n:
                log.warning("%d turno(s) do agente retomado(s) depois do reinício", n)
        except Exception:
            log.exception("retoma dos turnos do agente falhou")
    else:
        log.warning("Agente desligado: falta ANTHROPIC_API_KEY.")


@app.get("/health")
def health():
    return {"status": "ok" if not app.state.maintenance else "maintenance",
            "uptime_s": round(time.time() - START, 1), "provider": config.COMPUTE_PROVIDER}


# -------------------------------------------------------------------- páginas
def _page(name: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC, name), media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
def landing():
    return _page("landing.html")


@app.get("/s/{token}")
def share_page(token: str, request: Request):
    """Página pública de um vídeo partilhado, com Open Graph para pré-visualização no WhatsApp/redes."""
    from studio.api import shared
    try:
        v = shared(token)
    except ApiError:
        return HTMLResponse(_not_found(), status_code=404)
    title = html.escape(f"{v['name']} — made with AwlsVelocity")
    base = config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    video, thumb = html.escape(v["video_url"] or ""), html.escape(v["thumbnail_url"] or "")
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title><meta name="theme-color" content="#0b0b0f">
<meta property="og:title" content="{title}"><meta property="og:type" content="video.other">
<meta property="og:image" content="{thumb}"><meta property="og:video" content="{video}">
<meta property="og:url" content="{html.escape(base)}/s/{html.escape(token)}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/static/brand/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css"></head>
<body class="share-page"><main class="share-wrap">
<a class="brand-mark" href="/"><img src="/static/brand/logo.svg" alt="" width="28" height="28"><span>AwlsVelocity</span></a>
<div class="phone-frame"><video src="{video}" poster="{thumb}" controls playsinline autoplay muted loop></video></div>
<h1 class="share-title">{html.escape(v['name'])}</h1>
<p class="muted">Edited by AI from a reference video.</p>
<a class="btn btn-primary btn-lg" href="/signup">Make your own edit</a>
</main></body></html>""")


def _not_found() -> str:
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width,initial-scale=1'><title>Not found — AwlsVelocity</title>"
            "<link rel='stylesheet' href='/static/app.css'></head><body class='share-page'><main class='share-wrap'>"
            "<h1 class='share-title'>This page doesn't exist</h1><a class='btn btn-primary' href='/'>Go to AwlsVelocity</a>"
            "</main></body></html>")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # o service worker tem de ser servido da raiz para controlar todas as páginas
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")

# SPA: tem de ficar no FIM (o Starlette avalia as rotas por ordem; antes disto apanharia
# /static, /s/..., /manifest...).
_APP_ROUTES = ("home", "agent", "create", "projects", "edit", "profile", "credits", "admin", "login", "signup", "welcome",
               "auth/callback")


@app.get("/{section}")
@app.get("/{section}/{rest:path}")
def app_shell(section: str, rest: str = ""):
    if section in _APP_ROUTES or f"{section}/{rest}" in _APP_ROUTES:
        return _page("app.html")
    if section in ("conta", "explorar"):  # rotas da versão anterior
        return RedirectResponse("/profile" if section == "conta" else "/home")
    return HTMLResponse(_not_found(), status_code=404)
