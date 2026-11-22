"""Bentuk respons bersama klaster (AGENTS.md bagian 3) dan helper kecil."""
import sqlite3
from typing import Any, Optional

from . import clock
from .config import settings
from .errors import ApiError


def now_z() -> str:
    """Waktu sekarang dalam ISO 8601 UTC berakhiran Z."""
    return clock.now_iso()


def ok(conn: sqlite3.Connection, data: Any, *, stale: bool = False, total: Optional[int] = None,
       extra_meta: Optional[dict] = None) -> dict:
    """Envelope sukses: {"data": ..., "meta": {seq, updated_at, source, stale}}."""
    from .meta import current_seq  # hindari import melingkar

    meta: dict = {
        "seq": current_seq(conn),
        "updated_at": now_z(),
        "source": settings.source,
        "stale": bool(stale),
    }
    if total is not None:
        meta["total"] = total
    if extra_meta:
        meta.update(extra_meta)
    return {"data": data, "meta": meta}


# --- ID buram: integer di database, string berawalan di API (mis. "req_12") ---

def make_id(prefix: str, value: Optional[int]) -> Optional[str]:
    return None if value is None else f"{prefix}_{value}"


def parse_id(prefix: str, value: str, what: str = "Data") -> int:
    text = str(value)
    if text.startswith(prefix + "_") and text[len(prefix) + 1:].isdigit():
        return int(text[len(prefix) + 1:])
    raise ApiError(404, "not_found", f"{what} tidak ditemukan")


# --- Error yang sering dipakai ---

def not_found(what: str = "Data") -> ApiError:
    return ApiError(404, "not_found", f"{what} tidak ditemukan")


def conflict(message: str) -> ApiError:
    return ApiError(409, "conflict", message)


def invalid(message: str) -> ApiError:
    return ApiError(422, "validation_error", message)


def forbidden(message: str = "Peran Anda tidak diizinkan untuk aksi ini") -> ApiError:
    return ApiError(403, "forbidden", message)
