from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.places import lookup_place

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "projects.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    def __init__(self, path: Path = DATA_PATH) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"projects": []})

    def _read(self) -> dict[str, Any]:
        with self.path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            projects = self._read()["projects"]
        summaries = []
        for project in projects:
            if project.get("user_id") != user_id:
                continue
            summaries.append(
                {
                    "id": project["id"],
                    "name": project["name"],
                    "notes": project.get("notes") or "",
                    "ship_count": len(project.get("ships") or []),
                    "created_at": project.get("created_at"),
                    "updated_at": project.get("updated_at"),
                }
            )
        summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return summaries

    def get(self, project_id: str, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            for project in self._read()["projects"]:
                if project["id"] == project_id and project.get("user_id") == user_id:
                    return project
        return None

    def create(self, user_id: str, name: str, notes: str = "") -> dict[str, Any]:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("プロジェクト名を入力してください。")
        project = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": cleaned[:80],
            "notes": notes.strip()[:500],
            "ships": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            data = self._read()
            data["projects"].append(project)
            self._write(data)
        return project

    def update(self, project_id: str, user_id: str, name: str | None = None, notes: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            for project in data["projects"]:
                if project["id"] != project_id or project.get("user_id") != user_id:
                    continue
                if name is not None:
                    cleaned = name.strip()
                    if not cleaned:
                        raise ValueError("プロジェクト名を入力してください。")
                    project["name"] = cleaned[:80]
                if notes is not None:
                    project["notes"] = notes.strip()[:500]
                project["updated_at"] = _now()
                self._write(data)
                return project
        return None

    def delete(self, project_id: str, user_id: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data["projects"])
            data["projects"] = [
                p for p in data["projects"] if not (p["id"] == project_id and p.get("user_id") == user_id)
            ]
            if len(data["projects"]) == before:
                return False
            self._write(data)
            return True

    def add_ship(self, project_id: str, user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        ship = _normalize_ship(fields)
        with self._lock:
            data = self._read()
            project = next(
                (p for p in data["projects"] if p["id"] == project_id and p.get("user_id") == user_id),
                None,
            )
            if project is None:
                raise KeyError(project_id)
            existing = next((s for s in project["ships"] if _same_ship(s, ship)), None)
            if existing:
                raise ValueError("同じ船がすでにこのプロジェクトに入っています。")
            project["ships"].append(ship)
            project["updated_at"] = _now()
            self._write(data)
            return ship

    def update_ship(self, project_id: str, ship_id: str, user_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            project = next(
                (p for p in data["projects"] if p["id"] == project_id and p.get("user_id") == user_id),
                None,
            )
            if project is None:
                raise KeyError(project_id)
            for ship in project["ships"]:
                if ship["id"] != ship_id:
                    continue
                patched = _normalize_ship({**ship, **fields}, ship_id=ship_id)
                ship.update(patched)
                project["updated_at"] = _now()
                self._write(data)
                return ship
        return None

    def delete_ship(self, project_id: str, ship_id: str, user_id: str) -> bool:
        with self._lock:
            data = self._read()
            project = next(
                (p for p in data["projects"] if p["id"] == project_id and p.get("user_id") == user_id),
                None,
            )
            if project is None:
                raise KeyError(project_id)
            before = len(project["ships"])
            project["ships"] = [s for s in project["ships"] if s["id"] != ship_id]
            if len(project["ships"]) == before:
                return False
            project["updated_at"] = _now()
            self._write(data)
            return True


def _normalize_ship(fields: dict[str, Any], ship_id: str | None = None) -> dict[str, Any]:
    name = str(fields.get("name") or "").strip()[:80]
    call_sign = str(fields.get("call_sign") or "").strip()[:20].upper()
    notes = str(fields.get("notes") or "").strip()[:300]
    mmsi_raw = fields.get("mmsi")
    mmsi = None
    if mmsi_raw not in (None, ""):
        try:
            mmsi = int(mmsi_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("MMSI は数字で入力してください。") from exc
        if mmsi <= 0:
            raise ValueError("MMSI は正の整数です。")
    if not name and not call_sign and mmsi is None:
        raise ValueError("船名、呼出符号、MMSI のうち少なくとも1つを入れてください。")
    origin_id = str(fields.get("origin_place_id") or "").strip() or None
    dest_id = str(fields.get("dest_place_id") or "").strip() or None
    if origin_id and not lookup_place(origin_id):
        raise ValueError("出発地点がカタログにありません。")
    if dest_id and not lookup_place(dest_id):
        raise ValueError("行き先がカタログにありません。")
    origin = lookup_place(origin_id)
    dest = lookup_place(dest_id)
    origin_pt = None
    dest_pt = None
    if origin:
        min_lat, min_lon, max_lat, max_lon = origin["bbox"]
        origin_pt = [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2]
    if dest:
        min_lat, min_lon, max_lat, max_lon = dest["bbox"]
        dest_pt = [(min_lat + max_lat) / 2, (min_lon + max_lon) / 2]
    return {
        "id": ship_id or str(uuid.uuid4()),
        "name": name,
        "call_sign": call_sign,
        "mmsi": mmsi,
        "notes": notes,
        "origin_place_id": origin_id,
        "origin_name": origin["name"] if origin else None,
        "origin_lat": origin_pt[0] if origin_pt else None,
        "origin_lon": origin_pt[1] if origin_pt else None,
        "dest_place_id": dest_id,
        "dest_name": dest["name"] if dest else None,
        "dest_lat": dest_pt[0] if dest_pt else None,
        "dest_lon": dest_pt[1] if dest_pt else None,
        "planned_arrival_at": str(fields.get("planned_arrival_at") or "").strip() or None,
        "planned_departure_at": str(fields.get("planned_departure_at") or "").strip() or None,
        "updated_at": _now(),
    }


def _same_ship(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("mmsi") and right.get("mmsi") and left["mmsi"] == right["mmsi"]:
        return True
    if left.get("call_sign") and right.get("call_sign") and left["call_sign"] == right["call_sign"]:
        return True
    return False


def ship_matches_live(saved: dict[str, Any], live: dict[str, Any]) -> bool:
    if saved.get("mmsi") and live.get("mmsi") and int(saved["mmsi"]) == int(live["mmsi"]):
        return True
    saved_call = (saved.get("call_sign") or "").strip().upper()
    live_call = (live.get("call_sign") or "").strip().upper()
    if saved_call and live_call and saved_call == live_call:
        return True
    saved_name = (saved.get("name") or "").strip().upper()
    live_name = (live.get("name") or "").strip().upper()
    if saved_name and live_name and saved_name == live_name:
        return True
    return False
