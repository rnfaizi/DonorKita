"""Konteks Notifikasi (generic): notifikasi di dalam aplikasi.

Conformist terhadap event konteks lain: hanya membaca isi event, tidak membaca tabel mereka.
Push notification (Expo) ditambahkan di sisi aplikasi/layanan integrasi; di core cukup
kotak masuk yang dipoll aplikasi.
"""
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, Query

from .. import clock
from ..common import make_id, not_found, ok, parse_id
from ..db import get_conn, register_schema
from ..params import Page, page
from ..security import User, current_user
from . import donors

register_schema("""
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  ref_id TEXT,
  created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, read_at);
""")

router = APIRouter(tags=["notifikasi"])
STOCK_BROADCAST_LIMIT = 50


def notify(conn, user_ids: Iterable[int], kind: str, title: str, body: str, ref_id: Optional[str] = None) -> None:
    now = clock.now_iso()
    conn.executemany("INSERT INTO notifications (user_id, kind, title, body, ref_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", [(uid, kind, title, body, ref_id, now) for uid in set(user_ids)])


# ---------- reaksi atas event ----------

def on_request_verified(conn, b: dict) -> None:
    notify(conn, [b["requester_id"]], "request.verified", "Permintaan terverifikasi",
           "Permintaan darah Anda sudah diverifikasi. Sistem mulai mencari pendonor.", make_id("req", b["request_id"]))


def on_request_rejected(conn, b: dict) -> None:
    notify(conn, [b["requester_id"]], "request.rejected", "Permintaan ditolak",
           "Permintaan darah Anda ditolak petugas. Lihat detail untuk alasannya.", make_id("req", b["request_id"]))


def on_invitations_issued(conn, b: dict) -> None:
    title = {"emergency": "Darurat: butuh pendonor", "urgent": "Mendesak: butuh pendonor"}.get(
        b.get("urgency"), "Ada yang membutuhkan darah Anda")
    notify(conn, b["donor_user_ids"], "invitation.issued", title,
           f"Golongan darah Anda cocok untuk permintaan di {b['facility_name']}. Buka undangan untuk merespons.",
           make_id("req", b["request_id"]))


def on_donation_recorded(conn, b: dict) -> None:
    notify(conn, [b["donor_user_id"]], "donation.recorded", "Terima kasih sudah mendonor",
           "Donasi Anda tercatat. Cek tanggal Anda boleh mendonor lagi di profil.", b.get("ref_id"))


def on_request_fulfilled(conn, b: dict) -> None:
    notify(conn, [b["requester_id"]], "request.fulfilled", "Kebutuhan darah terpenuhi",
           "Semua kantong yang Anda minta sudah terpenuhi.", make_id("req", b["request_id"]))


def on_request_expired(conn, b: dict) -> None:
    notify(conn, [b["requester_id"]], "request.expired", "Permintaan kedaluwarsa",
           "Batas waktu permintaan sudah lewat sebelum kebutuhan terpenuhi.", make_id("req", b["request_id"]))


def on_stock_low(conn, b: dict) -> None:
    ids = donors.user_ids_by_groups(conn, [(b["blood_type"], b["rhesus"])], STOCK_BROADCAST_LIMIT)
    notify(conn, ids, "stock.low", f"Stok golongan {b['blood_type'].upper()} {b['rhesus']} menipis",
           "Golongan darah Anda sedang dibutuhkan. Lihat kegiatan donor terdekat.", make_id("fac", b["facility_id"]))


# ---------- API ----------

def _out(row) -> dict:
    return {"id": make_id("ntf", row["id"]), "kind": row["kind"], "title": row["title"], "body": row["body"],
            "ref_id": row["ref_id"], "created_at": row["created_at"], "read_at": row["read_at"]}


@router.get("/notifications", summary="Notifikasi saya")
def my_notifications(unread: bool = Query(default=False), pg: Page = Depends(page),
                     user: User = Depends(current_user), conn=Depends(get_conn)):
    sql = "SELECT * FROM notifications WHERE user_id = ?" + (" AND read_at IS NULL" if unread else "")
    rows = conn.execute(sql + " ORDER BY id DESC", (user.id,)).fetchall()
    return ok(conn, [_out(r) for r in pg.slice(rows)], total=len(rows))


@router.post("/notifications/{notification_id}/read", summary="Tandai notifikasi sudah dibaca")
def mark_read(notification_id: str, user: User = Depends(current_user), conn=Depends(get_conn)):
    nid = parse_id("ntf", notification_id, "Notifikasi")
    row = conn.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (nid, user.id)).fetchone()
    if row is None:
        raise not_found("Notifikasi")
    if row["read_at"] is None:
        conn.execute("UPDATE notifications SET read_at = ? WHERE id = ?", (clock.now_iso(), nid))
    return ok(conn, _out(conn.execute("SELECT * FROM notifications WHERE id = ?", (nid,)).fetchone()))
