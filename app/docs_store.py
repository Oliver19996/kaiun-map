from __future__ import annotations

import json
import math
import re
import uuid
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "vectors"


MAX_UPLOAD_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".txt", ".md", ".csv", ".json"}


def extract_text(filename: str, payload: bytes) -> str:
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("ファイルは2MBまでです。")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("対応形式は PDF / TXT / MD / CSV / JSON です。")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(payload))
        pages = []
        for page in reader.pages[:40]:
            pages.append(page.extract_text() or "")
        text = "\n".join(pages)
    else:
        text = payload.decode("utf-8", errors="ignore")
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) < 20:
        raise ValueError("本文を抽出できませんでした。スキャンPDFや空ファイルは使えません。")
    return cleaned[:80_000]


def chunk_text(text: str, size: int = 700, overlap: int = 80) -> list[str]:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(cleaned) and len(chunks) < 80:
        end = min(len(cleaned), start + size)
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for a, b in zip(left, right):
        dot += a * b
        na += a * a
        nb += b * b
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class DocStore:
    """Per-owner embedding index. Logged-in owners persist under data/vectors/."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._memory: dict[str, dict[str, Any]] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _path(self, owner_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", owner_id)[:64]
        return DATA_DIR / f"{safe}.json"

    def _load(self, owner_id: str, persist: bool) -> dict[str, Any]:
        if owner_id in self._memory:
            return self._memory[owner_id]
        payload = {"docs": [], "chunks": []}
        if persist:
            path = self._path(owner_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
        self._memory[owner_id] = payload
        return payload

    def _save(self, owner_id: str, persist: bool) -> None:
        if not persist:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = self._path(owner_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._memory[owner_id], ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def list_docs(self, owner_id: str, persist: bool) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load(owner_id, persist)
            return list(data.get("docs") or [])

    def add(
        self,
        owner_id: str,
        persist: bool,
        *,
        filename: str,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> dict[str, Any]:
        if len(chunks) != len(embeddings) or not chunks:
            raise ValueError("抽出できる本文がありません。")
        with self._lock:
            data = self._load(owner_id, persist)
            if len(data["docs"]) >= 12:
                raise ValueError("資料は12件までです。不要なファイルを削除してください。")
            doc = {
                "id": str(uuid.uuid4()),
                "filename": filename[:120],
                "chunk_count": len(chunks),
            }
            data["docs"].append(doc)
            for text, vector in zip(chunks, embeddings):
                data["chunks"].append({"doc_id": doc["id"], "filename": doc["filename"], "text": text, "embedding": vector})
            self._save(owner_id, persist)
            return doc

    def delete(self, owner_id: str, persist: bool, doc_id: str) -> bool:
        with self._lock:
            data = self._load(owner_id, persist)
            before = len(data["docs"])
            data["docs"] = [d for d in data["docs"] if d["id"] != doc_id]
            data["chunks"] = [c for c in data["chunks"] if c["doc_id"] != doc_id]
            if len(data["docs"]) == before:
                return False
            self._save(owner_id, persist)
            return True

    def search(self, owner_id: str, persist: bool, query_vec: list[float], limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            chunks = list(self._load(owner_id, persist).get("chunks") or [])
        scored = []
        for chunk in chunks:
            score = cosine(query_vec, chunk.get("embedding") or [])
            scored.append({**chunk, "score": round(score, 4)})
        scored.sort(key=lambda item: item["score"], reverse=True)
        return [
            {"filename": c["filename"], "text": c["text"], "score": c["score"]}
            for c in scored[:limit]
            if c["score"] > 0.15
        ]
