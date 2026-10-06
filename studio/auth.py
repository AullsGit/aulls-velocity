"""Autenticação. Sessão = cookie httponly com o access/refresh token do fornecedor de auth.

- `SupabaseAuth` (produção): Supabase Auth — o hash das passwords é do Supabase, nunca nosso.
  Google: o browser vai a /auth/v1/authorize do Supabase e volta com os tokens no fragmento
  do URL; a app envia-os a /api/auth/session, que os valida no Supabase antes de criar sessão.
- `MemoryAuth` (desenvolvimento/testes): mesmo contrato, passwords com scrypt.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, Request, Response

from studio import config
from studio.errors import ApiError

COOKIE = "awls_session"


@dataclass
class User:
    id: str
    email: str | None


@dataclass
class Session:
    user: User
    access_token: str
    refresh_token: str


class AuthFailed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class MemoryAuth:
    def __init__(self, persist_path: str | None = None):
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        self._access: dict[str, tuple[str, float]] = {}
        self._refresh: dict[str, str] = {}
        self._persist = persist_path
        if persist_path:
            self._load()
            threading.Thread(target=self._autosave, daemon=True).start()

    # só desenvolvimento/operador local (DEV_PERSIST=1): sessões e contas sobrevivem a reinícios
    def _load(self) -> None:
        import os
        if not os.path.isfile(self._persist):
            return
        with open(self._persist, encoding="utf-8") as fh:
            d = json.load(fh)
        self._users = {k: {**u, "salt": base64.b64decode(u["salt"]), "hash": base64.b64decode(u["hash"])}
                       for k, u in d.get("users", {}).items()}
        self._access = {k: tuple(v) for k, v in d.get("access", {}).items()}
        self._refresh = d.get("refresh", {})

    def _autosave(self) -> None:
        import os
        last = None
        while True:
            time.sleep(3)
            with self._lock:
                blob = json.dumps({
                    "users": {k: {**u, "salt": base64.b64encode(u["salt"]).decode(), "hash": base64.b64encode(u["hash"]).decode()}
                              for k, u in self._users.items()},
                    "access": self._access, "refresh": self._refresh})
            if blob != last:
                os.makedirs(os.path.dirname(self._persist), exist_ok=True)
                with open(self._persist + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(blob)
                os.replace(self._persist + ".tmp", self._persist)
                last = blob

    @staticmethod
    def _hash(pw: str, salt: bytes) -> bytes:
        return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)

    def _issue(self, uid: str) -> Session:
        a, r = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._access[a] = (uid, time.time() + 3600)
        self._refresh[r] = uid
        u = self._users[uid]
        return Session(User(uid, u["email"]), a, r)

    def signup(self, email: str, password: str) -> Session | None:
        with self._lock:
            if any(u["email"] == email for u in self._users.values()):
                raise AuthFailed("email_taken", "An account with this email already exists.")
            uid = str(uuid.uuid4())
            salt = secrets.token_bytes(16)
            self._users[uid] = {"email": email, "salt": salt, "hash": self._hash(password, salt)}
            return self._issue(uid)

    def login(self, email: str, password: str) -> Session:
        with self._lock:
            for uid, u in self._users.items():
                if u["email"] == email and secrets.compare_digest(self._hash(password, u["salt"]), u["hash"]):
                    return self._issue(uid)
        raise AuthFailed("invalid_login", "Wrong email or password.")

    def get_user(self, access_token: str) -> User | None:
        with self._lock:
            v = self._access.get(access_token)
            if not v or v[1] < time.time():
                return None
            return User(v[0], self._users[v[0]]["email"])

    def refresh(self, refresh_token: str) -> Session | None:
        with self._lock:
            uid = self._refresh.pop(refresh_token, None)
            return self._issue(uid) if uid else None

    def logout(self, access_token: str) -> None:
        with self._lock:
            self._access.pop(access_token, None)

    def oauth_url(self, provider: str, redirect_to: str) -> str | None:
        return None


class SupabaseAuth:
    def __init__(self):
        from supabase import create_client
        self._mk = lambda: create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
        self._cache: dict[str, tuple[User, float]] = {}

    def signup(self, email: str, password: str) -> Session | None:
        try:
            res = self._mk().auth.sign_up({"email": email, "password": password})
        except Exception as e:
            msg = str(e)
            if "registered" in msg.lower() or "exists" in msg.lower():
                raise AuthFailed("email_taken", "An account with this email already exists.")
            raise AuthFailed("signup_failed", "We couldn't create your account. Check your email and password.")
        if res.session is None:
            return None  # projeto com confirmação de email ligada
        return Session(User(res.user.id, res.user.email), res.session.access_token, res.session.refresh_token)

    def login(self, email: str, password: str) -> Session:
        try:
            res = self._mk().auth.sign_in_with_password({"email": email, "password": password})
        except Exception as e:
            if "confirm" in str(e).lower():
                raise AuthFailed("email_not_confirmed", "Confirm your email first — check your inbox.")
            raise AuthFailed("invalid_login", "Wrong email or password.")
        return Session(User(res.user.id, res.user.email), res.session.access_token, res.session.refresh_token)

    def get_user(self, access_token: str) -> User | None:
        key = hashlib.sha256(access_token.encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
        try:
            res = self._mk().auth.get_user(access_token)
        except Exception:
            return None
        if not res or not res.user:
            return None
        u = User(res.user.id, res.user.email)
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[key] = (u, time.time() + 60)  # evita uma ida ao Supabase em cada pedido
        return u

    def refresh(self, refresh_token: str) -> Session | None:
        try:
            res = self._mk().auth.refresh_session(refresh_token)
        except Exception:
            return None
        if not res or not res.session:
            return None
        return Session(User(res.user.id, res.user.email), res.session.access_token, res.session.refresh_token)

    def logout(self, access_token: str) -> None:
        self._cache.pop(hashlib.sha256(access_token.encode()).hexdigest(), None)

    def oauth_url(self, provider: str, redirect_to: str) -> str | None:
        if provider != "google" or not config.GOOGLE_AUTH_ENABLED:
            return None
        return f"{config.SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={quote(redirect_to, safe='')}"


_auth = None


def get_auth():
    global _auth
    if _auth is None:
        if config.USE_SUPABASE:
            _auth = SupabaseAuth()
        else:
            import os
            path = None
            if os.environ.get("DEV_PERSIST") == "1":
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dev_auth.json")
            _auth = MemoryAuth(path)
    return _auth


def set_auth(a) -> None:
    global _auth
    _auth = a


# --------------------------------------------------------------------- cookie
def _encode(s: Session) -> str:
    return base64.urlsafe_b64encode(json.dumps({"a": s.access_token, "r": s.refresh_token}).encode()).decode()


def _decode(v: str) -> tuple[str, str] | None:
    try:
        d = json.loads(base64.urlsafe_b64decode(v.encode()))
        return d["a"], d["r"]
    except Exception:
        return None


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def set_session(request: Request, response: Response, s: Session) -> None:
    response.set_cookie(COOKIE, _encode(s), httponly=True, secure=is_https(request), samesite="lax",
                        max_age=60 * 60 * 24 * 30, path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def current_user(request: Request, response: Response) -> User | None:
    raw = request.cookies.get(COOKIE)
    toks = _decode(raw) if raw else None
    if not toks:
        return None
    auth = get_auth()
    u = auth.get_user(toks[0])
    if u:
        return u
    s = auth.refresh(toks[1])
    if s:
        set_session(request, response, s)
        return s.user
    return None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise ApiError(401, "unauthorized", "Please log in again.")
    return user


def is_owner(user: User | None) -> bool:
    return bool(user and config.OWNER_EMAIL and (user.email or "").lower() == config.OWNER_EMAIL)


def require_owner(user: User = Depends(require_user)) -> User:
    if not is_owner(user):
        raise ApiError(403, "forbidden", "Not allowed.")
    return user
