"""Implementação em memória do contrato de dados (desenvolvimento e testes).

Mesma semântica que as funções SQL de produção: gastar créditos é atómico e idempotente
por job; reembolsar é idempotente; compras são idempotentes pelo id do fornecedor.
"""
from __future__ import annotations

import copy
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

from studio import config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class MemoryStore:
    _FIELDS = ("profiles", "projects", "jobs", "ledger", "payments", "config", "packs", "promos", "events", "threads")

    def __init__(self, persist_path: str | None = None):
        self._lock = threading.RLock()
        self.profiles: dict[str, dict] = {}
        self.projects: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.ledger: list[dict] = []
        self.payments: dict[str, dict] = {}
        self.config: dict = {}
        self.packs: dict[str, dict] = {p["id"]: dict(p) for p in config.DEFAULT_PACKS}
        self.promos: dict[str, dict] = {}
        self.redemptions: set[tuple[str, str]] = set()
        self.events: list[dict] = []
        self.threads: dict[str, dict] = {}
        self._persist = persist_path
        if persist_path:
            self._load()
            threading.Thread(target=self._autosave, daemon=True).start()

    # Persistência opcional (só desenvolvimento / operador local): um snapshot JSON a cada
    # 3 s, para reiniciar o servidor não apagar contas e projetos. Produção usa o Supabase.
    def _load(self) -> None:
        import json
        import os
        if not os.path.isfile(self._persist):
            return
        with open(self._persist, encoding="utf-8") as fh:
            d = json.load(fh)
        for k in self._FIELDS:
            if k in d:
                setattr(self, k, d[k])
        self.redemptions = {tuple(x) for x in d.get("redemptions", [])}

    def _autosave(self) -> None:
        import json
        import os
        import time
        last = None
        while True:
            time.sleep(3)
            with self._lock:
                d = {k: getattr(self, k) for k in self._FIELDS}
                d["redemptions"] = [list(x) for x in self.redemptions]
                blob = json.dumps(d, default=str)
            if blob != last:
                os.makedirs(os.path.dirname(self._persist), exist_ok=True)
                tmp = self._persist + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(blob)
                os.replace(tmp, self._persist)
                last = blob

    # ------------------------------------------------------------------ perfis
    def ensure_profile(self, user_id: str, email: str | None) -> dict:
        with self._lock:
            p = self.profiles.get(user_id)
            if p is None:
                p = self.profiles[user_id] = {
                    "id": user_id, "email": email, "display_name": (email or "").split("@")[0],
                    "paid_credits": 0, "plan": "pay_per_use", "onboarded": False, "created_at": now_iso(),
                }
            return copy.deepcopy(p)

    def get_profile(self, user_id: str) -> dict | None:
        with self._lock:
            p = self.profiles.get(user_id)
            return copy.deepcopy(p) if p else None

    def update_profile(self, user_id: str, fields: dict) -> dict:
        allowed = {"display_name", "onboarded", "plan", "agent_memory"}
        with self._lock:
            p = self.profiles[user_id]
            p.update({k: v for k, v in fields.items() if k in allowed})
            return copy.deepcopy(p)

    # ---------------------------------------------------------------- projetos
    def create_project(self, user_id: str, name: str) -> dict:
        with self._lock:
            pid = str(uuid.uuid4())
            p = {
                "id": pid, "user_id": user_id, "name": name, "status": "draft", "files": [], "settings": {},
                "reference_summary": None, "reference_dna": None, "timeline": None, "output_path": None,
                "thumbnail_path": None, "duration_s": None, "resolution": None, "share_token": None,
                "is_public": False, "last_job_id": None, "sources_purged": False,
                "created_at": now_iso(), "updated_at": now_iso(),
            }
            self.projects[pid] = p
            return copy.deepcopy(p)

    def get_project(self, project_id: str) -> dict | None:
        with self._lock:
            p = self.projects.get(project_id)
            return copy.deepcopy(p) if p else None

    def list_projects(self, user_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = [p for p in self.projects.values() if p["user_id"] == user_id]
            rows.sort(key=lambda p: p["updated_at"], reverse=True)
            return copy.deepcopy(rows[:limit])

    def update_project(self, project_id: str, fields: dict) -> dict:
        with self._lock:
            p = self.projects[project_id]
            p.update(copy.deepcopy(fields))
            p["updated_at"] = now_iso()
            return copy.deepcopy(p)

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            self.projects.pop(project_id, None)
            self.threads.pop(project_id, None)

    # ------------------------------------------------------- conversas do agente
    def create_thread(self, user_id: str, project_id: str) -> dict:
        with self._lock:
            t = self.threads.get(project_id)
            if t is None:
                t = self.threads[project_id] = {"project_id": project_id, "user_id": user_id, "status": "idle", "messages": [], "events": [],
                                                "state": {}, "usage": {}, "created_at": now_iso(), "updated_at": now_iso()}
            return copy.deepcopy(t)

    def get_thread(self, project_id: str) -> dict | None:
        with self._lock:
            t = self.threads.get(project_id)
            return copy.deepcopy(t) if t else None

    def update_thread(self, project_id: str, fields: dict) -> dict:
        with self._lock:
            t = self.threads[project_id]
            t.update(copy.deepcopy(fields))
            t["updated_at"] = now_iso()
            return copy.deepcopy(t)

    def list_threads(self, user_id: str, limit: int = 100) -> list[dict]:
        """Só o que a lista de conversas mostra (as mensagens não saem daqui)."""
        with self._lock:
            rows = [t for t in self.threads.values() if t["user_id"] == user_id]
            rows.sort(key=lambda t: t["updated_at"], reverse=True)
            return [{k: copy.deepcopy(t[k]) for k in ("project_id", "user_id", "status", "created_at", "updated_at")} for t in rows[:limit]]

    def list_running_threads(self) -> list[dict]:
        with self._lock:
            return [{k: t[k] for k in ("project_id", "user_id", "status")} for t in self.threads.values() if t["status"] == "running"]

    def find_project_by_share(self, token: str) -> dict | None:
        with self._lock:
            for p in self.projects.values():
                if p.get("share_token") and secrets.compare_digest(p["share_token"], token):
                    return copy.deepcopy(p)
            return None

    def list_public_projects(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = [p for p in self.projects.values() if p["is_public"] and p["status"] == "completed"]
            rows.sort(key=lambda p: p["updated_at"], reverse=True)
            return copy.deepcopy(rows[:limit])

    def list_projects_for_cleanup(self, draft_days: int, source_days: int) -> dict:
        with self._lock:
            now = datetime.now(timezone.utc)
            drafts = [copy.deepcopy(p) for p in self.projects.values()
                      if p["status"] == "draft" and _parse(p["updated_at"]) < now - timedelta(days=draft_days)]
            stale = [copy.deepcopy(p) for p in self.projects.values()
                     if p["status"] in ("completed", "failed") and not p.get("sources_purged")
                     and _parse(p["updated_at"]) < now - timedelta(days=source_days)]
            return {"drafts": drafts, "stale_sources": stale}

    # -------------------------------------------------------------------- jobs
    def create_job(self, job: dict) -> dict:
        with self._lock:
            row = {
                "status": "QUEUED", "stage": "queued", "progress": 0.0, "provider": None, "provider_job_id": None,
                "worker_id": None, "worker_type": None, "credits_used": 0, "refunded": False,
                "created_at": now_iso(), "started_at": None, "completed_at": None, "failed_at": None,
                "cancelled_at": None, "queue_time_s": None, "processing_time_s": None, "gpu_time_s": None,
                "est_cost_usd": None, "video_duration_s": None, "resolution": None, "error_message": None,
                "error_detail": None, "idempotency_key": None, "output_path": None, "metrics": {},
                **copy.deepcopy(job),
            }
            self.jobs[row["id"]] = row
            return copy.deepcopy(row)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            j = self.jobs.get(job_id)
            return copy.deepcopy(j) if j else None

    def update_job(self, job_id: str, fields: dict) -> dict:
        with self._lock:
            j = self.jobs[job_id]
            j.update(copy.deepcopy(fields))
            return copy.deepcopy(j)

    def list_jobs(self, user_id: str | None = None, project_id: str | None = None, statuses=None,
                  provider: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = [j for j in self.jobs.values()
                    if (user_id is None or j["user_id"] == user_id)
                    and (project_id is None or j["project_id"] == project_id)
                    and (statuses is None or j["status"] in statuses)
                    and (provider is None or j["provider"] == provider)]
            rows.sort(key=lambda j: j["created_at"], reverse=True)
            return copy.deepcopy(rows[:limit])

    def find_job_by_idempotency(self, user_id: str, key: str) -> dict | None:
        with self._lock:
            for j in self.jobs.values():
                if j["user_id"] == user_id and j.get("idempotency_key") == key:
                    return copy.deepcopy(j)
            return None

    def count_active_jobs(self) -> int:
        with self._lock:
            return sum(1 for j in self.jobs.values() if j["status"] in ("QUEUED", "PROCESSING"))

    # ----------------------------------------------------------------- créditos
    def balance(self, user_id: str) -> int:
        with self._lock:
            p = self.profiles.get(user_id)
            return int(p["paid_credits"]) if p else 0

    def _ledger(self, user_id: str, delta: int, reason: str, ref_id: str | None, key: str | None) -> bool:
        if key and any(t["idempotency_key"] == key for t in self.ledger):
            return False
        p = self.profiles[user_id]
        p["paid_credits"] += delta
        self.ledger.append({
            "id": str(uuid.uuid4()), "user_id": user_id, "delta": delta, "balance_after": p["paid_credits"],
            "reason": reason, "ref_id": ref_id, "idempotency_key": key, "created_at": now_iso(),
        })
        return True

    def spend_credits(self, user_id: str, amount: int, job_id: str) -> bool:
        with self._lock:
            key = f"spend:{job_id}"
            if any(t["idempotency_key"] == key for t in self.ledger):
                return True  # já gasto para este job (repetição do mesmo pedido)
            if amount <= 0:
                return True
            if self.balance(user_id) < amount:
                return False
            return self._ledger(user_id, -amount, "generation", job_id, key)

    def refund_job(self, job_id: str) -> bool:
        with self._lock:
            j = self.jobs.get(job_id)
            if not j or j["refunded"] or not j["credits_used"]:
                return False
            spent = any(t["idempotency_key"] == f"spend:{job_id}" for t in self.ledger)
            if not spent:
                return False
            ok = self._ledger(j["user_id"], int(j["credits_used"]), "refund", job_id, f"refund:{job_id}")
            if ok:
                j["refunded"] = True
            return ok

    def grant_credits(self, user_id: str, amount: int, reason: str, ref_id: str | None, idem_key: str) -> bool:
        with self._lock:
            self.ensure_profile(user_id, None)
            return self._ledger(user_id, int(amount), reason, ref_id, idem_key)

    def list_transactions(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = [t for t in self.ledger if t["user_id"] == user_id]
            return copy.deepcopy(sorted(rows, key=lambda t: t["created_at"], reverse=True)[:limit])

    # -------------------------------------------------------- preço e pacotes
    def get_config(self) -> dict:
        with self._lock:
            return {**copy.deepcopy(config.DEFAULT_PRICING), **copy.deepcopy(self.config)}

    def set_config(self, key: str, value) -> None:
        with self._lock:
            self.config[key] = copy.deepcopy(value)

    def schema_version(self) -> int:
        return 3

    def list_packs(self, active_only: bool = True) -> list[dict]:
        with self._lock:
            rows = [p for p in self.packs.values() if p["active"] or not active_only]
            return copy.deepcopy(sorted(rows, key=lambda p: p["sort"]))

    def upsert_pack(self, pack: dict) -> None:
        with self._lock:
            self.packs[pack["id"]] = {**self.packs.get(pack["id"], {}), **copy.deepcopy(pack)}

    # ------------------------------------------------------------------- promo
    def create_promo(self, code: str, credits: int, max_uses: int, expires_at: str | None) -> None:
        with self._lock:
            self.promos[code.upper()] = {"code": code.upper(), "credits": credits, "max_uses": max_uses,
                                         "uses": 0, "expires_at": expires_at, "active": True}

    def redeem_promo(self, user_id: str, code: str) -> tuple[bool, str | int]:
        with self._lock:
            c = self.promos.get(code.strip().upper())
            if not c or not c["active"]:
                return False, "invalid"
            if c["expires_at"] and _parse(c["expires_at"]) < datetime.now(timezone.utc):
                return False, "expired"
            if (user_id, c["code"]) in self.redemptions:
                return False, "already_used"
            if c["uses"] >= c["max_uses"]:
                return False, "exhausted"
            self.redemptions.add((user_id, c["code"]))
            c["uses"] += 1
            self.grant_credits(user_id, c["credits"], "promo", c["code"], f"promo:{c['code']}:{user_id}")
            return True, c["credits"]

    # --------------------------------------------------------------- pagamentos
    def record_purchase(self, user_id: str, provider: str, provider_payment_id: str, amount_usd: float,
                        pack_id: str, credits: int) -> bool:
        with self._lock:
            for p in self.payments.values():
                if p["provider"] == provider and p["provider_payment_id"] == provider_payment_id:
                    return False
            pid = str(uuid.uuid4())
            self.payments[pid] = {
                "id": pid, "user_id": user_id, "provider": provider, "provider_payment_id": provider_payment_id,
                "currency": "USD", "amount_usd": amount_usd, "amount_mt": None, "pack_id": pack_id,
                "credits": credits, "status": "confirmed", "reference": None,
                "created_at": now_iso(), "confirmed_at": now_iso(),
            }
            self.grant_credits(user_id, credits, "purchase", pid, f"purchase:{provider}:{provider_payment_id}")
            return True

    def declare_manual_payment(self, user_id: str, amount_mt: float, reference: str | None) -> dict:
        with self._lock:
            pid = str(uuid.uuid4())
            row = {"id": pid, "user_id": user_id, "provider": "emola", "provider_payment_id": None, "currency": "MT",
                   "amount_usd": None, "amount_mt": amount_mt, "pack_id": None, "credits": 0, "status": "pending",
                   "reference": reference, "created_at": now_iso(), "confirmed_at": None}
            self.payments[pid] = row
            return copy.deepcopy(row)

    def list_payments(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = [p for p in self.payments.values() if p["user_id"] == user_id]
            return copy.deepcopy(sorted(rows, key=lambda p: p["created_at"], reverse=True))

    def list_pending_manual_payments(self) -> list[dict]:
        with self._lock:
            rows = [{**p, "email": (self.profiles.get(p["user_id"]) or {}).get("email")}
                    for p in self.payments.values() if p["provider"] == "emola" and p["status"] == "pending"]
            return copy.deepcopy(sorted(rows, key=lambda p: p["created_at"]))

    def confirm_manual_payment(self, payment_id: str, credits: int) -> bool:
        with self._lock:
            p = self.payments.get(payment_id)
            if not p or p["status"] != "pending" or credits <= 0:
                return False
            p.update(status="confirmed", confirmed_at=now_iso(), credits=credits)
            return self.grant_credits(p["user_id"], credits, "manual_payment", payment_id, f"manual:{payment_id}")

    def reject_manual_payment(self, payment_id: str) -> bool:
        with self._lock:
            p = self.payments.get(payment_id)
            if not p or p["status"] != "pending":
                return False
            p["status"] = "rejected"
            return True

    # ---------------------------------------------------------------- analytics
    def track(self, name: str, user_id: str | None, anon_id: str | None, props: dict | None = None) -> None:
        with self._lock:
            self.events.append({"name": name, "user_id": user_id, "anon_id": anon_id,
                                "props": copy.deepcopy(props or {}), "created_at": now_iso()})

    def admin_list_users(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = sorted(self.profiles.values(), key=lambda p: p["created_at"], reverse=True)[:limit]
            out = []
            for p in rows:
                jobs = [j for j in self.jobs.values() if j["user_id"] == p["id"] and j["kind"] == "generate"]
                spent = sum(-t["delta"] for t in self.ledger if t["user_id"] == p["id"] and t["reason"] == "generation")
                out.append({**copy.deepcopy(p), "generations": len(jobs),
                            "completed": sum(1 for j in jobs if j["status"] == "COMPLETED"), "credits_spent": spent})
            return out

    def admin_stats(self) -> dict:
        with self._lock:
            return compute_stats(list(self.profiles.values()), list(self.jobs.values()),
                                 list(self.payments.values()), self.ledger, self.events)


def compute_stats(profiles, jobs, payments, ledger, events) -> dict:
    """Métricas do painel admin. Partilhado com testes; em produção o equivalente é a função
    SQL `admin_stats()` (uma só ida à base de dados)."""
    gens = [j for j in jobs if j.get("kind") == "generate"]
    done = [j for j in gens if j["status"] == "COMPLETED"]
    failed = [j for j in gens if j["status"] == "FAILED"]
    proc = [j["processing_time_s"] for j in done if j.get("processing_time_s") is not None]
    queue = [j["queue_time_s"] for j in done if j.get("queue_time_s") is not None]
    costs = [j["est_cost_usd"] for j in jobs if j.get("est_cost_usd") is not None]
    revenue_usd = sum(float(p["amount_usd"] or 0) for p in payments if p["status"] == "confirmed" and p["currency"] == "USD")
    revenue_mt = sum(float(p["amount_mt"] or 0) for p in payments if p["status"] == "confirmed" and p["currency"] == "MT")
    buyers = {p["user_id"] for p in payments if p["status"] == "confirmed"}
    creators = {j["user_id"] for j in done}
    first_done: dict[str, str] = {}
    for j in sorted(done, key=lambda j: j["completed_at"] or ""):
        first_done.setdefault(j["user_id"], j["completed_at"])
    signup = {p["id"]: p["created_at"] for p in profiles}
    ttfv = [(_parse(t) - _parse(signup[u])).total_seconds() for u, t in first_done.items() if u in signup and t]
    days: dict[str, set] = {}
    for j in gens:
        days.setdefault(j["user_id"], set()).add(j["created_at"][:10])
    visitors = {e["anon_id"] for e in events if e["name"] == "page_view" and e.get("anon_id")}
    return {
        "users": len(profiles),
        "visitors": len(visitors),
        "generations": len(gens),
        "completed": len(done),
        "failed": len(failed),
        "queued": sum(1 for j in jobs if j["status"] == "QUEUED"),
        "processing": sum(1 for j in jobs if j["status"] == "PROCESSING"),
        "avg_processing_s": round(sum(proc) / len(proc), 1) if proc else None,
        "avg_queue_s": round(sum(queue) / len(queue), 1) if queue else None,
        "compute_cost_usd": round(sum(costs), 4) if costs else 0.0,
        "avg_cost_per_video_usd": round(sum(costs) / len(done), 4) if costs and done else None,
        "revenue_usd": round(revenue_usd, 2),
        "revenue_mt": round(revenue_mt, 2),
        "est_profit_usd": round(revenue_usd - sum(costs), 2),
        "credits_spent": sum(-t["delta"] for t in ledger if t["reason"] == "generation"),
        "credits_refunded": sum(t["delta"] for t in ledger if t["reason"] == "refund"),
        "users_with_video": len(creators),
        "paying_users": len(buyers),
        "conversion_to_paid": round(len(buyers) / len(profiles), 3) if profiles else None,
        "videos_per_user": round(len(done) / len(creators), 2) if creators else None,
        "avg_time_to_first_video_s": round(sum(ttfv) / len(ttfv), 1) if ttfv else None,
        "returning_users": sum(1 for d in days.values() if len(d) >= 2),
        "revenue_per_user_usd": round(revenue_usd / len(profiles), 3) if profiles else None,
    }
