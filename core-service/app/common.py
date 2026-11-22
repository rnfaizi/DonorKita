"""Bentuk respons bersama klaster (AGENTS.md bagian 3)."""
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import settings


def now_z() -> str:
    """Waktu sekarang dalam ISO 8601 UTC berakhiran Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ok(conn: sqlite3.Connection, data: Any, *, stale: bool = False, total: int | None = None,
       extra_meta: dict | None = None) -> dict:
    """Envelope sukses: {"data": ..., "meta": {seq, updated_at, source, stale}}."""
    from .meta import current_seq  # hindari import melingkar

    meta: dict[str, Any] = {
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
