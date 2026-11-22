"""Interactive sessions in the browser: VM console (noVNC), container console and shells (xterm.js)
and interactive community scripts.

The browser connects to ``/api/console/ws/<session>`` (session cookie), the agent opens
``/agent/stream/<session>`` (mutual TLS) for the same session, and the app pipes the two
together. A session can only be used once, by the user who opened it, within 90 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from mmn.services import agent_hub
from mmn.services.agent_hub import AgentError

log = logging.getLogger("mmn.console")

ATTACH_WINDOW = 90


@dataclass
class Session:
    id: str
    node_id: str
    kind: str  # vnc | pty
    mode: str | None
    vmid: int | None
    user_id: str
    username: str
    label: str
    password: str | None = None
    job_id: str | None = None
    created: float = field(default_factory=time.monotonic)
    agent: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Event = field(default_factory=asyncio.Event)
    browser_attached: bool = False
    exit_code: int | None = None


_sessions: dict[str, Session] = {}


def _cleanup() -> None:
    now = time.monotonic()
    for sid, s in list(_sessions.items()):
        if not s.browser_attached and now - s.created > ATTACH_WINDOW:
            s.done.set()
            _sessions.pop(sid, None)


async def open_session(node_id: str, *, kind: str, user_id: str, username: str, label: str, vmid: int | None = None,
                       mode: str | None = None, script: str | None = None, env: dict[str, str] | None = None,
                       cols: int = 120, rows: int = 32, job_id: str | None = None) -> Session:
    _cleanup()
    s = Session(id=secrets.token_hex(16), node_id=node_id, kind=kind, mode=mode, vmid=vmid, user_id=user_id,
                username=username, label=label, job_id=job_id)
    _sessions[s.id] = s
    params: dict[str, Any] = {"session": s.id, "kind": kind, "vmid": vmid or 0, "mode": mode or "",
                              "cols": cols, "rows": rows}
    if script:
        params["script"] = script
    if env:
        params["env"] = env
    try:
        res = await agent_hub.request(node_id, "stream.open", params, timeout=45)
    except AgentError:
        _sessions.pop(s.id, None)
        raise
    s.password = (res or {}).get("password")
    return s


def get(sid: str) -> Session | None:
    _cleanup()
    return _sessions.get(sid)


async def attach_agent(sid: str, node_id: str, ws) -> None:
    """Called for the agent's stream WebSocket; returns when the session is over."""
    s = _sessions.get(sid)
    if s is None or s.node_id != node_id or s.agent.done():
        await ws.close(code=4404)
        return
    await ws.accept()
    s.agent.set_result(ws)
    try:
        await s.done.wait()
    finally:
        _sessions.pop(sid, None)


class _LineRecorder:
    """Turns terminal output into clean log lines for the job of an interactive script."""

    def __init__(self, node_id: str, job_id: str) -> None:
        self.node_id, self.job_id, self.buf = node_id, job_id, ""

    def feed(self, data: bytes) -> None:
        import re

        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]", "",
                      data.decode("utf-8", "replace"))
        self.buf += text
        parts = re.split(r"[\r\n]+", self.buf)
        self.buf = parts.pop()
        for p in parts:
            p = re.sub(r"[\x00-\x08\x0b-\x1f]", "", p).rstrip()
            if p:
                agent_hub.add_line(self.node_id, self.job_id, p[:4000])
        if len(self.buf) > 8000:
            self.buf = self.buf[-2000:]


async def bridge(s: Session, browser) -> None:
    """Pipe the browser WebSocket and the agent stream into each other."""
    s.browser_attached = True
    try:
        agent_ws = await asyncio.wait_for(asyncio.shield(s.agent), timeout=25)
    except TimeoutError:
        await browser.send_text(json.dumps({"type": "exit", "code": -1, "message": "the node did not connect"}))
        await browser.close(code=4504)
        s.done.set()
        return
    rec = _LineRecorder(s.node_id, s.job_id) if s.job_id else None

    async def up() -> None:  # browser -> agent
        while True:
            msg = await browser.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await agent_ws.send_bytes(msg["bytes"])
            elif msg.get("text") is not None:
                await agent_ws.send_text(msg["text"][:4096])

    async def down() -> None:  # agent -> browser
        while True:
            msg = await agent_ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                if rec:
                    rec.feed(msg["bytes"])
                await browser.send_bytes(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "exit":
                        s.exit_code = int(ctrl.get("code") or 0)
                except (ValueError, TypeError):
                    pass
                await browser.send_text(msg["text"])

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except Exception:  # noqa: BLE001
        pass
    finally:
        for t in tasks:
            t.cancel()
        for ws in (agent_ws, browser):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        s.done.set()
        if s.job_id:
            if rec and rec.buf.strip():
                agent_hub.add_line(s.node_id, s.job_id, rec.buf.strip()[:4000])
            code = s.exit_code
            await agent_hub.finish_job(s.job_id, "success" if code == 0 else "failed", {"exit_code": code},
                                       None if code == 0 else (f"exit code {code}" if code is not None
                                                               else "the terminal was closed"))
