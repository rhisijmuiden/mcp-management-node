"""Monitoring of all nodes: trends, storage and disk health, alerts and their settings."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.network import read
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import User
from mmn.services import monitoring, nodes
from mmn.services.audit import audit

router = APIRouter(tags=["monitoring"])
Range = Literal["1h", "24h", "7d", "30d"]


@router.get("/api/monitoring")
async def overview(range: Range = "24h", environment_id: str | None = None, db: AsyncSession = Depends(get_db),
                   _: User = Depends(current_user)):
    return await monitoring.overview(db, range, environment_id)


@router.get("/api/monitoring/nodes/{node_id}")
async def node_history(node_id: str, range: Range = "24h", db: AsyncSession = Depends(get_db),
                       _: User = Depends(current_user)):
    try:
        n = await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    h = await monitoring.history(db, [n.id], range)
    return {"node_id": n.id, "name": n.name, "range": range, "points": h.get(n.id, []), "health": n.health or {}}


@router.post("/api/monitoring/nodes/{node_id}/health")
async def refresh_health(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        n = await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    return await read(monitoring.collect_health(n.id))


@router.get("/api/monitoring/nodes/{node_id}/smart")
async def smart(node_id: str, disk: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        n = await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    return await read(monitoring.smart(n.id, disk))


@router.get("/api/monitoring/alerts")
async def alerts(open: bool = False, limit: int = 100, db: AsyncSession = Depends(get_db),
                 _: User = Depends(current_user)):
    return await monitoring.list_alerts(db, open_only=open, limit=limit)


@router.post("/api/monitoring/evaluate")
async def evaluate(_: User = Depends(require_admin)):
    return await monitoring.evaluate()


@router.get("/api/monitoring/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await monitoring.get_settings(db)


@router.put("/api/monitoring/settings")
async def put_settings(body: monitoring.MonitoringSettings, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    out = await monitoring.save_settings(db, body)
    await audit(db, "monitoring.settings", user=admin, ip=client_ip(request), **body.model_dump())
    await db.commit()
    return out
