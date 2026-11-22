"""Konteks Lokasi (generic): fasilitas kesehatan (UDD, rumah sakit, BDRS) dan koordinat.

Konteks lain memakai antarmuka get_facility() dan tidak membaca tabel facilities langsung.
Data OpenStreetMap (Overpass) nanti masuk lewat adapter di app/adapters/ (ACL); untuk saat ini
fasilitas diisi oleh admin atau data uji (seed).
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import clock, events
from ..common import make_id, not_found, ok, parse_id
from ..db import get_conn, register_schema
from ..params import Near, Page, near, page
from ..security import Caller, User, api_key_or_user, require_roles

register_schema("""
CREATE TABLE IF NOT EXISTS facilities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('udd', 'hospital', 'bdrs')),
  address TEXT NOT NULL DEFAULT '',
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  created_at TEXT NOT NULL
);
""")

router = APIRouter(tags=["lokasi"])
FacilityKind = Literal["udd", "hospital", "bdrs"]


def _out(row, distance_m: Optional[int] = None) -> dict:
    data = {"id": make_id("fac", row["id"]), "name": row["name"], "kind": row["kind"],
            "address": row["address"], "lat": row["lat"], "lon": row["lon"]}
    if distance_m is not None:
        data["distance_m"] = distance_m
    return data


# ---------- antarmuka untuk konteks lain ----------

def get_facility(conn, facility_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM facilities WHERE id = ?", (facility_id,)).fetchone()
    return _out(row) if row else None


def create_facility(conn, name: str, kind: str, address: str, lat: float, lon: float) -> int:
    cur = conn.execute("INSERT INTO facilities (name, kind, address, lat, lon, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (name, kind, address, lat, lon, clock.now_iso()))
    return int(cur.lastrowid)


# ---------- API ----------

class FacilityIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    kind: FacilityKind
    address: str = Field(default="", max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@router.get("/facilities", summary="Daftar fasilitas (UDD, rumah sakit, BDRS)")
def list_facilities(kind: Optional[FacilityKind] = Query(default=None), where: Near = Depends(near),
                    pg: Page = Depends(page), _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM facilities ORDER BY id").fetchall()
    items = [_out(r, where.distance(r["lat"], r["lon"])) for r in rows
             if (kind is None or r["kind"] == kind) and where.accepts(r["lat"], r["lon"])]
    if where.active:
        items.sort(key=lambda x: x["distance_m"])
    return ok(conn, pg.slice(items), total=len(items))


@router.get("/facilities/{facility_id}", summary="Detail fasilitas")
def get_one(facility_id: str, _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    data = get_facility(conn, parse_id("fac", facility_id, "Fasilitas"))
    if not data:
        raise not_found("Fasilitas")
    return ok(conn, data)


@router.post("/facilities", status_code=201, summary="Tambah fasilitas (admin)")
def create(body: FacilityIn, user: User = Depends(require_roles("admin")), conn=Depends(get_conn)):
    fid = create_facility(conn, body.name, body.kind, body.address, body.lat, body.lon)
    events.publish(conn, "FasilitasDitambahkan", change_type="facility.created",
                   entity_id=make_id("fac", fid), summary="Fasilitas baru ditambahkan")
    return ok(conn, get_facility(conn, fid))
