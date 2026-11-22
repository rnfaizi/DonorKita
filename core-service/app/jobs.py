"""Job latar belakang: kedaluwarsa permintaan dan eskalasi radius pencocokan."""
import asyncio
import logging

from .config import settings
from .db import connect
from .modules import matching, requests

log = logging.getLogger("donorkita.jobs")


def run_once() -> dict:
    conn = connect()
    try:
        expired = requests.expire_overdue(conn)
        escalated = matching.escalate_all(conn)
        conn.commit()
        return {"expired": expired, "escalated": escalated}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def run_forever() -> None:
    while True:
        await asyncio.sleep(settings.job_interval_seconds)
        try:
            run_once()
        except Exception:  # job tidak boleh mematikan layanan
            log.exception("job gagal")
