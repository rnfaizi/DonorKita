"""DonorKita core service.

Bounded context bisnis berjalan sebagai modul di app/modules/ dalam satu proses dan
berkomunikasi lewat domain event (events.py, handlers.py):
  Permintaan Darah (core), Pencocokan (core), Pendonor & Kelayakan, Kegiatan Donor,
  Stok Darah, Notifikasi, Lokasi.
Identitas & Akses berjalan di auth-service. Aturan domain murni (kompatibilitas golongan
darah, kelayakan, skor prioritas) ada di app/domain/ tanpa akses database.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import errors, handlers, jobs
from .config import settings
from .db import connect, init_db
from .modules import changes, donors, drives, locations, matching, notifications, requests, stock
from .seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

DESCRIPTION = """
Layanan inti pencocokan kebutuhan darah dengan pendonor sukarela. Semua path relatif
terhadap `/api` (base URL `https://donorkita.stei.site/api`).

**Autentikasi**
- Pengguna aplikasi: `Authorization: Bearer <JWT>` dari `POST /api/auth/login`.
  Peran: `donor`, `requester`, `staff`, `admin`.
- Kelompok lain (server ke server): header `X-API-Key`. Endpoint baca publik
  (`/donation-events`, `/blood-stock`, `/requests/summary`, `/facilities`, `/changes`)
  menerima X-API-Key atau JWT.

**Format respons**
- Sukses: `{"data": ..., "meta": {"seq", "updated_at", "source", "stale"}}`.
- Error: `{"error": {"code", "message", "request_id"}}`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    handlers.register()
    if settings.seed_demo:
        conn = connect()
        try:
            seed_demo(conn)
            conn.commit()
        finally:
            conn.close()
    task = asyncio.create_task(jobs.run_forever()) if settings.enable_jobs else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="DonorKita Core Service", version=settings.version,
              description=DESCRIPTION, lifespan=lifespan)
errors.install(app)

for module in (requests, matching, donors, drives, stock, locations, notifications, changes):
    app.include_router(module.router)


@app.get("/health", tags=["umum"], summary="Cek layanan hidup (publik)")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}
