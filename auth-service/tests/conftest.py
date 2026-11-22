import os
import sys
import tempfile

import pytest

os.environ["AUTH_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "auth.db")
os.environ["JWT_SECRET"] = "rahasia-uji-minimal-32-karakter-aman!!"
os.environ["INTERNAL_TOKEN"] = "token-internal-uji"
os.environ["ADMIN_EMAIL"] = "admin@donorkita.test"
os.environ["ADMIN_PASSWORD"] = "admin-uji-123"
os.environ["SEED_DEMO"] = "true"
os.environ["DEMO_PASSWORD"] = "demo-uji-123"
os.environ["SEED_API_KEY"] = "dk_kunci_uji_seed"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def login(client, email, password):
    r = client.post("/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]


@pytest.fixture
def admin_headers(client):
    return {"Authorization": "Bearer " + login(client, "admin@donorkita.test", "admin-uji-123")["access_token"]}
