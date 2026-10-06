"""Implementação Supabase do contrato de dados (produção).

Fina de propósito: tudo o que tem de ser atómico (créditos, compras, promo, métricas) é uma
função SQL em `supabase-schema.sql`, testada num Postgres real em `tests/sql/`. Aqui só se
fazem leituras/escritas simples via PostgREST com a service_role key (nunca exposta ao browser).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from studio import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SupabaseStore:
    def __init__(self):
        from supabase import create_client
        self.c = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)

    def _t(self, name: str):
        return self.c.table(name)

    @staticmethod
    def _first(res) -> dict | None:
        return res.data[0] if res.data else None

    # ------------------------------------------------------------------ perfis
    def ensure_profile(self, user_id: str, email: str | None) -> dict:
        p = self.get_profile(user_id)
        if p is None:
            # normalmente criado pelo trigger on_auth_user_created; isto é só rede de segurança
            self._t("profiles").upsert({"id": user_id, "email": email,
                                        "display_name": (email or "").split("@")[0]},
                                       on_conflict="id", ignore_duplicates=True).execute()
            p = self.get_profile(user_id)
        return p

    def get_profile(self, user_id: str) -> dict | None:
        return self._first(self._t("profiles").select("*").eq("id", user_id).limit(1).execute())

    def update_profile(self, user_id: str, fields: dict) -> dict:
        allowed = {k: v for k, v in fields.items() if k in ("display_name", "onboarded", "plan")}
        return self._first(self._t("profiles").update(allowed).eq("id", user_id).execute())

    # ---------------------------------------------------------------- projetos
    def create_project(self, user_id: str, name: str) -> dict:
        return self._first(self._t("projects").insert({"user_id": user_id, "name": name}).execute())

    def get_project(self, project_id: str) -> dict | None:
        return self._first(self._t("projects").select("*").eq("id", project_id).limit(1).execute())

    def list_projects(self, user_id: str, limit: int = 100) -> list[dict]:
        cols = ("id,user_id,name,status,files,settings,reference_summary,output_path,thumbnail_path,duration_s,"
                "resolution,share_token,is_public,last_job_id,sources_purged,created_at,updated_at")
        return self._t("projects").select(cols).eq("user_id", user_id).order("updated_at", desc=True).limit(limit).execute().data or []

    def update_project(self, project_id: str, fields: dict) -> dict:
        return self._first(self._t("projects").update({**fields, "updated_at": _now()}).eq("id", project_id).execute())

    def delete_project(self, project_id: str) -> None:
        self._t("projects").delete().eq("id", project_id).execute()

    def find_project_by_share(self, token: str) -> dict | None:
        return self._first(self._t("projects").select("*").eq("share_token", token).limit(1).execute())

    def list_public_projects(self, limit: int = 30) -> list[dict]:
        return (self._t("projects").select("id,name,output_path,thumbnail_path,duration_s,share_token,updated_at")
                .eq("is_public", True).eq("status", "completed").order("updated_at", desc=True).limit(limit).execute().data or [])

    def list_projects_for_cleanup(self, draft_days: int, source_days: int) -> dict:
        now = datetime.now(timezone.utc)
        drafts = (self._t("projects").select("*").eq("status", "draft")
                  .lt("updated_at", (now - timedelta(days=draft_days)).isoformat()).limit(200).execute().data or [])
        stale = (self._t("projects").select("*").in_("status", ["completed", "failed"]).eq("sources_purged", False)
                 .lt("updated_at", (now - timedelta(days=source_days)).isoformat()).limit(200).execute().data or [])
        return {"drafts": drafts, "stale_sources": stale}

    # -------------------------------------------------------------------- jobs
    def create_job(self, job: dict) -> dict:
        return self._first(self._t("jobs").insert(job).execute())

    def get_job(self, job_id: str) -> dict | None:
        return self._first(self._t("jobs").select("*").eq("id", job_id).limit(1).execute())

    def update_job(self, job_id: str, fields: dict) -> dict:
        return self._first(self._t("jobs").update(fields).eq("id", job_id).execute())

    def list_jobs(self, user_id: str | None = None, project_id: str | None = None, statuses=None,
                  provider: str | None = None, limit: int = 50) -> list[dict]:
        q = self._t("jobs").select("*")
        if user_id:
            q = q.eq("user_id", user_id)
        if project_id:
            q = q.eq("project_id", project_id)
        if statuses:
            q = q.in_("status", list(statuses))
        if provider:
            q = q.eq("provider", provider)
        return q.order("created_at", desc=True).limit(limit).execute().data or []

    def find_job_by_idempotency(self, user_id: str, key: str) -> dict | None:
        return self._first(self._t("jobs").select("*").eq("user_id", user_id).eq("idempotency_key", key).limit(1).execute())

    def count_active_jobs(self) -> int:
        r = self._t("jobs").select("id", count="exact").in_("status", ["QUEUED", "PROCESSING"]).limit(1).execute()
        return int(r.count or 0)

    # ----------------------------------------------------------------- créditos
    def balance(self, user_id: str) -> int:
        p = self.get_profile(user_id)
        return int(p["paid_credits"]) if p else 0

    def spend_credits(self, user_id: str, amount: int, job_id: str) -> bool:
        return bool(self.c.rpc("spend_credits", {"p_user": user_id, "p_amount": amount, "p_job": job_id}).execute().data)

    def refund_job(self, job_id: str) -> bool:
        return bool(self.c.rpc("refund_job", {"p_job": job_id}).execute().data)

    def grant_credits(self, user_id: str, amount: int, reason: str, ref_id: str | None, idem_key: str) -> bool:
        return bool(self.c.rpc("grant_credits", {"p_user": user_id, "p_amount": amount, "p_reason": reason,
                                                 "p_ref": ref_id, "p_key": idem_key}).execute().data)

    def list_transactions(self, user_id: str, limit: int = 50) -> list[dict]:
        return (self._t("credit_transactions").select("*").eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data or [])

    # -------------------------------------------------------- preço e pacotes
    def get_config(self) -> dict:
        rows = self._t("app_config").select("key,value").execute().data or []
        return {**config.DEFAULT_PRICING, **{r["key"]: r["value"] for r in rows if r["key"] != "schema_version"}}

    def schema_version(self) -> int:
        r = self._first(self._t("app_config").select("value").eq("key", "schema_version").limit(1).execute())
        return int(r["value"]) if r else 0

    def set_config(self, key: str, value) -> None:
        self._t("app_config").upsert({"key": key, "value": value, "updated_at": _now()}, on_conflict="key").execute()

    def list_packs(self, active_only: bool = True) -> list[dict]:
        q = self._t("credit_packs").select("*")
        if active_only:
            q = q.eq("active", True)
        return q.order("sort").execute().data or []

    def upsert_pack(self, pack: dict) -> None:
        self._t("credit_packs").upsert(pack, on_conflict="id").execute()

    # ------------------------------------------------------------------- promo
    def create_promo(self, code: str, credits: int, max_uses: int, expires_at: str | None) -> None:
        self._t("promo_codes").upsert({"code": code.upper(), "credits": credits, "max_uses": max_uses,
                                       "expires_at": expires_at, "active": True}, on_conflict="code").execute()

    def redeem_promo(self, user_id: str, code: str) -> tuple[bool, str | int]:
        r = self.c.rpc("redeem_promo", {"p_user": user_id, "p_code": code}).execute().data or {}
        return (True, int(r["credits"])) if r.get("ok") else (False, r.get("error", "invalid"))

    # --------------------------------------------------------------- pagamentos
    def record_purchase(self, user_id: str, provider: str, provider_payment_id: str, amount_usd: float,
                        pack_id: str, credits: int) -> bool:
        return bool(self.c.rpc("record_purchase", {
            "p_user": user_id, "p_provider": provider, "p_ppid": provider_payment_id,
            "p_amount_usd": amount_usd, "p_pack": pack_id, "p_credits": credits}).execute().data)

    def declare_manual_payment(self, user_id: str, amount_mt: float, reference: str | None) -> dict:
        return self._first(self._t("payments").insert({"user_id": user_id, "provider": "emola", "currency": "MT",
                                                       "amount_mt": amount_mt, "reference": reference}).execute())

    def list_payments(self, user_id: str) -> list[dict]:
        return self._t("payments").select("*").eq("user_id", user_id).order("created_at", desc=True).execute().data or []

    def list_pending_manual_payments(self) -> list[dict]:
        # payments e profiles apontam ambos para auth.users (não um para o outro), por isso o
        # PostgREST não consegue embutir profiles(email) — duas consultas em vez de uma junção.
        rows = (self._t("payments").select("*").eq("provider", "emola").eq("status", "pending")
                .order("created_at").execute().data or [])
        ids = list({r["user_id"] for r in rows})
        emails = {}
        if ids:
            emails = {p["id"]: p["email"] for p in self._t("profiles").select("id,email").in_("id", ids).execute().data or []}
        return [{**r, "email": emails.get(r["user_id"])} for r in rows]

    def confirm_manual_payment(self, payment_id: str, credits: int) -> bool:
        return bool(self.c.rpc("confirm_manual_payment", {"p_payment": payment_id, "p_credits": credits}).execute().data)

    def reject_manual_payment(self, payment_id: str) -> bool:
        r = (self._t("payments").update({"status": "rejected"}).eq("id", payment_id)
             .eq("status", "pending").execute())
        return bool(r.data)

    # ---------------------------------------------------------------- analytics
    def track(self, name: str, user_id: str | None, anon_id: str | None, props: dict | None = None) -> None:
        self._t("events").insert({"name": name, "user_id": user_id, "anon_id": anon_id, "props": props or {}}).execute()

    def admin_list_users(self, limit: int = 100) -> list[dict]:
        return self.c.rpc("admin_users", {"p_limit": limit}).execute().data or []

    def admin_stats(self) -> dict:
        return self.c.rpc("admin_stats", {}).execute().data or {}
