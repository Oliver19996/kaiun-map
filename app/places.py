"""Catalog of bays/ports. The LLM may only pick ids from this list."""

from __future__ import annotations

from typing import Any

# bbox: [min_lat, min_lon, max_lat, max_lon]
PLACES: dict[str, dict[str, Any]] = {
    "japan": {
        "name": "日本周辺",
        "aliases": ["日本", "japan", "にほん"],
        "bbox": [24.0, 122.0, 46.5, 148.0],
    },
    "tokyo_bay": {
        "name": "東京湾",
        "aliases": ["東京湾", "東京", "横浜", "川崎", "千葉", "木更津", "tokyo", "yokohama"],
        "bbox": [34.95, 139.35, 35.72, 140.12],
    },
    "osaka_bay": {
        "name": "大阪湾",
        "aliases": ["大阪湾", "大阪", "神戸", "堺", "尼崎", "osaka", "kobe"],
        "bbox": [34.25, 134.85, 34.78, 135.52],
    },
    "seto_inland": {
        "name": "瀬戸内海",
        "aliases": ["瀬戸内", "瀬戸内海", "広島", "今治", "高松"],
        "bbox": [33.7, 131.0, 34.8, 135.2],
    },
    "kanmon": {
        "name": "関門海峡",
        "aliases": ["関門", "下関", "門司", "北九州"],
        "bbox": [33.85, 130.85, 34.05, 131.15],
    },
    "ise_bay": {
        "name": "伊勢湾",
        "aliases": ["伊勢湾", "名古屋", "四日市", "nagoya"],
        "bbox": [34.5, 136.5, 35.15, 137.15],
    },
    "sendai": {
        "name": "仙台湾",
        "aliases": ["仙台", "仙台湾"],
        "bbox": [37.9, 140.85, 38.4, 141.4],
    },
    "hakodate": {
        "name": "津軽海峡・函館",
        "aliases": ["函館", "津軽", "青森"],
        "bbox": [41.3, 140.2, 41.9, 141.3],
    },
    "naha": {
        "name": "那覇・沖縄本島周辺",
        "aliases": ["那覇", "沖縄", "naha", "okinawa"],
        "bbox": [25.9, 127.3, 26.7, 128.1],
    },
    "tomakomai": {
        "name": "苫小牧",
        "aliases": ["苫小牧", "北海道"],
        "bbox": [42.5, 141.4, 42.75, 141.9],
    },
    "singapore": {
        "name": "シンガポール海峡",
        "aliases": ["シンガポール", "singapore"],
        "bbox": [1.1, 103.5, 1.5, 104.15],
    },
    "shanghai": {
        "name": "上海・長江河口",
        "aliases": ["上海", "shanghai"],
        "bbox": [30.7, 121.3, 31.7, 122.3],
    },
    "busan": {
        "name": "釜山",
        "aliases": ["釜山", "busan", "プサン"],
        "bbox": [34.85, 128.9, 35.2, 129.2],
    },
    "rotterdam": {
        "name": "ロッテルダム",
        "aliases": ["ロッテルダム", "rotterdam"],
        "bbox": [51.85, 3.9, 52.05, 4.55],
    },
    "los_angeles": {
        "name": "ロサンゼルス／ロングビーチ",
        "aliases": ["ロサンゼルス", "ロングビーチ", "los angeles", "long beach", "la"],
        "bbox": [33.65, -118.35, 33.85, -118.1],
    },
}

SHIP_TYPE_HINTS = {
    "tanker": {"label": "タンカー", "codes": range(80, 90)},
    "cargo": {"label": "貨物船", "codes": range(70, 80)},
    "container": {"label": "コンテナ船（貨物船として分類）", "codes": range(70, 80)},
    "passenger": {"label": "旅客船", "codes": range(60, 70)},
    "fishing": {"label": "漁船", "codes": [30]},
    "tug": {"label": "タグボート", "codes": [31, 32, 52]},
    "pleasure": {"label": "プレジャーボート", "codes": [36, 37]},
}


def place_catalog_for_prompt() -> list[dict[str, str]]:
    return [{"id": key, "name": value["name"]} for key, value in PLACES.items()]


def lookup_place(place_id: str | None) -> dict[str, Any] | None:
    if not place_id:
        return None
    return PLACES.get(place_id)


def match_place_from_text(text: str) -> str | None:
    lowered = text.casefold()
    for place_id, meta in PLACES.items():
        for alias in meta["aliases"]:
            if alias.casefold() in lowered:
                return place_id
    return None


def match_ship_hint_from_text(text: str) -> str | None:
    lowered = text.casefold()
    keywords = {
        "tanker": ["タンカー", "tanker", "油槽"],
        "container": ["コンテナ", "container"],
        "cargo": ["貨物", "cargo", "ばら積み", "bulk"],
        "passenger": ["旅客", "フェリー", "passenger", "ferry"],
        "fishing": ["漁船", "fishing"],
        "tug": ["タグ", "tug"],
        "pleasure": ["ヨット", "プレジャー", "yacht"],
    }
    for hint, words in keywords.items():
        if any(word.casefold() in lowered for word in words):
            return hint
    return None
