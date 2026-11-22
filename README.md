# DonorKita: Layanan Inti Pencocokan Kebutuhan Darah

Tugas Besar II3160 Teknologi Sistem Terintegrasi × II3140 Pengembangan Aplikasi Web & Mobile.
Kelompok **donorkita** (Kelas K02, tema Health & Wellbeing): Reza Nashwan Faizi (18224008).

- **Base URL (rencana):** `https://donorkita.stei.site/api`
- **Dokumentasi interaktif:** `https://donorkita.stei.site/api/docs`
- **Kontrak OpenAPI:** `docs/openapi-core.yaml` (diekspor dari kode)
- **Kesepakatan klaster:** `AGENTS.md` (milik KotaSiaga, disalin utuh; jangan diedit di repo ini)

> README ini masih kerangka. Bagian yang bertanda *(menyusul)* dilengkapi pada tahap berikutnya,
> lalu diekspor menjadi `README.pdf` untuk pengumpulan Tugas 2.

## Status pengerjaan

| Tahap | Isi | Status |
|---|---|---|
| 1 | Kerangka repo: konfigurasi, SQLite WAL, `meta.seq`, umpan perubahan, event bus, envelope sukses/error, `/health`, Dockerfile, ekspor OpenAPI | selesai |
| 2 | Aturan domain dan modul bounded context | belum |
| 3 | auth-service, mock mode, pengujian, kontrak lengkap | belum |
| 4 | Docker Compose, gateway Nginx, Cloudflare Tunnel, deploy STB | belum |
| 5 | README.pdf, API Registry, API key untuk kelompok lain | belum |

## Struktur

```
core-service/app/
  main.py        aplikasi FastAPI, pendaftaran router
  config.py      semua konfigurasi dari environment
  db.py          koneksi SQLite (WAL) dan pendaftaran skema per konteks
  meta.py        meta.seq dan umpan perubahan (500 terakhir)
  events.py      domain event sinkron; setiap publish menaikkan seq
  common.py      envelope sukses {data, meta}
  errors.py      envelope error {error: {code, message, request_id}}
  domain/        aturan domain murni tanpa database (Tahap 2)
  modules/       satu file per bounded context (Tahap 2)
  adapters/      pemanggilan sumber luar dengan timeout, retry, cache (Tahap 2)
core-service/tests/   pengujian pytest
tools/export_openapi.py
docs/openapi-core.yaml
```

## Menjalankan di laptop

```bash
cd core-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
CORE_DB_PATH=./dev.db uvicorn app.main:app --reload --root-path /api
# buka http://127.0.0.1:8000/docs  dan  http://127.0.0.1:8000/health
```

## Pengujian

```bash
cd core-service && python -m pytest tests -q
```

## Ekspor kontrak

```bash
python tools/export_openapi.py core > docs/openapi-core.yaml
```

## Format respons

Sukses: `{"data": ..., "meta": {"seq", "updated_at", "source", "stale"}}`; daftar menambah `meta.total`.
Error: `{"error": {"code", "message", "request_id"}}`; header `X-Request-ID` ikut dikirim.

| HTTP | `error.code` |
|---|---|
| 401 | `invalid_api_key`, `invalid_token`, `invalid_credentials` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 409 | `conflict` |
| 422 | `validation_error` |
| 429 | `rate_limited` |
| 503 | `upstream_unavailable` |
| 500 | `internal_error` (lihat "Perubahan kontrak") |

## Perubahan kontrak yang perlu disampaikan ke owner klaster

- Usulan kode `internal_error` (HTTP 500) sebagai jaring pengaman untuk error tak terduga; AGENTS.md bagian 3 belum mencantumkannya.

## Cara mendapat akses *(menyusul, Tahap 3)*

## Daftar endpoint *(menyusul, Tahap 2)*

## Mock mode *(menyusul, Tahap 3)*

## Deploy di STB *(menyusul, Tahap 4)*
