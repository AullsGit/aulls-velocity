"""PayPal (Orders API v2 + webhook com assinatura verificada pela API do próprio PayPal).

Porquê PayPal: verificado que o Stripe não aceita contas de vendedor de Moçambique; o PayPal
aceita. Porquê pacotes e não $0.50 avulso: à taxa padrão (2.99% + $0.49) uma cobrança de
$0.50 dá prejuízo — ver docs/DEPLOY.md.

Créditos são concedidos por DUAS vias, ambas servidor-a-servidor e idempotentes entre si:
  1. `confirm`: depois de o cliente aprovar, o backend captura a order com as suas credenciais
     e lê o resultado diretamente do PayPal (o browser não decide nada);
  2. `handle_webhook`: PAYMENT.CAPTURE.COMPLETED com assinatura verificada — rede de segurança
     se a confirmação síncrona falhar (rede, fecho do separador...).
"""
from __future__ import annotations

import os
import time

import requests


class PayPalProvider:
    name = "paypal"

    def __init__(self):
        self.env = os.environ.get("PAYPAL_ENV", "sandbox")
        self.base = "https://api-m.paypal.com" if self.env == "live" else "https://api-m.sandbox.paypal.com"
        self._tok = {"v": None, "exp": 0.0}

    def enabled(self) -> bool:
        return bool(os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))

    def public_config(self) -> dict:
        return {"client_id": os.environ.get("PAYPAL_CLIENT_ID", ""), "env": self.env}

    def _token(self) -> str:
        if self._tok["v"] and time.time() < self._tok["exp"] - 30:
            return self._tok["v"]
        r = requests.post(f"{self.base}/v1/oauth2/token", data={"grant_type": "client_credentials"},
                          auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]), timeout=15)
        r.raise_for_status()
        j = r.json()
        self._tok = {"v": j["access_token"], "exp": time.time() + j.get("expires_in", 3600)}
        return self._tok["v"]

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def create_checkout(self, user_id: str, pack: dict) -> dict:
        price = f"{pack['price_cents'] / 100:.2f}"
        body = {"intent": "CAPTURE", "purchase_units": [{
            "custom_id": f"{user_id}:{pack['id']}",  # o webhook só recebe dados do PayPal: é assim que sabe a quem creditar
            "amount": {"currency_code": pack.get("currency", "USD"), "value": price},
            "description": f"AwlsVelocity — {pack['label']}",
        }]}
        r = requests.post(f"{self.base}/v2/checkout/orders", json=body, headers=self._h(), timeout=20)
        r.raise_for_status()
        return {"order_id": r.json()["id"]}

    @staticmethod
    def _captures(resource: dict) -> list[dict]:
        out = []
        for pu in resource.get("purchase_units", []):
            for c in (pu.get("payments") or {}).get("captures", []):
                out.append({**c, "custom_id": c.get("custom_id") or pu.get("custom_id")})
        return out

    def confirm(self, order_id: str) -> list[dict]:
        """Captura a order; devolve as capturas COMPLETED [{id, custom_id, amount_usd}]."""
        r = requests.post(f"{self.base}/v2/checkout/orders/{order_id}/capture", headers=self._h(), timeout=30)
        if r.status_code == 422:  # já capturada antes (ex.: duplo clique): lê o estado atual
            r = requests.get(f"{self.base}/v2/checkout/orders/{order_id}", headers=self._h(), timeout=20)
        r.raise_for_status()
        return [{"id": c["id"], "custom_id": c.get("custom_id") or "", "amount_usd": float(c["amount"]["value"])}
                for c in self._captures(r.json()) if c.get("status") == "COMPLETED"]

    def verify_webhook(self, headers: dict, body: dict) -> bool:
        webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
        if not webhook_id:
            return False
        h = {k.lower(): v for k, v in headers.items()}
        payload = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
                   "transmission_id": h.get("paypal-transmission-id"), "transmission_sig": h.get("paypal-transmission-sig"),
                   "transmission_time": h.get("paypal-transmission-time"), "webhook_id": webhook_id, "webhook_event": body}
        r = requests.post(f"{self.base}/v1/notifications/verify-webhook-signature", json=payload, headers=self._h(), timeout=15)
        r.raise_for_status()
        return r.json().get("verification_status") == "SUCCESS"

    def webhook_captures(self, body: dict) -> list[dict]:
        if body.get("event_type") != "PAYMENT.CAPTURE.COMPLETED":
            return []
        res = body.get("resource") or {}
        return [{"id": res.get("id"), "custom_id": res.get("custom_id") or "",
                 "amount_usd": float((res.get("amount") or {}).get("value") or 0)}]
