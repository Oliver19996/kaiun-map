from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from app.roles import normalize_role, public_profile

USERS_PATH = Path(__file__).resolve().parent.parent / "data" / "users.json"
USER_ID_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


class UserStore:
    def __init__(self, path: Path = USERS_PATH) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"users": []})

    def _read(self) -> dict[str, Any]:
        with self.path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _write(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def create(self, user_id: str, password: str, role: str = "analyst", company: str = "") -> dict[str, Any]:
        user_id = user_id.strip()
        if not USER_ID_RE.match(user_id):
            raise ValueError("ユーザーIDは3〜32文字の英数字・._- です。")
        if len(password) < 8:
            raise ValueError("パスワードは8文字以上にしてください。")
        role_id = normalize_role(role)
        company_name = (company or "").strip()[:80]
        with self._lock:
            data = self._read()
            if any(u["user_id"].casefold() == user_id.casefold() for u in data["users"]):
                raise ValueError("そのユーザーIDはすでに使われています。")
            record = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "role": role_id,
                "company": company_name,
                "password_salt": secrets.token_hex(16),
                "password_hash": "",
            }
            record["password_hash"] = _hash_password(password, record["password_salt"])
            data["users"].append(record)
            self._write(data)
            return {"id": record["id"], "user_id": record["user_id"], **public_profile(role_id, company_name)}

    def authenticate(self, user_id: str, password: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
        for record in data["users"]:
            if record["user_id"].casefold() != user_id.strip().casefold():
                continue
            digest = _hash_password(password, record["password_salt"])
            if hmac.compare_digest(digest, record["password_hash"]):
                return _public_user(record)
        return None

    def get_by_id(self, internal_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
        for record in data["users"]:
            if record["id"] == internal_id:
                return _public_user(record)
        return None

    def update_profile(self, internal_id: str, role: str | None = None, company: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            for record in data["users"]:
                if record["id"] != internal_id:
                    continue
                if role is not None:
                    record["role"] = normalize_role(role)
                if company is not None:
                    record["company"] = company.strip()[:80]
                self._write(data)
                return _public_user(record)
        return None


def _public_user(record: dict[str, Any]) -> dict[str, Any]:
    profile = public_profile(record.get("role"), record.get("company") or "")
    return {"id": record["id"], "user_id": record["user_id"], **profile}


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 210_000).hex()


def current_user_id(request) -> str | None:
    return request.session.get("uid")


def require_user(request) -> str:
    uid = current_user_id(request)
    if not uid:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="ログインしてください。")
    return uid
