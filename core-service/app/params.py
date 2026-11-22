"""Parameter query bersama: paginasi, titik + radius, bbox (AGENTS.md bagian 3)."""
from dataclasses import dataclass
from typing import Optional

from fastapi import Query

from .common import invalid
from .domain import geo


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int

    def slice(self, items: list) -> list:
        return items[self.offset:self.offset + self.limit]


def page(limit: int = Query(default=50, ge=1, le=200, description="Jumlah data per halaman (maks 200)"),
         offset: int = Query(default=0, ge=0)) -> Page:
    return Page(limit, offset)


@dataclass(frozen=True)
class Near:
    lat: Optional[float]
    lon: Optional[float]
    radius_m: Optional[int]

    @property
    def active(self) -> bool:
        return self.lat is not None

    def distance(self, lat: float, lon: float) -> Optional[int]:
        if not self.active:
            return None
        return int(round(geo.haversine_m(self.lat, self.lon, lat, lon)))

    def accepts(self, lat: float, lon: float) -> bool:
        if not self.active or self.radius_m is None:
            return True
        return geo.haversine_m(self.lat, self.lon, lat, lon) <= self.radius_m


def near(lat: Optional[float] = Query(default=None, ge=-90, le=90),
         lon: Optional[float] = Query(default=None, ge=-180, le=180),
         radius_m: Optional[int] = Query(default=None, ge=100, le=100000,
                                         description="Radius dalam meter; perlu lat dan lon")) -> Near:
    if (lat is None) != (lon is None):
        raise invalid("lat dan lon harus diisi bersama")
    if radius_m is not None and lat is None:
        raise invalid("radius_m memerlukan lat dan lon")
    return Near(lat, lon, radius_m)


def bbox(bbox: Optional[str] = Query(default=None, description="min_lon,min_lat,max_lon,max_lat")) -> Optional[geo.BBox]:
    if bbox is None:
        return None
    try:
        return geo.parse_bbox(bbox)
    except ValueError as exc:
        raise invalid(str(exc))
