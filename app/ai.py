from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.roles import role_prompt
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


async def assistant_chat(message: str, context: dict[str, Any]) -> dict[str, Any]:
    facts = _chat_facts_text(context)
    docs = context.get("documents") or []
    role_line = context.get("role_prompt") or role_prompt(None)
    if not settings.openai_api_key:
        extra = ""
        if docs:
            extra = "資料抜粋: " + " / ".join(d.get("text", "")[:180] for d in docs[:3])
        return {"reply": facts + extra, "source": "facts_only", "facts": facts, "used_docs": [d.get("filename") for d in docs]}
    system = (
        role_line
        + " 公開AISマップのアシスタントとして、日本語で詳しく答えてください。"
        + " 回答は次の見出しを使ったMarkdownにしてください: 【事実】【資料からの根拠】【役割に応じた含意】【不明・確認が必要】。"
        + " 各見出しの下に2〜5個の短い箇条書きを書いてください。空の見出しは『該当なし』と書いてください。"
        + " JSONのfactsとdocumentsに無い貨物・契約・公式スケジュールは作らないでください。"
        + " 船積・積み替え・船卸はB/L上の港、寄港地はB/L外の運航寄港として必ず区別してください。"
        + " 推測は【役割に応じた含意】にだけ書き、断定しないでください。"
        + " Reply JSON with key reply (Japanese markdown string)."
    )
    payload = {
        "question": message,
        "facts": {k: v for k, v in context.items() if k not in {"role_prompt", "documents"}},
        "documents": docs,
    }
    try:
        data = await _chat_json(
            system=system,
            user=json.dumps(payload, ensure_ascii=False)[:12000],
            max_tokens=min(max(settings.openai_max_tokens, 400), 900),
        )
        reply = str(data.get("reply") or facts)
        return {"reply": reply, "source": "llm", "facts": facts, "used_docs": [d.get("filename") for d in docs]}
    except Exception:
        logger.exception("LLM chat failed")
        return {"reply": facts, "source": "facts_only", "facts": facts, "used_docs": [d.get("filename") for d in docs]}


def _chat_facts_text(context: dict[str, Any]) -> str:
    vessel = context.get("vessel") or {}
    bl = context.get("bl") or {}
    name = vessel.get("name") or "船未選択"
    lines = [f"対象: {name}（MMSI {vessel.get('mmsi') or '未選択'}）。"]
    load = bl.get("load") or {}
    discharge = bl.get("discharge") or {}
    trans = "、".join(p.get("name") or "" for p in (bl.get("transship") or []) if p.get("name")) or "未設定"
    calls = "、".join(p.get("name") or "" for p in (context.get("call_ports") or []) if p.get("name")) or "未設定"
    lines.append(f"B/L 船積 {load.get('name') or '未設定'} / 積み替え {trans} / 船卸 {discharge.get('name') or '未設定'}。")
    lines.append(f"寄港地（B/L外）: {calls}。")
    weather = context.get("weather") or {}
    for key, label in (("load", "船積港"), ("discharge", "船卸港")):
        item = weather.get(key)
        if not item:
            continue
        lines.append(
            f"{label}（{item.get('name') or '不明'}）の天気: {item.get('summary') or '不明'}、"
            f"{item.get('temperature_c')}℃、風 {item.get('wind_kn')} kn。"
        )
    eta = context.get("eta")
    if eta:
        nxt = (eta.get("next") or {}).get("name") or "次地点"
        hours = eta.get("hours")
        lines.append(
            f"現在地から{nxt}まで約 {eta.get('distance_nm')} 海里。"
            + (f"速力 {eta.get('sog')} kn なら約 {hours} 時間。" if hours is not None else "速力が低いため所要時間は出せません。")
        )
    delay = context.get("delay") or {}
    if delay.get("hours") is not None:
        lines.append(f"遅れ: {delay.get('hours')} 時間。{delay.get('reason') or ''}")
    elif delay.get("ais_eta"):
        lines.append(f"予告着港（AIS ETA）: {delay.get('ais_eta')}。遅延時間はまだ算出できません。")
    else:
        lines.append("予告着港時刻が無いため遅れは算出できません。")
    if vessel.get("lat") is not None:
        lines.append(f"現在位置 {vessel.get('lat')}, {vessel.get('lon')}、速力 {vessel.get('sog')} kn。")
    return "".join(lines)


def _facts_text(facts: dict[str, Any]) -> str:
    name = facts.get("name") or "船名未着"
    return (
        f"{name}（MMSI {facts.get('mmsi')}）。"
        f"位置 {facts.get('lat')}, {facts.get('lon')}。"
        f"速力 {facts.get('sog')} kn、針路 {facts.get('cog')}°、船種 {facts.get('ship_type_label') or '不明'}。"
        f"呼出符号 {facts.get('call_sign') or '不明'}、目的地 {facts.get('destination') or '不明'}。"
        f"最終更新 {facts.get('updated_at')}。"
    )


async def embed_texts(texts: list[str]) -> list[list[float]]:
    cleaned = [t.strip()[:4000] for t in texts if t and t.strip()]
    if not cleaned:
        return []
    if not settings.openai_api_key:
        return [_hash_embedding(t) for t in cleaned]
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    body = {"model": settings.openai_embed_model, "input": cleaned}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post("https://api.openai.com/v1/embeddings", headers=headers, json=body)
        response.raise_for_status()
        data = response.json()["data"]
    data.sort(key=lambda item: item["index"])
    return [item["embedding"] for item in data]


def _hash_embedding(text: str, dim: int = 64) -> list[float]:
    import hashlib
    import re

    vec = [0.0] * dim
    for token in re.findall(r"[A-Za-z0-9]+|[一-龥ぁ-んァ-ンー]+", text.lower()):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        vec[digest[0] % dim] += 1.0 + digest[1] / 255.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


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
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM did not return an object")
    return parsed
