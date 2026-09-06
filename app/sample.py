from __future__ import annotations

from app.projects import _normalize_ship

SAMPLE_ID = "sample"


def sample_project() -> dict:
    ships = [
        _normalize_ship(
            {
                "name": "GAS FRONTIER",
                "call_sign": "3E8838",
                "mmsi": 352005602,
                "notes": "サンプル: 大阪湾 → 東京湾",
                "origin_place_id": "osaka_bay",
                "dest_place_id": "tokyo_bay",
            },
            ship_id="sample-ship-1",
        ),
        _normalize_ship(
            {
                "name": "OGASAWARA MARU",
                "call_sign": "7JWG",
                "mmsi": 431347000,
                "notes": "サンプル: 東京湾 → 那覇",
                "origin_place_id": "tokyo_bay",
                "dest_place_id": "naha",
            },
            ship_id="sample-ship-2",
        ),
        _normalize_ship(
            {
                "name": "HOUOU MARU",
                "call_sign": "JD2207",
                "mmsi": 431101139,
                "notes": "サンプル: 伊勢湾 → 東京湾",
                "origin_place_id": "ise_bay",
                "dest_place_id": "tokyo_bay",
            },
            ship_id="sample-ship-3",
        ),
    ]
    return {
        "id": SAMPLE_ID,
        "user_id": None,
        "readonly": True,
        "name": "サンプル",
        "notes": "だれでも見える固定の例です。削除できません。",
        "ships": ships,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def sample_summary() -> dict:
    project = sample_project()
    return {
        "id": SAMPLE_ID,
        "name": project["name"],
        "notes": project["notes"],
        "ship_count": 3,
        "readonly": True,
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
    }
