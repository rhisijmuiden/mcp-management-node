"""Integrations overview (v0.26.0): the state of every connection in one call, for the Integrations page."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import current_user
from mmn.db import get_db
from mmn.models import (
    ApiToken,
    BackupTarget,
    Environment,
    M365Tenant,
    McpUpstream,
    Node,
    UnifiConnection,
    User,
)
from mmn.services import agent_hub, deploy
from mmn.services.settings_store import get_setting

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


async def _all(db: AsyncSession, model):
    return list((await db.execute(select(model))).scalars())


@router.get("")
async def integrations(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    customers = await db.execute(select(func.count()).select_from(Environment))
    n_customers = int(customers.scalar() or 0)
    nodes_ = await _all(db, Node)
    tenants = await _all(db, M365Tenant)
    conns = await _all(db, UnifiConnection)
    mcp = await _all(db, McpUpstream)
    tokens = [t for t in await _all(db, ApiToken) if not t.revoked]
    mail = await get_setting(db, "mail")
    targets = await _all(db, BackupTarget)
    return {
        "customers": n_customers,
        "github": await deploy.github_status(db),
        "m365": {"tenants": len(tenants), "customers": len({t.customer_id for t in tenants}),
                 "errors": sum(1 for t in tenants if t.status == "error")},
        "unifi": {"connections": len(conns), "customers": len({c.environment_id for c in conns}),
                  "errors": sum(1 for c in conns if c.status == "error")},
        "mcp": {"servers": len(mcp), "enabled": sum(1 for m in mcp if m.enabled),
                "errors": sum(1 for m in mcp if m.enabled and m.status == "error")},
        "ai": {"tokens": len(tokens), "oauth": sum(1 for t in tokens if t.kind == "oauth")},
        "agents": {"machines": len(nodes_), "online": sum(1 for n in nodes_ if agent_hub.is_online(n.id))},
        "mail": {"enabled": bool(mail.get("enabled")), "host": mail.get("host") or None},
        "app_backups": {"targets": len(targets)},
    }
