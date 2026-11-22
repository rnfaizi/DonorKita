"""Autentikasi dan otorisasi di core-service.

- Pengguna aplikasi: JWT HS256 dari auth-service (klaim sub, role, name, exp).
  Peran: donor, requester, staff, admin.
- Kelompok lain: header X-API-Key, diverifikasi ke auth-service (/internal/verify-key)
  dan di-cache sebentar. Bila AUTH_INTERNAL_URL kosong (pengembangan), dicocokkan ke
  STATIC_API_KEYS.
- Endpoint baca publik menerima X-API-Key ATAU JWT pengguna (AGENTS.md bagian 4).
"""
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
import jwt
from fastapi import Depends, Header

from .config import settings
from .errors import ApiError

ROLES = ("donor", "requester", "staff", "admin")
_key_cache: Dict[str, Tuple[float, bool]] = {}


@dataclass(frozen=True)
class User:
    id: int
    role: str
    name: str = ""


@dataclass(frozen=True)
class Caller:
    """Pemanggil endpoint baca: pengguna (user) atau kelompok lain (api_key)."""
    user: Optional[User] = None
    api_key: bool = False


def _invalid_token(message: str = "Token tidak valid atau kedaluwarsa") -> ApiError:
    return ApiError(401, "invalid_token", message)


def decode_token(token: str) -> User:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user = User(id=int(claims["sub"]), role=str(claims["role"]), name=str(claims.get("name", "")))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise _invalid_token()
    if user.role not in ROLES:
        raise _invalid_token("Peran pada token tidak dikenal")
    return user


def optional_user(authorization: Optional[str] = Header(default=None)) -> Optional[User]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        raise _invalid_token("Format Authorization harus 'Bearer <token>'")
    return decode_token(authorization[7:].strip())


def current_user(user: Optional[User] = Depends(optional_user)) -> User:
    if user is None:
        raise _invalid_token("Login diperlukan")
    return user


def require_roles(*roles: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise ApiError(403, "forbidden", "Peran Anda tidak diizinkan untuk aksi ini")
        return user
    return dependency


def verify_api_key(key: str) -> bool:
    """True bila API key valid. Hasil di-cache 60 detik (valid) atau 10 detik (tidak valid)."""
    now = time.time()
    hit = _key_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    if not settings.auth_url:
        valid = key in settings.static_api_keys
    else:
        try:
            r = httpx.post(f"{settings.auth_url}/internal/verify-key", json={"key": key},
                           headers={"X-Internal-Token": settings.internal_token}, timeout=3)
        except httpx.HTTPError:
            raise ApiError(503, "upstream_unavailable", "Layanan autentikasi tidak dapat dihubungi")
        if r.status_code not in (200, 401, 404):
            raise ApiError(503, "upstream_unavailable", "Layanan autentikasi tidak dapat dihubungi")
        valid = r.status_code == 200
    _key_cache[key] = (now + (60 if valid else 10), valid)
    return valid


def clear_key_cache() -> None:
    _key_cache.clear()


def api_key_or_user(x_api_key: Optional[str] = Header(default=None),
                    user: Optional[User] = Depends(optional_user)) -> Caller:
    """Untuk endpoint baca publik: X-API-Key (kelompok lain) atau JWT (pengguna aplikasi)."""
    if user is not None:
        return Caller(user=user)
    if not x_api_key:
        raise ApiError(401, "invalid_api_key", "Header X-API-Key atau Authorization diperlukan")
    if not verify_api_key(x_api_key):
        raise ApiError(401, "invalid_api_key", "API key tidak valid")
    return Caller(api_key=True)


def is_staff(user: Optional[User]) -> bool:
    return bool(user and user.role in ("staff", "admin"))
