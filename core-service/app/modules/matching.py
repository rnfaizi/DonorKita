"""Konteks Pencocokan (core).

Saat menerima PermintaanTerverifikasi, konteks ini membuka SesiPencocokan dengan salinan data
permintaan dari isi event (tidak membaca tabel blood_requests), lalu mengundang kandidat secara
bertahap:
1. Golongan yang boleh diundang dari KebijakanKompatibilitas (domain/compatibility.py).
2. Kandidat layak dalam radius dari konteks Pendonor & Kelayakan (find_eligible_candidates).
3. Urutan dari PenilaiPrioritas (domain/scoring.py); keandalan dihitung dari undangan sendiri.
4. Ukuran batch = sisa kantong x faktor urgensi; radius diperluas (5 -> 10 -> 20 km) bila
   pendonor yang bersedia belum cukup setelah waktu tunggu eskalasi.

Status undangan: invited -> accepted / declined / expired
                 accepted -> donated / deferred / no_show / cancelled

Menerbitkan: SesiPencocokanDimulai, UndanganDiterbitkan, UndanganDiterima, UndanganDitolak,
SesiDiperluas, DonasiTercatat, PendonorDitunda.
Bereaksi pada: PermintaanTerverifikasi, PermintaanTerpenuhi/Kedaluwarsa/Dibatalkan.
"""
import math
import secrets
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import clock, events
from ..common import conflict, make_id, not_found, ok, parse_id
from ..config import settings
from ..db import get_conn, register_schema
from ..domain import compatibility, scoring
from ..params import Page, page
from ..security import User, current_user, is_staff, require_roles
from . import donors

register_schema("""
CREATE TABLE IF NOT EXISTS match_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL UNIQUE,
  requester_id INTEGER NOT NULL,
  facility_id INTEGER NOT NULL,
  facility_name TEXT NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  blood_type TEXT NOT NULL,
  rhesus TEXT NOT NULL,
  component TEXT NOT NULL,
  units_needed INTEGER NOT NULL,
  urgency TEXT NOT NULL,
  needed_by TEXT NOT NULL,
  radius_step INTEGER NOT NULL DEFAULT 0,
  radius_m INTEGER NOT NULL,
  round INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'open',
  close_reason TEXT,
  opened_at TEXT NOT NULL,
  last_escalated_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS invitations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES match_sessions(id),
  request_id INTEGER NOT NULL,
  donor_user_id INTEGER NOT NULL,
  donor_blood_type TEXT NOT NULL,
  donor_rhesus TEXT NOT NULL,
  distance_m INTEGER NOT NULL,
  score REAL NOT NULL,
  round INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'invited',
  code TEXT NOT NULL,
  invited_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  responded_at TEXT,
  outcome_at TEXT,
  outcome_by INTEGER,
  UNIQUE (session_id, donor_user_id)
);
CREATE INDEX IF NOT EXISTS idx_inv_donor ON invitations(donor_user_id, status);
CREATE INDEX IF NOT EXISTS idx_inv_code ON invitations(code);
""")

router = APIRouter(tags=["pencocokan"])
PENDING = ("invited", "accepted")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # tanpa 0/O/1/I agar mudah dibacakan


# ---------- bantuan ----------

def _new_code(conn) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        hit = conn.execute("SELECT 1 FROM invitations WHERE code = ? AND status IN ('invited', 'accepted')",
                           (code,)).fetchone()
        if not hit:
            return code


def _counts(conn, session_id: int) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) AS c FROM invitations WHERE session_id = ? GROUP BY status",
                        (session_id,)).fetchall()
    counts = {s: 0 for s in ("invited", "accepted", "declined", "expired", "donated", "deferred",
                             "no_show", "cancelled")}
    counts.update({r["status"]: r["c"] for r in rows})
    return counts


def _reliability_inputs(conn, user_id: int) -> tuple:
    row = conn.execute("SELECT SUM(status = 'donated') AS d, COUNT(*) AS n FROM invitations "
                       "WHERE donor_user_id = ? AND status NOT IN ('cancelled', 'invited')",
                       (user_id,)).fetchone()
    return int(row["d"] or 0), int(row["n"] or 0)


def invite_more(conn, session) -> int:
    """Undang kandidat tambahan sampai jumlah undangan tertunda mencapai target batch."""
    if session["status"] != "open":
        return 0
    counts = _counts(conn, session["id"])
    remaining = session["units_needed"] - counts["donated"]
    if remaining <= 0:
        return 0
    factor = settings.match_batch_factor.get(session["urgency"], 2.0)
    want = math.ceil(remaining * factor) - (counts["invited"] + counts["accepted"])
    if want <= 0:
        return 0
    already = {r["donor_user_id"] for r in
               conn.execute("SELECT donor_user_id FROM invitations WHERE session_id = ?", (session["id"],))}
    groups = compatibility.compatible_donor_groups(session["blood_type"], session["rhesus"], session["component"])
    candidates = donors.find_eligible_candidates(conn, groups=groups, lat=session["lat"], lon=session["lon"],
                                                 radius_m=session["radius_m"], exclude=already)
    scored = []
    for c in candidates:
        donated, invited = _reliability_inputs(conn, c.user_id)
        facts = scoring.CandidateFacts(
            distance_m=c.distance_m, radius_m=session["radius_m"], donated_count=donated, invited_count=invited,
            exact_match=(c.blood_type, c.rhesus) == (session["blood_type"], session["rhesus"]),
            days_since_eligible=c.days_since_eligible)
        scored.append((scoring.score(facts, settings.score_weights), c))
    scored.sort(key=lambda x: (-x[0], x[1].distance_m))
    chosen = scored[:want]
    if not chosen:
        return 0
    now = clock.utcnow()
    expires = min(now + clock.scaled_minutes(settings.invite_ttl_minutes.get(session["urgency"], 120)),
                  clock.parse_iso(session["needed_by"]))
    invited_ids: List[int] = []
    for score, c in chosen:
        conn.execute("""
            INSERT INTO invitations (session_id, request_id, donor_user_id, donor_blood_type, donor_rhesus,
              distance_m, score, round, code, invited_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session["id"], session["request_id"], c.user_id, c.blood_type, c.rhesus, int(round(c.distance_m)),
              score, session["round"], _new_code(conn), clock.iso(now), clock.iso(expires)))
        invited_ids.append(c.user_id)
    events.publish(conn, "UndanganDiterbitkan", change_type="invitation.issued",
                   entity_id=make_id("req", session["request_id"]),
                   summary=f"{len(invited_ids)} pendonor diundang",
                   request_id=session["request_id"], donor_user_ids=invited_ids,
                   facility_name=session["facility_name"], urgency=session["urgency"])
    return len(invited_ids)


def _session(conn, session_id: int):
    return conn.execute("SELECT * FROM match_sessions WHERE id = ?", (session_id,)).fetchone()


# ---------- reaksi atas event ----------

def on_request_verified(conn, body: dict) -> None:
    if conn.execute("SELECT 1 FROM match_sessions WHERE request_id = ?", (body["request_id"],)).fetchone():
        return
    now = clock.now_iso()
    steps = settings.match_radius_steps_m
    cur = conn.execute("""
        INSERT INTO match_sessions (request_id, requester_id, facility_id, facility_name, lat, lon, blood_type,
          rhesus, component, units_needed, urgency, needed_by, radius_m, opened_at, last_escalated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (body["request_id"], body["requester_id"], body["facility_id"], body["facility_name"], body["lat"],
          body["lon"], body["blood_type"], body["rhesus"], body["component"], body["units_needed"],
          body["urgency"], body["needed_by"], steps[0], now, now))
    events.publish(conn, "SesiPencocokanDimulai", change_type="match.started",
                   entity_id=make_id("req", body["request_id"]), summary="Pencarian pendonor dimulai",
                   request_id=body["request_id"])
    invite_more(conn, _session(conn, int(cur.lastrowid)))


def on_request_closed(conn, body: dict) -> None:
    session = conn.execute("SELECT * FROM match_sessions WHERE request_id = ? AND status = 'open'",
                           (body["request_id"],)).fetchone()
    if session is None:
        return
    reason = {"PermintaanTerpenuhi": "fulfilled", "PermintaanKedaluwarsa": "expired",
              "PermintaanDibatalkan": "cancelled"}.get(body.get("event", ""), "closed")
    now = clock.now_iso()
    conn.execute("UPDATE match_sessions SET status = 'closed', close_reason = ?, closed_at = ? WHERE id = ?",
                 (reason, now, session["id"]))
    conn.execute("UPDATE invitations SET status = 'cancelled', outcome_at = ? "
                 "WHERE session_id = ? AND status IN ('invited', 'accepted')", (now, session["id"]))


# ---------- job ----------

def escalate_all(conn) -> int:
    """Kedaluwarsakan undangan basi, lalu perluas radius bila pendonor yang bersedia belum cukup."""
    now = clock.utcnow()
    conn.execute("UPDATE invitations SET status = 'expired' WHERE status = 'invited' AND expires_at < ?",
                 (clock.iso(now),))
    escalated = 0
    for session in conn.execute("SELECT * FROM match_sessions WHERE status = 'open'").fetchall():
        counts = _counts(conn, session["id"])
        if counts["accepted"] + counts["donated"] >= session["units_needed"]:
            continue
        wait = clock.scaled_minutes(settings.escalate_after_minutes.get(session["urgency"], 60))
        if clock.parse_iso(session["last_escalated_at"]) + wait > now:
            invite_more(conn, session)  # isi ulang slot undangan yang kedaluwarsa/ditolak
            continue
        steps = settings.match_radius_steps_m
        previous = session["radius_step"]
        step = min(previous + 1, len(steps) - 1)
        conn.execute("UPDATE match_sessions SET radius_step = ?, radius_m = ?, round = round + 1, "
                     "last_escalated_at = ? WHERE id = ?", (step, steps[step], clock.iso(now), session["id"]))
        session = _session(conn, session["id"])
        if step != previous:
            events.publish(conn, "SesiDiperluas", change_type="match.escalated",
                           entity_id=make_id("req", session["request_id"]),
                           summary=f"Radius pencarian diperluas ke {session['radius_m'] // 1000} km",
                           request_id=session["request_id"], radius_m=session["radius_m"])
            escalated += 1
        invite_more(conn, session)
    return escalated


# ---------- API ----------

class ArrivalIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, description="Kode kedatangan yang ditunjukkan pendonor")
    outcome: Literal["donated", "deferred", "no_show"]


def _invitation_out(row, session, include_code: bool) -> dict:
    data = {
        "id": make_id("inv", row["id"]),
        "request_id": make_id("req", row["request_id"]),
        "status": row["status"],
        "facility_id": make_id("fac", session["facility_id"]), "facility_name": session["facility_name"],
        "lat": session["lat"], "lon": session["lon"],
        "blood_type": session["blood_type"], "rhesus": session["rhesus"], "component": session["component"],
        "urgency": session["urgency"], "needed_by": session["needed_by"],
        "distance_m": row["distance_m"], "invited_at": row["invited_at"], "expires_at": row["expires_at"],
        "responded_at": row["responded_at"],
    }
    if include_code and row["status"] == "accepted":
        data["code"] = row["code"]
    return data


def _load_invitation(conn, invitation_id: str, user: User):
    row = conn.execute("SELECT * FROM invitations WHERE id = ?",
                       (parse_id("inv", invitation_id, "Undangan"),)).fetchone()
    if row is None or row["donor_user_id"] != user.id:
        raise not_found("Undangan")
    return row, _session(conn, row["session_id"])


@router.get("/invitations", summary="Undangan donor untuk saya")
def my_invitations(status: Optional[str] = Query(default=None), pg: Page = Depends(page),
                   user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    sql, args = "SELECT * FROM invitations WHERE donor_user_id = ?", [user.id]
    if status:
        sql, args = sql + " AND status = ?", args + [status]
    rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    data = [_invitation_out(r, _session(conn, r["session_id"]), True) for r in pg.slice(rows)]
    return ok(conn, data, total=len(rows))


@router.get("/invitations/{invitation_id}", summary="Detail undangan")
def get_invitation(invitation_id: str, user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    row, session = _load_invitation(conn, invitation_id, user)
    return ok(conn, _invitation_out(row, session, True))


@router.post("/invitations/{invitation_id}/accept", summary="Terima undangan; mendapat kode kedatangan")
def accept(invitation_id: str, user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    row, session = _load_invitation(conn, invitation_id, user)
    if row["status"] != "invited":
        raise conflict(f"Undangan berstatus {row['status']}")
    if session["status"] != "open":
        raise conflict("Permintaan sudah ditutup")
    if clock.parse_iso(row["expires_at"]) < clock.utcnow():
        conn.execute("UPDATE invitations SET status = 'expired' WHERE id = ?", (row["id"],))
        raise conflict("Undangan sudah kedaluwarsa")
    e = donors.check_eligibility(conn, user.id)
    if not e.eligible:
        raise conflict("Anda belum memenuhi syarat: " + ", ".join(e.reasons))
    now = clock.now_iso()
    conn.execute("UPDATE invitations SET status = 'accepted', responded_at = ?, expires_at = ? WHERE id = ?",
                 (now, session["needed_by"], row["id"]))
    events.publish(conn, "UndanganDiterima", change_type="invitation.accepted",
                   entity_id=make_id("req", row["request_id"]), summary="Pendonor menerima undangan",
                   request_id=row["request_id"], donor_user_id=user.id)
    row = conn.execute("SELECT * FROM invitations WHERE id = ?", (row["id"],)).fetchone()
    return ok(conn, _invitation_out(row, session, True))


@router.post("/invitations/{invitation_id}/decline", summary="Tolak undangan")
def decline(invitation_id: str, user: User = Depends(require_roles("donor")), conn=Depends(get_conn)):
    row, session = _load_invitation(conn, invitation_id, user)
    if row["status"] not in PENDING:
        raise conflict(f"Undangan berstatus {row['status']}")
    conn.execute("UPDATE invitations SET status = 'declined', responded_at = ? WHERE id = ?",
                 (clock.now_iso(), row["id"]))
    events.publish(conn, "UndanganDitolak", change_type="invitation.declined",
                   entity_id=make_id("req", row["request_id"]), summary="Pendonor menolak undangan",
                   request_id=row["request_id"])
    invite_more(conn, session)
    row = conn.execute("SELECT * FROM invitations WHERE id = ?", (row["id"],)).fetchone()
    return ok(conn, _invitation_out(row, session, False))


@router.post("/arrivals", summary="Catat kedatangan pendonor dengan kode undangan (petugas)")
def record_arrival(body: ArrivalIn, user: User = Depends(require_roles("staff", "admin")), conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM invitations WHERE code = ? AND status IN ('invited', 'accepted')",
                       (body.code.strip().upper(),)).fetchone()
    if row is None:
        raise not_found("Undangan aktif dengan kode tersebut")
    session = _session(conn, row["session_id"])
    if session["status"] != "open":
        raise conflict("Permintaan sudah ditutup")
    now = clock.now_iso()
    conn.execute("UPDATE invitations SET status = ?, outcome_at = ?, outcome_by = ? WHERE id = ?",
                 (body.outcome, now, user.id, row["id"]))
    if body.outcome == "donated":
        events.publish(conn, "DonasiTercatat", change_type="donation.recorded",
                       entity_id=make_id("req", row["request_id"]), summary="Donasi tercatat untuk permintaan",
                       request_id=row["request_id"], donor_user_id=row["donor_user_id"],
                       facility_id=session["facility_id"], donated_at=now, source="invitation",
                       ref_id=make_id("inv", row["id"]))
    elif body.outcome == "deferred":
        events.publish(conn, "PendonorDitunda", change_type="donor.deferred",
                       entity_id=make_id("req", row["request_id"]), summary="Pendonor ditunda setelah pemeriksaan",
                       request_id=row["request_id"], donor_user_id=row["donor_user_id"], reason="hasil pemeriksaan")
    else:
        events.publish(conn, "PendonorTidakHadir", change_type="invitation.no_show",
                       entity_id=make_id("req", row["request_id"]), summary="Pendonor tidak hadir",
                       request_id=row["request_id"])
    session = _session(conn, row["session_id"])
    if body.outcome != "donated":
        invite_more(conn, session)
    row = conn.execute("SELECT * FROM invitations WHERE id = ?", (row["id"],)).fetchone()
    return ok(conn, _invitation_out(row, session, False))


@router.get("/requests/{request_id}/progress", summary="Progres pencarian pendonor (pemohon atau petugas)")
def progress(request_id: str, user: User = Depends(current_user), conn=Depends(get_conn)):
    rid = parse_id("req", request_id, "Permintaan")
    session = conn.execute("SELECT * FROM match_sessions WHERE request_id = ?", (rid,)).fetchone()
    if session is None or not (is_staff(user) or session["requester_id"] == user.id):
        raise not_found("Sesi pencocokan")
    counts = _counts(conn, session["id"])
    return ok(conn, {"request_id": request_id, "status": session["status"], "close_reason": session["close_reason"],
                     "radius_m": session["radius_m"], "round": session["round"],
                     "units_needed": session["units_needed"], "units_donated": counts["donated"],
                     "invitations": counts})
