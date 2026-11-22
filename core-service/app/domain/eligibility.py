"""Kebijakan kelayakan pendonor (subdomain supporting: Kelayakan Pendonor).

Aturan bersumber dari Permenkes 91/2015; semua angka berasal dari konfigurasi karena
praktik antar-UDD berbeda (mis. jeda 60 hari atau 12 minggu). Hasilnya hanya
pra-penyaringan: keputusan medis akhir tetap di tangan petugas UDD.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class EligibilityPolicy:
    min_age: int = 17
    max_age_first: int = 60
    max_age_repeat: int = 65
    min_weight_kg: float = 45.0
    interval_days: int = 60
    max_per_year: int = 5
    screening_valid_days: int = 30


@dataclass(frozen=True)
class DonorFacts:
    birth_date: Optional[date]
    weight_kg: Optional[float]
    donation_dates: Sequence[datetime] = ()       # riwayat donasi, urutan bebas
    deferred_until: Optional[datetime] = None     # penundaan aktif terpanjang
    screening_at: Optional[datetime] = None       # waktu pra-skrining terakhir
    screening_flagged: bool = False                # pra-skrining terakhir ada jawaban berisiko


@dataclass
class Eligibility:
    eligible: bool
    reasons: List[str] = field(default_factory=list)
    next_eligible_at: Optional[datetime] = None
    donations_last_365d: int = 0


def age_on(birth: date, on: date) -> int:
    years = on.year - birth.year
    if (on.month, on.day) < (birth.month, birth.day):
        years -= 1
    return years


def evaluate(facts: DonorFacts, on: datetime, policy: EligibilityPolicy) -> Eligibility:
    """Kode alasan (bahasa Inggris, huruf kecil):
    profile_incomplete, under_age, over_age_review, under_weight, interval_not_met,
    yearly_limit_reached, deferred, screening_required, screening_flagged.
    """
    reasons: List[str] = []
    next_at: Optional[datetime] = None

    def later(candidate: datetime) -> None:
        nonlocal next_at
        if next_at is None or candidate > next_at:
            next_at = candidate

    if facts.birth_date is None or facts.weight_kg is None:
        return Eligibility(False, ["profile_incomplete"])

    donations = sorted(d for d in facts.donation_dates if d <= on)
    age = age_on(facts.birth_date, on.date())
    if age < policy.min_age:
        reasons.append("under_age")
    elif (not donations and age > policy.max_age_first) or (donations and age > policy.max_age_repeat):
        reasons.append("over_age_review")

    if facts.weight_kg < policy.min_weight_kg:
        reasons.append("under_weight")

    if donations:
        ready = donations[-1] + timedelta(days=policy.interval_days)
        if ready > on:
            reasons.append("interval_not_met")
            later(ready)

    window_start = on - timedelta(days=365)
    in_window = [d for d in donations if d > window_start]
    if len(in_window) >= policy.max_per_year:
        reasons.append("yearly_limit_reached")
        # boleh lagi saat donasi tertua dalam jendela keluar dari 365 hari
        oldest_needed = in_window[len(in_window) - policy.max_per_year]
        later(oldest_needed + timedelta(days=365))

    if facts.deferred_until and facts.deferred_until > on:
        reasons.append("deferred")
        later(facts.deferred_until)

    if facts.screening_at is None or facts.screening_at + timedelta(days=policy.screening_valid_days) < on:
        reasons.append("screening_required")
    elif facts.screening_flagged:
        reasons.append("screening_flagged")

    # next_eligible_at hanya bermakna bila semua alasan bersifat sementara-berbasis-waktu
    time_based = {"interval_not_met", "yearly_limit_reached", "deferred"}
    if any(r not in time_based for r in reasons):
        next_at = None
    return Eligibility(not reasons, reasons, next_at, len(in_window))
