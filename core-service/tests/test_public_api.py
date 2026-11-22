"""API publik untuk kelompok lain (kontrak klaster) dan konteks Kegiatan Donor / Stok Darah."""
from datetime import timedelta

from app import clock
from app.config import settings
from app.db import connect
from app import events

from conftest import API_KEY, REQUESTER, STAFF, donor
from helpers import make_donor, make_facility, make_request, offset

TITIK_TUJUAN_FIELDS = {"id", "name", "lat", "lon", "opens_at", "closes_at"}


def test_public_endpoints_require_key_or_jwt(client):
    for path in ("/donation-events", "/blood-stock", "/requests/summary", "/facilities", "/changes"):
        r = client.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key", path
        r = client.get(path, headers={"X-API-Key": "salah"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key", path
        r = client.get(path, headers={"Authorization": "Bearer bukan.token.valid"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token", path
        assert client.get(path, headers=API_KEY).status_code == 200, path
        assert client.get(path, headers=donor(9001)).status_code == 200, path


def test_donation_events_contract_for_kotasiaga(client):
    body = client.get("/donation-events", headers=API_KEY).json()
    assert body["meta"]["total"] >= 2  # data uji
    for item in body["data"]:
        assert TITIK_TUJUAN_FIELDS <= set(item)
        assert item["id"].startswith("evt_")
        assert item["opens_at"].endswith("Z") and item["closes_at"].endswith("Z")
        assert 0 <= item["slots_free"] <= item["slots_total"]
    near = client.get("/donation-events?lat=-6.89&lon=107.61&radius_m=5000", headers=API_KEY).json()["data"]
    assert near and all("distance_m" in e for e in near)
    assert [e["distance_m"] for e in near] == sorted(e["distance_m"] for e in near)
    assert client.get("/donation-events?lat=-6.89", headers=API_KEY).status_code == 422


def test_reservation_flow_changes_slots(client):
    base = (-13.50, 116.00)
    fac = make_facility(client, *base, name="UDD Uji Kegiatan", kind="udd")
    opens = (clock.utcnow() + timedelta(hours=2)).replace(minute=0, second=0)
    r = client.post("/donation-events", headers=STAFF, json={
        "name": "Donor Uji Kegiatan", "organizer": "Panitia Uji", "facility_id": fac,
        "opens_at": clock.iso(opens), "closes_at": clock.iso(opens + timedelta(hours=2)), "capacity_per_hour": 1})
    assert r.status_code == 201, r.text
    ev = r.json()["data"]
    assert ev["slots_total"] == 2 and ev["slots_free"] == 2 and len(ev["slots"]) == 2
    assert ev["lat"] == base[0]  # koordinat diambil dari fasilitas

    make_donor(client, 501, "a", "positive", *offset(*base, km_north=1))
    make_donor(client, 502, "b", "positive", *offset(*base, km_north=1))
    make_donor(client, 503, "o", "positive", *offset(*base, km_north=1))
    make_donor(client, 504, "o", "positive", *offset(*base, km_north=1), screened=False)
    slot1, slot2 = ev["slots"][0]["slot_start"], ev["slots"][1]["slot_start"]

    seq0 = client.get(f"/donation-events/{ev['id']}", headers=API_KEY).json()["meta"]["seq"]
    r = client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(501), json={"slot_start": slot1})
    assert r.status_code == 201, r.text
    res1 = r.json()["data"]
    assert res1["code"].startswith("R")
    detail = client.get(f"/donation-events/{ev['id']}", headers=API_KEY).json()
    assert detail["data"]["slots_free"] == 1 and detail["meta"]["seq"] > seq0

    assert client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(501),
                       json={"slot_start": slot2}).status_code == 409  # sudah punya reservasi
    assert client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(502),
                       json={"slot_start": slot1}).status_code == 409  # slot penuh
    r = client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(504), json={"slot_start": slot2})
    assert r.status_code == 409 and "screening_required" in r.json()["error"]["message"]
    bad_slot = clock.iso(clock.parse_iso(slot1) + timedelta(minutes=30))
    assert client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(502),
                       json={"slot_start": bad_slot}).status_code == 422
    assert client.post(f"/donation-events/{ev['id']}/reservations", headers=donor(502),
                       json={"slot_start": slot2}).status_code == 201
    assert client.get(f"/donation-events/{ev['id']}", headers=API_KEY).json()["data"]["slots_free"] == 0

    assert client.delete(f"/reservations/{res1['id']}", headers=donor(502)).status_code == 404  # bukan miliknya
    assert client.delete(f"/reservations/{res1['id']}", headers=donor(501)).status_code == 200
    assert client.get(f"/donation-events/{ev['id']}", headers=API_KEY).json()["data"]["slots_free"] == 1

    res2 = client.get("/reservations/mine", headers=donor(502)).json()["data"][0]
    r = client.post(f"/reservations/{res2['id']}/outcome", headers=STAFF, json={"outcome": "donated"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "donated"
    assert len(client.get("/donors/me/history", headers=donor(502)).json()["data"]) == 1
    staff_list = client.get(f"/donation-events/{ev['id']}/reservations", headers=STAFF).json()["data"]
    assert {x["status"] for x in staff_list} == {"cancelled", "donated"}
    assert client.get(f"/donation-events/{ev['id']}/reservations", headers=donor(502)).status_code == 403

    assert client.patch(f"/donation-events/{ev['id']}/cancel", headers=STAFF).status_code == 200
    assert all(e["id"] != ev["id"] for e in client.get("/donation-events", headers=API_KEY).json()["data"])


def test_event_validation(client):
    opens = clock.utcnow() + timedelta(hours=1)
    base = {"name": "Uji", "organizer": "Uji", "lat": -6.9, "lon": 107.6, "capacity_per_hour": 5,
            "opens_at": clock.iso(opens), "closes_at": clock.iso(opens - timedelta(hours=1))}
    assert client.post("/donation-events", headers=STAFF, json=base).status_code == 422
    too_long = {**base, "closes_at": clock.iso(opens + timedelta(hours=13))}
    assert client.post("/donation-events", headers=STAFF, json=too_long).status_code == 422
    no_place = {k: v for k, v in base.items() if k not in ("lat", "lon")}
    no_place["closes_at"] = clock.iso(opens + timedelta(hours=2))
    assert client.post("/donation-events", headers=STAFF, json=no_place).status_code == 422
    assert client.post("/donation-events", headers=donor(1), json={**base, "closes_at": clock.iso(
        opens + timedelta(hours=2))}).status_code == 403


def test_stock_levels_low_event_and_needed_types(client):
    base = (-14.50, 117.00)
    fac = make_facility(client, *base, name="UDD Uji Stok", kind="udd")
    opens = clock.utcnow() + timedelta(days=1)
    ev = client.post("/donation-events", headers=STAFF, json={
        "name": "Donor Uji Stok", "organizer": "UDD Uji", "facility_id": fac, "opens_at": clock.iso(opens),
        "closes_at": clock.iso(opens + timedelta(hours=3)), "capacity_per_hour": 5}).json()["data"]
    make_donor(client, 601, "o", "negative", *offset(*base, km_north=1))

    r = client.put(f"/blood-stock/{fac}", headers=STAFF, json={"items": [
        {"blood_type": "o", "rhesus": "negative", "component": "prc", "units": 20},
        {"blood_type": "a", "rhesus": "positive", "component": "prc", "units": 50}]})
    assert r.status_code == 200
    assert {i["level"] for i in r.json()["data"]["items"]} == {"sufficient"}

    seq = client.get("/changes", headers=STAFF).json()["meta"]["latest_seq"]
    r = client.put(f"/blood-stock/{fac}", headers=STAFF, json={"items": [
        {"blood_type": "o", "rhesus": "negative", "component": "prc", "units": 2}]})
    levels = {(i["blood_type"], i["rhesus"]): i["level"] for i in r.json()["data"]["items"]}
    assert levels[("o", "negative")] == "critical" and levels[("a", "positive")] == "sufficient"
    types = [c["type"] for c in client.get(f"/changes?since={seq}", headers=STAFF).json()["data"]]
    assert "stock.updated" in types and "stock.low" in types

    assert client.get(f"/donation-events/{ev['id']}", headers=API_KEY).json()["data"]["needed_blood_types"] == ["o_negative"]
    assert any(n["kind"] == "stock.low" for n in client.get("/notifications", headers=donor(601)).json()["data"])

    critical = client.get(f"/blood-stock?facility_id={fac}&level=critical", headers=API_KEY).json()["data"]
    assert len(critical) == 1 and len(critical[0]["items"]) == 1
    assert client.put(f"/blood-stock/{fac}", headers=REQUESTER, json={"items": [
        {"blood_type": "o", "rhesus": "negative", "component": "prc", "units": 5}]}).status_code == 403


def test_requests_summary_has_no_personal_data(client):
    base = (-15.50, 118.00)
    fac = make_facility(client, *base, name="RS Uji Ringkasan")
    make_request(client, STAFF, fac, blood_type="ab", rhesus="negative", units=3, urgency="emergency")
    make_request(client, REQUESTER, fac, blood_type="ab", rhesus="negative", units=2)  # belum terverifikasi
    box = f"{base[1] - 0.1},{base[0] - 0.1},{base[1] + 0.1},{base[0] + 0.1}"
    data = client.get(f"/requests/summary?bbox={box}", headers=API_KEY).json()["data"]
    assert data == [{"blood_type": "ab", "rhesus": "negative", "component": "prc", "requests": 1,
                     "units_remaining": 3, "by_urgency": {"normal": 0, "urgent": 0, "emergency": 1}}]
    assert client.get("/requests/summary?bbox=1,2,3", headers=API_KEY).status_code == 422


def test_changes_feed_reset_when_too_old(client, monkeypatch):
    monkeypatch.setattr(settings, "changes_keep", 50)
    conn = connect()
    try:
        for i in range(60):
            events.publish(conn, "Uji", change_type="test.bulk", entity_id=str(i), summary="uji")
        conn.commit()
    finally:
        conn.close()
    body = client.get("/changes?since=1&limit=5", headers=API_KEY).json()
    assert body["meta"]["reset"] is True and len(body["data"]) == 5
    latest = body["meta"]["latest_seq"]
    body = client.get(f"/changes?since={latest - 3}", headers=API_KEY).json()
    assert body["meta"]["reset"] is False and [c["seq"] for c in body["data"]] == [latest - 2, latest - 1, latest]


def test_facilities_near(client):
    data = client.get("/facilities?lat=-6.91&lon=107.61&radius_m=10000&kind=udd", headers=API_KEY).json()["data"]
    assert data and data[0]["kind"] == "udd" and "distance_m" in data[0]
    assert client.get("/facilities/fac_abc", headers=API_KEY).status_code == 404


def test_donor_profile_validation(client):
    h = donor(701)
    assert client.get("/donors/me", headers=h).status_code == 404
    bad = {"display_name": "X Uji", "blood_type": "a", "rhesus": "positive", "birth_date": "2999-01-01",
           "weight_kg": 60, "lat": -6.9, "lon": 107.6}
    assert client.put("/donors/me", headers=h, json=bad).status_code == 422
    assert client.put("/donors/me", headers=h, json={**bad, "birth_date": "2000-01-01", "weight_kg": 10}).status_code == 422
    assert client.put("/donors/me", headers=REQUESTER, json={**bad, "birth_date": "2000-01-01"}).status_code == 403
    ok = client.put("/donors/me", headers=h, json={**bad, "birth_date": "2000-01-01"})
    assert ok.status_code == 200
    assert ok.json()["data"]["eligibility"]["reasons"] == ["screening_required"]
