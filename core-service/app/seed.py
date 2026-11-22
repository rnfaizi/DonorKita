"""Data uji (SEED_DEMO=true). Nama memakai kata "Uji": bukan data resmi, koordinat perkiraan.

Hanya mengisi bila tabel fasilitas masih kosong, jadi aman dijalankan berulang.
"""
from datetime import timedelta

from . import clock
from .config import settings
from .modules import donors, drives, locations, stock

FACILITIES = [
    ("UDD Uji Kota Bandung", "udd", "Jl. Uji Aceh, Bandung", -6.9125, 107.6155),
    ("RS Uji Pasteur", "hospital", "Jl. Uji Pasteur, Bandung", -6.8946, 107.5980),
    ("RS Uji Dago", "hospital", "Jl. Uji Dago, Bandung", -6.8850, 107.6135),
    ("RS Uji Buah Batu", "hospital", "Jl. Uji Buah Batu, Bandung", -6.9480, 107.6330),
    ("BDRS Uji Cimahi", "bdrs", "Jl. Uji Cimahi, Cimahi", -6.8722, 107.5425),
]


def seed_demo(conn) -> bool:
    if conn.execute("SELECT 1 FROM facilities LIMIT 1").fetchone():
        return False
    ids = [locations.create_facility(conn, *f) for f in FACILITIES]
    udd = ids[0]
    start = (clock.utcnow() + timedelta(days=2)).replace(hour=1, minute=0, second=0)  # 08.00 WIB
    drives.create_event(conn, name="Donor Darah Uji Kampus Ganesha", organizer="Unit Uji Kampus", facility_id=udd,
                        address="Kampus Uji Ganesha, Bandung", lat=-6.8915, lon=107.6107,
                        opens_at=start, closes_at=start + timedelta(hours=5), capacity_per_hour=10, created_by=0)
    drives.create_event(conn, name="Mobil Unit Uji Alun-Alun", organizer="UDD Uji Kota Bandung", facility_id=udd,
                        address="Alun-Alun Uji, Bandung", lat=-6.9218, lon=107.6071,
                        opens_at=start + timedelta(days=1), closes_at=start + timedelta(days=1, hours=4),
                        capacity_per_hour=8, created_by=0)
    items = []
    for bt, units in (("o", 24), ("a", 15), ("b", 9), ("ab", 4)):
        items.append(stock.StockItemIn(blood_type=bt, rhesus="positive", component="prc", units=units))
        items.append(stock.StockItemIn(blood_type=bt, rhesus="negative", component="prc", units=max(1, units // 8)))
    stock.apply_update(conn, udd, items, None)
    # Profil untuk akun uji pendonor@demo.test (ID 2 di auth-service pada basis data baru)
    donors.seed_profile(conn, settings.demo_donor_user_id, "Pendonor Uji", "o", "positive", "2000-01-15",
                        62.0, -6.8930, 107.6100)
    return True
