"""Cliente Supabase (Postgres + Auth + Storage) partilhado pelo servidor.

Usa sempre a service_role key: quem decide quem pode ver/escrever o quê é o backend
(ver `studio/auth.py` e as verificações de dono em `studio/server.py`), nunca o browser —
essa chave nunca é enviada ao cliente.

Se SUPABASE_URL/SUPABASE_SERVICE_KEY não estiverem definidos (ex.: a correr localmente
via `run_studio.py`, uso de um único operador), `enabled()` devolve False e o servidor
volta ao comportamento original: sem login, sem limite de edições.
"""
from __future__ import annotations

import os

BUCKET = "videos"

_client = None


def enabled() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def get_client():
    global _client
    if _client is None:
        from supabase import create_client
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    return _client


def ensure_profile(user_id: str, email: str | None) -> dict:
    c = get_client()
    r = c.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    if r.data:
        return r.data[0]
    c.table("profiles").insert({"id": user_id, "email": email}).execute()
    r = c.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    return r.data[0]


def can_edit(user_id: str) -> bool:
    c = get_client()
    r = c.rpc("can_edit", {"p_user_id": user_id}).execute()
    return bool(r.data)


def consume_edit(user_id: str) -> None:
    get_client().rpc("consume_edit", {"p_user_id": user_id}).execute()


def upsert_job(job_id: str, user_id: str, **fields) -> None:
    row = {"id": job_id, "user_id": user_id, **fields}
    get_client().table("jobs").upsert(row).execute()


def update_job(job_id: str, **fields) -> None:
    get_client().table("jobs").update(fields).eq("id", job_id).execute()


def list_my_jobs(user_id: str, limit: int = 50) -> list[dict]:
    r = (get_client().table("jobs").select("*").eq("user_id", user_id)
         .order("created_at", desc=True).limit(limit).execute())
    return r.data or []


def set_job_public(job_id: str, user_id: str, is_public: bool) -> bool:
    r = (get_client().table("jobs").update({"is_public": is_public})
         .eq("id", job_id).eq("user_id", user_id).execute())
    return bool(r.data)


def list_public_gallery(limit: int = 60) -> list[dict]:
    r = (get_client().table("jobs").select("id,video_url,created_at,profile")
         .eq("is_public", True).eq("status", "done")
         .order("created_at", desc=True).limit(limit).execute())
    return r.data or []


def upload_video(job_id: str, local_path: str) -> str:
    """Sobe o vídeo final para o Storage e devolve o URL público (persiste entre reinícios)."""
    c = get_client()
    dest = f"{job_id}.mp4"
    with open(local_path, "rb") as f:
        c.storage.from_(BUCKET).upload(dest, f, {"content-type": "video/mp4", "upsert": "true"})
    return c.storage.from_(BUCKET).get_public_url(dest)


def declare_payment(user_id: str, amount_mt: float, reference: str | None) -> dict:
    r = (get_client().table("payments")
         .insert({"user_id": user_id, "amount_mt": amount_mt, "reference": reference})
         .execute())
    return r.data[0]


def list_my_payments(user_id: str) -> list[dict]:
    r = (get_client().table("payments").select("*").eq("user_id", user_id)
         .order("created_at", desc=True).execute())
    return r.data or []


def list_pending_payments() -> list[dict]:
    r = (get_client().table("payments").select("*, profiles(email)")
         .eq("status", "pending").order("created_at").execute())
    return r.data or []


def confirm_payment(payment_id: str) -> None:
    get_client().rpc("confirm_payment", {"p_payment_id": payment_id}).execute()
