"""Penilai prioritas kandidat (subdomain core: Pencocokan).

skor = w_jarak * (1 - jarak/radius)
     + w_keandalan * keandalan        (keandalan = (donasi + 1) / (diundang + 2))
     + w_identik * golongan identik   (1 jika golongan & rhesus sama dengan penerima)
     + w_kesegaran * min(hari_sejak_layak / 90, 1)

Bobot berasal dari konfigurasi. Urgensi tidak mengubah skor per kandidat, tetapi
mengubah ukuran batch undangan dan kecepatan eskalasi radius (lihat modul matching).
"""
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class CandidateFacts:
    distance_m: float
    radius_m: float
    donated_count: int        # jumlah undangan yang berakhir dengan donasi
    invited_count: int        # jumlah undangan yang pernah diterima pendonor
    exact_match: bool
    days_since_eligible: float


def reliability(donated: int, invited: int) -> float:
    """Rata-rata dengan prior: pendonor baru mendapat 0,5, bukan 0 atau 1."""
    return (donated + 1) / (invited + 2)


def score(c: CandidateFacts, weights: Dict[str, float]) -> float:
    radius = max(c.radius_m, 1.0)
    closeness = max(0.0, 1.0 - min(c.distance_m / radius, 1.0))
    freshness = max(0.0, min(c.days_since_eligible / 90.0, 1.0))
    total = (weights.get("distance", 0) * closeness
             + weights.get("reliability", 0) * reliability(c.donated_count, c.invited_count)
             + weights.get("exact_match", 0) * (1.0 if c.exact_match else 0.0)
             + weights.get("freshness", 0) * freshness)
    return round(total, 4)
