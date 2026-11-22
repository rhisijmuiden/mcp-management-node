"""VMs and containers: list, details, power, snapshots, migration, console, create."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import SESSION_COOKIE, client_ip, current_user, require_admin
from mmn.api.proxmox import _err
from mmn.db import get_db, sessionmaker
from mmn.models import Role, User, UserSession
from mmn.security import hash_token
from mmn.services import console, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit

router = APIRouter(tags=["guests"])


async def _guest(db: AsyncSession, node_id: str, vmid: int):
    try:
        return await guests.find_guest(db, vmid, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e


async def _act(db: AsyncSession, request: Request, admin: User, action: str, target: str, coro, **detail):
    try:
        res = await coro
    except (nodes.NodeError, AgentError) as e:
        await audit(db, action, user=admin, target=target, ip=client_ip(request), success=False, error=str(e), **detail)
        await db.commit()
        raise _err(e) from e
    extra = {"job_id": res.id} if hasattr(res, "id") else {}
    await audit(db, action, user=admin, target=target, ip=client_ip(request), **detail, **extra)
    await db.commit()
    return nodes.job_out(res) if hasattr(res, "kind") else res


def _label(g: dict) -> str:
    return f"{g['vmid']} {g['name']}".strip()


# ------------------------------------------------------------------ list / detail
@router.get("/api/guests")
async def list_guests(environment_id: str | None = None, node_id: str | None = None,
                      type: Literal["qemu", "lxc"] | None = None, q: str | None = None,
                      db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await guests.list_guests(db, environment_id=environment_id, node_id=node_id, type_=type, query=q)


@router.get("/api/guests/suggest-node")
async def suggest_node(environment_id: str | None = None, db: AsyncSession = Depends(get_db),
                       _: User = Depends(current_user)):
    return await guests.suggest_node(db, environment_id) or {}


@router.get("/api/guests/cloud-images")
async def cloud_images(_: User = Depends(current_user)):
    return [{"key": k, **v} for k, v in guests.CLOUD_IMAGES.items()]


@router.get("/api/guests/{node_id}/{vmid}")
async def guest_detail(node_id: str, vmid: int, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    node, g = await _guest(db, node_id, vmid)
    try:
        return await guests.guest_detail(node, g)
    except AgentError as e:
        return {**g, "config": None, "snapshots": [], "error": str(e)}


@router.post("/api/nodes/{node_id}/guests/refresh")
async def refresh_guests(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await nodes.get_node(db, node_id)
    await guests.refresh(n.id)
    return {"ok": True}


@router.get("/api/nodes/{node_id}/resources")
async def node_resources(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        n = await nodes.get_node(db, node_id)
        return await guests.node_resources(n)
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e


# ------------------------------------------------------------------ actions
class PowerIn(BaseModel):
    action: Literal["start", "stop", "shutdown", "reboot", "suspend", "resume"]


@router.post("/api/guests/{node_id}/{vmid}/power", status_code=202)
async def power(node_id: str, vmid: int, body: PowerIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    return await _act(db, request, admin, f"guest.{body.action}", _label(g),
                      guests.power(node, g, body.action, requested_by=admin.username), node=node.name)


class SnapshotIn(BaseModel):
    name: str = Field(min_length=2, max_length=40)
    description: str = Field(default="", max_length=500)
    vmstate: bool = False


@router.get("/api/guests/{node_id}/{vmid}/snapshots")
async def list_snapshots(node_id: str, vmid: int, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    node, g = await _guest(db, node_id, vmid)
    try:
        return await guests.list_snapshots(node, g)
    except AgentError as e:
        raise _err(e) from e


@router.post("/api/guests/{node_id}/{vmid}/snapshots", status_code=202)
async def create_snapshot(node_id: str, vmid: int, body: SnapshotIn, request: Request,
                          db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    return await _act(db, request, admin, "guest.snapshot", _label(g),
                      guests.create_snapshot(node, g, body.name, body.description, body.vmstate,
                                             requested_by=admin.username), snapshot=body.name)


class ConfirmIn(BaseModel):
    confirm: str = ""
    start: bool = False


@router.post("/api/guests/{node_id}/{vmid}/snapshots/{name}/rollback", status_code=202)
async def rollback(node_id: str, vmid: int, name: str, body: ConfirmIn, request: Request,
                   db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    if body.confirm != name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="confirm_mismatch")
    return await _act(db, request, admin, "guest.rollback", _label(g),
                      guests.rollback_snapshot(node, g, name, start=body.start, requested_by=admin.username),
                      snapshot=name)


@router.delete("/api/guests/{node_id}/{vmid}/snapshots/{name}", status_code=202)
async def delete_snapshot(node_id: str, vmid: int, name: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    return await _act(db, request, admin, "guest.snapshot.delete", _label(g),
                      guests.delete_snapshot(node, g, name, requested_by=admin.username), snapshot=name)


class MigrateIn(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    online: bool = True


@router.post("/api/guests/{node_id}/{vmid}/migrate", status_code=202)
async def migrate(node_id: str, vmid: int, body: MigrateIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    return await _act(db, request, admin, "guest.migrate", _label(g),
                      guests.migrate(db, node, g, body.target, online=body.online, requested_by=admin.username),
                      target_node=body.target)


@router.put("/api/guests/{node_id}/{vmid}/resources")
async def set_resources(node_id: str, vmid: int, body: guests.ResourcesIn, request: Request,
                        db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    return await _act(db, request, admin, "guest.config", _label(g), guests.set_resources(node, g, body),
                      **body.model_dump(exclude_none=True, exclude={"description"}))


@router.post("/api/guests/{node_id}/{vmid}/destroy", status_code=202)
async def destroy(node_id: str, vmid: int, body: ConfirmIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    if body.confirm not in (str(g["vmid"]), g["name"]) or not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="confirm_mismatch")
    return await _act(db, request, admin, "guest.destroy", _label(g),
                      guests.destroy(node, g, requested_by=admin.username))


# ------------------------------------------------------------------ create
@router.post("/api/guests/create/ct", status_code=202)
async def create_ct(body: guests.CreateCtIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    return await _act(db, request, admin, "guest.create", body.name,
                      guests.create_ct(db, body, requested_by=admin.username), type="lxc", node_id=body.node_id,
                      template=body.template_id or body.ostemplate)


@router.post("/api/guests/create/vm", status_code=202)
async def create_vm(body: guests.CreateVmIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    return await _act(db, request, admin, "guest.create", body.name,
                      guests.create_vm(db, body, requested_by=admin.username), type="qemu", node_id=body.node_id,
                      source=body.source, image=body.iso or body.iso_url or body.cloud_image or body.cloud_url)


# ------------------------------------------------------------------ console
class ConsoleIn(BaseModel):
    mode: Literal["auto", "vnc", "lxc-console", "lxc-shell"] = "auto"
    cols: int = Field(default=120, ge=10, le=1000)
    rows: int = Field(default=32, ge=5, le=500)


@router.post("/api/guests/{node_id}/{vmid}/console")
async def open_console(node_id: str, vmid: int, body: ConsoleIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    node, g = await _guest(db, node_id, vmid)
    if g["status"] != "running":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="guest_not_running")
    mode = body.mode
    if mode == "auto":
        mode = "vnc" if g["type"] == "qemu" else "lxc-console"
    if (mode == "vnc") != (g["type"] == "qemu"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="console mode does not fit this guest")
    kind = "vnc" if mode == "vnc" else "pty"
    try:
        s = await console.open_session(node.id, kind=kind, mode=None if kind == "vnc" else mode, vmid=g["vmid"],
                                       user_id=admin.id, username=admin.username, label=_label(g),
                                       cols=body.cols, rows=body.rows)
    except AgentError as e:
        await audit(db, "guest.console", user=admin, target=_label(g), ip=client_ip(request), success=False,
                    error=str(e))
        await db.commit()
        raise _err(e) from e
    await audit(db, "guest.console", user=admin, target=_label(g), ip=client_ip(request), mode=mode, node=node.name)
    await db.commit()
    return {"session": s.id, "kind": kind, "mode": mode, "password": s.password, "label": s.label,
            "type": g["type"], "node": node.name, "vmid": g["vmid"]}


class ShellIn(BaseModel):
    cols: int = Field(default=120, ge=10, le=1000)
    rows: int = Field(default=32, ge=5, le=500)


@router.post("/api/nodes/{node_id}/shell")
async def node_shell(node_id: str, body: ShellIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    n = await nodes.get_node(db, node_id)
    if "terminal" not in (n.capabilities or []):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="this agent has no terminal support yet (update it)")
    try:
        s = await console.open_session(n.id, kind="pty", mode="node-shell", user_id=admin.id,
                                       username=admin.username, label=n.name, cols=body.cols, rows=body.rows)
    except AgentError as e:
        raise _err(e) from e
    await audit(db, "node.shell", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    return {"session": s.id, "kind": "pty", "mode": "node-shell", "label": n.name, "node": n.name}


@router.get("/api/console/{sid}")
async def console_info(sid: str, user: User = Depends(current_user)):
    s = console.get(sid)
    if s is None or s.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return {"session": s.id, "kind": s.kind, "mode": s.mode, "label": s.label, "password": s.password,
            "vmid": s.vmid, "attached": s.browser_attached, "job_id": s.job_id}


@router.websocket("/api/console/ws/{sid}")
async def console_ws(ws: WebSocket, sid: str) -> None:
    token = ws.cookies.get(SESSION_COOKIE)
    origin, host = ws.headers.get("origin"), ws.headers.get("host")
    if origin and host and origin.split("://", 1)[-1] != host:
        await ws.close(code=4403)
        return
    async with sessionmaker()() as db:
        sess = await db.get(UserSession, hash_token(token)) if token else None
        if sess is None or sess.expires_at < datetime.now(UTC) or not sess.user.is_active \
                or sess.user.role != Role.admin:
            await ws.close(code=4401)
            return
        user_id = sess.user.id
    s = console.get(sid)
    if s is None or s.user_id != user_id or s.browser_attached:
        await ws.close(code=4404)
        return
    await ws.accept()
    await console.bridge(s, ws)


@router.delete("/api/console/{sid}", status_code=204)
async def close_console(sid: str, user: User = Depends(current_user)):
    s = console.get(sid)
    if s is not None and s.user_id == user.id:
        s.done.set()
    return Response(status_code=204)

