"""Pagamentos, desacoplados do resto da app.

Um fornecedor online implementa:
    name: str
    enabled() -> bool
    public_config() -> dict           # só dados públicos (ex.: client id), nunca segredos
    create_checkout(user_id, pack) -> dict
    confirm(user_id, ref) -> dict      # confirmação servidor-a-servidor (ex.: captura PayPal)
    handle_webhook(headers, body) -> dict

Garantias comuns (impostas em `store.record_purchase`, função SQL atómica):
    * o preço vem SEMPRE da tabela credit_packs no servidor, nunca do browser;
    * cada pagamento do fornecedor credita UMA vez (único por provider + provider_payment_id),
      mesmo que o webhook e a confirmação síncrona cheguem os dois, ou repetidos.

Fornecedores atuais: PayPal (internacional, automático) e e-Mola (Moçambique, manual).
"""
from __future__ import annotations

from studio.payments.paypal import PayPalProvider

_providers = None


def online_providers() -> dict:
    global _providers
    if _providers is None:
        _providers = {p.name: p for p in (PayPalProvider(),)}
    return _providers


def get_provider(name: str):
    p = online_providers().get(name)
    return p if p and p.enabled() else None
