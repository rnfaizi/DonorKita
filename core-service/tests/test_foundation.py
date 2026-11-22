"""Pengujian fondasi: /health, envelope sukses, envelope error, seq, umpan perubahan, event."""
import re

from app import events
from app.config import settings
from app.db import connect
from app.meta import current_seq

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def assert_error(resp, status, code):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["error"]["request_id"]
    assert resp.headers["X-Request-ID"] == body["error"]["request_id"]


def test_health_is_public_and_exact(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_success_envelope_shape(client):
    r = client.get("/_test/echo", params={"n": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {"n": 3}
    meta = body["meta"]
    assert set(meta) == {"seq", "updated_at", "source", "stale", "total"}
    assert isinstance(meta["seq"], int)
    assert ISO_Z.match(meta["updated_at"])
    assert meta["source"] == "live"
    assert meta["stale"] is False


def test_source_is_mock_when_mock_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "mock_mode", True)
    assert client.get("/_test/echo", params={"n": 1}).json()["meta"]["source"] == "mock"


def test_not_found_envelope(client):
    assert_error(client.get("/tidak-ada"), 404, "not_found")


def test_method_not_allowed_maps_to_not_found(client):
    assert_error(client.post("/health"), 404, "not_found")


def test_validation_error_envelope(client):
    assert_error(client.get("/_test/echo", params={"n": 0}), 422, "validation_error")
    assert_error(client.get("/_test/echo"), 422, "validation_error")


def test_api_error_keeps_its_code(client):
    assert_error(client.get("/_test/api-key"), 401, "invalid_api_key")


def test_unhandled_error_is_enveloped(client):
    assert_error(client.get("/_test/crash"), 500, "internal_error")


def test_request_id_is_echoed(client):
    r = client.get("/tidak-ada", headers={"X-Request-ID": "abc123"})
    assert r.json()["error"]["request_id"] == "abc123"


def test_publish_bumps_seq_records_change_and_calls_handler(client):
    seen = []
    events.subscribe("UjiTerjadi", lambda conn, body: seen.append(body))
    conn = connect()
    try:
        before = current_seq(conn)
        seq = events.publish(conn, "UjiTerjadi", change_type="test.happened",
                             entity_id="x1", summary="Uji", nilai=7)
        conn.commit()
        assert seq == before + 1 == current_seq(conn)
        row = conn.execute("SELECT * FROM changes WHERE seq=?", (seq,)).fetchone()
        assert (row["type"], row["entity_id"], row["summary"]) == ("test.happened", "x1", "Uji")
        assert ISO_Z.match(row["at"])
        assert seen and seen[-1]["nilai"] == 7 and seen[-1]["seq"] == seq
    finally:
        conn.close()
    # seq di envelope ikut naik
    assert client.get("/_test/echo", params={"n": 1}).json()["meta"]["seq"] == seq


def test_change_log_keeps_only_last_n(monkeypatch):
    monkeypatch.setattr(settings, "changes_keep", 50)
    conn = connect()
    try:
        for i in range(80):
            events.publish(conn, "UjiBanyak", change_type="test.bulk", entity_id=str(i), summary="x")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) c FROM changes").fetchone()["c"]
        newest = current_seq(conn)
        oldest = conn.execute("SELECT MIN(seq) s FROM changes").fetchone()["s"]
        assert count == 50 and oldest == newest - 49
    finally:
        conn.close()
