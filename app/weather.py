from __future__ import annotations

from typing import Any

import httpx

WMO = {
    0: "快晴",
    1: "ほぼ晴れ",
    2: "一部曇り",
    3: "曇り",
    45: "霧",
    48: "着氷霧",
    51: "弱い霧雨",
    53: "霧雨",
    55: "強い霧雨",
    61: "弱い雨",
    63: "雨",
    65: "強い雨",
    71: "弱い雪",
    73: "雪",
    75: "強い雪",
    80: "にわか雨",
    81: "強いにわか雨",
    95: "雷雨",
}


async def forecast(lat: float, lon: float) -> dict[str, Any] | None:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,wind_speed_10m,weather_code",
        "wind_speed_unit": "kn",
        "timezone": "Asia/Tokyo",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except Exception:
        return None
    current = data.get("current") or {}
    code = current.get("weather_code")
    return {
        "temperature_c": current.get("temperature_2m"),
        "wind_kn": current.get("wind_speed_10m"),
        "weather_code": code,
        "summary": WMO.get(int(code), "天候不明") if code is not None else "天候不明",
        "time": current.get("time"),
    }
