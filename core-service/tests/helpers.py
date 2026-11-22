"""Helper pengujian. Setiap tes memakai wilayah (koordinat) sendiri yang berjauhan
(> 100 km) agar pendonor dari tes lain tidak ikut tercocokkan."""
from datetime import timedelta

from app import clock
from app.domain import geo

from conftest import ADMIN, donor


def offset(lat: float, lon: float, km_north: float = 0.0, km_east: float = 0.0):
    return lat + km_north / 111.0, lon + km_east / 110.0


def make_facility(client, lat: float, lon: float, name: str = "RS Uji Wilayah", kind: str = "hospital") -> str:
    r = client.post("/facilities", headers=ADMIN, json={"name": name, "kind": kind, "lat": lat, "lon": lon})
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def make_donor(client, uid: int, blood_type: str, rhesus: str, lat: float, lon: float, *,
               screened: bool = True, available: bool = True, weight: float = 60, birth: str = "1998-05-01") -> dict:
    h = donor(uid)
    r = client.put("/donors/me", headers=h, json={
        "display_name": f"Pendonor {uid}", "blood_type": blood_type, "rhesus": rhesus, "birth_date": birth,
        "weight_kg": weight, "lat": lat, "lon": lon, "available": available})
    assert r.status_code == 200, r.text
    if screened:
        r = client.post("/donors/me/screening", headers=h, json={
            "feeling_healthy": True, "fever_last_2_weeks": False, "antibiotics_last_week": False,
            "tattoo_or_piercing_last_6_months": False, "pregnant_or_breastfeeding": False,
            "transfusion_last_year": False})
        assert r.status_code == 200, r.text
    return h


def make_request(client, headers, facility_id: str, *, blood_type="a", rhesus="positive", component="prc",
                 units=1, urgency="normal", hours=24) -> dict:
    r = client.post("/requests", headers=headers, json={
        "facility_id": facility_id, "patient_ref": "FRM-UJI-001", "blood_type": blood_type, "rhesus": rhesus,
        "component": component, "units": units, "urgency": urgency,
        "needed_by": clock.iso(clock.utcnow() + timedelta(hours=hours))})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def distance_km(a, b) -> float:
    return geo.haversine_m(a[0], a[1], b[0], b[1]) / 1000
