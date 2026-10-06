"""Armazenamento de ficheiros. Mesmo protocolo nos dois backends, para o browser e o worker
usarem exatamente o mesmo código em desenvolvimento e em produção:

- upload: `PUT <url>` multipart com o campo `file` (o formato do Supabase Storage para URLs
  de upload assinadas — confirmado na fonte do cliente oficial `storage3`).
- download: `GET <url>` assinada e temporária.

Buckets (todos privados): `uploads` (vídeos do utilizador, referências, música),
`outputs` (vídeos gerados), `thumbs` (miniaturas). Caminhos começam sempre por
`{user_id}/{project_id}/...` — é isso que o backend verifica antes de emitir uma URL.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import time

from studio import config

BUCKETS = ("uploads", "outputs", "thumbs")
DATA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "storage")


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(config.APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def verify_token(token: str) -> dict | None:
    try:
        raw, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(config.APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return None
    payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    if payload.get("exp", 0) < time.time():
        return None
    return payload


def _safe_path(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


class LocalStorage:
    """Desenvolvimento: ficheiros em disco, servidos pela própria API com tokens HMAC."""

    is_local = True

    def local_path(self, bucket: str, path: str) -> str:
        assert bucket in BUCKETS
        return os.path.join(DATA_ROOT, bucket, *_safe_path(path).split("/"))

    def create_upload_url(self, bucket: str, path: str, max_bytes: int | None = None) -> dict:
        tok = _sign({"op": "put", "b": bucket, "p": _safe_path(path), "exp": time.time() + 3600,
                     "max": max_bytes or config.MAX_UPLOAD_MB * 1024 * 1024})
        return {"url": f"/api/files/upload?t={tok}", "method": "PUT", "field": "file"}

    def signed_url(self, bucket: str, path: str, expires_s: int = 3600, download_name: str | None = None) -> str:
        tok = _sign({"op": "get", "b": bucket, "p": _safe_path(path), "exp": time.time() + expires_s,
                     "dl": download_name})
        return f"/api/files/get?t={tok}"

    def exists(self, bucket: str, path: str) -> bool:
        return os.path.isfile(self.local_path(bucket, path))

    def size(self, bucket: str, path: str) -> int | None:
        p = self.local_path(bucket, path)
        return os.path.getsize(p) if os.path.isfile(p) else None

    def remove(self, bucket: str, paths: list[str]) -> None:
        for path in paths:
            p = self.local_path(bucket, path)
            if os.path.isfile(p):
                os.remove(p)

    def remove_prefix(self, bucket: str, prefix: str) -> None:
        d = self.local_path(bucket, prefix)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)

    def copy(self, bucket: str, src: str, dst: str) -> None:
        d = self.local_path(bucket, dst)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(self.local_path(bucket, src), d)


class SupabaseStorage:
    is_local = False

    def __init__(self):
        from supabase import create_client
        self._c = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY).storage

    def create_upload_url(self, bucket: str, path: str, max_bytes: int | None = None) -> dict:
        # o limite de tamanho é imposto pelo próprio bucket (file_size_limit, ver o SQL)
        r = self._c.from_(bucket).create_signed_upload_url(_safe_path(path))
        return {"url": r["signed_url"], "method": "PUT", "field": "file"}

    def signed_url(self, bucket: str, path: str, expires_s: int = 3600, download_name: str | None = None) -> str:
        opts = {"download": download_name} if download_name else None
        r = self._c.from_(bucket).create_signed_url(_safe_path(path), expires_s, opts)
        return r.get("signedURL") or r.get("signedUrl")

    def exists(self, bucket: str, path: str) -> bool:
        try:
            return bool(self._c.from_(bucket).exists(_safe_path(path)))
        except Exception:
            return False

    def size(self, bucket: str, path: str) -> int | None:
        try:
            info = self._c.from_(bucket).info(_safe_path(path))
            return int(info.get("size") or (info.get("metadata") or {}).get("size") or 0) or None
        except Exception:
            return None

    def remove(self, bucket: str, paths: list[str]) -> None:
        paths = [_safe_path(p) for p in paths if p]
        for i in range(0, len(paths), 100):
            self._c.from_(bucket).remove(paths[i:i + 100])

    def copy(self, bucket: str, src: str, dst: str) -> None:
        self._c.from_(bucket).copy(_safe_path(src), _safe_path(dst))

    def remove_prefix(self, bucket: str, prefix: str) -> None:
        prefix = _safe_path(prefix)
        items = self._c.from_(bucket).list(prefix, {"limit": 1000}) or []
        files = [f"{prefix}/{it['name']}" for it in items if it.get("id")]
        subdirs = [f"{prefix}/{it['name']}" for it in items if not it.get("id")]
        if files:
            self.remove(bucket, files)
        for d in subdirs:
            self.remove_prefix(bucket, d)


_storage = None


def get_storage():
    global _storage
    if _storage is None:
        _storage = SupabaseStorage() if config.USE_SUPABASE else LocalStorage()
    return _storage


def set_storage(s) -> None:
    global _storage
    _storage = s
