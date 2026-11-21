"""In-process event bus that feeds the WebSocket endpoint."""

from __future__ import annotations

import asyncio
from typing import Any

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def publish(event: str, data: dict[str, Any] | None = None) -> None:
    msg = {"event": event, "data": data or {}}
    for q in list(_subscribers):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass
