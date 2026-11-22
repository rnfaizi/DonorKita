"""Konfigurasi layanan, semuanya dari environment (.env). Tidak ada rahasia di kode.

Semua angka aturan domain (kelayakan, pencocokan, stok) ada di sini agar dapat disetel
tanpa mengubah kode, sesuai makalah DDD: kebijakan jeda dan frekuensi berbeda antar-UDD.
"""
import os
from typing import Dict, List, Optional


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _int(name: str, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    value = int(os.getenv(name, str(default)))
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int_list(name: str, default: str) -> List[int]:
    return [int(x) for x in os.getenv(name, default).split(",") if x.strip()]


class Settings:
    def __init__(self) -> None:
        self.service_name = "donorkita-core"
        self.version = "0.2.0"
        self.db_path = os.getenv("CORE_DB_PATH", "/data/core.db")

        # --- Identitas & Akses ---
        self.jwt_secret = os.getenv("JWT_SECRET", "dev-only-secret-ganti-di-env-min-32-karakter")
        self.auth_url = os.getenv("AUTH_INTERNAL_URL", "").rstrip("/")
        self.internal_token = os.getenv("INTERNAL_TOKEN", "")
        # Daftar API key statis (dipisah koma) untuk pengembangan bila auth-service belum ada.
        self.static_api_keys = {k.strip() for k in os.getenv("STATIC_API_KEYS", "").split(",") if k.strip()}

        # --- Data uji ---
        self.seed_demo = _bool("SEED_DEMO")
        self.demo_donor_user_id = _int("DEMO_DONOR_USER_ID", 2)

        # --- Mock mode (AGENTS.md bagian 7) ---
        self.mock_mode = _bool("MOCK_MODE")
        self.mock_tick_seconds = _int("MOCK_TICK_SECONDS", 5, lo=2, hi=60)
        self.mock_time_scale = _int("MOCK_TIME_SCALE", 60, lo=1)
        self.mock_seed = int(os.environ["MOCK_SEED"]) if os.getenv("MOCK_SEED") else None
        self.mock_admin_api_key = os.getenv("MOCK_ADMIN_API_KEY", "").strip()
        self.mock_donors = _int("MOCK_DONORS", 60, lo=10, hi=500)
        # Pendonor palsu dianggap "pulih" (boleh donor lagi) setelah N menit nyata,
        # agar kumpulan pendonor tidak habis selama demo panjang.
        self.mock_donor_recovery_minutes = _int("MOCK_DONOR_RECOVERY_MINUTES", 10, lo=1)
        self.mock_max_active_requests = _int("MOCK_MAX_ACTIVE_REQUESTS", 8, lo=1)

        # --- Umpan perubahan dan paginasi (AGENTS.md bagian 3 dan 4) ---
        self.changes_keep = _int("CHANGES_KEEP", 500, lo=50)
        self.page_default = 50
        self.page_max = 200

        # --- Pendonor & Kelayakan (Permenkes 91/2015; angka dapat disetel per UDD) ---
        self.elig_min_age = _int("ELIG_MIN_AGE", 17)
        self.elig_max_age_first = _int("ELIG_MAX_AGE_FIRST", 60)
        self.elig_max_age_repeat = _int("ELIG_MAX_AGE_REPEAT", 65)
        self.elig_min_weight_kg = _float("ELIG_MIN_WEIGHT_KG", 45)
        self.donation_interval_days = _int("DONATION_INTERVAL_DAYS", 60, lo=1)
        self.max_donations_per_year = _int("MAX_DONATIONS_PER_YEAR", 5, lo=1)
        self.screening_valid_days = _int("SCREENING_VALID_DAYS", 30, lo=1)
        self.deferral_days = _int("DEFERRAL_DAYS", 14, lo=1)

        # --- Pencocokan ---
        self.match_radius_steps_m = _int_list("MATCH_RADIUS_STEPS_M", "5000,10000,20000")
        self.match_batch_factor: Dict[str, float] = {"normal": 2.0, "urgent": 3.0, "emergency": 4.0}
        self.invite_ttl_minutes: Dict[str, int] = {"normal": 120, "urgent": 60, "emergency": 30}
        self.escalate_after_minutes: Dict[str, int] = {"normal": 60, "urgent": 30, "emergency": 15}
        self.score_weights: Dict[str, float] = {
            "distance": _float("SCORE_W_DISTANCE", 0.5),
            "reliability": _float("SCORE_W_RELIABILITY", 0.3),
            "exact_match": _float("SCORE_W_EXACT_MATCH", 0.1),
            "freshness": _float("SCORE_W_FRESHNESS", 0.1),
        }
        self.request_max_days_ahead = _int("REQUEST_MAX_DAYS_AHEAD", 14, lo=1)

        # --- Stok Darah ---
        self.stock_low_units = _int("STOCK_LOW_UNITS", 10, lo=0)
        self.stock_critical_units = _int("STOCK_CRITICAL_UNITS", 3, lo=0)

        # --- Kegiatan Donor ---
        self.event_max_hours = _int("EVENT_MAX_HOURS", 12, lo=1)

        # --- Job latar belakang (kedaluwarsa permintaan, eskalasi radius) ---
        self.enable_jobs = _bool("ENABLE_JOBS", "true")
        self.job_interval_seconds = _int("JOB_INTERVAL_SECONDS", 30, lo=2)

    @property
    def source(self) -> str:
        return "mock" if self.mock_mode else "live"


settings = Settings()
