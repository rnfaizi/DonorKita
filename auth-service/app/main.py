"""DonorKita auth-service: bounded context Identitas & Akses (generic).

- Akun pengguna aplikasi dengan peran donor, requester, staff, admin.
  Pendaftaran publik hanya untuk donor dan requester; staff dibuat oleh admin.
- Login menghasilkan access token JWT (HS256, klaim sub/role/name/exp) dan refresh token.
- API key untuk kelompok lain (satu key per kelompok, dapat dicabut). Key hanya ditampilkan
  sekali saat dibuat; yang disimpan hanya hash SHA-256.
- /internal/verify-key dipanggil core-service (header X-Internal-Token) dan diblokir gateway.

Path di sini relatif terhadap /api/auth (uvicorn --root-path /api/auth).
"""
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Literal, Optional

import jwt
from fastapi import Depends, FastAPI, Header
from pydantic import BaseModel, Field, field_validator

from . import errors
from .errors import ApiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("donorkita.auth")

DEFAULT_SECRET = "dev-only-secret-ganti-di-env-min-32-karakter"
ROLES = ("donor", "requester", "staff", "admin")
PBKDF2_ITERATIONS = 120_000


class Settings:
    def __init__(self) -> None:
        self.db_path = os.getenv("AUTH_DB_PATH", "/data/auth.db")
        self.jwt_secret = os.getenv("JWT_SECRET", DEFAULT_SECRET)
        self.internal_token = os.getenv("INTERNAL_TOKEN", "")
        self.access_token_hours = int(os.getenv("ACCESS_TOKEN_HOURS", "12"))
        self.refresh_token_days = int(os.getenv("REFRESH_TOKEN_DAYS", "30"))
        self.admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.seed_demo = os.getenv("SEED_DEMO", "false").lower() == "true"
        self.demo_password = os.getenv("DEMO_PASSWORD", "demo12345")
        self.seed_api_key = os.getenv("SEED_API_KEY", "").strip()
        self.mock_mode = os.getenv("MOCK_MODE", "false").lower() == "true"


settings = Settings()

SCHEMA = """
CREATE TABLE IF NOT EXISTS seq_counter (id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL);
INSERT OR IGNORE INTO seq_counter (id, value) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('donor', 'requester', 'staff', 'admin')),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
"""


# ---------- utilitas ----------

def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_seq(conn) -> int:
    return int(conn.execute("SELECT value FROM seq_counter WHERE id = 1").fetchone()["value"])


def bump_seq(conn) -> None:
    conn.execute("UPDATE seq_counter SET value = value + 1 WHERE id = 1")


def ok(conn, data, total: Optional[int] = None) -> dict:
    meta = {"seq": current_seq(conn), "updated_at": iso(now()),
            "source": "mock" if settings.mock_mode else "live", "stale": False}
    if total is not None:
        meta["total"] = total
    return {"data": data, "meta": meta}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except ValueError:
        return False


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_api_key() -> str:
    return "dk_" + secrets.token_urlsafe(24)


def make_access_token(user) -> str:
    exp = now() + timedelta(hours=settings.access_token_hours)
    payload = {"sub": str(user["id"]), "role": user["role"], "name": user["name"], "exp": int(exp.timestamp()),
               "iat": int(time.time())}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def issue_tokens(conn, user) -> dict:
    refresh = secrets.token_urlsafe(32)
    expires = now() + timedelta(days=settings.refresh_token_days)
    conn.execute("INSERT INTO refresh_tokens (token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                 (sha256(refresh), user["id"], iso(expires), iso(now())))
    return {"access_token": make_access_token(user), "token_type": "bearer",
            "expires_in": settings.access_token_hours * 3600, "refresh_token": refresh, "user": user_out(user)}


def user_out(row) -> dict:
    return {"id": str(row["id"]), "email": row["email"], "name": row["name"], "role": row["role"],
            "created_at": row["created_at"]}


def ensure_user(conn, email: str, name: str, role: str, password: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                       (email, name, role, hash_password(password), iso(now())))
    bump_seq(conn)
    return int(cur.lastrowid)


def seed(conn) -> None:
    """Admin dari ADMIN_EMAIL/ADMIN_PASSWORD; akun uji dan API key uji bila SEED_DEMO=true.
    Urutan pembuatan membuat ID akun uji stabil pada basis data baru: admin=1, pendonor=2,
    pemohon=3, petugas=4 (core memakai DEMO_DONOR_USER_ID=2 untuk profil pendonor uji)."""
    if settings.admin_email and settings.admin_password:
        ensure_user(conn, settings.admin_email, "Admin DonorKita", "admin", settings.admin_password)
    if settings.seed_demo:
        ensure_user(conn, "pendonor@demo.test", "Pendonor Uji", "donor", settings.demo_password)
        ensure_user(conn, "pemohon@demo.test", "Pemohon Uji", "requester", settings.demo_password)
        ensure_user(conn, "petugas@demo.test", "Petugas UDD Uji", "staff", settings.demo_password)
        if settings.seed_api_key and not conn.execute("SELECT 1 FROM api_keys WHERE key_hash = ?",
                                                      (sha256(settings.seed_api_key),)).fetchone():
            conn.execute("INSERT INTO api_keys (name, key_hash, key_prefix, created_at) VALUES (?, ?, ?, ?)",
                         ("Kunci uji (SEED_API_KEY)", sha256(settings.seed_api_key),
                          settings.seed_api_key[:7], iso(now())))
            bump_seq(conn)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.jwt_secret == DEFAULT_SECRET:
        log.warning("JWT_SECRET masih nilai bawaan; isi di .env sebelum deploy")
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        seed(conn)
        conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(title="DonorKita Auth Service", version="1.0.0", lifespan=lifespan,
              description="Identitas & Akses DonorKita: akun, JWT, refresh token, dan API key kelompok lain. "
                          "Semua path relatif terhadap `/api/auth`.")
errors.install(app)


# ---------- dependensi ----------

def current_claims(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "invalid_token", "Login diperlukan")
    try:
        return jwt.decode(authorization[7:].strip(), settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Token tidak valid atau kedaluwarsa")


def admin_only(claims: dict = Depends(current_claims)) -> dict:
    if claims.get("role") != "admin":
        raise ApiError(403, "forbidden", "Hanya admin")
    return claims


# ---------- skema ----------

class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=2, max_length=80)
    role: Literal["donor", "requester"] = "donor"

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        local, _, domain = v.partition("@")
        if not local or "." not in domain or " " in v:
            raise ValueError("format email tidak valid")
        return v


class AdminUserIn(RegisterIn):
    role: Literal["donor", "requester", "staff", "admin"]


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=10)


class KeyIn(BaseModel):
    name: str = Field(min_length=3, max_length=80, description="Nama pemegang key, mis. 'KotaSiaga (Smart City)'")


class VerifyIn(BaseModel):
    key: str


# ---------- endpoint ----------

@app.get("/health", tags=["umum"])
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/register", status_code=201, tags=["akun"], summary="Daftar akun pendonor atau pemohon")
def register(body: RegisterIn, conn=Depends(get_conn)):
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (body.email,)).fetchone():
        raise ApiError(409, "conflict", "Email sudah terdaftar")
    uid = ensure_user(conn, body.email, body.name.strip(), body.role, body.password)
    return ok(conn, user_out(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()))


@app.post("/login", tags=["akun"], summary="Login; mengembalikan access token (JWT) dan refresh token")
def login(body: LoginIn, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM users WHERE email = ? AND active = 1",
                       (body.email.strip().lower(),)).fetchone()
    if row is None or not check_password(body.password, row["password_hash"]):
        raise ApiError(401, "invalid_credentials", "Email atau password salah")
    return ok(conn, issue_tokens(conn, row))


@app.post("/refresh", tags=["akun"], summary="Tukar refresh token dengan token baru (rotasi)")
def refresh(body: RefreshIn, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (sha256(body.refresh_token),)).fetchone()
    if row is None or row["revoked_at"] or row["expires_at"] < iso(now()):
        raise ApiError(401, "invalid_token", "Refresh token tidak valid atau kedaluwarsa")
    user = conn.execute("SELECT * FROM users WHERE id = ? AND active = 1", (row["user_id"],)).fetchone()
    if user is None:
        raise ApiError(401, "invalid_token", "Akun tidak aktif")
    conn.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ?", (iso(now()), row["token_hash"]))
    return ok(conn, issue_tokens(conn, user))


@app.post("/logout", tags=["akun"], summary="Cabut refresh token")
def logout(body: RefreshIn, conn=Depends(get_conn)):
    conn.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                 (iso(now()), sha256(body.refresh_token)))
    return ok(conn, {"logged_out": True})


@app.get("/me", tags=["akun"], summary="Profil pengguna yang sedang login")
def me(claims: dict = Depends(current_claims), conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (int(claims["sub"]),)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "Akun tidak ditemukan")
    return ok(conn, user_out(row))


@app.post("/users", status_code=201, tags=["admin"], summary="Buat akun dengan peran apa pun, mis. petugas (admin)")
def create_user(body: AdminUserIn, _=Depends(admin_only), conn=Depends(get_conn)):
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (body.email,)).fetchone():
        raise ApiError(409, "conflict", "Email sudah terdaftar")
    uid = ensure_user(conn, body.email, body.name.strip(), body.role, body.password)
    return ok(conn, user_out(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()))


@app.get("/users", tags=["admin"], summary="Daftar akun (admin)")
def list_users(_=Depends(admin_only), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return ok(conn, [user_out(r) for r in rows], total=len(rows))


def key_out(row) -> dict:
    return {"id": f"key_{row['id']}", "name": row["name"], "prefix": row["key_prefix"],
            "created_at": row["created_at"], "last_used_at": row["last_used_at"], "revoked_at": row["revoked_at"]}


@app.post("/api-keys", status_code=201, tags=["api-key"], summary="Buat API key untuk kelompok lain (admin)")
def create_key(body: KeyIn, claims: dict = Depends(admin_only), conn=Depends(get_conn)):
    key = new_api_key()
    cur = conn.execute("INSERT INTO api_keys (name, key_hash, key_prefix, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                       (body.name.strip(), sha256(key), key[:7], int(claims["sub"]), iso(now())))
    bump_seq(conn)
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    return ok(conn, {**key_out(row), "key": key, "note": "Simpan key ini sekarang; tidak akan ditampilkan lagi."})


@app.get("/api-keys", tags=["api-key"], summary="Daftar API key (admin)")
def list_keys(_=Depends(admin_only), conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
    return ok(conn, [key_out(r) for r in rows], total=len(rows))


@app.delete("/api-keys/{key_id}", tags=["api-key"], summary="Cabut API key (admin)")
def revoke_key(key_id: str, _=Depends(admin_only), conn=Depends(get_conn)):
    try:
        kid = int(key_id.removeprefix("key_"))
    except ValueError:
        raise ApiError(404, "not_found", "API key tidak ditemukan")
    row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "API key tidak ditemukan")
    if row["revoked_at"] is None:
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (iso(now()), kid))
        bump_seq(conn)
    return ok(conn, key_out(conn.execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()))


@app.post("/internal/verify-key", include_in_schema=False)
def verify_key(body: VerifyIn, x_internal_token: Optional[str] = Header(default=None), conn=Depends(get_conn)):
    if not settings.internal_token or not x_internal_token or \
            not hmac.compare_digest(x_internal_token, settings.internal_token):
        raise ApiError(403, "forbidden", "Token internal tidak valid")
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
                       (sha256(body.key),)).fetchone()
    if row is None:
        raise ApiError(401, "invalid_api_key", "API key tidak valid")
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (iso(now()), row["id"]))
    return {"valid": True, "name": row["name"]}
