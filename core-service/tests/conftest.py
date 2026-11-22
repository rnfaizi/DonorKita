import os
import sys
import tempfile

import pytest

os.environ["CORE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "core.db")
os.environ["MOCK_MODE"] = "false"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends, Query  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.common import ok  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.main import app  # noqa: E402


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
