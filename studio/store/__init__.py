"""Acesso a dados. Duas implementações com o MESMO contrato (mesmos métodos, mesmos dicts):

- `SupabaseStore` (produção): Postgres via PostgREST; tudo o que mexe em créditos corre em
  funções SQL atómicas (ver `supabase/migrations/`), nunca em read-modify-write em Python.
- `MemoryStore` (desenvolvimento e testes): mesma semântica com um lock, para testar o fluxo
  inteiro (créditos, idempotência, reembolsos) sem tocar na base de dados de produção.

Estados de job: QUEUED | PROCESSING | COMPLETED | FAILED | CANCELLED.
Estados de projeto: draft | processing | completed | failed.
"""
from __future__ import annotations

from studio import config

JOB_ACTIVE = ("QUEUED", "PROCESSING")
JOB_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")

_store = None


def get_store():
    global _store
    if _store is None:
        if config.USE_SUPABASE:
            from studio.store.supabase import SupabaseStore
            _store = SupabaseStore()
        else:
            from studio.store.memory import MemoryStore
            _store = MemoryStore()
    return _store


def set_store(store) -> None:
    """Só para testes."""
    global _store
    _store = store
