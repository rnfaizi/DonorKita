"""Mock mode: generator menggerakkan data lewat kode domain asli, dan skenario demo."""
import random

import pytest

from app import mock
from app.config import settings
from app.db import connect
from app.meta import current_seq

ADMIN_KEY = {"X-API-Key": "kunci-admin-mock-uji"}


@pytest.fixture
def mock_on(monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    monkeypatch.setattr(settings, "mock_admin_api_key", "kunci-admin-mock-uji")
    monkeypatch.setattr(settings, "mock_seed", 42)
    mock.start()
    yield


def test_mock_endpoints_hidden_when_off(client):
    assert client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "reset"}).status_code == 404
    assert client.get("/_mock/state", headers=ADMIN_KEY).status_code == 404


def test_mock_endpoints_need_admin_key(client, mock_on):
    r = client.get("/_mock/state")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    assert client.get("/_mock/state", headers={"X-API-Key": "salah"}).status_code == 401
    body = client.get("/_mock/state", headers=ADMIN_KEY).json()
    assert body["data"]["seed"] == 42 and body["meta"]["source"] == "mock"


def test_every_tick_moves_seq_and_uses_domain_flow(client, mock_on):
    rng = random.Random(7)
    conn = connect()
    try:
        seqs = [current_seq(conn)]
        for _ in range(40):
            seqs.append(mock.tick(conn, rng))
            conn.commit()
        assert all(b > a for a, b in zip(seqs, seqs[1:])), "seq harus naik di setiap tick"
        types = {r["type"] for r in conn.execute("SELECT type FROM changes WHERE seq > ?", (seqs[0],))}
        for t in ("request.created", "request.verified", "match.started", "invitation.issued", "stock.updated"):
            assert t in types, t
        # status hanya berubah lewat kode domain: setiap permintaan mock yang terpenuhi punya donasi tercatat
        for r in conn.execute("SELECT * FROM blood_requests WHERE requester_id >= 900000 AND status = 'fulfilled'"):
            assert r["units_fulfilled"] >= r["units_needed"]
    finally:
        conn.close()
    assert set(mock.state.counts) & {"new_request", "donor_response", "arrival", "stock_change"}


def test_scenarios(client, mock_on):
    r = client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "emergency_request", "lat": -6.89, "lon": 107.61})
    assert r.status_code == 200, r.text
    rid = r.json()["data"]["request_id"]
    assert rid and rid.startswith("req_")
    from conftest import STAFF
    progress = client.get(f"/requests/{rid}/progress", headers=STAFF).json()["data"]
    assert progress["invitations"]["invited"] > 0 and progress["units_needed"] == 3

    seq = current_seq_via_api(client)
    r = client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "stock_low"})
    assert r.status_code == 200 and r.json()["data"]["units"] == settings.stock_critical_units
    types = [c["type"] for c in client.get(f"/changes?since={seq}", headers=STAFF).json()["data"]]
    assert "stock.low" in types

    r = client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "event_full"})
    assert r.status_code == 200 and r.json()["data"]["reserved"] > 0

    assert client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "mass_arrival"}).status_code == 200
    r = client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "reset"})
    assert r.status_code == 200
    conn = connect()
    try:
        active = conn.execute("SELECT COUNT(*) c FROM blood_requests WHERE requester_id >= 900000 "
                              "AND status IN ('submitted', 'verified', 'searching')").fetchone()["c"]
        assert active == 0
    finally:
        conn.close()
    assert client.post("/_mock/scenario", headers=ADMIN_KEY, json={"name": "tidak_ada"}).status_code == 422


def current_seq_via_api(client):
    from conftest import STAFF
    return client.get("/changes", headers=STAFF).json()["meta"]["latest_seq"]
