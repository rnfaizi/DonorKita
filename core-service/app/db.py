"""Koneksi SQLite (mode WAL) dan skema dasar.

Setiap bounded context menambahkan tabelnya sendiri lewat register_schema() di modulnya.
Satu konteks tidak pernah membaca tabel konteks lain (AGENTS.md aturan 4).
"""
import os
import sqlite3
from typing import Iterator

from .config import settings

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS seq_counter (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  value INTEGER NOT NULL
);
INSERT OR IGNORE INTO seq_counter (id, value) VALUES (1, 0);

-- Umpan perubahan untuk GET /changes (dipakai polling aplikasi dan kelompok lain)
CREATE TABLE IF NOT EXISTS changes (
  seq INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  summary TEXT NOT NULL
);
"""

_schemas: list[str] = [BASE_SCHEMA]


def register_schema(sql: str) -> None:
    """Dipanggil modul bounded context saat di-import untuk mendaftarkan tabelnya."""
    if sql not in _schemas:
        _schemas.append(sql)


def connect() -> sqlite3.Connection:
    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        for sql in _schemas:
            conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """Dependency FastAPI: satu koneksi per request, commit bila sukses, rollback bila gagal."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
