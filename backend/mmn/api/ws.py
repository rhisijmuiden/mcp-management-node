from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from mmn.api.deps import SESSION_COOKIE
from mmn.db import sessionmaker
from mmn.models import UserSession
from mmn.security import hash_token
from mmn.services import events

router = APIRouter()


@router.websocket("/api/ws")
async def ws_events(ws: WebSocket) -> None:
    """Live events (backup progress, update status, maintenance). Authenticated by the session cookie."""
    token = ws.cookies.get(SESSION_COOKIE)
    origin, host = ws.headers.get("origin"), ws.headers.get("host")
    if origin and host and origin.split("://", 1)[-1] != host:
        await ws.close(code=4403)
        return
    async with sessionmaker()() as db:
        sess = await db.get(UserSession, hash_token(token)) if token else None
        if sess is None or sess.expires_at < datetime.now(UTC) or not sess.user.is_active:
            await ws.close(code=4401)
            return
    await ws.accept()
    q = events.subscribe()
    try:
        await ws.send_json({"event": "hello", "data": {}})
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                await ws.send_json(msg)
            except TimeoutError:
                await ws.send_json({"event": "ping", "data": {}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        events.unsubscribe(q)
