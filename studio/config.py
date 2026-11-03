"""Configuração central (lida do ambiente). Nada de segredos com valor por defeito."""
from __future__ import annotations

import os
import secrets


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


APP_NAME = "AwlsVelocity"

# --- Backends -----------------------------------------------------------------------------
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY")
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

COMPUTE_PROVIDER = _env("COMPUTE_PROVIDER", "local").lower()  # local | runpod
RUNPOD_API_KEY = _env("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = _env("RUNPOD_ENDPOINT_ID")
RUNPOD_EXECUTION_TIMEOUT_S = _int("RUNPOD_EXECUTION_TIMEOUT_S", 900)

# URL pública da API (webhooks do RunPod/PayPal, links de partilha). Sem isto, o RunPod é
# acompanhado só por polling — funciona, mas o webhook acorda o serviço mais depressa.
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")

# Assina tokens internos (URLs do armazenamento local, webhooks). Em produção DEVE vir do
# ambiente; o valor aleatório só serve para desenvolvimento (muda a cada arranque).
APP_SECRET = _env("APP_SECRET") or secrets.token_hex(32)

OWNER_EMAIL = _env("VELOCITY_OWNER_EMAIL").lower()
GOOGLE_AUTH_ENABLED = _env("GOOGLE_AUTH_ENABLED") == "1"

# Desenvolvimento local (sem Supabase): créditos dados a cada conta nova, para conseguir
# testar o fluxo completo. Ignorado quando há Supabase (produção nunca dá créditos grátis).
DEV_SIGNUP_CREDITS = _int("DEV_SIGNUP_CREDITS", 0)

# --- Limites (validados no browser antes do upload E de novo no worker) ----------------------
# 50 MB = limite por ficheiro do plano Free do Supabase; subir só depois de mudar de plano.
MAX_UPLOAD_MB = _int("VELOCITY_MAX_UPLOAD_MB", 50)
MAX_SOURCE_FILES = _int("VELOCITY_MAX_CLIPS", 20)
MAX_REFERENCE_S = _int("VELOCITY_MAX_REFERENCE_S", 90)
MAX_SOURCE_S = _int("VELOCITY_MAX_SOURCE_S", 180)
MAX_SOURCES_TOTAL_S = _int("VELOCITY_MAX_SOURCES_TOTAL_S", 600)
MAX_MUSIC_S = _int("VELOCITY_MAX_MUSIC_S", 600)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# Retenção no armazenamento (o Supabase Free tem 1 GB no total): rascunhos abandonados e os
# ficheiros-fonte de projetos antigos são apagados; o vídeo final fica.
DRAFT_RETENTION_DAYS = _int("VELOCITY_DRAFT_RETENTION_DAYS", 3)
SOURCE_RETENTION_DAYS = _int("VELOCITY_SOURCE_RETENTION_DAYS", 14)

# --- Preço (valores por defeito; o admin muda-os na base de dados sem novo deploy) -----------
DEFAULT_PRICING = {
    "credits_per_generation": {"fast": 1, "standard": 1, "hd": 2},
    "credits_per_edit_export": 0,
    "credits_per_agent_revision": 1,  # cada versão final corrigida pelo agente (a 1.ª custa uma geração)
    "credit_usd_value": 0.50,
    # Custo do fornecedor de computação por segundo de execução (ver a página de preços do
    # RunPod para a GPU escolhida). 0 = ainda não configurado; o custo por vídeo aparece "—".
    "compute_cost_per_second_usd": 0.0,
}
DEFAULT_PACKS = [
    {"id": "pack4", "credits": 4, "price_cents": 200, "currency": "USD", "label": "4 generations", "active": True, "sort": 1},
    {"id": "pack10", "credits": 10, "price_cents": 500, "currency": "USD", "label": "10 generations", "active": True, "sort": 2},
]

EMOLA_NUMBER = _env("EMOLA_NUMBER", "868116236")

# --- Agente de edição (studio/agent) ----------------------------------------------------------
# Sem credenciais da Anthropic o agente fica desligado e a app explica porquê; o resto funciona.
AGENT_ENABLED = bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN")) and _env("AGENT_ENABLED", "1") == "1"
AGENT_MODEL = _env("AGENT_MODEL", "claude-opus-5-5")
AGENT_EFFORT = _env("AGENT_EFFORT", "high")  # low | medium | high | xhigh | max
AGENT_MAX_TOKENS = _int("AGENT_MAX_TOKENS", 32000)  # por resposta (o pensamento conta)
AGENT_MAX_ITERATIONS = _int("AGENT_MAX_ITERATIONS", 40)  # chamadas ao modelo por mensagem do utilizador
AGENT_MAX_TURN_INPUT_TOKENS = _int("AGENT_MAX_TURN_INPUT_TOKENS", 400000)  # tokens não-cache por mensagem: trava de custo
AGENT_MAX_CONCURRENT_TURNS = _int("AGENT_MAX_CONCURRENT_TURNS", 8)
AGENT_MAX_PREVIEWS_PER_TURN = _int("AGENT_MAX_PREVIEWS_PER_TURN", 6)
AGENT_MAX_FINALS_PER_TURN = _int("AGENT_MAX_FINALS_PER_TURN", 2)
AGENT_KEEP_VERSIONS = _int("AGENT_KEEP_VERSIONS", 4)  # vídeos guardados por projeto (o Supabase Free tem 1 GB)
RATE_AGENT_TURNS_PER_HOUR = _int("VELOCITY_RATE_LIMIT_AGENT", 30)
AGENT_FREE_TURNS_PER_DAY = _int("AGENT_FREE_TURNS_PER_DAY", 6)  # para quem ainda não tem créditos conversar e experimentar
AGENT_TOOL_TIMEOUT_S = _int("AGENT_TOOL_TIMEOUT_S", 1500)

# --- Rate limits (por utilizador / IP, janela deslizante em memória) -------------------------
RATE_GENERATIONS_PER_HOUR = _int("VELOCITY_RATE_LIMIT_JOBS", 10)
RATE_ANALYSES_PER_HOUR = _int("VELOCITY_RATE_LIMIT_ANALYSES", 30)
RATE_AUTH_PER_10MIN = _int("VELOCITY_RATE_LIMIT_AUTH", 20)
MAX_QUEUE = _int("VELOCITY_MAX_QUEUE", 20)
