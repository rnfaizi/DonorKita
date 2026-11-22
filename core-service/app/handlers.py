"""Pemetaan domain event ke konteks hilir (context map di dalam proses).

Urutan pendaftaran penting untuk DonasiTercatat: Permintaan Darah lebih dulu (bisa
menerbitkan PermintaanTerpenuhi yang menutup sesi), lalu Pendonor & Kelayakan, lalu Notifikasi.
"""
from . import events
from .modules import donors, drives, matching, notifications, requests


def register() -> None:
    s = events.subscribe
    # Permintaan Darah -> Pencocokan, Notifikasi
    s("PermintaanTerverifikasi", matching.on_request_verified)
    s("PermintaanTerverifikasi", notifications.on_request_verified)
    s("PermintaanDitolak", notifications.on_request_rejected)
    s("PermintaanTerpenuhi", matching.on_request_closed)
    s("PermintaanTerpenuhi", notifications.on_request_fulfilled)
    s("PermintaanKedaluwarsa", matching.on_request_closed)
    s("PermintaanKedaluwarsa", notifications.on_request_expired)
    s("PermintaanDibatalkan", matching.on_request_closed)
    # Pencocokan -> Permintaan Darah, Notifikasi
    s("SesiPencocokanDimulai", requests.on_matching_started)
    s("UndanganDiterbitkan", notifications.on_invitations_issued)
    # Pencocokan / Kegiatan Donor -> Permintaan Darah, Pendonor & Kelayakan, Notifikasi
    s("DonasiTercatat", requests.on_donation_recorded)
    s("DonasiTercatat", donors.on_donation_recorded)
    s("DonasiTercatat", notifications.on_donation_recorded)
    s("PendonorDitunda", donors.on_donor_deferred)
    # Stok Darah -> Kegiatan Donor, Notifikasi
    s("StokDiperbarui", drives.on_stock_updated)
    s("StokMenipis", notifications.on_stock_low)
