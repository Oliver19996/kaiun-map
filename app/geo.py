from __future__ import annotations

import math
from typing import Any

from app.places import PLACES, lookup_place


def nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return round(2 * r_nm * math.asin(min(1.0, math.sqrt(a))), 1)


def hours_for_nm(distance_nm: float, sog: float | None) -> float | None:
    if sog is None or sog < 0.5:
        return None
    return round(distance_nm / sog, 1)


def place_center(place_id: str | None) -> tuple[float, float] | None:
    place = lookup_place(place_id)
    if not place:
        return None
    min_lat, min_lon, max_lat, max_lon = place["bbox"]
    return ((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)


def nearest_place_ids(lat: float, lon: float, limit: int = 4) -> list[str]:
    ranked: list[tuple[float, str]] = []
    for place_id, meta in PLACES.items():
        if place_id == "japan":
            continue
        min_lat, min_lon, max_lat, max_lon = meta["bbox"]
        clat = (min_lat + max_lat) / 2
        clon = (min_lon + max_lon) / 2
        ranked.append((nm_between(lat, lon, clat, clon), place_id))
    ranked.sort(key=lambda item: item[0])
    return [place_id for _, place_id in ranked[:limit]]


def place_on_segment(origin_id: str, dest_id: str) -> str | None:
    origin = place_center(origin_id)
    dest = place_center(dest_id)
    if origin is None or dest is None:
        return None
    best: tuple[float, str] | None = None
    for place_id, meta in PLACES.items():
        if place_id in {origin_id, dest_id, "japan"}:
            continue
        min_lat, min_lon, max_lat, max_lon = meta["bbox"]
        lat = (min_lat + max_lat) / 2
        lon = (min_lon + max_lon) / 2
        via = nm_between(origin[0], origin[1], lat, lon) + nm_between(lat, lon, dest[0], dest[1])
        direct = nm_between(origin[0], origin[1], dest[0], dest[1])
        extra = via - direct
        if extra < 80 and extra >= 0:
            if best is None or extra < best[0]:
                best = (extra, place_id)
    return best[1] if best else None


def places_payload(ids: list[str] | None) -> list[dict[str, Any]]:
    payload = []
    for place_id in ids or []:
        center = place_center(place_id)
        place = lookup_place(place_id)
        if not center or not place:
            continue
        payload.append({"place_id": place_id, "name": place["name"], "lat": center[0], "lon": center[1]})
    return payload
