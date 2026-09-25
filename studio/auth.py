"""Sessão de login: cookie httponly a guardar o access/refresh token do Supabase Auth.

O browser nunca vê a service_role key (fica só em `studio/db.py`, lado do servidor);
o login em si passa pelo Supabase Auth normal (email+password, hash da password feito
pelo próprio Supabase — nunca guardamos nem vemos passwords em claro).
"""
from __future__ import annotations

import base64
import json
import os

from fastapi import Cookie, Depends, HTTPException, Response

from studio import db

COOKIE_NAME = "sb_session"
OWNER_EMAIL = os.environ.get("VELOCITY_OWNER_EMAIL", "")


def _encode(access_token: str, refresh_token: str) -> str:
    raw = json.dumps({"a": access_token, "r": refresh_token}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode(cookie_value: str) -> tuple[str, str]:
    raw = base64.urlsafe_b64decode(cookie_value.encode())
    d = json.loads(raw)
    return d["a"], d["r"]


def set_session_cookie(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, _encode(access_token, refresh_token),
        httponly=True, secure=True, samesite="lax", max_age=60 * 60 * 24 * 30, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def get_current_user(response: Response, sb_session: str | None = Cookie(default=None)):
    """Dependency FastAPI: devolve o utilizador Supabase autenticado, ou None."""
    if not db.enabled() or not sb_session:
        return None
    try:
        access_token, refresh_token = _decode(sb_session)
    except Exception:
        return None
    client = db.get_client()
    try:
        res = client.auth.get_user(access_token)
        if res and res.user:
            return res.user
    except Exception:
        pass
    try:
        res = client.auth.refresh_session(refresh_token)
        if res and res.session:
            set_session_cookie(response, res.session.access_token, res.session.refresh_token)
            return res.user
    except Exception:
        pass
    return None


def require_user(user=Depends(get_current_user)):
    if user is None:
        raise HTTPException(status_code=401, detail="sessão expirada — inicia sessão novamente")
    return user


def require_owner(user=Depends(require_user)):
    if not OWNER_EMAIL or (user.email or "").lower() != OWNER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="acesso restrito")
    return user
