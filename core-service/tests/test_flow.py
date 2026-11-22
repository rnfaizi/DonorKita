"""Alur utama: permintaan -> verifikasi -> undangan -> kedatangan -> terpenuhi, plus eskalasi,
kedaluwarsa, penolakan, dan hak akses."""
from app import jobs

from conftest import REQUESTER, REQUESTER2, STAFF, donor
from helpers import make_donor, make_facility, make_request, offset


def invited_ids(client, request_id):
    progress = client.get(f"/requests/{request_id}/progress", headers=STAFF).json()["data"]
    return progress


def test_full_request_lifecycle(client):
    base = (-7.50, 110.00)
    fac = make_facility(client, *base, name="RS Uji Alur")
    make_donor(client, 101, "o", "negative", *offset(*base, km_north=1))    # cocok (O- untuk A+ PRC)
    make_donor(client, 102, "a", "positive", *offset(*base, km_east=2))     # cocok, identik
    make_donor(client, 103, "b", "positive", *offset(*base, km_north=1))    # tidak cocok
    make_donor(client, 104, "a", "positive", *offset(*base, km_north=15))   # di luar radius 5 km
    make_donor(client, 105, "a", "positive", *offset(*base, km_east=1), screened=False)  # belum pra-skrining
    make_donor(client, 107, "o", "positive", *offset(*base, km_east=1), available=False)  # tidak bersedia

    seq_before = client.get("/changes", headers=STAFF).json()["meta"]["latest_seq"]
    req = make_request(client, REQUESTER, fac, blood_type="a", rhesus="positive", units=1)
    assert req["status"] == "submitted" and req["patient_ref"] == "FRM-UJI-001"
    rid = req["id"]

    # belum terverifikasi: tidak ada sesi
    assert client.get(f"/requests/{rid}/progress", headers=REQUESTER).status_code == 404

    r = client.patch(f"/requests/{rid}/verify", headers=STAFF, json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert client.get(f"/requests/{rid}", headers=REQUESTER).json()["data"]["status"] == "searching"

    progress = client.get(f"/requests/{rid}/progress", headers=REQUESTER).json()["data"]
    assert progress["radius_m"] == 5000 and progress["invitations"]["invited"] == 2  # 1 kantong x faktor 2

    inv_101 = client.get("/invitations", headers=donor(101)).json()["data"]
    inv_102 = client.get("/invitations", headers=donor(102)).json()["data"]
    assert len(inv_101) == 1 and len(inv_102) == 1
    for uid in (103, 104, 105, 107):
        assert client.get("/invitations", headers=donor(uid)).json()["data"] == []
    assert "code" not in inv_102[0]  # kode baru muncul setelah menerima

    acc = client.post(f"/invitations/{inv_102[0]['id']}/accept", headers=donor(102))
    assert acc.status_code == 200, acc.text
    code = acc.json()["data"]["code"]
    assert len(code) == 6
    assert client.post(f"/invitations/{inv_102[0]['id']}/accept", headers=donor(102)).status_code == 409
    assert client.post(f"/invitations/{inv_101[0]['id']}/decline", headers=donor(101)).status_code == 200
    # pendonor lain tidak bisa mengakses undangan orang lain
    assert client.get(f"/invitations/{inv_102[0]['id']}", headers=donor(101)).status_code == 404

    arr = client.post("/arrivals", headers=STAFF, json={"code": code, "outcome": "donated"})
    assert arr.status_code == 200, arr.text
    assert arr.json()["data"]["status"] == "donated"

    final = client.get(f"/requests/{rid}", headers=REQUESTER).json()["data"]
    assert final["status"] == "fulfilled" and final["units_fulfilled"] == 1
    progress = client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]
    assert progress["status"] == "closed" and progress["close_reason"] == "fulfilled"

    elig = client.get("/donors/me/eligibility", headers=donor(102)).json()["data"]
    assert not elig["eligible"] and elig["reasons"] == ["interval_not_met"] and elig["next_eligible_at"]
    assert len(client.get("/donors/me/history", headers=donor(102)).json()["data"]) == 1

    kinds = [n["kind"] for n in client.get("/notifications", headers=REQUESTER).json()["data"]]
    assert "request.verified" in kinds and "request.fulfilled" in kinds
    assert any(n["kind"] == "invitation.issued" for n in client.get("/notifications", headers=donor(102)).json()["data"])

    types = [c["type"] for c in client.get(f"/changes?since={seq_before}", headers=STAFF).json()["data"]]
    for t in ("request.created", "request.verified", "match.started", "invitation.issued",
              "invitation.accepted", "invitation.declined", "donation.recorded", "request.fulfilled"):
        assert t in types, t
    assert types.index("donation.recorded") < types.index("request.fulfilled")

    # kode sudah dipakai
    assert client.post("/arrivals", headers=STAFF, json={"code": code, "outcome": "donated"}).status_code == 404


def test_escalation_expands_radius(client):
    base = (-8.50, 111.00)
    fac = make_facility(client, *base, name="RS Uji Eskalasi")
    make_donor(client, 201, "b", "positive", *offset(*base, km_north=8))
    req = make_request(client, STAFF, fac, blood_type="b", rhesus="positive", urgency="urgent")  # petugas: auto verify
    rid = req["id"]
    assert req["status"] == "searching"  # terverifikasi otomatis dan sesi pencocokan langsung dibuka
    p = client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]
    assert p["radius_m"] == 5000 and p["invitations"]["invited"] == 0

    jobs.run_once()  # belum waktunya eskalasi
    assert client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]["radius_m"] == 5000

    from app import clock
    clock.advance(31 * 60)  # eskalasi urgent: 30 menit
    result = jobs.run_once()
    assert result["escalated"] >= 1
    p = client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]
    assert p["radius_m"] == 10000 and p["round"] == 2 and p["invitations"]["invited"] == 1
    assert len(client.get("/invitations", headers=donor(201)).json()["data"]) == 1


def test_deferred_outcome_blocks_and_reinvites(client):
    base = (-9.50, 112.00)
    fac = make_facility(client, *base, name="RS Uji Tunda")
    make_donor(client, 301, "ab", "positive", *offset(*base, km_north=1))
    make_donor(client, 302, "ab", "positive", *offset(*base, km_north=2))
    make_donor(client, 303, "ab", "positive", *offset(*base, km_north=3))
    rid = make_request(client, STAFF, fac, blood_type="ab", rhesus="positive", component="platelets")["id"]
    invited = [uid for uid in (301, 302, 303) if client.get("/invitations", headers=donor(uid)).json()["data"]]
    assert len(invited) == 2
    first = invited[0]
    inv = client.get("/invitations", headers=donor(first)).json()["data"][0]
    code = client.post(f"/invitations/{inv['id']}/accept", headers=donor(first)).json()["data"]["code"]
    assert client.post("/arrivals", headers=STAFF, json={"code": code, "outcome": "deferred"}).status_code == 200
    elig = client.get("/donors/me/eligibility", headers=donor(first)).json()["data"]
    assert elig["reasons"] == ["deferred"]
    # pendonor ketiga diundang menggantikan yang ditunda
    assert all(client.get("/invitations", headers=donor(uid)).json()["data"] for uid in (301, 302, 303))
    assert client.get(f"/requests/{rid}", headers=STAFF).json()["data"]["status"] == "searching"


def test_expiry_job_closes_request_and_cancels_invitations(client):
    base = (-10.50, 113.00)
    fac = make_facility(client, *base, name="RS Uji Kedaluwarsa")
    make_donor(client, 401, "o", "positive", *offset(*base, km_north=1))
    rid = make_request(client, STAFF, fac, blood_type="o", rhesus="positive", hours=2)["id"]
    assert client.get("/invitations", headers=donor(401)).json()["data"][0]["status"] == "invited"
    from app import clock
    clock.advance(3 * 3600)
    assert jobs.run_once()["expired"] >= 1
    assert client.get(f"/requests/{rid}", headers=STAFF).json()["data"]["status"] == "expired"
    assert client.get("/invitations", headers=donor(401)).json()["data"][0]["status"] == "cancelled"
    assert client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]["close_reason"] == "expired"


def test_verification_rules_and_access(client):
    base = (-11.50, 114.00)
    fac = make_facility(client, *base, name="RS Uji Akses")
    rid = make_request(client, REQUESTER, fac)["id"]
    assert client.patch(f"/requests/{rid}/verify", headers=REQUESTER, json={"decision": "approve"}).status_code == 403
    assert client.get(f"/requests/{rid}", headers=REQUESTER2).status_code == 404
    r = client.patch(f"/requests/{rid}/verify", headers=STAFF, json={"decision": "reject"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.patch(f"/requests/{rid}/verify", headers=STAFF, json={"decision": "reject", "reason": "Formulir tidak cocok"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "rejected"
    r = client.patch(f"/requests/{rid}/verify", headers=STAFF, json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    assert client.patch(f"/requests/{rid}/cancel", headers=REQUESTER, json={"reason": "batal"}).status_code == 409

    rid2 = make_request(client, REQUESTER, fac)["id"]
    assert client.patch(f"/requests/{rid2}/cancel", headers=REQUESTER2, json={"reason": "iseng"}).status_code == 404
    r = client.patch(f"/requests/{rid2}/cancel", headers=REQUESTER, json={"reason": "Pasien dirujuk"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    mine = client.get("/requests/mine", headers=REQUESTER).json()
    assert mine["meta"]["total"] >= 2


def test_request_validation(client):
    base = (-12.50, 115.00)
    fac = make_facility(client, *base, name="RS Uji Validasi")
    from app import clock
    bad = {"facility_id": fac, "patient_ref": "FRM-1", "blood_type": "a", "rhesus": "positive", "component": "prc",
           "units": 1, "needed_by": clock.now_iso()}
    assert client.post("/requests", headers=REQUESTER, json=bad).status_code == 422
    assert client.post("/requests", headers=REQUESTER, json={**bad, "blood_type": "x"}).status_code == 422
    assert client.post("/requests", headers=REQUESTER, json={**bad, "needed_by": "2026-12-01T10:00:00"}).status_code == 422
    far = clock.iso(clock.utcnow().replace(year=clock.utcnow().year + 1))
    assert client.post("/requests", headers=REQUESTER, json={**bad, "needed_by": far}).status_code == 422
    assert client.post("/requests", headers=REQUESTER, json={**bad, "facility_id": "fac_99999",
                                                             "needed_by": far}).status_code == 404
    assert client.post("/requests", headers=donor(999), json=bad).status_code == 403
