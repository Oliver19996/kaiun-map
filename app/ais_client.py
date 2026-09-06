from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import datetime, timedelta, timezone
from typing import Any

import certifi
import websockets
from websockets.exceptions import ConnectionClosed

from app.config import settings
from app.vessel_store import Vessel, VesselStore

logger = logging.getLogger(__name__)

AIS_URL = "wss://stream.aisstream.io/v0/stream"
MAX_SPAN_DEG = 12.0
JAPAN_BBOX = (24.0, 122.0, 46.5, 148.0)
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def clip_bbox(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> tuple[float, float, float, float]:
    min_lat = max(-85.0, min(min_lat, 85.0))
    max_lat = max(-85.0, min(max_lat, 85.0))
    min_lon = max(-180.0, min(min_lon, 180.0))
    max_lon = max(-180.0, min(max_lon, 180.0))
    if max_lat < min_lat:
        min_lat, max_lat = max_lat, min_lat
    if max_lon < min_lon:
        min_lon, max_lon = max_lon, min_lon

    lat_span = max_lat - min_lat
    lon_span = max_lon - min_lon
    if lat_span > MAX_SPAN_DEG or lon_span > MAX_SPAN_DEG:
        center_lat = (min_lat + max_lat) / 2
        center_lon = (min_lon + max_lon) / 2
        j_min_lat, j_min_lon, j_max_lat, j_max_lon = JAPAN_BBOX
        if j_min_lat <= center_lat <= j_max_lat and j_min_lon <= center_lon <= j_max_lon:
            return JAPAN_BBOX
        half = MAX_SPAN_DEG / 2
        min_lat = max(-85.0, center_lat - half)
        max_lat = min(85.0, center_lat + half)
        min_lon = max(-180.0, center_lon - half)
        max_lon = min(180.0, center_lon + half)
    return min_lat, min_lon, max_lat, max_lon


class AisHub:
    def __init__(self, store: VesselStore) -> None:
        self.store = store
        self.status = "idle"
        self._bbox = clip_bbox(*JAPAN_BBOX)
        self._bbox_event = asyncio.Event()
        self._subscribers: set[asyncio.Queue] = set()
        self._pending: dict[int, Vessel] = {}
        self._lock = asyncio.Lock()

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return self._bbox

    def set_bbox(self, min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> tuple[float, float, float, float]:
        clipped = clip_bbox(min_lat, min_lon, max_lat, max_lon)
        if clipped != self._bbox:
            self._bbox = clipped
            self._bbox_event.set()
        return clipped

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            async with self._lock:
                batch = list(self._pending.values())
                self._pending.clear()
            if not batch:
                continue
            payload = {
                "type": "update",
                "vessels": [v.to_public_dict() for v in batch],
                "count": self.store.count(),
                "status": self.status,
            }
            await self._broadcast(payload)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[asyncio.Queue] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self.unsubscribe(queue)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("MessageType")
        body = (message.get("Message") or {}).get(message_type) or {}
        meta = message.get("MetaData") or {}
        mmsi = body.get("UserID") or meta.get("MMSI")
        if not mmsi:
            return
        mmsi = int(mmsi)

        if message_type in {"PositionReport", "StandardClassBPositionReport"}:
            lat = body.get("Latitude", meta.get("latitude"))
            lon = body.get("Longitude", meta.get("longitude"))
            if lat is None or lon is None:
                return
            heading = body.get("TrueHeading")
            vessel = self.store.upsert_position(
                mmsi=mmsi,
                lat=float(lat),
                lon=float(lon),
                sog=_maybe_float(body.get("Sog")),
                cog=_maybe_float(body.get("Cog")),
                heading=_maybe_float(heading),
                name=meta.get("ShipName"),
            )
            async with self._lock:
                self._pending[mmsi] = vessel
            return

        if message_type == "ShipStaticData":
            dim = body.get("Dimension") or {}
            length = None
            width = None
            try:
                length = float(dim.get("A") or 0) + float(dim.get("B") or 0)
                width = float(dim.get("C") or 0) + float(dim.get("D") or 0)
            except (TypeError, ValueError):
                pass
            vessel = self.store.upsert_static(
                mmsi=mmsi,
                name=body.get("Name") or meta.get("ShipName"),
                call_sign=body.get("CallSign"),
                ship_type=body.get("Type"),
                destination=body.get("Destination"),
                length_m=length or None,
                width_m=width or None,
                ais_eta=_parse_ais_eta(body.get("Eta")),
            )
            async with self._lock:
                self._pending[mmsi] = vessel

    async def run(self) -> None:
        asyncio.create_task(self._flush_loop())
        asyncio.create_task(self._prune_loop())
        if not settings.aisstream_api_key:
            self.status = "no_key"
            logger.warning("AISSTREAM_API_KEY is not set; live AIS is disabled")
            return
        backoff = 1.0
        while True:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AISStream connection failed")
                self.status = "disconnected"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.store.prune(*self._bbox)

    async def _connect_once(self) -> None:
        bbox = self._bbox
        self.status = "connecting"
        async with websockets.connect(
            AIS_URL,
            ssl=SSL_CONTEXT,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:
            await _subscribe(ws, bbox)
            self.status = "connected"
            logger.info("AISStream subscribed to %s", bbox)
            while True:
                recv = asyncio.create_task(ws.recv())
                bbox_wait = asyncio.create_task(self._bbox_event.wait())
                done, pending = await asyncio.wait({recv, bbox_wait}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if bbox_wait in done:
                    self._bbox_event.clear()
                    if self._bbox != bbox:
                        return
                    continue
                try:
                    raw = recv.result()
                except ConnectionClosed:
                    self.status = "disconnected"
                    return
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(message)


async def _subscribe(ws: Any, bbox: tuple[float, float, float, float]) -> None:
    min_lat, min_lon, max_lat, max_lon = bbox
    payload = {
        "APIKey": settings.aisstream_api_key,
        "BoundingBoxes": [[[min_lat, min_lon], [max_lat, max_lon]]],
        "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ShipStaticData"],
    }
    await ws.send(json.dumps(payload))


def _parse_ais_eta(eta: Any) -> str | None:
    if not isinstance(eta, dict):
        return None
    try:
        month = int(eta.get("Month") or 0)
        day = int(eta.get("Day") or 0)
        hour = int(eta.get("Hour") or 0)
        minute = int(eta.get("Minute") or 0)
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    now = datetime.now(timezone.utc)
    year = now.year
    try:
        parsed = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    if parsed < now.replace(month=1, day=1):
        return parsed.isoformat()
    if parsed < now - timedelta(days=60):
        try:
            parsed = datetime(year + 1, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return parsed.isoformat()
    return parsed.isoformat()


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
