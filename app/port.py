from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.geo import place_center
from app.places import PLACES, lookup_place

IN_PORT_SOG = 0.5
UNDERWAY_SOG = 1.0


def containing_place(lat: float | None, lon: float | None) -> dict[str, Any] | None:
    if lat is None or lon is None:
        return None
    for place_id, meta in PLACES.items():
        if place_id == "japan":
            continue
        min_lat, min_lon, max_lat, max_lon = meta["bbox"]
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return {"place_id": place_id, "name": meta["name"]}
    return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hours_between(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds() / 3600, 1)


def port_fields(
    *,
    lat: float | None,
    lon: float | None,
    sog: float | None,
    in_port_since: datetime | None,
    dest_place_id: str | None,
    planned_arrival_at: str | None,
    planned_departure_at: str | None,
    ais_eta: str | None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    near = containing_place(lat, lon)
    dest = lookup_place(dest_place_id)
    in_dest = False
    if dest and lat is not None and lon is not None:
        min_lat, min_lon, max_lat, max_lon = dest["bbox"]
        in_dest = min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
    slow = sog is not None and sog < IN_PORT_SOG
    in_port = bool(slow and (in_dest or near))
    stay_hours = None
    since_iso = None
    if in_port and in_port_since:
        stay_hours = max(0.0, hours_between(now, in_port_since))
        since_iso = in_port_since.isoformat()
    planned_arr = parse_iso(planned_arrival_at) or parse_iso(ais_eta)
    planned_dep = parse_iso(planned_departure_at)
    delay_hours = None
    delay_reason = None
    if in_port and planned_dep and now > planned_dep:
        delay_hours = hours_between(now, planned_dep)
        delay_reason = "予定出港時刻を過ぎて港に留まっています"
    elif in_port and planned_arr and in_port_since and in_port_since > planned_arr:
        delay_hours = hours_between(in_port_since, planned_arr)
        delay_reason = "予定到着より遅れて入港しています"
    elif in_port and planned_arr and now > planned_arr and delay_hours is None:
        delay_hours = hours_between(now, planned_arr)
        delay_reason = "予定到着を過ぎても港にいます"
    elif in_port and not planned_arr and not planned_dep:
        delay_reason = "到着・出港予定が未設定のため遅延は算出できません"
    return {
        "in_port": in_port,
        "port_name": (dest["name"] if in_dest and dest else None) or (near["name"] if near else None),
        "in_port_since": since_iso,
        "stay_hours": stay_hours,
        "delay_hours": delay_hours,
        "delay_reason": delay_reason,
    }
