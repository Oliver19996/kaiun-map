from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.ais_client import AisHub, clip_bbox
from app.ai import brief_vessel, interpret_search
from app.config import settings
from app.places import SHIP_TYPE_HINTS
from app.projects import ProjectStore, ship_matches_live
from app.rate_limit import RateLimiter
from app.vessel_store import VesselStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
store = VesselStore()
projects = ProjectStore()
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
async def list_projects() -> dict:
    return {"projects": projects.list_projects()}


@app.post("/api/projects")
async def create_project(body: ProjectBody) -> dict:
    try:
        return projects.create(body.name, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    project = projects.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return _project_with_live(project)


@app.patch("/api/projects/{project_id}")
async def patch_project(project_id: str, body: ProjectPatchBody) -> dict:
    try:
        project = projects.update(project_id, body.name, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return _project_with_live(project)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    if not projects.delete(project_id):
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。")
    return {"ok": True}


@app.post("/api/projects/{project_id}/ships")
async def add_ship(project_id: str, body: ShipBody) -> dict:
    try:
        ship = projects.add_ship(project_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    project = projects.get(project_id)
    return {"ship": _live_ship(ship), "project": _project_with_live(project)}


@app.patch("/api/projects/{project_id}/ships/{ship_id}")
async def patch_ship(project_id: str, ship_id: str, body: ShipBody) -> dict:
    try:
        ship = projects.update_ship(project_id, ship_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロジェクトが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ship is None:
        raise HTTPException(status_code=404, detail="船が見つかりません。")
    return {"ship": _live_ship(ship)}


@app.delete("/api/projects/{project_id}/ships/{ship_id}")
async def remove_ship(project_id: str, ship_id: str) -> dict:
    try:
        ok = projects.delete_ship(project_id, ship_id)
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


def _enforce_ai_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    ok, reason = limiter.allow(ip)
    if not ok:
        raise HTTPException(status_code=429, detail=reason)


def _live_ship(saved: dict) -> dict:
    if saved.get("mmsi"):
        vessel = store.get(int(saved["mmsi"]))
        if vessel:
            return {**saved, "live": vessel.to_public_dict()}
    query = saved.get("call_sign") or saved.get("name") or ""
    if query:
        for vessel in store.search(query, limit=30):
            public = vessel.to_public_dict()
            if ship_matches_live(saved, public):
                return {**saved, "live": public}
    return {**saved, "live": None}


def _project_with_live(project: dict | None) -> dict:
    if project is None:
        return {}
    return {**project, "ships": [_live_ship(ship) for ship in project.get("ships") or []]}
