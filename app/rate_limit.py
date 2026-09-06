from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock


class RateLimiter:
    def __init__(self, per_minute: int, daily: int) -> None:
        self.per_minute = per_minute
        self.daily = daily
        self._lock = Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._daily: dict[str, tuple[str, int]] = {}

    def allow(self, ip: str) -> tuple[bool, str]:
        now = datetime.now(timezone.utc)
        now_ts = now.timestamp()
        day = now.date().isoformat()
        with self._lock:
            window = self._hits[ip]
            cutoff = now_ts - 60
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= self.per_minute:
                return False, "短時間のリクエストが多すぎます。少し待ってから再試行してください。"
            stored_day, count = self._daily.get(ip, (day, 0))
            if stored_day != day:
                count = 0
            if count >= self.daily:
                return False, "本日の AI 利用上限に達しました。"
            window.append(now_ts)
            self._daily[ip] = (day, count + 1)
            return True, ""
