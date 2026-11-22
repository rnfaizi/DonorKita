"""Satu sumber waktu untuk seluruh layanan.

- Semua waktu disimpan dan dikirim sebagai ISO 8601 UTC berakhiran Z.
- scaled_minutes() mempercepat durasi domain (masa berlaku undangan, eskalasi) saat
  MOCK_MODE=true sesuai MOCK_TIME_SCALE, agar demo tidak perlu menunggu berjam-jam.
- advance() hanya untuk pengujian: menggeser jam tanpa menunggu.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .config import settings

_offset = timedelta(0)


def utcnow() -> datetime:
    return (datetime.now(timezone.utc) + _offset).replace(microsecond=0)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utcnow())


def parse_iso(value: str) -> datetime:
    """Terima ISO 8601 dengan zona waktu (Z atau +07:00). Tanpa zona dianggap UTC."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_opt(value: Optional[str]) -> Optional[datetime]:
    return parse_iso(value) if value else None


def today() -> date:
    return utcnow().date()


def scaled_minutes(minutes: float) -> timedelta:
    seconds = minutes * 60.0
    if settings.mock_mode:
        seconds = seconds / settings.mock_time_scale
    return timedelta(seconds=seconds)


def advance(seconds: float) -> None:
    """Hanya untuk pengujian."""
    global _offset
    _offset += timedelta(seconds=seconds)


def reset() -> None:
    """Hanya untuk pengujian."""
    global _offset
    _offset = timedelta(0)
