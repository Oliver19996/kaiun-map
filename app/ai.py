from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.places import (
    SHIP_TYPE_HINTS,
    lookup_place,
    match_place_from_text,
    match_ship_hint_from_text,
    place_catalog_for_prompt,
)
from app.vessel_store import Vessel

logger = logging.getLogger(__name__)

SEARCH_SCHEMA_HINT = """\
Return JSON only with keys:
- place_id: one of the provided catalog ids, or null
- ship_type_hint: one of tanker, cargo, container, passenger, fishing, tug, pleasure, or null
- query: optional ship name / MMSI fragment or empty string
- clarification: Japanese question if the place is unknown, else empty string
Do not invent coordinates. Do not pick a place_id that is not in the catalog.
"""


async def interpret_search(user_text: str) -> dict[str, Any]:
    """Map a natural-language query onto a catalog place. Never trust model coords."""
    fallback = _heuristic_search(user_text)
    if not settings.openai_api_key:
        fallback["source"] = "heuristic"
        return fallback

    catalog = place_catalog_for_prompt()
    system = (
        "You convert maritime search queries into structured filters. "
        + SEARCH_SCHEMA_HINT
        + " Place catalog:\n"
        + json.dumps(catalog, ensure_ascii=False)
    )
    try:
        data = await _chat_json(
            system=system,
            user=user_text[:500],
            max_tokens=min(settings.openai_max_tokens, 250),
        )
    except Exception:
        logger.exception("LLM search failed; using heuristic")
        fallback["source"] = "heuristic"
        fallback["clarification"] = fallback.get("clarification") or "AI を呼べなかったため、キーワード照合で処理しました。"
        return fallback

    place_id = data.get("place_id") if data.get("place_id") in {p["id"] for p in catalog} else None
    hint = data.get("ship_type_hint")
    if hint not in SHIP_TYPE_HINTS:
        hint = None
    place = lookup_place(place_id)
    clarification = (data.get("clarification") or "").strip()
    if not place_id and not clarification:
        clarification = "どの海域・港か、カタログにある地名で指定してください。"
    return {
        "place_id": place_id,
        "place_name": place["name"] if place else None,
        "bbox": place["bbox"] if place else None,
        "ship_type_hint": hint,
        "ship_type_label": SHIP_TYPE_HINTS[hint]["label"] if hint else None,
        "query": str(data.get("query") or "").strip()[:80],
        "clarification": clarification if not place_id else "",
        "source": "llm",
    }


def _heuristic_search(user_text: str) -> dict[str, Any]:
    place_id = match_place_from_text(user_text)
    hint = match_ship_hint_from_text(user_text)
    place = lookup_place(place_id)
    clarification = ""
    if not place_id:
        clarification = "どの海域・港か、東京湾・大阪湾など具体的な地名で指定してください。"
    return {
        "place_id": place_id,
        "place_name": place["name"] if place else None,
        "bbox": place["bbox"] if place else None,
        "ship_type_hint": hint,
        "ship_type_label": SHIP_TYPE_HINTS[hint]["label"] if hint else None,
        "query": "",
        "clarification": clarification,
        "source": "heuristic",
    }


async def brief_vessel(vessel: Vessel) -> dict[str, Any]:
    facts = vessel.to_public_dict()
    if not settings.openai_api_key:
        return {
            "facts": _facts_text(facts),
            "speculation": "LLM キーが無いため推測は生成していません。船種から用途を一般論として読む程度に留めてください。",
            "source": "facts_only",
        }

    system = (
        "You write a short Japanese briefing for a public AIS map. "
        "Input is JSON facts from AIS. "
        "Reply JSON with keys facts (string: only restate given fields, no extra claims) "
        "and speculation (string: clearly labeled guesses such as typical use of that ship type). "
        "Never claim official schedules, cargo, ownership, or crew. "
        "If a field is missing, say it is unknown."
    )
    try:
        data = await _chat_json(
            system=system,
            user=json.dumps(facts, ensure_ascii=False),
            max_tokens=min(settings.openai_max_tokens, 400),
        )
        return {
            "facts": str(data.get("facts") or _facts_text(facts)),
            "speculation": str(data.get("speculation") or ""),
            "source": "llm",
        }
    except Exception:
        logger.exception("LLM brief failed")
        return {
            "facts": _facts_text(facts),
            "speculation": "解説の生成に失敗したため、AIS の数値のみ表示しています。",
            "source": "facts_only",
        }


def _facts_text(facts: dict[str, Any]) -> str:
    name = facts.get("name") or "船名未着"
    return (
        f"{name}（MMSI {facts.get('mmsi')}）。"
        f"位置 {facts.get('lat')}, {facts.get('lon')}。"
        f"速力 {facts.get('sog')} kn、針路 {facts.get('cog')}°、船種 {facts.get('ship_type_label') or '不明'}。"
        f"呼出符号 {facts.get('call_sign') or '不明'}、目的地 {facts.get('destination') or '不明'}。"
        f"最終更新 {facts.get('updated_at')}。"
    )


async def _chat_json(*, system: str, user: str, max_tokens: int) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.openai_model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM did not return an object")
    return parsed
