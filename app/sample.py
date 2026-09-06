from __future__ import annotations

import random
from threading import Lock
from typing import Any

from app.geo import nearest_place_ids, place_on_segment
from app.places import match_place_from_text
from app.port import UNDERWAY_SOG
from app.projects import _normalize_ship
from app.vessel_store import Vessel, VesselStore

SAMPLE_ID = "sample"

_lock = Lock()
_picks: dict[str, list[int]] = {}


def sample_summary(store: VesselStore, session_key: str) -> dict:
    project = sample_project(store, session_key)
    return {
        "id": SAMPLE_ID,
        "name": project["name"],
        "notes": project["notes"],
        "ship_count": len(project.get("ships") or []),
        "readonly": True,
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
    }


def sample_project(store: VesselStore, session_key: str) -> dict:
    vessels = _session_vessels(store, session_key)
    ships = [_ship_from_live(vessel, index) for index, vessel in enumerate(vessels)]
    note = (
        "いま海上にいる船からランダムに最大2隻です。船積・積み替えはB/L想定、寄港地はAIS目的地などB/L外の候補です。"
        if ships
        else "海上のAISがまだ足りません。数秒後に自動で取り直します。"
    )
    return {
        "id": SAMPLE_ID,
        "user_id": None,
        "readonly": True,
        "name": "サンプル（海上2隻）",
        "notes": note,
        "ships": ships,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _session_vessels(store: VesselStore, session_key: str) -> list[Vessel]:
    with _lock:
        mmsis = list(_picks.get(session_key) or [])
    vessels = [v for v in (store.get(mmsi) for mmsi in mmsis) if v and v.lat is not None and v.lon is not None]
    if len(vessels) >= 2:
        store.protect(v.mmsi for v in vessels)
        return vessels[:2]
    picked = _pick_underway(store, 2)
    if len(picked) < 2:
        return picked
    store.protect(v.mmsi for v in picked)
    with _lock:
        _picks[session_key] = [v.mmsi for v in picked]
    return picked


def _pick_underway(store: VesselStore, count: int) -> list[Vessel]:
    underway: list[Vessel] = []
    fallback: list[Vessel] = []
    for vessel in store.all():
        if vessel.lat is None or vessel.lon is None:
            continue
        public = vessel.to_public_dict()
        if public.get("in_port"):
            continue
        fallback.append(vessel)
        if vessel.sog is not None and vessel.sog >= UNDERWAY_SOG:
            underway.append(vessel)
    pool = underway or fallback
    if len(pool) <= count:
        return pool
    return random.sample(pool, count)


def _ship_from_live(vessel: Vessel, index: int) -> dict[str, Any]:
    nearby = nearest_place_ids(vessel.lat or 0, vessel.lon or 0, limit=5)
    dest_id = match_place_from_text(vessel.destination or "")
    load_id = nearby[0] if nearby else None
    discharge_id = dest_id if dest_id and dest_id != load_id else (nearby[1] if len(nearby) > 1 else None)
    transship_id = place_on_segment(load_id, discharge_id) if load_id and discharge_id else None
    call_ids = []
    if dest_id and dest_id not in {load_id, discharge_id, transship_id}:
        call_ids = [dest_id]
    return _normalize_ship(
        {
            "name": (vessel.name or "").strip(),
            "call_sign": vessel.call_sign or "",
            "mmsi": vessel.mmsi,
            "notes": "サンプル: 船積・積み替えはB/L想定。寄港地はB/Lに無い候補です。",
            "origin_place_id": load_id,
            "dest_place_id": discharge_id,
            "transship_place_ids": [transship_id] if transship_id else [],
            "call_place_ids": call_ids,
        },
        ship_id=f"sample-live-{index}-{vessel.mmsi}",
    )
