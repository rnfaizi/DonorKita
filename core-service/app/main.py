"""DonorKita core service.

Bounded context bisnis (Permintaan Darah, Pencocokan, Pendonor & Kelayakan, Kegiatan Donor,
Stok Darah, Notifikasi) berjalan sebagai modul di app/modules/ dalam satu proses dan
berkomunikasi lewat domain event (events.py). Identitas & Akses berjalan di auth-service.
Aturan domain murni (kompatibilitas golongan darah, kelayakan, skor prioritas) ada di
app/domain/ tanpa akses database.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import errors
from .config import settings
from .db import connect, init_db

DESCRIPTION = """
Layanan inti pencocokan kebutuhan darah dengan pendonor sukarela. Semua path relatif
terhadap `/api` (base URL `https://donorkita.stei.site/api`).

**Autentikasi**
- Pengguna aplikasi: `Authorization: Bearer <JWT>` dari `POST /api/auth/login`.
- Kelompok lain (server ke server): header `X-API-Key`.

**Format respons**
- Sukses: `{"data": ..., "meta": {"seq", "updated_at", "source", "stale"}}`.
- Error: `{"error": {"code", "message", "request_id"}}`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="DonorKita Core Service", version=settings.version,
              description=DESCRIPTION, lifespan=lifespan)
errors.install(app)

# Router bounded context didaftarkan di sini pada Tahap 2, misalnya:
# for module in (donors, requests, matching, drives, stock, notifications, changes):
#     app.include_router(module.router)


@app.get("/health", tags=["umum"], summary="Cek layanan hidup (publik)")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}
