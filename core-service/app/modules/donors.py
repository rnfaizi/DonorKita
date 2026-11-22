"""Konteks Pendonor & Kelayakan (supporting).

Satu-satunya sumber kebenaran tentang boleh tidaknya seseorang diundang mendonor.
Konteks lain memakai antarmuka check_eligibility(), find_eligible_candidates(), dan
user_ids_by_groups(); mereka tidak membaca tabel donors secara langsung.

Bereaksi pada event:
- DonasiTercatat   -> menambah riwayat donasi (jeda berikutnya ikut berubah)
- PendonorDitunda  -> mencatat penundaan sementara
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, List, Literal, Optional, Set

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import clock, events
from ..common import invalid, make_id, not_found, ok
from ..config import settings
from ..db import get_conn, register_schema
from ..domain import eligibility as elig
from ..domain import geo
from ..domain.compatibility import BloodGroup
from ..security import User, require_roles

register_schema("""
CREATE TABLE IF NOT EXISTS donors (
  user_id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  blood_type TEXT NOT NULL CHECK (blood_type IN ('a', 'b', 'ab', 'o')),
  rhesus TEXT NOT NULL CHECK (rhesus IN ('positive', 'negative')),
  birth_date TEXT NOT NULL,
  weight_kg REAL NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  available INTEGER NOT NULL DEFAULT 1,
  screening_at TEXT,
  screening_flagged INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_donors_group ON donors(blood_type, rhesus, available);

CREATE TABLE IF NOT EXISTS donation_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  donated_at TEXT NOT NULL,
  facility_id INTEGER,
  source TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  UNIQUE (source, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_history_user ON donation_history(user_id, donated_at);

CREATE TABLE IF NOT EXISTS deferrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  until TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deferrals_user ON deferrals(user_id, until);
""")

router = APIRouter(tags=["pendonor"])
DonorOnly = require_roles("donor")


def policy() -> elig.EligibilityPolicy:
    return elig.EligibilityPolicy(
        min_age=settings.elig_min_age, max_age_first=settings.elig_max_age_first,
        max_age_repeat=settings.elig_max_age_repeat, min_weight_kg=settings.elig_min_weight_kg,
        interval_days=settings.donation_interval_days, max_per_year=settings.max_donations_per_year,
        screening_valid_days=settings.screening_valid_days)


# ---------- fakta & kelayakan ----------

def _facts(conn, row) -> elig.DonorFacts:
    uid = row["user_id"]
    dates = [clock.parse_iso(r["donated_at"]) for r in
             conn.execute("SELECT donated_at FROM donation_history WHERE user_id = ?", (uid,))]
    deferred = conn.execute("SELECT MAX(until) AS u FROM deferrals WHERE user_id = ?", (uid,)).fetchone()["u"]
    return elig.DonorFacts(
        birth_date=date.fromisoformat(row["birth_date"]), weight_kg=row["weight_kg"],
        donation_dates=dates, deferred_until=clock.parse_iso_opt(deferred),
        screening_at=clock.parse_iso_opt(row["screening_at"]),
        screening_flagged=bool(row["screening_flagged"]))


def _eligibility_out(e: elig.Eligibility) -> dict:
    return {"eligible": e.eligible, "reasons": e.reasons,
            "next_eligible_at": clock.iso(e.next_eligible_at) if e.next_eligible_at else None,
            "donations_last_365d": e.donations_last_365d}


def check_eligibility(conn, user_id: int, on: Optional[datetime] = None) -> elig.Eligibility:
    """Antarmuka untuk konteks lain (Kegiatan Donor, Pencocokan)."""
    row = conn.execute("SELECT * FROM donors WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return elig.Eligibility(False, ["profile_incomplete"])
    return elig.evaluate(_facts(conn, row), on or clock.utcnow(), policy())


@dataclass(frozen=True)
class Candidate:
    user_id: int
    blood_type: str
    rhesus: str
    distance_m: float
    days_since_eligible: float


def find_eligible_candidates(conn, *, groups: Iterable[BloodGroup], lat: float, lon: float,
                             radius_m: float, exclude: Set[int], on: Optional[datetime] = None) -> List[Candidate]:
    """Pendonor yang bersedia, golongannya termasuk `groups`, berada dalam radius, dan layak hari ini."""
    on = on or clock.utcnow()
    wanted = set(groups)
    if not wanted:
        return []
    clauses = " OR ".join("(blood_type = ? AND rhesus = ?)" for _ in wanted)
    args: List = [x for g in wanted for x in g]
    rows = conn.execute(f"SELECT * FROM donors WHERE available = 1 AND ({clauses})", args).fetchall()
    result: List[Candidate] = []
    for row in rows:
        if row["user_id"] in exclude:
            continue
        distance = geo.haversine_m(lat, lon, row["lat"], row["lon"])
        if distance > radius_m:
            continue
        facts = _facts(conn, row)
        e = elig.evaluate(facts, on, policy())
        if not e.eligible:
            continue
        last = max(facts.donation_dates) if facts.donation_dates else None
        since = (on - (last + timedelta(days=settings.donation_interval_days))) if last else timedelta(days=90)
        result.append(Candidate(row["user_id"], row["blood_type"], row["rhesus"], distance,
                                max(0.0, since.total_seconds() / 86400)))
    return result


def user_ids_by_groups(conn, groups: Iterable[BloodGroup], limit: int = 50) -> List[int]:
    wanted = set(groups)
    if not wanted:
        return []
    clauses = " OR ".join("(blood_type = ? AND rhesus = ?)" for _ in wanted)
    args: List = [x for g in wanted for x in g] + [limit]
    rows = conn.execute(f"SELECT user_id FROM donors WHERE available = 1 AND ({clauses}) "
                        "ORDER BY updated_at DESC LIMIT ?", args).fetchall()
    return [r["user_id"] for r in rows]


# ---------- reaksi atas event ----------

def on_donation_recorded(conn, body: dict) -> None:
    conn.execute("INSERT OR IGNORE INTO donation_history (user_id, donated_at, facility_id, source, ref_id) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (body["donor_user_id"], body["donated_at"], body.get("facility_id"),
                  body["source"], body["ref_id"]))


def on_donor_deferred(conn, body: dict) -> None:
    now = clock.utcnow()
    until = now + timedelta(days=settings.deferral_days)
    conn.execute("INSERT INTO deferrals (user_id, until, reason, created_at) VALUES (?, ?, ?, ?)",
                 (body["donor_user_id"], clock.iso(until), body.get("reason", "hasil pemeriksaan"), clock.iso(now)))


# ---------- API ----------

class DonorProfileIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    blood_type: Literal["a", "b", "ab", "o"]
    rhesus: Literal["positive", "negative"]
    birth_date: date
    weight_kg: float = Field(ge=30, le=250)
    lat: float = Field(ge=-90, le=90, description="Perkiraan lokasi domisili")
    lon: float = Field(ge=-180, le=180)
    available: bool = Field(default=True, description="Bersedia menerima undangan donor")


class ScreeningIn(BaseModel):
    feeling_healthy: bool
    fever_last_2_weeks: bool
    antibiotics_last_week: bool
    tattoo_or_piercing_last_6_months: bool
    pregnant_or_breastfeeding: bool
    transfusion_last_year: bool


def _profile_out(conn, row) -> dict:
    e = elig.evaluate(_facts(conn, row), clock.utcnow(), policy())
    return {"user_id": str(row["user_id"]), "display_name": row["display_name"],
            "blood_type": row["blood_type"], "rhesus": row["rhesus"],
            "birth_date": row["birth_date"], "weight_kg": row["weight_kg"],
            "lat": row["lat"], "lon": row["lon"], "available": bool(row["available"]),
            "screening_at": row["screening_at"], "updated_at": row["updated_at"],
            "eligibility": _eligibility_out(e)}


def _row(conn, user_id: int):
    row = conn.execute("SELECT * FROM donors WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise not_found("Profil pendonor")
    return row


@router.get("/donors/me", summary="Profil pendonor saya beserta status kelayakan")
def get_me(user: User = Depends(DonorOnly), conn=Depends(get_conn)):
    return ok(conn, _profile_out(conn, _row(conn, user.id)))


@router.put("/donors/me", summary="Buat atau perbarui profil pendonor")
def put_me(body: DonorProfileIn, user: User = Depends(DonorOnly), conn=Depends(get_conn)):
    if body.birth_date > clock.today():
        raise invalid("birth_date tidak boleh di masa depan")
    now = clock.now_iso()
    conn.execute("""
        INSERT INTO donors (user_id, display_name, blood_type, rhesus, birth_date, weight_kg, lat, lon,
                            available, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name,
          blood_type = excluded.blood_type, rhesus = excluded.rhesus, birth_date = excluded.birth_date,
          weight_kg = excluded.weight_kg, lat = excluded.lat, lon = excluded.lon,
          available = excluded.available, updated_at = excluded.updated_at
    """, (user.id, body.display_name, body.blood_type, body.rhesus, body.birth_date.isoformat(),
          body.weight_kg, body.lat, body.lon, int(body.available), now, now))
    events.publish(conn, "ProfilPendonorDiperbarui", change_type="donor.updated",
                   entity_id=make_id("don", user.id), summary="Profil pendonor diperbarui")
    return ok(conn, _profile_out(conn, _row(conn, user.id)))


@router.post("/donors/me/screening", summary="Kirim kuesioner pra-skrining")
def screening(body: ScreeningIn, user: User = Depends(DonorOnly), conn=Depends(get_conn)):
    _row(conn, user.id)
    flagged = (not body.feeling_healthy) or any([
        body.fever_last_2_weeks, body.antibiotics_last_week, body.tattoo_or_piercing_last_6_months,
        body.pregnant_or_breastfeeding, body.transfusion_last_year])
    now = clock.now_iso()
    conn.execute("UPDATE donors SET screening_at = ?, screening_flagged = ?, updated_at = ? WHERE user_id = ?",
                 (now, int(flagged), now, user.id))
    events.publish(conn, "PraSkriningDikirim", change_type="donor.updated",
                   entity_id=make_id("don", user.id), summary="Pra-skrining pendonor diperbarui")
    return ok(conn, _eligibility_out(check_eligibility(conn, user.id)))


@router.get("/donors/me/eligibility", summary="Status kelayakan saya")
def my_eligibility(user: User = Depends(DonorOnly), conn=Depends(get_conn)):
    _row(conn, user.id)
    return ok(conn, _eligibility_out(check_eligibility(conn, user.id)))


@router.get("/donors/me/history", summary="Riwayat donasi saya")
def my_history(user: User = Depends(DonorOnly), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM donation_history WHERE user_id = ? ORDER BY donated_at DESC",
                        (user.id,)).fetchall()
    data = [{"donated_at": r["donated_at"], "facility_id": make_id("fac", r["facility_id"]),
             "source": r["source"]} for r in rows]
    return ok(conn, data, total=len(data))
