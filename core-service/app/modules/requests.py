"""Konteks Permintaan Darah (core).

Siklus hidup agregat PermintaanDarah:
  submitted -> verified -> searching -> fulfilled
                       \\-> rejected      \\-> expired / cancelled

- Permintaan dari keluarga pasien (peran requester) harus diverifikasi petugas fasilitas
  dengan mencocokkan nomor formulir rumah sakit (patient_ref). Permintaan yang dibuat
  petugas langsung terverifikasi.
- Identitas pasien tidak disimpan; cukup nomor formulir.

Menerbitkan: PermintaanDiajukan, PermintaanTerverifikasi, PermintaanDitolak,
PermintaanTerpenuhi, PermintaanKedaluwarsa, PermintaanDibatalkan.
Bereaksi pada: SesiPencocokanDimulai (-> searching), DonasiTercatat (kantong terpenuhi).
"""
from collections import defaultdict
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import clock, events
from ..common import conflict, forbidden, invalid, make_id, not_found, ok, parse_id
from ..config import settings
from ..db import get_conn, register_schema
from ..domain import geo
from ..params import Page, bbox, page
from ..security import Caller, User, api_key_or_user, current_user, is_staff, require_roles
from . import locations

register_schema("""
CREATE TABLE IF NOT EXISTS blood_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  requester_id INTEGER NOT NULL,
  patient_ref TEXT NOT NULL,
  facility_id INTEGER NOT NULL,
  facility_name TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  blood_type TEXT NOT NULL,
  rhesus TEXT NOT NULL,
  component TEXT NOT NULL,
  units_needed INTEGER NOT NULL,
  units_fulfilled INTEGER NOT NULL DEFAULT 0,
  urgency TEXT NOT NULL,
  needed_by TEXT NOT NULL,
  status TEXT NOT NULL,
  verified_by INTEGER,
  verified_at TEXT,
  closed_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON blood_requests(status, needed_by);
CREATE INDEX IF NOT EXISTS idx_requests_owner ON blood_requests(requester_id);
""")

router = APIRouter(tags=["permintaan-darah"])
ACTIVE = ("submitted", "verified", "searching")
Status = Literal["submitted", "verified", "searching", "fulfilled", "expired", "cancelled", "rejected"]


class RequestIn(BaseModel):
    facility_id: str = Field(description="ID fasilitas tujuan, mis. fac_1")
    patient_ref: str = Field(min_length=3, max_length=40, description="Nomor formulir permintaan darah RS")
    blood_type: Literal["a", "b", "ab", "o"]
    rhesus: Literal["positive", "negative"]
    component: Literal["whole_blood", "prc", "platelets", "plasma"]
    units: int = Field(ge=1, le=10, description="Jumlah kantong")
    urgency: Literal["normal", "urgent", "emergency"] = "normal"
    needed_by: AwareDatetime = Field(description="Batas waktu, ISO 8601 dengan zona waktu")


class VerifyIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _reason_for_reject(self):
        if self.decision == "reject" and not (self.reason and self.reason.strip()):
            raise ValueError("reason wajib diisi saat menolak permintaan")
        return self


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


def _out(row, include_private: bool) -> dict:
    data = {
        "id": make_id("req", row["id"]),
        "facility_id": make_id("fac", row["facility_id"]),
        "facility_name": row["facility_name"],
        "lat": row["lat"], "lon": row["lon"],
        "blood_type": row["blood_type"], "rhesus": row["rhesus"], "component": row["component"],
        "units_needed": row["units_needed"], "units_fulfilled": row["units_fulfilled"],
        "urgency": row["urgency"], "needed_by": row["needed_by"], "status": row["status"],
        "verified_at": row["verified_at"], "closed_reason": row["closed_reason"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if include_private:
        data["patient_ref"] = row["patient_ref"]
    return data


def _load(conn, request_id: str):
    row = conn.execute("SELECT * FROM blood_requests WHERE id = ?",
                       (parse_id("req", request_id, "Permintaan"),)).fetchone()
    if row is None:
        raise not_found("Permintaan")
    return row


def _can_see(user: User, row) -> bool:
    return is_staff(user) or row["requester_id"] == user.id


def _set_status(conn, rid: int, status: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in ["status", "updated_at", *fields])
    conn.execute(f"UPDATE blood_requests SET {sets} WHERE id = ?",
                 (status, clock.now_iso(), *fields.values(), rid))


def _publish_verified(conn, row) -> None:
    events.publish(conn, "PermintaanTerverifikasi", change_type="request.verified",
                   entity_id=make_id("req", row["id"]), summary="Permintaan darah terverifikasi",
                   request_id=row["id"], requester_id=row["requester_id"],
                   facility_id=row["facility_id"], facility_name=row["facility_name"],
                   lat=row["lat"], lon=row["lon"], blood_type=row["blood_type"], rhesus=row["rhesus"],
                   component=row["component"], units_needed=row["units_needed"],
                   urgency=row["urgency"], needed_by=row["needed_by"])


# ---------- reaksi atas event ----------

def on_matching_started(conn, body: dict) -> None:
    conn.execute("UPDATE blood_requests SET status = 'searching', updated_at = ? "
                 "WHERE id = ? AND status = 'verified'", (clock.now_iso(), body["request_id"]))


def on_donation_recorded(conn, body: dict) -> None:
    rid = body.get("request_id")
    if not rid:
        return
    row = conn.execute("SELECT * FROM blood_requests WHERE id = ?", (rid,)).fetchone()
    if row is None or row["status"] not in ("verified", "searching"):
        return
    filled = row["units_fulfilled"] + 1
    conn.execute("UPDATE blood_requests SET units_fulfilled = ?, updated_at = ? WHERE id = ?",
                 (filled, clock.now_iso(), rid))
    if filled >= row["units_needed"]:
        _set_status(conn, rid, "fulfilled", closed_reason="fulfilled")
        events.publish(conn, "PermintaanTerpenuhi", change_type="request.fulfilled",
                       entity_id=make_id("req", rid), summary="Kebutuhan kantong terpenuhi",
                       request_id=rid, requester_id=row["requester_id"])


# ---------- job ----------

def expire_overdue(conn) -> int:
    now = clock.now_iso()
    rows = conn.execute(f"SELECT * FROM blood_requests WHERE status IN ({','.join('?' * len(ACTIVE))}) "
                        "AND needed_by < ?", (*ACTIVE, now)).fetchall()
    for row in rows:
        _set_status(conn, row["id"], "expired", closed_reason="deadline_passed")
        events.publish(conn, "PermintaanKedaluwarsa", change_type="request.expired",
                       entity_id=make_id("req", row["id"]), summary="Permintaan melewati batas waktu",
                       request_id=row["id"], requester_id=row["requester_id"])
    return len(rows)


# ---------- API ----------

@router.post("/requests", status_code=201, summary="Ajukan permintaan darah")
def create(body: RequestIn, user: User = Depends(require_roles("requester", "staff", "admin")),
           conn=Depends(get_conn)):
    fid = parse_id("fac", body.facility_id, "Fasilitas")
    facility = locations.get_facility(conn, fid)
    if not facility:
        raise not_found("Fasilitas")
    now = clock.utcnow()
    needed_by = body.needed_by.astimezone(now.tzinfo)
    if needed_by <= now:
        raise invalid("needed_by harus di masa depan")
    if needed_by > now + timedelta(days=settings.request_max_days_ahead):
        raise invalid(f"needed_by paling lama {settings.request_max_days_ahead} hari dari sekarang")
    cur = conn.execute("""
        INSERT INTO blood_requests (requester_id, patient_ref, facility_id, facility_name, lat, lon,
          blood_type, rhesus, component, units_needed, urgency, needed_by, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitted', ?, ?)
    """, (user.id, body.patient_ref.strip(), fid, facility["name"], facility["lat"],
          facility["lon"], body.blood_type, body.rhesus, body.component, body.units, body.urgency,
          clock.iso(needed_by), clock.iso(now), clock.iso(now)))
    rid = int(cur.lastrowid)
    events.publish(conn, "PermintaanDiajukan", change_type="request.created",
                   entity_id=make_id("req", rid), summary="Permintaan darah baru diajukan",
                   request_id=rid, requester_id=user.id)
    if is_staff(user):  # dibuat petugas fasilitas: langsung terverifikasi
        _set_status(conn, rid, "verified", verified_by=user.id, verified_at=clock.now_iso())
        _publish_verified(conn, conn.execute("SELECT * FROM blood_requests WHERE id = ?", (rid,)).fetchone())
    row = conn.execute("SELECT * FROM blood_requests WHERE id = ?", (rid,)).fetchone()
    return ok(conn, _out(row, include_private=True))


@router.get("/requests/summary", summary="Ringkasan permintaan aktif per golongan (tanpa data pribadi)")
def summary(box=Depends(bbox), _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM blood_requests WHERE status IN ('verified', 'searching')").fetchall()
    groups: dict = defaultdict(lambda: {"requests": 0, "units_remaining": 0,
                                        "by_urgency": {"normal": 0, "urgent": 0, "emergency": 0}})
    for r in rows:
        if box and not geo.in_bbox(r["lat"], r["lon"], box):
            continue
        g = groups[(r["blood_type"], r["rhesus"], r["component"])]
        g["requests"] += 1
        g["units_remaining"] += max(0, r["units_needed"] - r["units_fulfilled"])
        g["by_urgency"][r["urgency"]] += 1
    data = [{"blood_type": k[0], "rhesus": k[1], "component": k[2], **v} for k, v in sorted(groups.items())]
    return ok(conn, data, total=len(data))


@router.get("/requests/mine", summary="Permintaan yang saya ajukan")
def mine(status: Optional[Status] = Query(default=None), pg: Page = Depends(page),
         user: User = Depends(current_user), conn=Depends(get_conn)):
    sql, args = "SELECT * FROM blood_requests WHERE requester_id = ?", [user.id]
    if status:
        sql, args = sql + " AND status = ?", args + [status]
    rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    return ok(conn, [_out(r, True) for r in pg.slice(rows)], total=len(rows))


@router.get("/requests", summary="Semua permintaan (petugas)")
def list_all(status: Optional[Status] = Query(default=None), facility_id: Optional[str] = Query(default=None),
             pg: Page = Depends(page), user: User = Depends(require_roles("staff", "admin")),
             conn=Depends(get_conn)):
    sql, args = "SELECT * FROM blood_requests WHERE 1 = 1", []
    if status:
        sql, args = sql + " AND status = ?", args + [status]
    if facility_id:
        sql, args = sql + " AND facility_id = ?", args + [parse_id("fac", facility_id, "Fasilitas")]
    rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    return ok(conn, [_out(r, True) for r in pg.slice(rows)], total=len(rows))


@router.get("/requests/{request_id}", summary="Detail permintaan (pemohon atau petugas)")
def get_one(request_id: str, user: User = Depends(current_user), conn=Depends(get_conn)):
    row = _load(conn, request_id)
    if not _can_see(user, row):
        raise not_found("Permintaan")
    return ok(conn, _out(row, True))


@router.patch("/requests/{request_id}/verify", summary="Verifikasi atau tolak permintaan (petugas)")
def verify(request_id: str, body: VerifyIn, user: User = Depends(require_roles("staff", "admin")),
           conn=Depends(get_conn)):
    row = _load(conn, request_id)
    if row["status"] != "submitted":
        raise conflict(f"Permintaan berstatus {row['status']}, tidak dapat diverifikasi")
    if body.decision == "approve":
        _set_status(conn, row["id"], "verified", verified_by=user.id, verified_at=clock.now_iso())
        _publish_verified(conn, conn.execute("SELECT * FROM blood_requests WHERE id = ?", (row["id"],)).fetchone())
    else:
        _set_status(conn, row["id"], "rejected", verified_by=user.id, verified_at=clock.now_iso(),
                    closed_reason=body.reason.strip())
        events.publish(conn, "PermintaanDitolak", change_type="request.rejected",
                       entity_id=make_id("req", row["id"]), summary="Permintaan darah ditolak",
                       request_id=row["id"], requester_id=row["requester_id"])
    row = conn.execute("SELECT * FROM blood_requests WHERE id = ?", (row["id"],)).fetchone()
    return ok(conn, _out(row, True))


@router.patch("/requests/{request_id}/cancel", summary="Batalkan permintaan (pemohon atau petugas)")
def cancel(request_id: str, body: CancelIn, user: User = Depends(current_user), conn=Depends(get_conn)):
    row = _load(conn, request_id)
    if not _can_see(user, row):
        raise not_found("Permintaan")
    if row["requester_id"] != user.id and not is_staff(user):
        raise forbidden()
    if row["status"] not in ACTIVE:
        raise conflict(f"Permintaan berstatus {row['status']}, tidak dapat dibatalkan")
    _set_status(conn, row["id"], "cancelled", closed_reason=body.reason.strip())
    events.publish(conn, "PermintaanDibatalkan", change_type="request.cancelled",
                   entity_id=make_id("req", row["id"]), summary="Permintaan darah dibatalkan",
                   request_id=row["id"], requester_id=row["requester_id"])
    row = conn.execute("SELECT * FROM blood_requests WHERE id = ?", (row["id"],)).fetchone()
    return ok(conn, _out(row, True))
