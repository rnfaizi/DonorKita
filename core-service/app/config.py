"""Konfigurasi layanan, semuanya dari environment (.env). Tidak ada rahasia di kode."""
import os


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


class Settings:
    def __init__(self) -> None:
        self.service_name = "donorkita-core"
        self.version = "0.1.0"
        self.db_path = os.getenv("CORE_DB_PATH", "/data/core.db")

        # Mock mode (AGENTS.md bagian 7)
        self.mock_mode = _bool("MOCK_MODE")
        self.mock_tick_seconds = _int("MOCK_TICK_SECONDS", 5, lo=2, hi=60)
        self.mock_time_scale = _int("MOCK_TIME_SCALE", 60, lo=1)
        self.mock_seed = int(os.environ["MOCK_SEED"]) if os.getenv("MOCK_SEED") else None

        # Umpan perubahan: simpan N perubahan terakhir (AGENTS.md bagian 4: 500)
        self.changes_keep = _int("CHANGES_KEEP", 500, lo=50)

        # Paginasi (AGENTS.md bagian 3)
        self.page_default = 50
        self.page_max = 200

    @property
    def source(self) -> str:
        return "mock" if self.mock_mode else "live"


settings = Settings()
