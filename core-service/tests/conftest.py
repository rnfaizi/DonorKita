import os
import sys
import tempfile
import time

import pytest

os.environ["CORE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "core.db")
os.environ["MOCK_MODE"] = "false"
os.environ["ENABLE_JOBS"] = "false"
os.environ["SEED_DEMO"] = "true"
os.environ["JWT_SECRET"] = "rahasia-uji-minimal-32-karakter-aman!!"
os.environ["STATIC_API_KEYS"] = "dk_test_key"
os.environ.pop("AUTH_INTERNAL_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt  # noqa: E402
from fastapi import Depends, Query  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import clock, security  # noqa: E402
from app.common import ok  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.main import app  # noqa: E402

API_KEY = {"X-API-Key": "dk_test_key"}


def token(uid: int, role: str, name: str = "Uji") -> dict:
    payload = {"sub": str(uid), "role": role, "name": name, "exp": int(time.time()) + 3600}
    return {"Authorization": "Bearer " + jwt.encode(payload, settings.jwt_secret, algorithm="HS256")}


ADMIN = token(1, "admin", "Admin Uji")
STAFF = token(2, "staff", "Petugas Uji")
REQUESTER = token(3, "requester", "Pemohon Uji")
REQUESTER2 = token(4, "requester", "Pemohon Dua")


def donor(uid: int) -> dict:
    return token(uid, "donor", f"Pendonor {uid}")


# Route khusus pengujian untuk memeriksa fondasi (tidak ada di aplikasi sebenarnya).
@app.get("/_test/echo", include_in_schema=False)
def _echo(n: int = Query(..., ge=1), conn=Depends(get_conn)):
    return ok(conn, {"n": n}, total=1)


@app.get("/_test/api-key", include_in_schema=False)
def _api_key():
    raise ApiError(401, "invalid_api_key", "API key tidak valid")


@app.get("/_test/crash", include_in_schema=False)
def _crash():
    raise RuntimeError("boom")


@pytest.fixture(scope="session")
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate():
    security.clear_key_cache()
    clock.reset()
    yield
    clock.reset()
