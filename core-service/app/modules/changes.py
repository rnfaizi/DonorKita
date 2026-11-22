"""Umpan perubahan untuk live update (AGENTS.md bagian 4 dan 7).

GET /changes?since=<seq> mengembalikan perubahan dengan seq > since, urut naik.
Hanya 500 perubahan terakhir yang disimpan; bila `since` lebih tua dari itu, meta.reset=true
dan konsumen sebaiknya mengambil ulang resource secara penuh.
"""
from fastapi import APIRouter, Depends, Query

from ..common import ok
from ..db import get_conn
from ..meta import current_seq, oldest_seq
from ..security import Caller, api_key_or_user

router = APIRouter(tags=["umum"])


@router.get("/changes", summary="Umpan perubahan sejak seq tertentu")
def changes(since: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200),
            _: Caller = Depends(api_key_or_user), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM changes WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit)).fetchall()
    oldest = oldest_seq(conn)
    reset = since > 0 and oldest is not None and since < oldest - 1
    data = [{"seq": r["seq"], "at": r["at"], "type": r["type"], "entity_id": r["entity_id"],
             "summary": r["summary"]} for r in rows]
    return ok(conn, data, extra_meta={"reset": reset, "latest_seq": current_seq(conn)})
