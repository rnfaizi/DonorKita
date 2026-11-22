"""Domain event sinkron di dalam satu proses.

Nama event mengikuti makalah DDD (bahasa Indonesia, misalnya PermintaanTerverifikasi).
Konteks hilir hanya bereaksi pada isi event, tidak membaca tabel konteks hulu, sehingga
tiap modul kelak dapat dipindah menjadi service sendiri tanpa mengubah modelnya.

Setiap publish() juga mencatat satu perubahan (meta.seq naik, baris baru di /changes).
Tipe perubahan memakai bahasa Inggris huruf kecil bertitik, misalnya "request.verified".
"""
import sqlite3
from collections import defaultdict
from typing import Any, Callable

from .meta import record_change

Handler = Callable[[sqlite3.Connection, dict[str, Any]], None]
_handlers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(event: str, handler: Handler) -> None:
    if handler not in _handlers[event]:
        _handlers[event].append(handler)


def clear() -> None:
    """Untuk pengujian."""
    _handlers.clear()


def publish(conn: sqlite3.Connection, event: str, *, change_type: str, entity_id: str,
            summary: str, **payload: Any) -> int:
    """Terbitkan event domain. Mengembalikan seq perubahan yang tercatat."""
    seq = record_change(conn, change_type, entity_id, summary)
    body = {"event": event, "entity_id": str(entity_id), "seq": seq, **payload}
    for handler in list(_handlers[event]):
        handler(conn, body)
    return seq
