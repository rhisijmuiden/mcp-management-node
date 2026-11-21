from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Small in-memory sliding-window limiter (the app runs as a single process)."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = RateLimiter(limit=10, window_seconds=15 * 60)
forgot_limiter = RateLimiter(limit=5, window_seconds=60 * 60)
