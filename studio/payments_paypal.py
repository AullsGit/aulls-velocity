"""Pagamento internacional automático via PayPal (Orders API v2 + webhook).

Por que PayPal e não Stripe: verificado (não assumido) que o Stripe não aceita contas de
vendedor registadas em Moçambique. O PayPal aceita.

Por que "pacotes" e não $0.50 isolado: à taxa PADRÃO do PayPal (2.99% + $0.49), uma cobrança
de $0.50 dá PREJUÍZO (a taxa fixa sozinha já ultrapassa o preço) — só compensaria com a taxa
"Micropayments" do PayPal, que exige pedir aprovação direta ao PayPal (não é automático, não é
algo que se resolva por código). Em vez de depender dessa aprovação, vende-se em pacotes
(créditos): o preço por geração continua $0.50 para o cliente, mas cada cobrança ao PayPal é
maior, o que dilui a taxa fixa para uma margem saudável já com a taxa padrão — sem esperar por
ninguém. Ver docs/DEPLOY.md para os números exatos.

Segurança (arquitetura pedida): o frontend NUNCA decide o preço nem credita nada. O backend
cria a order com o preço do PACKS (nunca um valor vindo do cliente), e só o WEBHOOK do PayPal,
com assinatura verificada pela própria API do PayPal, é que concede créditos
(`studio/db.py::grant_paypal_credits`, que é idempotente — o mesmo evento nunca credita 2x).
"""
from __future__ import annotations

import os
import time

import requests

PAYPAL_ENV = os.environ.get("PAYPAL_ENV", "sandbox")  # sandbox | live
BASE_URL = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"

# Preço por geração: $0.50. Vendido em pacotes para a taxa fixa do PayPal não comer o preço
# (ver módulo docstring). Ajustável sem tocar no resto do código.
PACKS = {
    "pack4": {"credits": 4, "usd": 2.00, "label": "4 gerações — $2.00"},
    "pack10": {"credits": 10, "usd": 5.00, "label": "10 gerações — $5.00"},
}

_token_cache: dict = {"token": None, "exp": 0.0}


def enabled() -> bool:
    return bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))


def client_id() -> str:
    """Só isto (a chave PÚBLICA) pode ir para o frontend — nunca o client secret."""
    return os.environ.get("PAYPAL_CLIENT_ID", "")


def _get_access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 30:
        return _token_cache["token"]
    r = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"}, timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    _token_cache["token"] = j["access_token"]
    _token_cache["exp"] = time.time() + j.get("expires_in", 3600)
    return _token_cache["token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_access_token()}", "Content-Type": "application/json"}


def create_order(pack_id: str, user_id: str) -> dict:
    """Cria a order no PayPal com o preço do PACKS — NUNCA um valor recebido do cliente.

    `custom_id` guarda "user_id:pack_id": é a única forma de o webhook (que só recebe dados
    do PayPal, nunca do nosso frontend) saber a quem creditar quando o pagamento se confirma.
    """
    pack = PACKS[pack_id]
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "custom_id": f"{user_id}:{pack_id}",
            "amount": {"currency_code": "USD", "value": f"{pack['usd']:.2f}"},
            "description": f"AwlsVelocity — {pack['label']}",
        }],
    }
    r = requests.post(f"{BASE_URL}/v2/checkout/orders", headers=_headers(), json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def capture_order(order_id: str) -> dict:
    r = requests.post(f"{BASE_URL}/v2/checkout/orders/{order_id}/capture", headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def verify_webhook_signature(headers: dict, body: dict) -> bool:
    """Confirma com a própria API do PayPal que o webhook é genuíno (não confiamos em nada
    localmente — sem isto, qualquer um podia enviar um POST falso a fingir um pagamento)."""
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return False
    payload = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": webhook_id,
        "webhook_event": body,
    }
    r = requests.post(f"{BASE_URL}/v1/notifications/verify-webhook-signature",
                       headers=_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json().get("verification_status") == "SUCCESS"
