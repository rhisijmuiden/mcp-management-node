"""Customers: the top layer. Everything (Proxmox VE nodes, servers, workstations, UniFi) hangs
under a customer. Stored in the environments table; /api/environments stays for compatibility."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, User
from mmn.services import customer_links, customers, nodes
from mmn.services.audit import audit

router = APIRouter(prefix="/api/customers", tags=["customers"])


async def _get(db: AsyncSession, cid: str) -> Environment:
    try:
        return await nodes.get_environment(db, cid)
    except nodes.NodeError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.get("")
async def list_customers(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await customers.summaries(db)


@router.post("", status_code=201)
async def create_customer(body: nodes.EnvironmentIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    try:
        e = await nodes.create_environment(db, body)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(ex)) from ex
    await audit(db, "customer.create", user=admin, target=e.name, ip=client_ip(request))
    await db.commit()
    return nodes.env_out(e, [])


@router.get("/{cid}")
async def get_customer(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return nodes.env_out(await _get(db, cid))


@router.put("/{cid}")
async def update_customer(cid: str, body: nodes.EnvironmentIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    e = await _get(db, cid)
    try:
        await nodes.update_environment(db, e, body)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(ex)) from ex
    await audit(db, "customer.update", user=admin, target=e.name, ip=client_ip(request))
    await db.commit()
    return nodes.env_out(e)


@router.delete("/{cid}", status_code=204)
async def delete_customer(cid: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    e = await _get(db, cid)
    name = e.name
    try:
        await nodes.delete_environment(db, e)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(ex)) from ex
    await audit(db, "customer.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.get("/{cid}/overview")
async def customer_overview(cid: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    return await customers.overview(db, await _get(db, cid), user_id=user.id)


@router.get("/{cid}/links")
async def customer_links_(cid: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    """What is linked to the customer and what is not set up yet (tiles on the customer overview)."""
    return await customer_links.links(db, await _get(db, cid), user_id=user.id)


@router.post("/{cid}/scan")
async def customer_scan(cid: str, request: Request, live: bool = True, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    e = await _get(db, cid)
    res = await customers.optimisation_scan(db, e, live=live)
    await audit(db, "customer.scan", user=admin, target=e.name, ip=client_ip(request), findings=len(res["findings"]))
    await db.commit()
    return res


@router.get("/{cid}/context")
async def customer_context(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """What the AI gets as the customer context (MCP resource stackpilot://customers/<id>)."""
    e = await _get(db, cid)
    return {"uri": f"stackpilot://customers/{e.id}", "markdown": customers.context_markdown(e)}
