from __future__ import annotations

from functools import lru_cache
from typing import Any

import searoute as sr

from app.geo import nm_between


def sea_polyline(waypoints: list[tuple[float, float]]) -> dict[str, Any]:
    """Maritime-looking path along shipping lanes. Not for navigation."""
    cleaned: list[tuple[float, float]] = []
    for lat, lon in waypoints:
        if lat is None or lon is None:
            continue
        point = (float(lat), float(lon))
        if cleaned and nm_between(cleaned[-1][0], cleaned[-1][1], point[0], point[1]) < 0.3:
            continue
        cleaned.append(point)
    if len(cleaned) < 2:
        return {"path": [[lat, lon] for lat, lon in cleaned], "nm": 0.0, "source": "none"}
    path: list[list[float]] = []
    total = 0.0
    source = "searoute"
    for start, end in zip(cleaned, cleaned[1:]):
        segment, length, seg_source = _segment(start, end)
        source = seg_source if source == "searoute" else source
        if path and segment:
            segment = segment[1:]
        path.extend(segment)
        total += length
    return {"path": path, "nm": round(total, 1), "source": source}


@lru_cache(maxsize=512)
def _segment(start: tuple[float, float], end: tuple[float, float]) -> tuple[list[list[float]], float, str]:
    lat1, lon1 = start
    lat2, lon2 = end
    try:
        feature = sr.searoute([lon1, lat1], [lon2, lat2], units="nm")
        coords = feature["geometry"]["coordinates"]
        path = [[float(lat), float(lon)] for lon, lat in coords]
        length = float(feature.get("properties", {}).get("length") or 0.0)
        if len(path) >= 2:
            return path, length, "searoute"
    except Exception:
        pass
    return [[lat1, lon1], [lat2, lon2]], nm_between(lat1, lon1, lat2, lon2), "straight"
