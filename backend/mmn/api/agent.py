"""Endpoints the agents use.

``/agent/*`` is only reachable through the agent port of Nginx (mutual TLS). Nginx adds a
shared secret plus the verified client certificate to every request; requests without the
secret are refused, so the endpoint cannot be reached by bypassing Nginx's TLS check.

``/api/agent/install.sh`` and ``/api/agent/download/*`` are public: they serve the install script
and the (non-secret) agent packages through the normal web address.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from mmn.config import get_settings
from mmn.db import get_db, sessionmaker
from mmn.models import Node
from mmn.services import agent_ca, agent_hub, agent_packages, nodes
from mmn.services.audit import audit

log = logging.getLogger("mmn.agents")
router = APIRouter()

H_SECRET = "x-mmn-agent-secret"
H_VERIFY = "x-mmn-client-verify"
H_CERT = "x-mmn-client-cert"


def _remote(headers, client) -> str | None:
    fwd = headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (client.host if client else None)


def _secret_ok(ca: agent_ca.CA, headers) -> bool:
    return hmac.compare_digest(headers.get(H_SECRET, ""), ca.proxy_secret)


async def _authenticate(db: AsyncSession, headers) -> Node | None:
    ca = await agent_ca.get_ca(db)
    if not _secret_ok(ca, headers) or headers.get(H_VERIFY) != "SUCCESS":
        return None
    cert = agent_ca.parse_client_cert(headers.get(H_CERT, ""))
    if cert is None or not agent_ca.verify_client(ca, cert):
        return None
    return (await db.execute(select(Node).where(Node.cert_fingerprint == agent_ca.fingerprint(cert)))
            ).scalar_one_or_none()


@router.post("/agent/enroll", include_in_schema=False)
async def enroll(body: nodes.EnrollIn, request: Request, db: AsyncSession = Depends(get_db)):
    ca = await agent_ca.get_ca(db)
    if not _secret_ok(ca, request.headers):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    ip = _remote(request.headers, request.client)
    try:
        res = await nodes.enroll(db, body, ip)
    except (nodes.NodeError, agent_ca.AgentCAError) as e:
        await db.rollback()
        await audit(db, "node.enroll", source="agent", target=body.hostname[:255] or None, ip=ip, success=False,
                    error=str(e))
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(e)) from e
    await audit(db, "node.enroll", source="agent", target=res["node_name"], ip=ip,
                environment=res["environment"], node_id=res["node_id"])
    await db.commit()
    return res


@router.get("/agent/download/{name}", include_in_schema=False)
async def agent_download(name: str, request: Request, db: AsyncSession = Depends(get_db)):
    if await _authenticate(db, request.headers) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    p = agent_packages.file_path(name)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(p, media_type="application/octet-stream", filename=name)


@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket) -> None:
    async with sessionmaker()() as db:
        node = await _authenticate(db, ws.headers)
        node_id = node.id if node else None
    if node_id is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    lock = asyncio.Lock()

    async def send_json(msg: dict) -> None:
        async with lock:
            await ws.send_text(json.dumps(msg, default=str))

    async def close() -> None:
        await ws.close(code=4000)

    conn = agent_hub.Connection(node_id=node_id, send_json=send_json, close=close,
                                remote_ip=_remote(ws.headers, ws.client))
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=30))
        if first.get("type") != "hello" or not isinstance(first.get("data"), dict):
            await ws.close(code=4400)
            return
        if int(first["data"].get("protocol", 1)) > agent_hub.PROTOCOL:
            log.warning("node %s speaks a newer protocol; update the app", node_id)
        await agent_hub.register(conn)
        await agent_hub.on_hello(conn, first["data"])
        while True:
            raw = await ws.receive_text()
            if len(raw) > 4 * 1024 * 1024:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                try:
                    await agent_hub.handle_message(conn, msg)
                except Exception:  # noqa: BLE001
                    log.exception("message from %s", node_id)
    except (WebSocketDisconnect, TimeoutError, RuntimeError, ValueError):
        pass
    finally:
        await agent_hub.unregister(conn)


@router.websocket("/agent/stream/{sid}")
async def agent_stream(ws: WebSocket, sid: str) -> None:
    """Second connection of an agent for one interactive session (console, terminal)."""
    from mmn.services import console

    async with sessionmaker()() as db:
        node = await _authenticate(db, ws.headers)
        node_id = node.id if node else None
    if node_id is None:
        await ws.close(code=4403)
        return
    await console.attach_agent(sid, node_id, ws)


@router.get("/agent/templates/{tpl_id}/{filename}", include_in_schema=False)
async def agent_template(tpl_id: str, filename: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Custom container templates for the nodes (mTLS only)."""
    from mmn.models import CtTemplate
    from mmn.services import templates

    if await _authenticate(db, request.headers) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    t = await db.get(CtTemplate, tpl_id)
    p = templates.custom_file(t) if t is not None and t.filename == filename else None
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(p, media_type="application/octet-stream", filename=filename)


# ------------------------------------------------------------------ public: install script + packages
def _request_base(request: Request) -> str:
    bp = get_settings().base_path
    return f"{request.url.scheme}://{request.headers.get('host', 'localhost')}{'' if bp == '/' else bp}"


@router.get("/api/agent/install.sh", include_in_schema=False)
async def install_script(request: Request, db: AsyncSession = Depends(get_db)):
    if not agent_packages.manifest():
        return PlainTextResponse("echo 'This build of MCP Management Node has no agent packages.' >&2; exit 1\n",
                                 media_type="text/x-shellscript")
    info = await nodes.connect_info(db, request.headers.get("host"), _request_base(request))
    return PlainTextResponse(agent_packages.install_script(info["app_url"], info["endpoint"], info["fingerprint"]),
                             media_type="text/x-shellscript")


@router.get("/api/agent/download/{name}", include_in_schema=False)
async def public_download(name: str):
    p = agent_packages.file_path(name)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(p, media_type="application/octet-stream", filename=name)
