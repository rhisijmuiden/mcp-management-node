"""Workstations (phase 10): inventory, security status and remote actions per PC or laptop."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import User
from mmn.services import nodes, workstations
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit

router = APIRouter(prefix="/api/workstations", tags=["workstations"])


@router.get("")
async def list_workstations(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                            _: User = Depends(current_user)):
    return await workstations.list_workstations(db, customer_id)


@router.get("/software")
async def find_software(q: str, customer_id: str | None = None, servers: bool = False,
                        db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        return await workstations.find_software(db, q, customer_id, include_servers=servers)
    except nodes.NodeError as e:
        raise _err(e) from e


@router.get("/{wid}")
async def get_workstation(wid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        return workstations.detail(await workstations.get_workstation(db, wid))
    except nodes.NodeError as e:
        raise _err(e) from e


@router.post("/{wid}/inventory")
async def refresh_inventory(wid: str, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    try:
        n = await workstations.get_workstation(db, wid)
        out = await workstations.collect(n.id)
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e
    await audit(db, "workstation.inventory", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    return out


class ActionIn(BaseModel):
    action: Literal["defender_quick_scan", "defender_full_scan", "defender_update", "gpupdate", "message", "logoff"]
    message: str | None = Field(default=None, max_length=500)
    user: str | None = Field(default=None, max_length=64)


@router.post("/{wid}/actions")
async def run_action(wid: str, body: ActionIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    n = None
    try:
        n = await workstations.get_workstation(db, wid)
        if body.action == "logoff":
            res = await workstations.log_off(n, body.user or "")
        else:
            res = await workstations.run_action(n, body.action, message=body.message)
    except (nodes.NodeError, AgentError) as e:
        if n is not None:
            await audit(db, f"workstation.{body.action}", user=admin, target=n.name, ip=client_ip(request),
                        success=False, error=str(e)[:300])
            await db.commit()
        raise _err(e) from e
    await audit(db, f"workstation.{body.action}", user=admin, target=n.name, ip=client_ip(request),
                success=bool(res.get("ok")), user_name=body.user, message=body.message)
    await db.commit()
    return res
