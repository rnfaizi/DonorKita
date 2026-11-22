"""Penghitung meta.seq dan umpan perubahan.

seq naik pada setiap perubahan data. Setiap kenaikan juga dicatat sebagai satu baris di
tabel changes, yang hanya menyimpan N baris terakhir (settings.changes_keep).
"""
import sqlite3

from .common import now_z
from .config import settings


def current_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM seq_counter WHERE id = 1").fetchone()
    return int(row["value"]) if row else 0


def oldest_seq(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MIN(seq) AS s FROM changes").fetchone()
    return row["s"] if row and row["s"] is not None else None


def record_change(conn: sqlite3.Connection, change_type: str, entity_id: str, summary: str) -> int:
    """Naikkan seq dan tulis satu baris perubahan. Mengembalikan seq yang baru."""
    conn.execute("UPDATE seq_counter SET value = value + 1 WHERE id = 1")
    seq = current_seq(conn)
    conn.execute("INSERT INTO changes (seq, at, type, entity_id, summary) VALUES (?, ?, ?, ?, ?)",
                 (seq, now_z(), change_type, str(entity_id), summary))
    conn.execute("DELETE FROM changes WHERE seq <= ?", (seq - settings.changes_keep,))
    return seq
