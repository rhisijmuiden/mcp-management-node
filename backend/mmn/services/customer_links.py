"""What is linked to a customer, at a glance (v0.26.0).

One call for the tiles on the customer overview: machines, network, Microsoft 365, MCP servers, deployments,
backups, tickets, AI access, documentation, the portal, uptime checks and certificates, plus a list of what
is not set up yet. Only counts and states that are already stored: nothing here contacts a machine.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import (
    BackupTarget,
    Certificate,
    Deployment,
    Document,
    Environment,
    M365Tenant,
    McpUpstream,
    Node,
    PortalUser,
    Repo,
    RepoAccess,
    Ticket,
    UnifiConnection,
    UptimeCheck,
    VaultItem,
)
from mmn.services import agent_hub, protection
from mmn.services.customers import ai_access, machine_type
from mmn.services.settings_store import get_setting
from mmn.services.tickets import OPEN_STATUSES


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def links(db: AsyncSession, e: Environment, *, user_id: str | None = None) -> dict[str, Any]:
    ns: list[Node] = list(e.nodes)
    node_ids = [n.id for n in ns]

    def machines(t: str) -> dict[str, int]:
        rows = [n for n in ns if machine_type(n) == t]
        return {"total": len(rows), "online": sum(1 for n in rows if agent_hub.is_online(n.id))}

    conns = list((await db.execute(select(UnifiConnection).where(UnifiConnection.environment_id == e.id))).scalars())
    tenants = list((await db.execute(select(M365Tenant).where(M365Tenant.customer_id == e.id))).scalars())
    mcp = list((await db.execute(select(McpUpstream).where(McpUpstream.environment_id == e.id))).scalars())
    shared_mcp = await _count(db, select(func.count()).select_from(McpUpstream)
                              .where(McpUpstream.environment_id.is_(None), McpUpstream.enabled.is_(True)))
    deps = list((await db.execute(select(Deployment).where(Deployment.node_id.in_(node_ids)))).scalars()) if node_ids else []
    open_tickets = await _count(db, select(func.count()).select_from(Ticket)
                                .where(Ticket.customer_id == e.id, Ticket.status.in_(OPEN_STATUSES)))
    docs = await _count(db, select(func.count()).select_from(Document).where(Document.customer_id == e.id))
    vault = await _count(db, select(func.count()).select_from(VaultItem).where(VaultItem.customer_id == e.id))
    portal_users = list((await db.execute(select(PortalUser).where(PortalUser.customer_id == e.id))).scalars())
    portal_cfg = await get_setting(db, "portal")
    uptime = await _count(db, select(func.count()).select_from(UptimeCheck).where(UptimeCheck.customer_id == e.id))
    certs = await _count(db, select(func.count()).select_from(Certificate).where(Certificate.customer_id == e.id))
    tokens = await ai_access(db, e.id, user_id)
    cov = protection.view(e, await protection.stored(db, e))["summary"]
    linked = set((await db.execute(select(RepoAccess.repo_id).where(RepoAccess.customer_id == e.id))).scalars())
    owned = set((await db.execute(select(Repo.id).where(Repo.owner_customer_id == e.id))).scalars())
    app_backup_targets = await _count(db, select(func.count()).select_from(BackupTarget))

    px, srv, ws = machines("proxmox"), machines("server"), machines("workstation")
    offline = sum(m["total"] - m["online"] for m in (px, srv, ws) if m)
    tiles = {
        "proxmox": px,
        "servers": srv,
        "workstations": ws,
        "network": {"connections": len(conns), "errors": sum(1 for c in conns if c.status == "error"),
                    "devices": sum(((c.snapshot or {}).get("totals") or {}).get("devices") or 0 for c in conns)},
        "m365": {"tenants": len(tenants), "errors": sum(1 for t in tenants if t.status == "error"),
                 "names": [t.name for t in tenants]},
        "mcp": {"servers": len(mcp), "enabled": sum(1 for m in mcp if m.enabled), "shared": shared_mcp,
                "errors": sum(1 for m in mcp if m.enabled and m.status == "error")},
        "deployments": {"total": len(deps), "failed": sum(1 for d in deps if d.last_status == "failed"),
                        "repos": sorted({d.repo for d in deps})},
        "software": {"repos": len(linked | owned), "own": len(owned)},
        "backups": {"considered": cov["considered"], "protected": cov["protected"], "problems": cov["problems"],
                    "last_backup": cov["last_backup"]},
        "tickets": {"open": open_tickets},
        "ai": {"tokens": len(tokens), "customer_only": sum(1 for t in tokens if not t["all_customers"])},
        "docs": {"documents": docs, "vault": vault},
        "portal": {"enabled": bool(portal_cfg.get("enabled", True)),
                   "users": sum(1 for u in portal_users if u.is_active),
                   "active": sum(1 for u in portal_users if u.is_active and u.password_hash)},
        "uptime": {"checks": uptime},
        "certificates": {"total": certs},
    }

    # What is not set up yet, most important first. "key" is the text key in the app, "to" where to fix it.
    todo: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []

    def item(ok: bool, key: str, to: str, **vals: Any) -> None:
        (done if ok else todo).append({"key": key, "to": to, "values": vals})

    has_machines = bool(ns)
    item(has_machines or bool(conns), "machines", "/admin/agents")
    if has_machines:
        item(offline == 0, "offline", "/servers", count=offline)
    if cov["considered"]:
        item(cov["problems"] == 0, "backups", "/backups", count=cov["problems"])
    elif has_machines:
        item(False, "backups_unknown", "/backups")
    item(bool(e.maintenance_window), "maintenance_window", f"/customers/{e.id}?tab=contacts")
    item(bool(e.sla), "sla", f"/customers/{e.id}?tab=contacts")
    item(bool(e.contacts or e.contact_name or e.contact_email), "contacts", f"/customers/{e.id}?tab=contacts")
    item(bool(e.notes), "notes", f"/customers/{e.id}?tab=contacts")
    item(uptime > 0, "uptime", "/uptime")
    item(bool(portal_cfg.get("enabled", True)) and tiles["portal"]["users"] > 0, "portal", "/tickets/portal")
    item(app_backup_targets > 0, "app_backup", "/admin/backups")
    return {"customer_id": e.id, "tiles": tiles, "todo": todo, "done": done}
