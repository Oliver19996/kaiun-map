from __future__ import annotations

import uuid
from threading import Lock
from typing import Any

from app.projects import _now, _normalize_ship, _refresh_project_places, _same_ship


class GuestProjectStore:
    """In-memory projects for guests. Lost when the browser session cookie is gone or the process restarts."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._by_guest: dict[str, list[dict[str, Any]]] = {}

    def _bucket(self, guest_id: str) -> list[dict[str, Any]]:
        return self._by_guest.setdefault(guest_id, [])

    def clear(self, guest_id: str) -> None:
        with self._lock:
            self._by_guest.pop(guest_id, None)

    def list_projects(self, guest_id: str) -> list[dict[str, Any]]:
        with self._lock:
            projects = list(self._bucket(guest_id))
        summaries = [
            {
                "id": project["id"],
                "name": project["name"],
                "notes": project.get("notes") or "",
                "ship_count": len(project.get("ships") or []),
                "created_at": project.get("created_at"),
                "updated_at": project.get("updated_at"),
            }
            for project in projects
        ]
        summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return summaries

    def get(self, project_id: str, guest_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = next((p for p in self._bucket(guest_id) if p["id"] == project_id), None)
        return _refresh_project_places(found) if found else None

    def create(self, guest_id: str, name: str, notes: str = "") -> dict[str, Any]:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("プロジェクト名を入力してください。")
        project = {
            "id": str(uuid.uuid4()),
            "user_id": guest_id,
            "name": cleaned[:80],
            "notes": notes.strip()[:500],
            "ships": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self._lock:
            self._bucket(guest_id).append(project)
        return project

    def update(self, project_id: str, guest_id: str, name: str | None = None, notes: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            project = next((p for p in self._bucket(guest_id) if p["id"] == project_id), None)
            if project is None:
                return None
            if name is not None:
                cleaned = name.strip()
                if not cleaned:
                    raise ValueError("プロジェクト名を入力してください。")
                project["name"] = cleaned[:80]
            if notes is not None:
                project["notes"] = notes.strip()[:500]
            project["updated_at"] = _now()
            return project

    def delete(self, project_id: str, guest_id: str) -> bool:
        with self._lock:
            bucket = self._bucket(guest_id)
            before = len(bucket)
            self._by_guest[guest_id] = [p for p in bucket if p["id"] != project_id]
            return len(self._by_guest[guest_id]) != before

    def add_ship(self, project_id: str, guest_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        ship = _normalize_ship(fields)
        with self._lock:
            project = next((p for p in self._bucket(guest_id) if p["id"] == project_id), None)
            if project is None:
                raise KeyError(project_id)
            if next((s for s in project["ships"] if _same_ship(s, ship)), None):
                raise ValueError("同じ船がすでにこのプロジェクトに入っています。")
            project["ships"].append(ship)
            project["updated_at"] = _now()
            return ship

    def update_ship(self, project_id: str, ship_id: str, guest_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            project = next((p for p in self._bucket(guest_id) if p["id"] == project_id), None)
            if project is None:
                raise KeyError(project_id)
            for ship in project["ships"]:
                if ship["id"] != ship_id:
                    continue
                patched = _normalize_ship({**ship, **fields}, ship_id=ship_id)
                ship.update(patched)
                project["updated_at"] = _now()
                return ship
        return None

    def delete_ship(self, project_id: str, ship_id: str, guest_id: str) -> bool:
        with self._lock:
            project = next((p for p in self._bucket(guest_id) if p["id"] == project_id), None)
            if project is None:
                raise KeyError(project_id)
            before = len(project["ships"])
            project["ships"] = [s for s in project["ships"] if s["id"] != ship_id]
            if len(project["ships"]) == before:
                return False
            project["updated_at"] = _now()
            return True
