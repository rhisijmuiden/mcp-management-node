"""Standalone Linux and Windows servers: overview, troubleshooting across servers and data copies."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import Transfer, User
from mmn.services import nodes, servers
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit

router = APIRouter(tags=["servers"])

OsFilter = Literal["linux", "windows", "proxmox"]


@router.get("/api/servers/overview")
async def overview(environment_id: str | None = None, os: Literal["linux", "windows"] | None = None,
                   role: Literal["server", "workstation"] | None = None,
                   db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await servers.overview(db, environment_id, os, role)


class TargetsIn(BaseModel):
    servers: list[str] | None = Field(default=None, max_length=200)
    environment: str | None = None
    os: OsFilter | None = None


async def _targets(db: AsyncSession, body: TargetsIn):
    try:
        return await servers.resolve_targets(db, body.servers, body.environment, body.os)
    except nodes.NodeError as e:
        raise _err(e) from e


class LogSearchIn(TargetsIn):
    pattern: str = Field(min_length=1, max_length=200)
    since: str = Field(default="24 hours ago", max_length=64)
    source: Literal["auto", "journal", "eventlog", "files"] = "auto"
    unit: str | None = Field(default=None, max_length=200)
    priority: str | None = Field(default=None, max_length=16)
    paths: list[str] | None = Field(default=None, max_length=20)
    lines: int = Field(default=100, ge=1, le=2000)
    log: str | None = Field(default=None, max_length=200)


@router.post("/api/servers/search-logs")
async def search_logs(body: LogSearchIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    targets = await _targets(db, body)
    try:
        res = await servers.search_logs(targets, body.pattern, since=body.since, source=body.source, unit=body.unit,
                                        priority=body.priority, paths=body.paths, lines=body.lines, log_name=body.log)
    except nodes.NodeError as e:
        raise _err(e) from e
    await audit(db, "servers.search_logs", user=admin, target=body.pattern, ip=client_ip(request),
                servers=len(targets), source=body.source, since=body.since)
    await db.commit()
    return res


class ErrorsIn(TargetsIn):
    since: str = Field(default="1 hour ago", max_length=64)


@router.post("/api/servers/errors")
async def find_errors(body: ErrorsIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    targets = await _targets(db, body)
    res = await servers.find_errors(targets, body.since)
    await audit(db, "servers.find_errors", user=admin, ip=client_ip(request), servers=len(targets), since=body.since)
    await db.commit()
    return res


class CveIn(TargetsIn):
    package: str = Field(min_length=1, max_length=100)
    fixed_version: str | None = Field(default=None, max_length=100)


@router.post("/api/servers/cve-check")
async def cve_check(body: CveIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    targets = await _targets(db, body)
    res = await servers.check_cve(targets, body.package, body.fixed_version or None)
    await audit(db, "servers.cve_check", user=admin, target=body.package, ip=client_ip(request),
                fixed_version=body.fixed_version, servers=len(targets))
    await db.commit()
    return res


@router.get("/api/servers/{node_id}/diagnose")
async def diagnose(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    try:
        n = await nodes.get_node(db, node_id)
        res = await servers.diagnose(n.id)
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e
    await audit(db, "server.diagnose", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    return res


# ------------------------------------------------------------------ data copies
class TransferIn(BaseModel):
    source: str
    paths: list[str] = Field(min_length=1, max_length=20)
    target: str
    target_path: str = Field(min_length=1, max_length=1000)
    overwrite: bool = False
    reason: str | None = Field(default=None, max_length=500)


@router.post("/api/transfers", status_code=201)
async def create_transfer(body: TransferIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    try:
        t = await servers.start_transfer(body.source, body.paths, body.target, body.target_path, overwrite=body.overwrite,
                                         reason=body.reason, origin="ui", requested_by=admin.username)
    except (nodes.NodeError, AgentError) as e:
        await audit(db, "transfer.start", user=admin, target=body.target, ip=client_ip(request), success=False,
                    error=str(e), paths=body.paths, target_path=body.target_path)
        await db.commit()
        raise _err(e) from e
    await audit(db, "transfer.start", user=admin, target=f"{t.source_name} → {t.target_name}", ip=client_ip(request),
                transfer_id=t.id, paths=t.source_paths, target_path=t.target_path, overwrite=t.overwrite)
    await db.commit()
    return servers.transfer_out(t)


@router.get("/api/transfers")
async def list_transfers(node_id: str | None = None, limit: int = Query(50, ge=1, le=500),
                         db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    q = select(Transfer).order_by(Transfer.started_at.desc()).limit(limit)
    if node_id:
        q = q.where(or_(Transfer.source_node_id == node_id, Transfer.target_node_id == node_id))
    return [servers.transfer_out(t) for t in (await db.execute(q)).scalars()]


@router.get("/api/transfers/{tid}")
async def get_transfer(tid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    t = await db.get(Transfer, tid)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return servers.transfer_out(t)
