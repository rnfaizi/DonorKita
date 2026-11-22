# DonorKita: Layanan Inti Pencocokan Kebutuhan Darah

Tugas Besar II3160 Teknologi Sistem Terintegrasi × II3140 Pengembangan Aplikasi Web & Mobile.
Kelompok **donorkita** (Kelas K02, tema Health & Wellbeing): Reza Nashwan Faizi (18224008).

- **Base URL (rencana):** `https://donorkita.stei.site/api`
- **Dokumentasi interaktif:** `https://donorkita.stei.site/api/docs`
- **Dokumentasi auth:** `https://donorkita.stei.site/api/auth/docs`
- **Kontrak OpenAPI:** `docs/openapi-core.yaml` dan `docs/openapi-auth.yaml` (diekspor dari kode)
- **Kesepakatan klaster:** `AGENTS.md` (milik KotaSiaga, disalin utuh; jangan diedit di repo ini)

> README ini masih berkembang. Bagian bertanda *(menyusul)* dilengkapi pada tahap berikutnya,
> lalu diekspor menjadi `README.pdf` untuk pengumpulan Tugas 2.

## Status pengerjaan

| Tahap | Isi | Status |
|---|---|---|
| 1 | Kerangka: konfigurasi, SQLite WAL, `meta.seq`, umpan perubahan, event bus, envelope, `/health`, Dockerfile | selesai |
| 2 | Aturan domain dan modul bounded context, API publik dan API aplikasi, job latar belakang, data uji | selesai |
| 3a | auth-service: akun, JWT + refresh token, API key kelompok lain, akun uji | selesai |
| 3b | Docker Compose (core, auth, gateway, tunnel) dan gateway Nginx | selesai |
| 3c | Mock mode (AGENTS.md bagian 7) | belum |
| 4 | Deploy STB dan Cloudflare Tunnel | panduan siap, menunggu eksekusi |
| 5 | README.pdf, API Registry, API key untuk kelompok lain | belum |

## Arsitektur deploy

```
Klien (aplikasi, kelompok lain)
  -> Cloudflare Tunnel (https://donorkita.stei.site)  -> tunnel  (cloudflared)
  -> gateway  (Nginx 1.28, :80; di STB dipetakan ke GATEWAY_PORT=8081)
       /api/auth/*  -> auth  (FastAPI, SQLite auth.db)
       /api/*       -> core  (FastAPI, SQLite core.db)
```

| Kontainer | Peran | Batas sumber daya |
|---|---|---|
| `gateway` (Nginx) | Satu-satunya pintu masuk: routing, pembatasan pemanggilan (10 permintaan/detik per IP, login 10/menit), memblokir `/api/auth/internal`, error JSON | 64 MB, 0,25 CPU |
| `core` (FastAPI) | Bounded context bisnis sebagai modul; SQLite `core.db` di volume `core-data` | 256 MB, 0,5 CPU |
| `auth` (FastAPI) | Identitas & Akses: akun, JWT, refresh token, API key; SQLite `auth.db` di volume `auth-data` | 128 MB, 0,25 CPU |
| `tunnel` (cloudflared, profil `tunnel`) | Membuka gateway ke internet tanpa membuka port | 64 MB, 0,25 CPU |

Semua kontainer memakai `restart: unless-stopped`. Basis data tidak membuka port. Pada uji
lokal, core memakai sekitar 65 MB dan auth sekitar 53 MB memori.

## Arsitektur kode

Bounded context dari makalah DDD berjalan sebagai modul dalam satu `core-service`, masing-masing
dengan tabelnya sendiri. Modul tidak membaca tabel modul lain; mereka berkomunikasi lewat domain
event sinkron (`app/events.py`, pemetaan di `app/handlers.py`) atau antarmuka kueri yang jelas.

| Bounded context (makalah) | Jenis | Modul | Event yang diterbitkan |
|---|---|---|---|
| Permintaan Darah | core | `modules/requests.py` | PermintaanDiajukan, PermintaanTerverifikasi, PermintaanDitolak, PermintaanTerpenuhi, PermintaanKedaluwarsa, PermintaanDibatalkan |
| Pencocokan | core | `modules/matching.py` + `domain/compatibility.py`, `domain/scoring.py` | SesiPencocokanDimulai, UndanganDiterbitkan, UndanganDiterima, UndanganDitolak, SesiDiperluas, DonasiTercatat, PendonorDitunda |
| Pendonor & Kelayakan | supporting | `modules/donors.py` + `domain/eligibility.py` | ProfilPendonorDiperbarui, PraSkriningDikirim |
| Kegiatan Donor | supporting | `modules/drives.py` | KegiatanDonorDibuat, KegiatanDonorDibatalkan, SlotKegiatanBerubah, DonasiTercatat |
| Stok Darah | supporting | `modules/stock.py` | StokDiperbarui, StokMenipis |
| Notifikasi | generic | `modules/notifications.py` | tidak ada (conformist terhadap event lain) |
| Lokasi | generic | `modules/locations.py` | FasilitasDitambahkan |
| Identitas & Akses | generic | `security.py` (core), auth-service *(Tahap 3)* | tidak ada |

Alur utama:

1. Pemohon membuat permintaan (`submitted`). Permintaan dari petugas langsung terverifikasi.
2. Petugas memverifikasi nomor formulir RS, lalu terbit `PermintaanTerverifikasi`.
3. Pencocokan membuka sesi pada radius 5 km, mengambil kandidat layak dari Pendonor & Kelayakan,
   mengurutkannya dengan skor prioritas, lalu mengundang sejumlah `sisa kantong × faktor urgensi`
   (normal 2, urgent 3, emergency 4). Status permintaan menjadi `searching`.
4. Pendonor menerima undangan dan mendapat kode kedatangan 6 karakter.
5. Petugas mencatat kedatangan dengan kode (`donated`, `deferred`, `no_show`). `DonasiTercatat`
   menambah kantong terpenuhi, memperbarui riwayat dan masa jeda pendonor, dan mengirim notifikasi.
6. Bila pendonor yang bersedia belum cukup, job latar belakang memperluas radius
   (5 → 10 → 20 km) setelah 60/30/15 menit (normal/urgent/emergency). Permintaan yang melewati
   `needed_by` menjadi `expired`.

Aturan domain (semua angka dapat disetel lewat `.env`):

- **Kompatibilitas:** PRC mengikuti aturan sel darah merah (O ke semua; AB menerima semua),
  plasma kebalikannya, darah lengkap dan trombosit harus ABO identik. Penerima rhesus negatif
  hanya dari negatif untuk komponen bersel darah merah. Ini hanya penyaringan kandidat; uji silang
  tetap di UDD/BDRS.
- **Kelayakan:** usia minimal 17 tahun (pendonor pertama > 60 atau ulang > 65 perlu pertimbangan
  dokter), berat minimal 45 kg, jeda donor 60 hari, maksimal 5 kali per 365 hari, tidak sedang
  ditunda, dan pra-skrining dalam 30 hari terakhir tanpa jawaban berisiko.
- **Skor prioritas:** 0,5 × kedekatan + 0,3 × keandalan (donasi+1)/(diundang+2)
  + 0,1 × golongan identik + 0,1 × lama sejak layak.

## Cara mendapat akses

| Pemanggil | Autentikasi | Cara mendapatkan |
|---|---|---|
| Pengguna aplikasi | `Authorization: Bearer <access_token>` | `POST /api/auth/register` (peran `donor` atau `requester`), lalu `POST /api/auth/login`. Access token berlaku 12 jam; perbarui dengan `POST /api/auth/refresh` |
| Petugas UDD/RS | JWT peran `staff` | Dibuat admin lewat `POST /api/auth/users` |
| Kelompok lain (server ke server) | `X-API-Key: <key>` | Minta ke admin DonorKita. Admin membuat lewat `POST /api/auth/api-keys`; key hanya tampil sekali dan dapat dicabut kapan saja |
| Publik | tanpa autentikasi | Hanya `/api/health` |

Endpoint baca publik (`/donation-events`, `/blood-stock`, `/requests/summary`, `/facilities`,
`/changes`) menerima `X-API-Key` atau JWT pengguna. Pencabutan API key berlaku paling lambat
60 detik karena core menyimpan hasil verifikasi sebentar.

**Akun uji** (data uji, bukan data pribadi asli), password sesuai `DEMO_PASSWORD` di `.env`:

| Email | Peran |
|---|---|
| `pendonor@demo.test` | `donor` (profil golongan O+ sudah lengkap dan layak) |
| `pemohon@demo.test` | `requester` |
| `petugas@demo.test` | `staff` |

Ditambah satu API key uji sesuai `SEED_API_KEY`, dan akun admin dari `ADMIN_EMAIL`/`ADMIN_PASSWORD`.

### Endpoint auth (`/api/auth`)

| Metode | Path | Akses | Fungsi |
|---|---|---|---|
| GET | `/health` | publik | `{"status":"ok"}` |
| POST | `/register` | publik | Daftar akun `donor` atau `requester` |
| POST | `/login` | publik (10/menit) | Access token (JWT) + refresh token; salah password: `invalid_credentials` |
| POST | `/refresh` | refresh token | Rotasi token |
| POST | `/logout` | refresh token | Cabut refresh token |
| GET | `/me` | jwt | Profil akun |
| POST/GET | `/users` | jwt admin | Buat akun peran apa pun (mis. petugas) / daftar akun |
| POST/GET | `/api-keys` | jwt admin | Buat API key / daftar key (tanpa nilai key) |
| DELETE | `/api-keys/{id}` | jwt admin | Cabut API key |

### Contoh request

```bash
BASE=https://donorkita.stei.site/api      # di LAN STB: http://192.168.137.96:8081/api

curl $BASE/health

# Kelompok lain: kegiatan donor terdekat dan stok yang menipis
curl -H "X-API-Key: <api-key>" "$BASE/donation-events?lat=-6.89&lon=107.61&radius_m=10000"
curl -H "X-API-Key: <api-key>" "$BASE/blood-stock?level=low"
curl -H "X-API-Key: <api-key>" "$BASE/changes?since=0"

# Login lalu simpan token
TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"pemohon@demo.test","password":"<DEMO_PASSWORD>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")

# Ajukan permintaan darah
curl -X POST $BASE/requests -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"facility_id":"fac_3","patient_ref":"FRM-001","blood_type":"o","rhesus":"positive",
       "component":"prc","units":1,"urgency":"urgent","needed_by":"2026-12-01T10:00:00Z"}'

# Admin membuat API key untuk kelompok lain
curl -X POST $BASE/auth/api-keys -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"KotaSiaga (Smart City)"}'
```

## Daftar endpoint

Akses: `key/jwt` = header `X-API-Key` (kelompok lain) atau JWT pengguna; `jwt <peran>` = JWT dengan peran tersebut.

### API publik untuk kelompok lain

| Metode | Path | Akses | Isi |
|---|---|---|---|
| GET | `/health` | publik | `{"status":"ok"}` |
| GET | `/donation-events?lat&lon&radius_m&from&to&limit&offset` | key/jwt | Kegiatan donor: `id, name, lat, lon, opens_at, closes_at, slots_total, slots_free`, plus `organizer, address, facility_id, needed_blood_types, distance_m` |
| GET | `/donation-events/{id}` | key/jwt | Detail kegiatan beserta slot per jam |
| GET | `/blood-stock?lat&lon&radius_m&facility_id&level` | key/jwt | Stok per fasilitas: `blood_type, rhesus, component, units, level` (`sufficient`, `low`, `critical`) |
| GET | `/requests/summary?bbox` | key/jwt | Jumlah permintaan aktif per golongan, komponen, dan urgensi, tanpa data pribadi |
| GET | `/facilities?lat&lon&radius_m&kind` | key/jwt | UDD, rumah sakit, BDRS |
| GET | `/changes?since&limit` | key/jwt | Umpan perubahan (500 terakhir); `meta.reset=true` bila `since` terlalu lama |

Tipe perubahan di `/changes`: `request.created`, `request.verified`, `request.rejected`, `request.fulfilled`,
`request.expired`, `request.cancelled`, `match.started`, `match.escalated`, `invitation.issued`,
`invitation.accepted`, `invitation.declined`, `invitation.no_show`, `donation.recorded`, `donor.deferred`,
`donor.updated`, `event.created`, `event.cancelled`, `event.slots_changed`, `stock.updated`, `stock.low`,
`facility.created`.

### API aplikasi

| Metode | Path | Akses | Fungsi |
|---|---|---|---|
| GET/PUT | `/donors/me` | jwt donor | Profil pendonor beserta status kelayakan |
| POST | `/donors/me/screening` | jwt donor | Kuesioner pra-skrining |
| GET | `/donors/me/eligibility`, `/donors/me/history` | jwt donor | Kelayakan, riwayat donasi |
| POST | `/requests` | jwt requester/staff | Ajukan permintaan darah |
| GET | `/requests/mine` | jwt | Permintaan saya |
| GET | `/requests` | jwt staff | Semua permintaan (filter `status`, `facility_id`) |
| GET | `/requests/{id}` | jwt pemilik/staff | Detail permintaan |
| PATCH | `/requests/{id}/verify` | jwt staff | `approve` atau `reject` (alasan wajib) |
| PATCH | `/requests/{id}/cancel` | jwt pemilik/staff | Batalkan permintaan |
| GET | `/requests/{id}/progress` | jwt pemilik/staff | Radius, putaran, dan jumlah undangan per status |
| GET | `/invitations`, `/invitations/{id}` | jwt donor | Undangan saya |
| POST | `/invitations/{id}/accept`, `/invitations/{id}/decline` | jwt donor | Terima (mendapat kode) atau tolak |
| POST | `/arrivals` | jwt staff | Catat kedatangan dengan kode: `donated`, `deferred`, `no_show` |
| POST | `/donation-events` | jwt staff | Jadwalkan kegiatan donor |
| PATCH | `/donation-events/{id}/cancel` | jwt staff | Batalkan kegiatan |
| POST | `/donation-events/{id}/reservations` | jwt donor | Pesan slot (kelayakan diperiksa) |
| GET | `/donation-events/{id}/reservations` | jwt staff | Daftar reservasi kegiatan |
| GET | `/reservations/mine` | jwt donor | Reservasi saya |
| DELETE | `/reservations/{id}` | jwt donor | Batalkan reservasi |
| POST | `/reservations/{id}/outcome` | jwt staff | Catat hasil kedatangan reservasi |
| PUT | `/blood-stock/{facility_id}` | jwt staff | Perbarui stok |
| POST | `/facilities` | jwt admin | Tambah fasilitas |
| GET | `/notifications`, POST `/notifications/{id}/read` | jwt | Notifikasi in-app |

ID berupa string buram berawalan: `req_`, `inv_`, `evt_`, `res_`, `fac_`, `ntf_`.

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

## Menjalankan dengan Docker

```bash
cp .env.example .env        # isi JWT_SECRET, INTERNAL_TOKEN, ADMIN_PASSWORD, DEMO_PASSWORD, SEED_API_KEY
docker compose up -d --build
curl http://localhost:8081/api/health            # {"status":"ok"}
docker compose ps                                # core, auth, gateway: healthy
docker compose --profile tunnel up -d            # tambah Cloudflare Tunnel (butuh TUNNEL_TOKEN)
```

## Menjalankan di laptop

```bash
cd core-service
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# Windows PowerShell: $env:CORE_DB_PATH="./dev.db"; $env:SEED_DEMO="true"; $env:STATIC_API_KEYS="dk_dev_key"
CORE_DB_PATH=./dev.db SEED_DEMO=true STATIC_API_KEYS=dk_dev_key uvicorn app.main:app --reload --root-path /api
# buka http://127.0.0.1:8000/docs
curl -H "X-API-Key: dk_dev_key" http://127.0.0.1:8000/donation-events
```

Untuk mencoba endpoint ber-JWT sebelum auth-service ada (Tahap 3), buat token uji dengan
`JWT_SECRET` yang sama, misalnya:

```bash
python -c "import jwt,time;print(jwt.encode({'sub':'10','role':'donor','name':'Uji','exp':int(time.time())+3600},'dev-only-secret-ganti-di-env-min-32-karakter',algorithm='HS256'))"
```

## Pengujian

```bash
cd core-service && python -m pytest tests -q
cd auth-service && python -m pytest tests -q
```

auth-service: 10 pengujian (daftar, login, klaim JWT, validasi, larangan mendaftar sebagai
petugas, rotasi refresh token, pembuatan akun petugas oleh admin, siklus API key termasuk
pencabutan, API key uji, kenaikan `seq`).

core-service: 52 pengujian: fondasi (envelope, error, seq, umpan perubahan), aturan domain (kompatibilitas,
kelayakan, skor, geo), alur penuh permintaan sampai terpenuhi, eskalasi radius, kedaluwarsa,
penundaan pendonor, verifikasi dan hak akses, validasi input, kontrak kegiatan donor untuk
KotaSiaga (`TitikTujuan`), reservasi dan slot, level stok dan notifikasi, ringkasan tanpa data
pribadi, dan reset umpan perubahan.

## Mock mode *(menyusul, Tahap 3)*

## Deploy di STB

STB: Armbian (Debian 13, arm64, RAM 2 GB) dengan Docker dan Docker Compose v2. Proyek diletakkan di
`/www/apps/donorkita`. Hanya kontainer microservice yang berjalan di STB.

### 1. Ambil kode dan siapkan `.env`

```bash
free -h && df -h /                       # pastikan RAM dan disk cukup
mkdir -p /www/apps && cd /www/apps
git clone https://github.com/<username>/donorkita.git && cd donorkita
cp .env.example .env
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 32)|; \
        s|^INTERNAL_TOKEN=.*|INTERNAL_TOKEN=$(openssl rand -hex 16)|; \
        s|^SEED_API_KEY=.*|SEED_API_KEY=dk_$(openssl rand -hex 16)|" .env
nano .env                                # isi ADMIN_PASSWORD dan DEMO_PASSWORD
chmod 600 .env
```

### 2. Build dan jalankan (tanpa tunnel)

```bash
docker compose up -d --build             # build pertama di ARM beberapa menit
docker compose ps                        # core, auth, gateway: healthy
curl localhost:8081/api/health           # {"status":"ok"}
docker stats --no-stream                 # cek memori tiap kontainer
ufw allow 8081/tcp                       # opsional: akses dari laptop lewat LAN
```

### 3. Cloudflare Tunnel ke `https://donorkita.stei.site`

1. Pastikan nama `donorkita` sudah dipesan di API Registry kelas.
2. Login ke akun Cloudflare kelas (lihat slide brief), buka **Networks > Tunnels > Create a tunnel >
   Cloudflared**, beri nama `k02-donorkita`.
3. Pada langkah instal connector, salin **token saja** (string panjang setelah `--token` atau setelah
   `service install`). Jangan jalankan perintah instal yang ditampilkan.
4. Tab **Public Hostname > Add**: Subdomain `donorkita`, Domain `stei.site`, Path kosong,
   Type `HTTP`, URL `gateway:80`. Simpan, lalu logout.
5. Di STB:

```bash
nano .env                                # TUNNEL_TOKEN=<token>
docker compose --profile tunnel up -d
docker compose logs tunnel | tail -5     # "Registered tunnel connection"
curl https://donorkita.stei.site/api/health
```

Aturan akun bersama: hanya ubah tunnel dan hostname milik sendiri, jangan ubah pengaturan lain,
dan jangan menyimpan kredensial akun atau token di repositori.

### 4. API key untuk kelompok lain

```bash
BASE=https://donorkita.stei.site/api
ADMIN_TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"<ADMIN_EMAIL>","password":"<ADMIN_PASSWORD>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['access_token'])")
curl -s -X POST $BASE/auth/api-keys -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"KotaSiaga (Smart City)"}'
curl -s -X POST $BASE/auth/api-keys -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"JelajahLokal (Tourism)"}'
```

Nilai `key` hanya tampil sekali; kirim ke kelompok terkait lewat jalur pribadi.

### Operasional

```bash
cd /www/apps/donorkita
git pull && docker compose --profile tunnel up -d --build    # perbarui dari GitHub
docker compose logs -f core                                  # log (ganti nama service)
docker compose restart core                                  # restart satu service
# Backup basis data (aman saat layanan berjalan)
docker compose exec core python -c "import sqlite3; sqlite3.connect('/data/core.db').execute(\"VACUUM INTO '/data/backup-core.db'\")"
docker compose cp core:/data/backup-core.db ./backup-core-$(date +%F).db
```

| Gejala | Penyebab dan solusi |
|---|---|
| Build gagal mengunduh paket | DNS STB; pastikan `/etc/netplan/30-eth0-static.yaml` memakai `1.1.1.1`, `8.8.8.8` |
| `port is already allocated` | Port 8081 terpakai; ubah `GATEWAY_PORT` di `.env` |
| Tunnel *Inactive/Down* | Token salah atau STB offline; cek `.env` dan `docker compose logs tunnel` |
| Error 502 di domain | URL hostname bukan `gateway:80`, atau gateway belum healthy (`docker compose ps`) |
| Kontainer berhenti karena memori | Matikan layanan lain yang tidak dipakai (`docker compose down` di folder proyek lain) |

## Perubahan kontrak yang perlu disampaikan ke owner klaster

- Usulan kode `internal_error` (HTTP 500) sebagai jaring pengaman; AGENTS.md bagian 3 belum mencantumkannya.
- Path Health di AGENTS.md (sebelumnya TBD) kini: `/donation-events`, `/donation-events/{id}`, `/blood-stock`,
  `/requests/summary`, `/facilities`, `/changes`. Tambahan dibanding Tabel 4 makalah: `/facilities` dan
  field `needed_blood_types` pada kegiatan donor.
- Base URL JelajahLokal yang sebenarnya: `https://chitatomigoreng.stei.site/api` (di AGENTS.md masih TBD).

## Batasan saat ini

- Petugas (`staff`) belum dibatasi per fasilitas; semua petugas dapat memverifikasi permintaan di fasilitas mana pun.
- Ambang stok `low`/`critical` berlaku global, belum per fasilitas atau komponen.
- Fasilitas diisi admin atau data uji; adapter OpenStreetMap (Overpass) belum dibuat.
- Notifikasi hanya in-app (dipoll aplikasi); push notification ditangani di aplikasi/layanan integrasi.
