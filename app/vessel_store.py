from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from app.port import IN_PORT_SOG, UNDERWAY_SOG, containing_place, port_fields

AIS_SHIP_TYPES: dict[int, str] = {
    30: "漁船",
    31: "曳航",
    32: "曳航（大型）",
    33: "浚渫",
    34: "潜水作業",
    35: "軍艦",
    36: "セーリング",
    37: "プレジャー",
    50: "パイロット",
    51: "捜索救助",
    52: "タグボート",
    53: "ポートテンダー",
    54: "防除",
    55: "法執行",
    58: "医療",
}


def ship_type_label(code: int | None) -> str | None:
    if code is None:
        return None
    if code in AIS_SHIP_TYPES:
        return AIS_SHIP_TYPES[code]
    if 20 <= code <= 29:
        return "WIG"
    if 40 <= code <= 49:
        return "高速船"
    if 60 <= code <= 69:
        return "旅客船"
    if 70 <= code <= 79:
        return "貨物船"
    if 80 <= code <= 89:
        return "タンカー"
    if 90 <= code <= 99:
        return "その他"
    return f"種別 {code}"


@dataclass
class Vessel:
    mmsi: int
    lat: float | None = None
    lon: float | None = None
    sog: float | None = None
    cog: float | None = None
    heading: float | None = None
    name: str | None = None
    call_sign: str | None = None
    ship_type: int | None = None
    destination: str | None = None
    length_m: float | None = None
    width_m: float | None = None
    in_port_since: datetime | None = None
    ais_eta: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "mmsi": self.mmsi,
            "lat": self.lat,
            "lon": self.lon,
            "sog": self.sog,
            "cog": self.cog,
            "heading": self.heading,
            "name": (self.name or "").strip() or None,
            "call_sign": (self.call_sign or "").strip() or None,
            "ship_type": self.ship_type,
            "ship_type_label": ship_type_label(self.ship_type),
            "destination": (self.destination or "").strip() or None,
            "length_m": self.length_m,
            "width_m": self.width_m,
            "ais_eta": self.ais_eta,
            "updated_at": self.updated_at.isoformat(),
            **port_fields(
                lat=self.lat,
                lon=self.lon,
                sog=self.sog,
                in_port_since=self.in_port_since,
                dest_place_id=None,
                planned_arrival_at=None,
                planned_departure_at=None,
                ais_eta=self.ais_eta,
            ),
        }

    def matches_query(self, query: str) -> bool:
        q = query.casefold().strip()
        if not q:
            return True
        haystacks = [
            str(self.mmsi),
            self.name or "",
            self.call_sign or "",
            self.destination or "",
        ]
        return any(q in item.casefold() for item in haystacks)

    def matches_type_hint(self, codes: list[int] | range | None) -> bool:
        if codes is None:
            return True
        if self.ship_type is None:
            return False
        return self.ship_type in codes


class VesselStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._vessels: dict[int, Vessel] = {}

    def upsert_position(
        self,
        mmsi: int,
        lat: float,
        lon: float,
        sog: float | None,
        cog: float | None,
        heading: float | None,
        name: str | None = None,
    ) -> Vessel:
        with self._lock:
            vessel = self._vessels.get(mmsi) or Vessel(mmsi=mmsi)
            vessel.lat = lat
            vessel.lon = lon
            if sog is not None:
                vessel.sog = sog
                now = datetime.now(timezone.utc)
                if sog < IN_PORT_SOG and containing_place(lat, lon):
                    if vessel.in_port_since is None:
                        vessel.in_port_since = now
                elif sog >= UNDERWAY_SOG:
                    vessel.in_port_since = None
            if cog is not None:
                vessel.cog = cog
            if heading is not None and 0 <= heading < 360:
                vessel.heading = heading
            if name and name.strip():
                vessel.name = name.strip()
            vessel.updated_at = datetime.now(timezone.utc)
            self._vessels[mmsi] = vessel
            return vessel

    def upsert_static(
        self,
        mmsi: int,
        name: str | None,
        call_sign: str | None,
        ship_type: int | None,
        destination: str | None,
        length_m: float | None,
        width_m: float | None,
        ais_eta: str | None = None,
    ) -> Vessel:
        with self._lock:
            vessel = self._vessels.get(mmsi) or Vessel(mmsi=mmsi)
            if name and name.strip():
                vessel.name = name.strip()
            if call_sign and call_sign.strip():
                vessel.call_sign = call_sign.strip()
            if ship_type is not None:
                vessel.ship_type = ship_type
            if destination and destination.strip():
                vessel.destination = destination.strip()
            if length_m:
                vessel.length_m = length_m
            if width_m:
                vessel.width_m = width_m
            if ais_eta:
                vessel.ais_eta = ais_eta
            vessel.updated_at = datetime.now(timezone.utc)
            self._vessels[mmsi] = vessel
            return vessel

    def get(self, mmsi: int) -> Vessel | None:
        with self._lock:
            return self._vessels.get(mmsi)

    def in_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        query: str = "",
        type_codes: list[int] | range | None = None,
    ) -> list[Vessel]:
        with self._lock:
            found: list[Vessel] = []
            for vessel in self._vessels.values():
                if vessel.lat is None or vessel.lon is None:
                    continue
                if not (min_lat <= vessel.lat <= max_lat and min_lon <= vessel.lon <= max_lon):
                    continue
                if query and not vessel.matches_query(query):
                    continue
                if not vessel.matches_type_hint(type_codes):
                    continue
                found.append(vessel)
            return found

    def search(self, query: str, limit: int = 50) -> list[Vessel]:
        with self._lock:
            hits = [v for v in self._vessels.values() if v.matches_query(query)]
        hits.sort(key=lambda v: v.updated_at, reverse=True)
        return hits[:limit]

    def prune(self, min_lat: float, min_lon: float, max_lat: float, max_lon: float, max_age_sec: float = 900) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            drop: list[int] = []
            for mmsi, vessel in self._vessels.items():
                age = (now - vessel.updated_at).total_seconds()
                inside = (
                    vessel.lat is not None
                    and vessel.lon is not None
                    and min_lat <= vessel.lat <= max_lat
                    and min_lon <= vessel.lon <= max_lon
                )
                if age > max_age_sec or (not inside and age > 120):
                    drop.append(mmsi)
            for mmsi in drop:
                del self._vessels[mmsi]

    def count(self) -> int:
        with self._lock:
            return len(self._vessels)
