"""Live connections to agents and the request/response protocol on top of them.

Every agent keeps one WebSocket open (``/agent/ws`` behind Nginx mTLS). The hub remembers the
connection per node, sends requests (``{"type": "req", "id", "method", "params"}``) and resolves
the answer (``res``). Status messages from the agent (hello, metrics, updates, cluster, job) are
stored on the node and announced to the browser through the event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from mmn.db import sessionmaker
from mmn.models import Node, NodeJob
from mmn.services import events

log = logging.getLogger("mmn.agents")

PROTOCOL = 1
MAX_OUTPUT = 512 * 1024


class AgentError(Exception):
    pass


class AgentOffline(AgentError):
    def __init__(self) -> None:
        super().__init__("the agent of this node is not connected")


@dataclass
class Connection:
    node_id: str
    send_json: Any  # async callable(dict)
    close: Any  # async callable()
    remote_ip: str | None = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending: dict[str, asyncio.Future] = field(default_factory=dict)

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> Any:
        rid = secrets.token_hex(8)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.send_json({"type": "req", "id": rid, "method": method, "params": params or {}})
            msg = await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            raise AgentError(f"the agent did not answer '{method}' within {int(timeout)}s") from e
        finally:
            self.pending.pop(rid, None)
        if not msg.get("ok"):
            raise AgentError(msg.get("error") or "the agent reported an error")
        return msg.get("result")

    def fail_pending(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(AgentOffline())
        self.pending.clear()


_conns: dict[str, Connection] = {}
_job_buffers: dict[str, list[str]] = {}
_bg: set[asyncio.Task] = set()
_flush_soon: set[str] = set()


def is_online(node_id: str) -> bool:
    return node_id in _conns


def online_ids() -> set[str]:
    return set(_conns)


def get(node_id: str) -> Connection:
    c = _conns.get(node_id)
    if c is None:
        raise AgentOffline()
    return c


async def request(node_id: str, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> Any:
    return await get(node_id).request(method, params, timeout)


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t


async def register(conn: Connection) -> None:
    old = _conns.get(conn.node_id)
    _conns[conn.node_id] = conn
    if old is not None and old is not conn:
        old.fail_pending()
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass


async def unregister(conn: Connection) -> None:
    conn.fail_pending()
    if _conns.get(conn.node_id) is conn:
        del _conns[conn.node_id]
        async with sessionmaker()() as db:
            node = await db.get(Node, conn.node_id)
            if node is not None:
                node.last_seen_at = datetime.now(UTC)
                await db.commit()
        events.publish("node.status", {"node_id": conn.node_id, "online": False})


async def disconnect(node_id: str) -> None:
    c = _conns.get(node_id)
    if c is not None:
        try:
            await c.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ incoming messages
def _clean(v: Any) -> Any:
    """Keep only JSON-safe data of a sane size."""
    raw = json.dumps(v, default=str)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("message too large")
    return json.loads(raw)


def _hello_fields(node: Node, data: dict[str, Any]) -> None:
    node.agent_version = str(data.get("agent_version", ""))[:64] or None
    node.os = str(data.get("os", ""))[:32] or None
    node.arch = str(data.get("arch", ""))[:16] or None
    node.install_kind = str(data.get("install", ""))[:16] or None
    node.hostname = str(data.get("hostname", ""))[:255] or node.hostname
    caps = data.get("capabilities") or []
    node.capabilities = [str(c)[:32] for c in caps][:50] if isinstance(caps, list) else []
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    node.info = _clean(info)
    cluster = info.get("cluster") if isinstance(info.get("cluster"), dict) else None
    node.cluster_name = (str(cluster.get("name"))[:100] or None) if cluster and cluster.get("name") else None
    if "pve" in node.capabilities:
        node.kind = "proxmox"
    if isinstance(data.get("governor"), dict):
        node.governor = _clean(data["governor"])


async def handle_message(conn: Connection, msg: dict[str, Any]) -> None:
    typ = msg.get("type")
    if typ == "res":
        fut = conn.pending.get(str(msg.get("id")))
        if fut is not None and not fut.done():
            fut.set_result(msg)
        return
    if typ == "job":
        await _job_message(conn.node_id, msg)
        return
    data = msg.get("data")
    if not isinstance(data, dict):
        return
    async with sessionmaker()() as db:
        node = await db.get(Node, conn.node_id)
        if node is None:
            return
        now = datetime.now(UTC)
        node.last_seen_at = now
        if typ == "metrics":
            node.metrics = _clean(data)
            if data.get("governor"):
                node.governor = {**(node.governor or {}), "current": str(data["governor"])[:32]}
        elif typ == "updates":
            node.updates = _clean(data)
        elif typ == "cluster":
            node.info = {**(node.info or {}), "cluster": _clean(data)}
            node.cluster_name = (str(data.get("name"))[:100] or None) if data.get("name") else None
        elif typ == "hello":
            _hello_fields(node, data)
        else:
            return
        await db.commit()
    events.publish("node.status", {"node_id": conn.node_id, "online": True, "type": typ})


async def on_hello(conn: Connection, data: dict[str, Any]) -> None:
    async with sessionmaker()() as db:
        node = await db.get(Node, conn.node_id)
        if node is None:
            return
        now = datetime.now(UTC)
        _hello_fields(node, data)
        node.last_seen_at = node.connected_at = now
        node.remote_ip = conn.remote_ip
        # A running agent update is finished once the agent comes back with the new version.
        jobs = (await db.execute(select(NodeJob).where(NodeJob.node_id == node.id, NodeJob.status == "running",
                                                       NodeJob.kind == "agent.update"))).scalars().all()
        for j in jobs:
            if j.params.get("version") and j.params["version"] == node.agent_version:
                _finish(j, "success", {"version": node.agent_version}, None)
            elif now - j.started_at > timedelta(minutes=10):
                _finish(j, "failed", {}, "the agent did not come back with the new version")
        await db.commit()
    events.publish("node.status", {"node_id": conn.node_id, "online": True, "type": "hello"})
    from mmn.services import nodes

    spawn(nodes.reconcile(conn.node_id))


# ------------------------------------------------------------------ jobs
def _finish(job: NodeJob, status: str, result: dict[str, Any] | None, error: str | None) -> None:
    job.status = status
    job.result = result or {}
    job.error = (error or None) and str(error)[:5000]
    job.finished_at = datetime.now(UTC)


async def _flush(job_id: str) -> None:
    lines = _job_buffers.get(job_id)
    if not lines:
        return
    _job_buffers[job_id] = []
    async with sessionmaker()() as db:
        job = await db.get(NodeJob, job_id)
        if job is None:
            return
        out = (job.output or "") + "\n".join(lines) + "\n"
        job.output = out[-MAX_OUTPUT:]
        await db.commit()


async def _job_message(node_id: str, msg: dict[str, Any]) -> None:
    job_id = str(msg.get("job_id") or "")
    if not job_id:
        return
    if msg.get("event") == "line":
        line = str(msg.get("line", ""))[:4000]
        buf = _job_buffers.setdefault(job_id, [])
        buf.append(line)
        events.publish("node.job", {"node_id": node_id, "job_id": job_id, "line": line})
        if len(buf) >= 200:
            await _flush(job_id)
        elif job_id not in _flush_soon:
            _flush_soon.add(job_id)

            async def later() -> None:
                await asyncio.sleep(1.5)
                _flush_soon.discard(job_id)
                await _flush(job_id)

            spawn(later())
        return
    if msg.get("event") == "done":
        await _flush(job_id)
        _job_buffers.pop(job_id, None)
        async with sessionmaker()() as db:
            job = await db.get(NodeJob, job_id)
            if job is None or job.node_id != node_id or job.status != "running":
                return
            if job.kind == "agent.update" and msg.get("status") == "success":
                # Wait for the agent to reconnect with the new version.
                job.output = (job.output or "") + "waiting for the agent to restart…\n"
                await db.commit()
                return
            result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            _finish(job, "success" if msg.get("status") == "success" else "failed", _clean(result), msg.get("error"))
            await db.commit()
        events.publish("node.job", {"node_id": node_id, "job_id": job_id, "done": True})
        if job.kind == "updates.apply":
            from mmn.services import nodes

            spawn(nodes.job_finished_mail(job_id))


async def expire_stale_jobs() -> None:
    """Jobs that never reported back (agent reinstalled, node gone) are closed after a while."""
    async with sessionmaker()() as db:
        cutoff = datetime.now(UTC) - timedelta(hours=3)
        jobs = (await db.execute(select(NodeJob).where(NodeJob.status == "running",
                                                       NodeJob.started_at < cutoff))).scalars().all()
        for j in jobs:
            _finish(j, "failed", {}, "no result from the agent (timed out)")
        await db.commit()
