"""Konteks Stok Darah (supporting): ringkasan stok per fasilitas, golongan, rhesus, dan komponen.

Level stok: critical (<= STOCK_CRITICAL_UNITS), low (<= STOCK_LOW_UNITS), sufficient.
Menerbitkan:
- StokDiperbarui (stock.updated) setiap pembaruan, berisi daftar golongan yang sedang menipis
  di fasilitas itu (dipakai Kegiatan Donor).
- StokMenipis (stock.low) untuk setiap item yang baru turun level (dipakai Notifikasi untuk
  mengajak pendonor bergolongan sama).
"""
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import clock, events
from ..common import make_id, not_found, ok, parse_id
from ..config import settings
from ..db import get_conn, register_schema
from ..domain.compatibility import group_key
from ..params import Near, Page, near, page
from ..security import Caller, User, api_key_or_user, require_roles
from . import locations

register_schema("""
CREATE TABLE IF NOT EXISTS blood_stock (
  facility_id INTEGER NOT NULL,
  blood_type TEXT NOT NULL,
  rhesus TEXT NOT NULL,
  component TEXT NOT NULL,
  units INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by INTEGER,
  PRIMARY KEY (facility_id, blood_type, rhesus, component)
);
""")

router = APIRouter(tags=["stok-darah"])
LEVEL_RANK = {"sufficient": 0, "low": 1, "critical": 2}


def level_of(units: int) -> str:
    if units <= settings.stock_critical_units:
        return "critical"
    if units <= settings.stock_low_units:
        return "low"
    return "sufficient"


class StockItemIn(BaseModel):
    blood_type: Literal["a", "b", "ab", "o"]
    rhesus: Literal["positive", "negative"]
    component: Literal["whole_blood", "prc", "platelets", "plasma"]
    units: int = Field(ge=0, le=100000)


class StockUpdateIn(BaseModel):
    items: List[StockItemIn] = Field(min_length=1, max_length=64)


def _low_groups(conn, facility_id: int) -> List[str]:
    rows = conn.execute("SELECT blood_type, rhesus, units FROM blood_stock WHERE facility_id = ?",
                        (facility_id,)).fetchall()
    return sorted({group_key(r["blood_type"], r["rhesus"]) for r in rows if level_of(r["units"]) != "sufficient"})


def apply_update(conn, facility_id: int, items: List[StockItemIn], user_id: Optional[int]) -> int:
    """Antarmuka juga untuk generator mock. Mengembalikan jumlah item yang baru menipis."""
    now = clock.now_iso()
    newly_low = []
    for it in items:
        prev = conn.execute("SELECT units FROM blood_stock WHERE facility_id = ? AND blood_type = ? AND rhesus = ? "
                            "AND component = ?", (facility_id, it.blood_type, it.rhesus, it.component)).fetchone()
        before = level_of(prev["units"]) if prev else "sufficient"
        after = level_of(it.units)
        conn.execute("""
            INSERT INTO blood_stock (facility_id, blood_type, rhesus, component, units, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(facility_id, blood_type, rhesus, component)
            DO UPDATE SET units = excluded.units, updated_at = excluded.updated_at, updated_by = excluded.updated_by
        """, (facility_id, it.blood_type, it.rhesus, it.component, it.units, now, user_id))
        if LEVEL_RANK[after] > LEVEL_RANK[before]:
            newly_low.append((it, after))
    events.publish(conn, "StokDiperbarui", change_type="stock.updated", entity_id=make_id("fac", facility_id),
                   summary="Stok darah diperbarui", facility_id=facility_id,
                   low_groups=_low_groups(conn, facility_id))
    for it, level in newly_low:
        events.publish(conn, "StokMenipis", change_type="stock.low", entity_id=make_id("fac", facility_id),
                       summary=f"Stok {it.blood_type.upper()} {it.rhesus} ({it.component}) {level}",
                       facility_id=facility_id, blood_type=it.blood_type, rhesus=it.rhesus,
                       component=it.component, level=level)
    return len(newly_low)


def _facility_stock(conn, facility: dict, distance_m: Optional[int]) -> Optional[dict]:
    fid = parse_id("fac", facility["id"])
    rows = conn.execute("SELECT * FROM blood_stock WHERE facility_id = ? ORDER BY component, blood_type, rhesus",
                        (fid,)).fetchall()
    if not rows:
        return None
    data = {"facility_id": facility["id"], "facility_name": facility["name"], "lat": facility["lat"],
            "lon": facility["lon"], "updated_at": max(r["updated_at"] for r in rows),
            "items": [{"blood_type": r["blood_type"], "rhesus": r["rhesus"], "component": r["component"],
                       "units": r["units"], "level": level_of(r["units"])} for r in rows]}
    if distance_m is not None:
        data["distance_m"] = distance_m
    return data


@router.get("/blood-stock", summary="Stok darah per fasilitas (untuk aplikasi dan kelompok lain)")
def list_stock(facility_id: Optional[str] = Query(default=None), where: Near = Depends(near),
               level: Optional[Literal["low", "critical"]] = Query(default=None,
                                                                   description="Hanya item dengan level ini atau lebih buruk"),
               pg: Page = Depends(page), _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    if facility_id:
        facility = locations.get_facility(conn, parse_id("fac", facility_id, "Fasilitas"))
        if not facility:
            raise not_found("Fasilitas")
        facilities = [facility]
    else:
        ids = [r["facility_id"] for r in conn.execute("SELECT DISTINCT facility_id FROM blood_stock")]
        facilities = [f for f in (locations.get_facility(conn, i) for i in ids) if f]
    result = []
    for f in facilities:
        if not where.accepts(f["lat"], f["lon"]):
            continue
        entry = _facility_stock(conn, f, where.distance(f["lat"], f["lon"]))
        if entry is None:
            continue
        if level:
            entry["items"] = [i for i in entry["items"] if LEVEL_RANK[i["level"]] >= LEVEL_RANK[level]]
            if not entry["items"]:
                continue
        result.append(entry)
    result.sort(key=lambda x: (x.get("distance_m", 0), x["facility_id"]))
    return ok(conn, pg.slice(result), total=len(result))


@router.put("/blood-stock/{facility_id}", summary="Perbarui stok darah fasilitas (petugas)")
def update_stock(facility_id: str, body: StockUpdateIn, user: User = Depends(require_roles("staff", "admin")),
                 conn=Depends(get_conn)):
    fid = parse_id("fac", facility_id, "Fasilitas")
    facility = locations.get_facility(conn, fid)
    if not facility:
        raise not_found("Fasilitas")
    apply_update(conn, fid, body.items, user.id)
    return ok(conn, _facility_stock(conn, facility, None))
