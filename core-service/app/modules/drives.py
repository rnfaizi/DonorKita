"""Konteks Kegiatan Donor (supporting): jadwal kegiatan, slot per jam, reservasi.

Data kegiatan dibuka untuk kelompok lain lewat GET /donation-events. KotaSiaga memetakan
setiap kegiatan menjadi TitikTujuan (id, name, lat, lon, opens_at, closes_at) untuk rute aman.

Reservasi meminta pemeriksaan kelayakan ke konteks Pendonor & Kelayakan (customer-supplier).
Bereaksi pada StokDiperbarui: menandai golongan darah yang sedang dibutuhkan pada kegiatan
di fasilitas yang sama (needed_blood_types).
"""
import json
import secrets
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import clock, events
from ..common import conflict, invalid, make_id, not_found, ok, parse_id
from ..config import settings
from ..db import get_conn, register_schema
from ..params import Near, Page, near, page
from ..security import Caller, User, api_key_or_user, require_roles
from . import donors, locations

register_schema("""
CREATE TABLE IF NOT EXISTS donation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  organizer TEXT NOT NULL,
  facility_id INTEGER,
  address TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  opens_at TEXT NOT NULL,
  closes_at TEXT NOT NULL,
  capacity_per_hour INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled',
  needed_types TEXT NOT NULL DEFAULT '[]',
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON donation_events(status, closes_at);

CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES donation_events(id),
  user_id INTEGER NOT NULL,
  slot_start TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',
  code TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_event ON reservations(event_id, status);
CREATE INDEX IF NOT EXISTS idx_res_user ON reservations(user_id, status);
""")

router = APIRouter(tags=["kegiatan-donor"])
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------- slot ----------

def slot_starts(opens_at: datetime, closes_at: datetime) -> List[datetime]:
    slots, t = [], opens_at
    while t < closes_at:
        slots.append(t)
        t += timedelta(hours=1)
    return slots


def _active_reservations(conn, event_id: int) -> List:
    return conn.execute("SELECT slot_start FROM reservations WHERE event_id = ? AND status != 'cancelled'",
                        (event_id,)).fetchall()


def _event_out(conn, row, distance_m: Optional[int] = None, with_slots: bool = False) -> dict:
    opens, closes = clock.parse_iso(row["opens_at"]), clock.parse_iso(row["closes_at"])
    starts = slot_starts(opens, closes)
    taken = [r["slot_start"] for r in _active_reservations(conn, row["id"])]
    total = len(starts) * row["capacity_per_hour"]
    data = {
        "id": make_id("evt", row["id"]), "name": row["name"], "organizer": row["organizer"],
        "facility_id": make_id("fac", row["facility_id"]), "address": row["address"],
        "lat": row["lat"], "lon": row["lon"], "opens_at": row["opens_at"], "closes_at": row["closes_at"],
        "slots_total": total, "slots_free": max(0, total - len(taken)),
        "capacity_per_hour": row["capacity_per_hour"], "status": row["status"],
        "needed_blood_types": json.loads(row["needed_types"]), "updated_at": row["updated_at"],
    }
    if distance_m is not None:
        data["distance_m"] = distance_m
    if with_slots:
        data["slots"] = [{"slot_start": clock.iso(s), "capacity": row["capacity_per_hour"],
                          "reserved": taken.count(clock.iso(s)),
                          "free": max(0, row["capacity_per_hour"] - taken.count(clock.iso(s)))} for s in starts]
    return data


def _load_event(conn, event_id: str):
    row = conn.execute("SELECT * FROM donation_events WHERE id = ?",
                       (parse_id("evt", event_id, "Kegiatan donor"),)).fetchone()
    if row is None:
        raise not_found("Kegiatan donor")
    return row


def _new_code(conn) -> str:
    while True:
        code = "R" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        if not conn.execute("SELECT 1 FROM reservations WHERE code = ? AND status = 'reserved'", (code,)).fetchone():
            return code


def _slots_changed(conn, event_id: int, summary: str, **payload) -> None:
    events.publish(conn, "SlotKegiatanBerubah", change_type="event.slots_changed",
                   entity_id=make_id("evt", event_id), summary=summary, event_id=event_id, **payload)


# ---------- reaksi atas event ----------

def on_stock_updated(conn, body: dict) -> None:
    conn.execute("UPDATE donation_events SET needed_types = ?, updated_at = ? "
                 "WHERE facility_id = ? AND status = 'scheduled'",
                 (json.dumps(sorted(body.get("low_groups", []))), clock.now_iso(), body["facility_id"]))


# ---------- antarmuka untuk mock/generator ----------

def create_event(conn, *, name: str, organizer: str, facility_id: Optional[int], address: str, lat: float,
                 lon: float, opens_at: datetime, closes_at: datetime, capacity_per_hour: int, created_by: int) -> int:
    now = clock.now_iso()
    cur = conn.execute("""
        INSERT INTO donation_events (name, organizer, facility_id, address, lat, lon, opens_at, closes_at,
          capacity_per_hour, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, organizer, facility_id, address, lat, lon, clock.iso(opens_at), clock.iso(closes_at),
          capacity_per_hour, created_by, now, now))
    eid = int(cur.lastrowid)
    events.publish(conn, "KegiatanDonorDibuat", change_type="event.created", entity_id=make_id("evt", eid),
                   summary="Kegiatan donor baru dijadwalkan", event_id=eid)
    return eid


# ---------- API ----------

class EventIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    organizer: str = Field(min_length=2, max_length=120)
    facility_id: Optional[str] = Field(default=None, description="Opsional; koordinat diambil dari fasilitas")
    address: str = Field(default="", max_length=200)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    capacity_per_hour: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _check(self):
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at harus setelah opens_at")
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat dan lon harus diisi bersama")
        if self.facility_id is None and self.lat is None:
            raise ValueError("isi facility_id atau lat dan lon")
        return self


class ReservationIn(BaseModel):
    slot_start: AwareDatetime


class OutcomeIn(BaseModel):
    outcome: Literal["donated", "deferred", "no_show"]


def _reservation_out(row, event_row=None) -> dict:
    data = {"id": make_id("res", row["id"]), "event_id": make_id("evt", row["event_id"]),
            "slot_start": row["slot_start"], "status": row["status"], "created_at": row["created_at"]}
    if row["status"] == "reserved":
        data["code"] = row["code"]
    if event_row is not None:
        data["event_name"] = event_row["name"]
        data["lat"], data["lon"] = event_row["lat"], event_row["lon"]
    return data


@router.get("/donation-events", summary="Kegiatan donor terdekat (untuk aplikasi dan kelompok lain)")
def list_events(where: Near = Depends(near),
                from_: Optional[AwareDatetime] = Query(default=None, alias="from",
                                                       description="Awal rentang (default: sekarang)"),
                to: Optional[AwareDatetime] = Query(default=None, description="Akhir rentang"),
                pg: Page = Depends(page), _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    start = clock.iso(from_) if from_ else clock.now_iso()
    sql, args = "SELECT * FROM donation_events WHERE status = 'scheduled' AND closes_at >= ?", [start]
    if to:
        sql, args = sql + " AND opens_at <= ?", args + [clock.iso(to)]
    rows = conn.execute(sql + " ORDER BY opens_at, id", args).fetchall()
    items = [_event_out(conn, r, where.distance(r["lat"], r["lon"])) for r in rows
             if where.accepts(r["lat"], r["lon"])]
    if where.active:
        items.sort(key=lambda x: (x["distance_m"], x["opens_at"]))
    return ok(conn, pg.slice(items), total=len(items))


@router.get("/donation-events/{event_id}", summary="Detail kegiatan donor beserta slot")
def get_event(event_id: str, _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    return ok(conn, _event_out(conn, _load_event(conn, event_id), with_slots=True))


@router.post("/donation-events", status_code=201, summary="Jadwalkan kegiatan donor (petugas)")
def create(body: EventIn, user: User = Depends(require_roles("staff", "admin")), conn=Depends(get_conn)):
    fid, lat, lon, address = None, body.lat, body.lon, body.address
    if body.facility_id:
        fid = parse_id("fac", body.facility_id, "Fasilitas")
        facility = locations.get_facility(conn, fid)
        if not facility:
            raise not_found("Fasilitas")
        if lat is None:
            lat, lon = facility["lat"], facility["lon"]
        address = address or facility["address"]
    opens, closes = body.opens_at.astimezone(clock.utcnow().tzinfo), body.closes_at.astimezone(clock.utcnow().tzinfo)
    if closes - opens > timedelta(hours=settings.event_max_hours):
        raise invalid(f"Durasi kegiatan paling lama {settings.event_max_hours} jam")
    if closes <= clock.utcnow():
        raise invalid("Kegiatan harus berakhir di masa depan")
    eid = create_event(conn, name=body.name, organizer=body.organizer, facility_id=fid, address=address,
                       lat=lat, lon=lon, opens_at=opens, closes_at=closes,
                       capacity_per_hour=body.capacity_per_hour, created_by=user.id)
    return ok(conn, _event_out(conn, _load_event(conn, make_id("evt", eid)), with_slots=True))


@router.patch("/donation-events/{event_id}/cancel", summary="Batalkan kegiatan donor (petugas)")
def cancel_event(event_id: str, user: User = Depends(require_roles("staff", "admin")), conn=Depends(get_conn)):
    row = _load_event(conn, event_id)
    if row["status"] != "scheduled":
        raise conflict("Kegiatan sudah dibatalkan")
    now = clock.now_iso()
    conn.execute("UPDATE donation_events SET status = 'cancelled', updated_at = ? WHERE id = ?", (now, row["id"]))
    conn.execute("UPDATE reservations SET status = 'cancelled', updated_at = ? "
                 "WHERE event_id = ? AND status = 'reserved'", (now, row["id"]))
    events.publish(conn, "KegiatanDonorDibatalkan", change_type="event.cancelled",
                   entity_id=make_id("evt", row["id"]), summary="Kegiatan donor dibatalkan", event_id=row["id"])
    return ok(conn, _event_out(conn, _load_event(conn, event_id)))


@router.post("/donation-events/{event_id}/reservations", status_code=201, summary="Pesan slot kegiatan (pendonor)")
def reserve(event_id: str, body: ReservationIn, user: User = Depends(require_roles("donor")),
            conn=Depends(get_conn)):
    row = _load_event(conn, event_id)
    if row["status"] != "scheduled":
        raise conflict("Kegiatan tidak aktif")
    slot = body.slot_start.astimezone(clock.utcnow().tzinfo).replace(microsecond=0)
    starts = slot_starts(clock.parse_iso(row["opens_at"]), clock.parse_iso(row["closes_at"]))
    if slot not in starts:
        raise invalid("slot_start tidak sesuai jadwal slot kegiatan")
    if slot + timedelta(hours=1) <= clock.utcnow():
        raise conflict("Slot sudah lewat")
    if conn.execute("SELECT 1 FROM reservations WHERE event_id = ? AND user_id = ? AND status = 'reserved'",
                    (row["id"], user.id)).fetchone():
        raise conflict("Anda sudah memiliki reservasi di kegiatan ini")
    taken = conn.execute("SELECT COUNT(*) AS c FROM reservations WHERE event_id = ? AND slot_start = ? "
                         "AND status != 'cancelled'", (row["id"], clock.iso(slot))).fetchone()["c"]
    if taken >= row["capacity_per_hour"]:
        raise conflict("Slot sudah penuh")
    e = donors.check_eligibility(conn, user.id, on=slot)
    if not e.eligible:
        raise conflict("Anda belum memenuhi syarat pada tanggal tersebut: " + ", ".join(e.reasons))
    now = clock.now_iso()
    cur = conn.execute("INSERT INTO reservations (event_id, user_id, slot_start, code, created_at, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (row["id"], user.id, clock.iso(slot), _new_code(conn), now, now))
    _slots_changed(conn, row["id"], "Slot kegiatan berkurang")
    res = conn.execute("SELECT * FROM reservations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return ok(conn, _reservation_out(res, row))


@router.get("/reservations/mine", summary="Reservasi saya")
def my_reservations(user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM reservations WHERE user_id = ? ORDER BY slot_start DESC", (user.id,)).fetchall()
    data = [_reservation_out(r, conn.execute("SELECT * FROM donation_events WHERE id = ?",
                                             (r["event_id"],)).fetchone()) for r in rows]
    return ok(conn, data, total=len(data))


@router.delete("/reservations/{reservation_id}", summary="Batalkan reservasi saya")
def cancel_reservation(reservation_id: str, user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM reservations WHERE id = ?",
                       (parse_id("res", reservation_id, "Reservasi"),)).fetchone()
    if row is None or row["user_id"] != user.id:
        raise not_found("Reservasi")
    if row["status"] != "reserved":
        raise conflict(f"Reservasi berstatus {row['status']}")
    conn.execute("UPDATE reservations SET status = 'cancelled', updated_at = ? WHERE id = ?",
                 (clock.now_iso(), row["id"]))
    _slots_changed(conn, row["event_id"], "Slot kegiatan bertambah")
    return ok(conn, _reservation_out(conn.execute("SELECT * FROM reservations WHERE id = ?", (row["id"],)).fetchone()))


@router.get("/donation-events/{event_id}/reservations", summary="Daftar reservasi kegiatan (petugas)")
def event_reservations(event_id: str, user: User = Depends(require_roles("staff", "admin")), conn=Depends(get_conn)):
    row = _load_event(conn, event_id)
    rows = conn.execute("SELECT * FROM reservations WHERE event_id = ? ORDER BY slot_start, id", (row["id"],)).fetchall()
    data = [{**_reservation_out(r), "user_id": str(r["user_id"]), "code": r["code"]} for r in rows]
    return ok(conn, data, total=len(data))


@router.post("/reservations/{reservation_id}/outcome", summary="Catat hasil kedatangan reservasi (petugas)")
def reservation_outcome(reservation_id: str, body: OutcomeIn, user: User = Depends(require_roles("staff", "admin")),
                        conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM reservations WHERE id = ?",
                       (parse_id("res", reservation_id, "Reservasi"),)).fetchone()
    if row is None:
        raise not_found("Reservasi")
    if row["status"] != "reserved":
        raise conflict(f"Reservasi berstatus {row['status']}")
    event_row = conn.execute("SELECT * FROM donation_events WHERE id = ?", (row["event_id"],)).fetchone()
    now = clock.now_iso()
    conn.execute("UPDATE reservations SET status = ?, updated_at = ? WHERE id = ?", (body.outcome, now, row["id"]))
    if body.outcome == "donated":
        events.publish(conn, "DonasiTercatat", change_type="donation.recorded",
                       entity_id=make_id("evt", row["event_id"]), summary="Donasi tercatat di kegiatan donor",
                       request_id=None, donor_user_id=row["user_id"], facility_id=event_row["facility_id"],
                       donated_at=now, source="reservation", ref_id=make_id("res", row["id"]))
    elif body.outcome == "deferred":
        events.publish(conn, "PendonorDitunda", change_type="donor.deferred",
                       entity_id=make_id("evt", row["event_id"]), summary="Pendonor ditunda setelah pemeriksaan",
                       donor_user_id=row["user_id"], reason="hasil pemeriksaan")
    else:
        _slots_changed(conn, row["event_id"], "Peserta kegiatan tidak hadir")
    return ok(conn, _reservation_out(conn.execute("SELECT * FROM reservations WHERE id = ?", (row["id"],)).fetchone()))
