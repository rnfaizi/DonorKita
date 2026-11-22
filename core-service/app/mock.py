"""Mock mode (AGENTS.md bagian 7): data bergerak sendiri untuk demo dan integrasi.

Prinsip: yang diacak hanya INPUT (aksi pemohon, pendonor, petugas, dan perubahan stok palsu).
Setiap aksi memanggil fungsi domain yang sama dengan endpoint API, sehingga verifikasi,
pencocokan, kelayakan, eskalasi, dan notifikasi berjalan dengan kode asli. Mock tidak pernah
menulis status permintaan atau undangan secara langsung.

Aktor palsu (ID >= 900000, tidak bentrok dengan akun auth-service):
  pemohon 900001-900005, petugas 900100, pendonor 900201 dst. (MOCK_DONORS orang)
  tersebar acak di Bandung (lat -6.98..-6.84, lon 107.54..107.72).

Setiap tick (MOCK_TICK_SECONDS) menjalankan 1-3 aksi acak, lalu job kedaluwarsa/eskalasi.
Bila tick itu tidak mengubah data, satu perubahan stok dipaksa agar meta.seq selalu naik.
Endpoint /_mock/scenario dan /_mock/state hanya aktif saat MOCK_MODE=true (selain itu 404)
dan memerlukan header X-API-Key = MOCK_ADMIN_API_KEY.

Core DonorKita tidak memanggil layanan luar, sehingga skenario upstream_down/recover dan
MOCK_UPSTREAM_* berlaku di layanan integrasi (Tugas 3), bukan di sini.
"""
import asyncio
import collections
import logging
import random
from datetime import timedelta
from typing import Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel

from . import clock
from .common import ok
from .config import settings
from .db import connect
from .errors import ApiError
from .meta import current_seq
from .modules import donors, drives, matching, requests, stock
from .security import User
from .seed import seed_demo

log = logging.getLogger("donorkita.mock")
router = APIRouter(tags=["mock"])

LAT_RANGE = (-6.98, -6.84)
LON_RANGE = (107.54, 107.72)
REQUESTERS = [User(900001 + i, "requester", f"Pemohon Mock {i + 1}") for i in range(5)]
STAFF = User(900100, "staff", "Petugas Mock")
DONOR_BASE = 900201
BLOOD_WEIGHTS = {"o": 37, "b": 29, "a": 26, "ab": 8}


class MockState:
    def __init__(self) -> None:
        self.seed: Optional[int] = None
        self.rng = random.Random()
        self.ticks = 0
        self.counts: Dict[str, int] = collections.Counter()
        self.recent: collections.deque = collections.deque(maxlen=20)
        self.started_at: Optional[str] = None

    def note(self, action: str, detail: str) -> None:
        self.counts[action] += 1
        self.recent.appendleft({"at": clock.now_iso(), "action": action, "detail": detail})


state = MockState()


def mock_donor_ids() -> List[int]:
    return list(range(DONOR_BASE, DONOR_BASE + settings.mock_donors))


def _donor(uid: int) -> User:
    return User(uid, "donor", f"Pendonor Mock {uid - DONOR_BASE + 1}")


def _rand_point(rng: random.Random) -> tuple:
    return round(rng.uniform(*LAT_RANGE), 5), round(rng.uniform(*LON_RANGE), 5)


def _blood(rng: random.Random) -> tuple:
    bt = rng.choices(list(BLOOD_WEIGHTS), weights=list(BLOOD_WEIGHTS.values()))[0]
    return bt, ("negative" if rng.random() < 0.05 else "positive")


def _try(fn: Callable, *args, **kwargs):
    """Panggil fungsi domain; konflik bisnis (409/422/404) dianggap aksi gagal biasa."""
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        if exc.status >= 500:
            raise
        return None


# ---------- persiapan ----------

def prepare(conn) -> None:
    """Pastikan data uji dan aktor palsu ada. Aman dipanggil berulang."""
    seed_demo(conn)
    rng = random.Random(state.seed)
    for uid in mock_donor_ids():
        bt, rh = _blood(rng)
        lat, lon = _rand_point(rng)
        birth = f"{rng.randint(1975, 2006)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        donors.seed_profile(conn, uid, f"Pendonor Mock {uid - DONOR_BASE + 1}", bt, rh, birth,
                            round(rng.uniform(48, 90), 1), lat, lon)


def _facilities(conn, kinds=("hospital", "udd", "bdrs")) -> List:
    marks = ",".join("?" * len(kinds))
    return conn.execute(f"SELECT * FROM facilities WHERE kind IN ({marks})", kinds).fetchall()


def _recycle_donors(conn) -> None:
    """Majukan 'waktu' pendonor palsu: riwayat donasi dan penundaan yang sudah lebih lama dari
    MOCK_DONOR_RECOVERY_MINUTES dianggap sudah lewat masa jeda."""
    cutoff = clock.iso(clock.utcnow() - timedelta(minutes=settings.mock_donor_recovery_minutes))
    ids = mock_donor_ids()
    lo, hi = ids[0], ids[-1]
    rows = conn.execute("SELECT id, donated_at FROM donation_history WHERE user_id BETWEEN ? AND ? "
                        "AND donated_at < ? AND donated_at > ?",
                        (lo, hi, cutoff, clock.iso(clock.utcnow() - timedelta(days=settings.donation_interval_days)))
                        ).fetchall()
    for r in rows:
        older = clock.parse_iso(r["donated_at"]) - timedelta(days=settings.donation_interval_days + 1)
        conn.execute("UPDATE donation_history SET donated_at = ? WHERE id = ?", (clock.iso(older), r["id"]))
    conn.execute("DELETE FROM deferrals WHERE user_id BETWEEN ? AND ? AND created_at < ?", (lo, hi, cutoff))


# ---------- aksi acak ----------

def act_new_request(conn, rng: random.Random, *, urgency: Optional[str] = None, units: Optional[int] = None,
                    near: Optional[tuple] = None, blood: Optional[tuple] = None) -> Optional[str]:
    active = conn.execute("SELECT COUNT(*) c FROM blood_requests WHERE requester_id >= 900000 AND status IN "
                          "('submitted', 'verified', 'searching')").fetchone()["c"]
    if urgency is None and active >= settings.mock_max_active_requests:
        return None
    facilities = _facilities(conn, ("hospital", "udd", "bdrs"))
    if not facilities:
        return None
    if near:
        from .domain.geo import haversine_m
        fac = min(facilities, key=lambda f: haversine_m(near[0], near[1], f["lat"], f["lon"]))
    else:
        fac = rng.choice(facilities)
    bt, rh = blood or _blood(rng)
    urgency = urgency or rng.choices(["normal", "urgent", "emergency"], weights=[60, 30, 10])[0]
    hours = rng.uniform(6, 48)
    needed_by = clock.utcnow() + timedelta(seconds=hours * 3600 / settings.mock_time_scale + 600)
    body = requests.RequestIn(facility_id=f"fac_{fac['id']}", patient_ref=f"MOCK-{rng.randint(1000, 9999)}",
                              blood_type=bt, rhesus=rh,
                              component=rng.choices(["prc", "whole_blood", "platelets"], weights=[75, 15, 10])[0],
                              units=units or rng.randint(1, 3), urgency=urgency, needed_by=needed_by)
    # Setengah permintaan dibuat petugas (langsung terverifikasi), sisanya oleh keluarga pasien.
    user = STAFF if (urgency == "emergency" or rng.random() < 0.5) else rng.choice(REQUESTERS)
    res = _try(requests.create, body=body, user=user, conn=conn)
    if res:
        state.note("new_request", f"{res['data']['id']} {bt.upper()}{'+' if rh == 'positive' else '-'} {urgency}")
        return res["data"]["id"]
    return None


def act_verify(conn, rng: random.Random) -> bool:
    row = conn.execute("SELECT id FROM blood_requests WHERE status = 'submitted' AND requester_id >= 900000 "
                       "ORDER BY id LIMIT 1").fetchone()
    if not row:
        return False
    approve = rng.random() < 0.9
    body = requests.VerifyIn(decision="approve" if approve else "reject",
                             reason=None if approve else "Nomor formulir tidak cocok (mock)")
    if _try(requests.verify, request_id=f"req_{row['id']}", body=body, user=STAFF, conn=conn):
        state.note("verify", f"req_{row['id']} {'disetujui' if approve else 'ditolak'}")
        return True
    return False


def act_donor_responds(conn, rng: random.Random) -> bool:
    ids = mock_donor_ids()
    rows = conn.execute("SELECT id, donor_user_id FROM invitations WHERE status = 'invited' "
                        "AND donor_user_id BETWEEN ? AND ? ORDER BY id LIMIT 20", (ids[0], ids[-1])).fetchall()
    if not rows:
        return False
    row = rng.choice(rows)
    accept = rng.random() < 0.7
    fn = matching.accept if accept else matching.decline
    if _try(fn, invitation_id=f"inv_{row['id']}", user=_donor(row["donor_user_id"]), conn=conn):
        state.note("donor_response", f"inv_{row['id']} {'diterima' if accept else 'ditolak'}")
        return True
    return False


def act_arrival(conn, rng: random.Random, outcome: Optional[str] = None) -> bool:
    ids = mock_donor_ids()
    rows = conn.execute("SELECT id, code FROM invitations WHERE status = 'accepted' "
                        "AND donor_user_id BETWEEN ? AND ? ORDER BY id LIMIT 20", (ids[0], ids[-1])).fetchall()
    if not rows:
        return False
    row = rng.choice(rows)
    outcome = outcome or rng.choices(["donated", "deferred", "no_show"], weights=[85, 10, 5])[0]
    if _try(matching.record_arrival, body=matching.ArrivalIn(code=row["code"], outcome=outcome), user=STAFF, conn=conn):
        state.note("arrival", f"inv_{row['id']} {outcome}")
        return True
    return False


def act_stock_change(conn, rng: random.Random) -> bool:
    rows = conn.execute("SELECT * FROM blood_stock").fetchall()
    if not rows:
        return False
    row = rng.choice(rows)
    delta = rng.choice([-5, -4, -3, -2, -1, 3, 5, 8, 10])
    units = max(0, row["units"] + delta)
    if units == row["units"]:
        units = row["units"] + 5
    item = stock.StockItemIn(blood_type=row["blood_type"], rhesus=row["rhesus"], component=row["component"], units=units)
    stock.apply_update(conn, row["facility_id"], [item], STAFF.id)
    state.note("stock_change", f"fac_{row['facility_id']} {row['blood_type']}_{row['rhesus']} {row['units']}->{units}")
    return True


def _ensure_events(conn, rng: random.Random) -> None:
    upcoming = conn.execute("SELECT COUNT(*) c FROM donation_events WHERE status = 'scheduled' AND closes_at > ?",
                            (clock.now_iso(),)).fetchone()["c"]
    if upcoming >= 2:
        return
    udd = _facilities(conn, ("udd",))
    lat, lon = _rand_point(rng)
    opens = (clock.utcnow() + timedelta(hours=1)).replace(minute=0, second=0)
    drives.create_event(conn, name=f"Donor Darah Mock {rng.randint(100, 999)}", organizer="Komunitas Mock",
                        facility_id=udd[0]["id"] if udd else None, address="Lokasi mock, Bandung", lat=lat, lon=lon,
                        opens_at=opens, closes_at=opens + timedelta(hours=3), capacity_per_hour=4, created_by=STAFF.id)
    state.note("event_created", "kegiatan mock baru")


def act_event_slots(conn, rng: random.Random) -> bool:
    _ensure_events(conn, rng)
    ids = mock_donor_ids()
    if rng.random() < 0.35:  # batalkan reservasi: slot bertambah
        rows = conn.execute("SELECT id, user_id FROM reservations WHERE status = 'reserved' AND user_id BETWEEN ? AND ?",
                            (ids[0], ids[-1])).fetchall()
        if rows:
            row = rng.choice(rows)
            if _try(drives.cancel_reservation, reservation_id=f"res_{row['id']}", user=_donor(row["user_id"]), conn=conn):
                state.note("reservation", f"res_{row['id']} dibatalkan")
                return True
    events = conn.execute("SELECT * FROM donation_events WHERE status = 'scheduled' AND closes_at > ?",
                          (clock.now_iso(),)).fetchall()
    if not events:
        return False
    ev = rng.choice(events)
    future = [s for s in drives.slot_starts(clock.parse_iso(ev["opens_at"]), clock.parse_iso(ev["closes_at"]))
              if s + timedelta(hours=1) > clock.utcnow()]
    if not future:
        return False
    for uid in rng.sample(ids, k=min(8, len(ids))):
        body = drives.ReservationIn(slot_start=rng.choice(future))
        res = _try(drives.reserve, event_id=f"evt_{ev['id']}", body=body, user=_donor(uid), conn=conn)
        if res:
            state.note("reservation", f"{res['data']['id']} di evt_{ev['id']}")
            return True
    return False


ACTIONS = [(act_new_request, 25), (act_verify, 15), (act_donor_responds, 25),
           (act_arrival, 15), (act_stock_change, 10), (act_event_slots, 10)]


def tick(conn, rng: Optional[random.Random] = None) -> int:
    """Satu langkah generator. Mengembalikan seq setelah tick."""
    rng = rng or state.rng
    before = current_seq(conn)
    _recycle_donors(conn)
    for _ in range(rng.randint(1, 3)):
        fn = rng.choices([a for a, _ in ACTIONS], weights=[w for _, w in ACTIONS])[0]
        fn(conn, rng)
    requests.expire_overdue(conn)
    matching.escalate_all(conn)
    if current_seq(conn) == before:  # jaminan: data selalu bergerak
        act_stock_change(conn, rng)
    state.ticks += 1
    return current_seq(conn)


# ---------- siklus hidup ----------

def start() -> None:
    state.seed = settings.mock_seed if settings.mock_seed is not None else random.randrange(1, 10**6)
    state.rng = random.Random(state.seed)
    state.started_at = clock.now_iso()
    conn = connect()
    try:
        prepare(conn)
        conn.commit()
    finally:
        conn.close()
    log.info("MOCK_MODE aktif: seed=%s tick=%ss time_scale=%s donors=%s", state.seed, settings.mock_tick_seconds,
             settings.mock_time_scale, settings.mock_donors)


async def run_forever() -> None:
    while True:
        await asyncio.sleep(settings.mock_tick_seconds)
        conn = connect()
        try:
            tick(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception("tick mock gagal")
        finally:
            conn.close()


# ---------- skenario demo ----------

class ScenarioIn(BaseModel):
    name: Literal["emergency_request", "stock_low", "event_full", "mass_arrival", "reset"]
    lat: Optional[float] = None
    lon: Optional[float] = None


def _guard(x_api_key: Optional[str]) -> None:
    if not settings.mock_mode:
        raise ApiError(404, "not_found", "Data tidak ditemukan")
    if not settings.mock_admin_api_key or x_api_key != settings.mock_admin_api_key:
        raise ApiError(401, "invalid_api_key", "API key admin mock tidak valid")


def run_scenario(conn, body: ScenarioIn) -> dict:
    rng = state.rng
    near = (body.lat, body.lon) if body.lat is not None and body.lon is not None else None
    if body.name == "emergency_request":
        rid = act_new_request(conn, rng, urgency="emergency", units=3, near=near, blood=("o", "positive"))
        return {"request_id": rid}
    if body.name == "stock_low":
        rows = conn.execute("SELECT * FROM blood_stock ORDER BY units DESC").fetchall()
        if not rows:
            raise ApiError(409, "conflict", "Belum ada data stok")
        row = rows[0]
        stock.apply_update(conn, row["facility_id"], [stock.StockItemIn(
            blood_type=row["blood_type"], rhesus=row["rhesus"], component=row["component"],
            units=settings.stock_critical_units)], STAFF.id)
        state.note("scenario", "stock_low")
        return {"facility_id": f"fac_{row['facility_id']}", "blood_type": row["blood_type"], "rhesus": row["rhesus"],
                "component": row["component"], "units": settings.stock_critical_units}
    if body.name == "event_full":
        _ensure_events(conn, rng)
        ev = conn.execute("SELECT * FROM donation_events WHERE status = 'scheduled' AND closes_at > ? "
                          "ORDER BY opens_at LIMIT 1", (clock.now_iso(),)).fetchone()
        filled = 0
        for uid in mock_donor_ids():
            detail = drives._event_out(conn, ev)
            if detail["slots_free"] == 0:
                break
            for s in drives.slot_starts(clock.parse_iso(ev["opens_at"]), clock.parse_iso(ev["closes_at"])):
                if s + timedelta(hours=1) <= clock.utcnow():
                    continue
                if _try(drives.reserve, event_id=f"evt_{ev['id']}", body=drives.ReservationIn(slot_start=s),
                        user=_donor(uid), conn=conn):
                    filled += 1
                    break
        state.note("scenario", f"event_full evt_{ev['id']} +{filled}")
        return {"event_id": f"evt_{ev['id']}", "reserved": filled,
                "slots_free": drives._event_out(conn, ev)["slots_free"]}
    if body.name == "mass_arrival":
        n = 0
        while act_arrival(conn, rng, outcome="donated"):
            n += 1
        state.note("scenario", f"mass_arrival {n}")
        return {"donations_recorded": n}
    # reset: tutup semua permintaan mock yang masih aktif dan kembalikan stok uji
    rows = conn.execute("SELECT id FROM blood_requests WHERE requester_id >= 900000 AND status IN "
                        "('submitted', 'verified', 'searching')").fetchall()
    for r in rows:
        _try(requests.cancel, request_id=f"req_{r['id']}", body=requests.CancelIn(reason="Reset skenario mock"),
             user=STAFF, conn=conn)
    for bt, units in (("o", 24), ("a", 15), ("b", 9), ("ab", 4)):
        udd = _facilities(conn, ("udd",))
        if udd:
            stock.apply_update(conn, udd[0]["id"], [
                stock.StockItemIn(blood_type=bt, rhesus="positive", component="prc", units=units),
                stock.StockItemIn(blood_type=bt, rhesus="negative", component="prc", units=max(1, units // 8))],
                STAFF.id)
    state.note("scenario", f"reset {len(rows)} permintaan")
    return {"requests_cancelled": len(rows)}


@router.post("/_mock/scenario", summary="Picu skenario demo (hanya MOCK_MODE=true, X-API-Key admin mock)")
def scenario(body: ScenarioIn, x_api_key: Optional[str] = Header(default=None)):
    _guard(x_api_key)
    conn = connect()
    try:
        result = run_scenario(conn, body)
        conn.commit()
        return ok(conn, {"scenario": body.name, **result})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/_mock/state", summary="Status generator mock (hanya MOCK_MODE=true, X-API-Key admin mock)")
def mock_state(x_api_key: Optional[str] = Header(default=None)):
    _guard(x_api_key)
    conn = connect()
    try:
        active = conn.execute("SELECT status, COUNT(*) c FROM blood_requests GROUP BY status").fetchall()
        return ok(conn, {"seed": state.seed, "ticks": state.ticks, "started_at": state.started_at,
                         "tick_seconds": settings.mock_tick_seconds, "time_scale": settings.mock_time_scale,
                         "mock_donors": settings.mock_donors, "actions": dict(state.counts),
                         "requests_by_status": {r["status"]: r["c"] for r in active},
                         "recent": list(state.recent)})
    finally:
        conn.close()
