from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.ais_client import AisHub, clip_bbox
from app.ai import assistant_chat, brief_vessel, interpret_search
from app.geo import hours_for_nm, nm_between
from app.config import settings
from app.guest import GuestProjectStore
from app.places import SHIP_TYPE_HINTS, place_catalog_for_prompt
from app.port import port_fields, parse_iso
from app.projects import ProjectStore, ship_matches_live
from app.rate_limit import RateLimiter
from app.sample import SAMPLE_ID, sample_project, sample_summary
from app.users import UserStore, current_user_id
from app.vessel_store import VesselStore
from app.weather import forecast

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
store = VesselStore()
projects = ProjectStore()
guests = GuestProjectStore()
users = UserStore()
hub = AisHub(store)
limiter = RateLimiter(
    per_minute=settings.ai_requests_per_minute,
    daily=settings.daily_ai_request_limit,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(hub.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="kaiun-map", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,
    max_age=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SearchBody(BaseModel):
    q: str = Field(min_length=1, max_length=500)


class BriefBody(BaseModel):
    mmsi: int


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=500)


class ProjectPatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=500)


class ShipBody(BaseModel):
    name: str = Field(default="", max_length=80)
    call_sign: str = Field(default="", max_length=20)
    mmsi: int | None = None
    notes: str = Field(default="", max_length=300)
    origin_place_id: str | None = None
    dest_place_id: str | None = None
    transship_place_ids: list[str] = Field(default_factory=list)
    call_place_ids: list[str] = Field(default_factory=list)
    planned_arrival_at: str | None = None
    planned_departure_at: str | None = None


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    mmsi: int | None = None
    project_id: str | None = None
    ship_id: str | None = None


class AuthBody(BaseModel):
    user_id: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/auth/register")
async def register(body: AuthBody, request: Request) -> dict:
    try:
        user = users.create(body.user_id, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.session["uid"] = user["id"]
    request.session["user_id"] = user["user_id"]
    return user


@app.post("/api/auth/login")
async def login(body: AuthBody, request: Request) -> dict:
    user = users.authenticate(body.user_id, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="ユーザーIDまたはパスワードが違います。")
    request.session["uid"] = user["id"]
    request.session["user_id"] = user["user_id"]
    return user


@app.post("/api/auth/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
async def me(request: Request) -> dict:
    uid = current_user_id(request)
    if not uid:
        _ensure_guest(request)
        return {"guest": True, "user_id": None}
    user = users.get_by_id(uid)
    if user is None:
        request.session.pop("uid", None)
        request.session.pop("user_id", None)
        _ensure_guest(request)
        return {"guest": True, "user_id": None}
    return {**user, "guest": False}


@app.get("/api/places")
async def places() -> dict:
    return {"places": [p for p in place_catalog_for_prompt() if p["id"] != "japan"]}


@app.get("/api/health")
async def health() -> dict:
    return {
        "ais": hub.status,
        "vessels": store.count(),
        "llm": bool(settings.openai_api_key),
        "bbox": hub.bbox,
    }


@app.get("/api/vessels")
async def vessels(q: str = "", limit: int = 50) -> dict:
    hits = store.search(q, limit=min(limit, 100))
    return {"vessels": [v.to_public_dict() for v in hits]}


@app.post("/api/ai/search")
async def ai_search(body: SearchBody, request: Request) -> dict:
    _enforce_ai_limit(request)
    interpreted = await interpret_search(body.q)
    type_codes = None
    hint = interpreted.get("ship_type_hint")
    if hint:
        type_codes = SHIP_TYPE_HINTS[hint]["codes"]
    vessels = []
    if interpreted.get("bbox"):
        min_lat, min_lon, max_lat, max_lon = interpreted["bbox"]
        vessels = [
            v.to_public_dict()
            for v in store.in_bbox(min_lat, min_lon, max_lat, max_lon, interpreted.get("query") or "", type_codes)
        ]
    return {**interpreted, "vessels": vessels[:80]}


@app.get("/api/projects")
async def list_projects(request: Request) -> dict:
    owner_store, owner_id = _owner_store(request)
    return {"projects": [sample_summary(store, _sample_key(request)), *owner_store.list_projects(owner_id)]}


@app.post("/api/projects")
async def create_project(body: ProjectBody, request: Request) -> dict:
    owner_store, owner_id = _owner_store(request)
    try:
        return owner_store.create(owner_id, body.name, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, request: Request) -> dict:
    if project_id == SAMPLE_ID:
        return _project_with_live(sample_project(store, _sample_key(request)))
    owner_store, owner_id = _owner_store(request)
    project = owner_store.get(project_id, owner_id)
    if project is None:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return _project_with_live(project)


@app.patch("/api/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatchBody, request: Request) -> dict:
    _forbid_sample(project_id)
    owner_store, owner_id = _owner_store(request)
    try:
        project = owner_store.update(project_id, owner_id, body.name, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return _project_with_live(project)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request) -> dict:
    _forbid_sample(project_id)
    owner_store, owner_id = _owner_store(request)
    if not owner_store.delete(project_id, owner_id):
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return {"ok": True}


@app.post("/api/projects/{project_id}/ships")
async def add_ship(project_id: str, body: ShipBody, request: Request) -> dict:
    _forbid_sample(project_id)
    owner_store, owner_id = _owner_store(request)
    try:
        ship = owner_store.add_ship(project_id, owner_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project = owner_store.get(project_id, owner_id)
    return {"ship": _live_ship(ship), "project": _project_with_live(project)}


@app.patch("/api/projects/{project_id}/ships/{ship_id}")
async def patch_ship(project_id: str, ship_id: str, body: ShipBody, request: Request) -> dict:
    _forbid_sample(project_id)
    owner_store, owner_id = _owner_store(request)
    try:
        ship = owner_store.update_ship(project_id, ship_id, owner_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ship is None:
        raise HTTPException(status_code=404, detail="船が見つかりません。")
    return {"ship": _live_ship(ship)}


@app.delete("/api/projects/{project_id}/ships/{ship_id}")
async def remove_ship(project_id: str, ship_id: str, request: Request) -> dict:
    _forbid_sample(project_id)
    owner_store, owner_id = _owner_store(request)
    try:
        ok = owner_store.delete_ship(project_id, ship_id, owner_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。") from exc
    if not ok:
        raise HTTPException(status_code=404, detail="船が見つかりません。")
    return {"ok": True}


@app.post("/api/ai/brief")
async def ai_brief(body: BriefBody, request: Request) -> dict:
    _enforce_ai_limit(request)
    vessel = store.get(body.mmsi)
    if vessel is None:
        raise HTTPException(status_code=404, detail="その MMSI はキャッシュにありません。地図上の船を選んでください。")
    briefing = await brief_vessel(vessel)
    return {"mmsi": body.mmsi, **briefing, "vessel": vessel.to_public_dict()}


@app.post("/api/ai/chat")
async def ai_chat(body: ChatBody, request: Request) -> dict:
    _enforce_ai_limit(request)
    context = await _chat_context(body, request)
    return await assistant_chat(body.message, context)


@app.websocket("/ws/ais")
async def ws_ais(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = hub.subscribe()
    try:
        snapshot = _snapshot_payload(*hub.bbox)
        await websocket.send_text(json.dumps(snapshot, ensure_ascii=False))
        sender = asyncio.create_task(_pump(websocket, queue))
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("type") != "bbox":
                    continue
                bbox = clip_bbox(
                    float(message["min_lat"]),
                    float(message["min_lon"]),
                    float(message["max_lat"]),
                    float(message["max_lon"]),
                )
                hub.set_bbox(*bbox)
                await websocket.send_text(json.dumps(_snapshot_payload(*bbox), ensure_ascii=False))
        finally:
            sender.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(queue)


async def _pump(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            payload = await queue.get()
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except (WebSocketDisconnect, asyncio.CancelledError):
        return


def _snapshot_payload(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> dict:
    vessels = [v.to_public_dict() for v in store.in_bbox(min_lat, min_lon, max_lat, max_lon)]
    return {
        "type": "snapshot",
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "vessels": vessels,
        "count": store.count(),
        "status": hub.status,
    }


def _sample_key(request: Request) -> str:
    return current_user_id(request) or _ensure_guest(request)


def _ensure_guest(request: Request) -> str:
    guest_id = request.session.get("guest_id")
    if not guest_id:
        guest_id = str(uuid.uuid4())
        request.session["guest_id"] = guest_id
    return guest_id


def _owner_store(request: Request):
    uid = current_user_id(request)
    if uid:
        return projects, uid
    return guests, _ensure_guest(request)


def _forbid_sample(project_id: str) -> None:
    if project_id == SAMPLE_ID:
        raise HTTPException(status_code=403, detail="サンプルプロジェクトは変更できません。")


def _enforce_ai_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    ok, reason = limiter.allow(ip)
    if not ok:
        raise HTTPException(status_code=429, detail=reason)


def _live_ship(saved: dict) -> dict:
    live = None
    if saved.get("mmsi"):
        vessel = store.get(int(saved["mmsi"]))
        if vessel:
            live = vessel.to_public_dict()
    if live is None:
        query = saved.get("call_sign") or saved.get("name") or ""
        if query:
            for vessel in store.search(query, limit=30):
                public = vessel.to_public_dict()
                if ship_matches_live(saved, public):
                    live = public
                    break
    if live:
        extra = port_fields(
            lat=live.get("lat"),
            lon=live.get("lon"),
            sog=live.get("sog"),
            in_port_since=parse_iso(live.get("in_port_since")),
            dest_place_id=saved.get("dest_place_id"),
            planned_arrival_at=saved.get("planned_arrival_at"),
            planned_departure_at=saved.get("planned_departure_at"),
            ais_eta=live.get("ais_eta"),
        )
        live = {**live, **extra}
    return {**saved, "live": live}


def _project_with_live(project: dict | None) -> dict:
    if project is None:
        return {}
    return {**project, "ships": [_live_ship(ship) for ship in project.get("ships") or []]}


def _resolve_saved_ship(body: ChatBody, request: Request) -> dict | None:
    if body.project_id == SAMPLE_ID or (not body.project_id and body.ship_id and str(body.ship_id).startswith("sample-")):
        project = _project_with_live(sample_project(store, _sample_key(request)))
    elif body.project_id:
        owner_store, owner_id = _owner_store(request)
        project = _project_with_live(owner_store.get(body.project_id, owner_id))
    else:
        return None
    ships = project.get("ships") or []
    if body.ship_id:
        return next((ship for ship in ships if ship.get("id") == body.ship_id), None)
    if body.mmsi:
        return next((ship for ship in ships if ship.get("mmsi") == body.mmsi), None)
    return None


async def _chat_context(body: ChatBody, request: Request) -> dict:
    saved = _resolve_saved_ship(body, request)
    live = (saved or {}).get("live") if saved else None
    if live is None and body.mmsi:
        vessel = store.get(body.mmsi)
        live = vessel.to_public_dict() if vessel else None
    load = {"name": (saved or {}).get("origin_name"), "lat": (saved or {}).get("origin_lat"), "lon": (saved or {}).get("origin_lon")}
    discharge = {"name": (saved or {}).get("dest_name"), "lat": (saved or {}).get("dest_lat"), "lon": (saved or {}).get("dest_lon")}
    transship = (saved or {}).get("transship_places") or []
    calls = (saved or {}).get("call_places") or []
    next_stop = None
    if calls:
        next_stop = {"kind": "寄港地", **calls[0]}
    elif discharge.get("lat") is not None:
        next_stop = {"kind": "船卸港", **discharge}
    eta = None
    if live and live.get("lat") is not None and next_stop and next_stop.get("lat") is not None:
        distance = nm_between(live["lat"], live["lon"], next_stop["lat"], next_stop["lon"])
        hours = hours_for_nm(distance, live.get("sog"))
        eta = {"distance_nm": distance, "hours": hours, "sog": live.get("sog"), "next": next_stop}
    weathers = {}
    for key, place in (("load", load), ("discharge", discharge)):
        if place.get("lat") is None:
            continue
        weathers[key] = {"name": place.get("name"), **((await forecast(place["lat"], place["lon"])) or {})}
    delay = None
    if live:
        delay = {
            "hours": live.get("delay_hours"),
            "reason": live.get("delay_reason"),
            "ais_eta": live.get("ais_eta") or (saved or {}).get("planned_arrival_at"),
            "in_port": live.get("in_port"),
        }
    return {
        "question": body.message,
        "vessel": live,
        "bl": {
            "load": load,
            "transship": transship,
            "discharge": discharge,
        },
        "call_ports": calls,
        "eta": eta,
        "weather": weathers,
        "delay": delay,
    }
