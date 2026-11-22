"""Network, firewall and SDN of Proxmox VE nodes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import Node, User
from mmn.services import agent_hub, network, nodes, sdn
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit

router = APIRouter(tags=["network"])


async def pve_node(db: AsyncSession, node_id: str) -> Node:
    try:
        n = await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    if "pve" not in (n.capabilities or []):
        raise _err(nodes.NodeError("this is not a Proxmox VE node"))
    if not agent_hub.is_online(n.id):
        raise _err(agent_hub.AgentOffline())
    return n


async def run(db: AsyncSession, request: Request, user: User, action: str, target: str, coro, **detail):
    """Await a change, write it to the audit log (also when it fails) and turn errors into HTTP."""
    try:
        res = await coro
    except (nodes.NodeError, AgentError) as e:
        await audit(db, action, user=user, target=target, ip=client_ip(request), success=False, error=str(e),
                    **detail)
        await db.commit()
        raise _err(e) from e
    await audit(db, action, user=user, target=target, ip=client_ip(request), **detail)
    await db.commit()
    return res


async def read(coro):
    try:
        return await coro
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e


# ------------------------------------------------------------------ interfaces and bridges
@router.get("/api/network/map")
async def bridge_map(environment_id: str | None = None, node_id: str | None = None,
                     db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await read(network.bridge_map(db, environment_id=environment_id, node_id=node_id))


@router.get("/api/network/{node_id}/interfaces")
async def interfaces(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(network.interfaces(n.id, fresh=True))


@router.get("/api/network/{node_id}/pending")
async def pending(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await read(network.pending(n.id))


@router.post("/api/network/{node_id}/bridges", status_code=201)
async def create_bridge(node_id: str, body: network.BridgeIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "network.bridge_stage", f"{n.name}/{body.iface}",
                     network.stage_bridge(n.id, body, create=True), node=n.name, change="create")


@router.put("/api/network/{node_id}/bridges/{iface}")
async def update_bridge(node_id: str, iface: str, body: network.BridgeIn, request: Request,
                        db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    body.iface = iface
    return await run(db, request, admin, "network.bridge_stage", f"{n.name}/{iface}",
                     network.stage_bridge(n.id, body, create=False), node=n.name, change="update")


@router.delete("/api/network/{node_id}/bridges/{iface}")
async def delete_bridge(node_id: str, iface: str, request: Request, force: bool = False,
                        db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "network.bridge_stage", f"{n.name}/{iface}",
                     network.stage_delete(n.id, iface, force=force), node=n.name, change="delete")


@router.post("/api/network/{node_id}/revert")
async def revert(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "network.revert", n.name, network.revert_pending(n.id))


class ApplyIn(BaseModel):
    dry_run: bool = True
    confirm: str | None = None  # the node name, needed when dry_run is false


@router.post("/api/network/{node_id}/apply")
async def apply(node_id: str, body: ApplyIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    if not body.dry_run and body.confirm != n.name:
        raise _err(nodes.NodeError(f"type the node name ({n.name}) to apply"))
    if body.dry_run:
        return await read(network.apply(n.id, dry_run=True))
    return await run(db, request, admin, "network.apply", n.name,
                     network.apply(n.id, dry_run=False, requested_by=admin.username))


@router.get("/api/network/backups")
async def backups(kind: Literal["network", "sdn"] | None = None, node_id: str | None = None,
                  db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await network.list_backups(db, kind=kind, node_id=node_id)


@router.get("/api/network/backups/{backup_id}")
async def backup_detail(backup_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    from mmn.models import ConfigBackup

    b = await db.get(ConfigBackup, backup_id)
    if b is None:
        raise _err(nodes.NodeError("backup not found"))
    return network.backup_out(b, with_files=True)


class RestoreIn(BaseModel):
    confirm: str


@router.post("/api/network/backups/{backup_id}/restore")
async def restore_backup(backup_id: str, body: RestoreIn, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    from mmn.models import ConfigBackup

    b = await db.get(ConfigBackup, backup_id)
    if b is None:
        raise _err(nodes.NodeError("backup not found"))
    n = await pve_node(db, b.node_id)
    if body.confirm != n.name:
        raise _err(nodes.NodeError(f"type the node name ({n.name}) to restore"))
    if b.kind == "sdn":
        return await run(db, request, admin, "sdn.rollback", n.name,
                         sdn.rollback(db, b.id, requested_by=admin.username), backup_id=b.id)
    return await run(db, request, admin, "network.restore", n.name,
                     network.restore_network_backup(b.id, requested_by=admin.username), backup_id=b.id)


# ------------------------------------------------------------------ firewall
@router.get("/api/firewall/{node_id}")
async def firewall(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await read(network.firewall_overview(n.id))


@router.get("/api/firewall/{node_id}/guest/{gtype}/{vmid}")
async def guest_firewall(node_id: str, gtype: Literal["qemu", "lxc"], vmid: int, db: AsyncSession = Depends(get_db),
                         _: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await read(network.guest_firewall(n.id, gtype, vmid))


@router.get("/api/firewall/{node_id}/group/{group}")
async def group_rules(node_id: str, group: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await read(network.group_rules(n.id, group))


class FwTarget(BaseModel):
    level: Literal["cluster", "node", "guest", "group"]
    type: Literal["qemu", "lxc"] | None = None
    vmid: int | None = None
    group: str | None = None


class FwOptionsIn(FwTarget):
    enable: bool | None = None
    policy_in: str | None = None
    policy_out: str | None = None
    confirm: str | None = None


def _fw_target(t: FwTarget) -> str:
    return {"cluster": "cluster", "node": "node", "guest": f"guest {t.vmid}", "group": f"group {t.group}"}[t.level]


@router.put("/api/firewall/{node_id}/options")
async def firewall_options(node_id: str, body: FwOptionsIn, request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    if body.level == "cluster" and body.enable is not None and body.confirm != n.cluster_name and body.confirm != n.name:
        raise _err(nodes.NodeError("type the cluster (or node) name to switch the cluster firewall"))
    return await run(db, request, admin, "firewall.options", f"{n.name} {_fw_target(body)}",
                     network.set_firewall_options(n.id, body.level, enable=body.enable, policy_in=body.policy_in,
                                                  policy_out=body.policy_out, gtype=body.type, vmid=body.vmid),
                     enable=body.enable, policy_in=body.policy_in, policy_out=body.policy_out)


class RuleAddIn(FwTarget):
    rule: network.RuleIn


@router.post("/api/firewall/{node_id}/rules", status_code=201)
async def add_rule(node_id: str, body: RuleAddIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "firewall.rule_add", f"{n.name} {_fw_target(body)}",
                     network.add_rule(n.id, body.level, body.rule, gtype=body.type, vmid=body.vmid, group=body.group),
                     rule=body.rule.model_dump(exclude_none=True))


class RuleDeleteIn(FwTarget):
    pos: int = Field(ge=0, le=10000)


@router.post("/api/firewall/{node_id}/rules/delete")
async def delete_rule(node_id: str, body: RuleDeleteIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "firewall.rule_delete", f"{n.name} {_fw_target(body)}",
                     network.delete_rule(n.id, body.level, body.pos, gtype=body.type, vmid=body.vmid, group=body.group),
                     pos=body.pos)


# ------------------------------------------------------------------ SDN
@router.get("/api/sdn/{node_id}/state")
async def sdn_state(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(sdn.state(n.id))


@router.get("/api/sdn/designs")
async def designs(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await sdn.list_designs(db)


@router.post("/api/sdn/designs", status_code=201)
async def create_design(body: sdn.DesignIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    d = await run(db, request, admin, "sdn.design_save", body.name, sdn.save_design(db, body, user=admin.username))
    return sdn.design_out(d)


class ImportIn(BaseModel):
    node_id: str
    zone: str
    name: str | None = None


@router.post("/api/sdn/designs/import", status_code=201)
async def import_zone(body: ImportIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    n = await pve_node(db, body.node_id)
    st = await read(sdn.state(n.id))
    try:
        spec = sdn.spec_from_state(st, body.zone)
    except nodes.NodeError as e:
        raise _err(e) from e
    d = await run(db, request, admin, "sdn.design_save", body.zone,
                  sdn.save_design(db, sdn.DesignIn(name=body.name or f"Zone {body.zone}", node_id=n.id, spec=spec),
                                  user=admin.username))
    return sdn.design_out(d)


@router.get("/api/sdn/designs/{design_id}")
async def get_design(design_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    d = await read(sdn.get_design(db, design_id))
    return sdn.design_out(d)


@router.put("/api/sdn/designs/{design_id}")
async def update_design(design_id: str, body: sdn.DesignIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    d = await read(sdn.get_design(db, design_id))
    d = await run(db, request, admin, "sdn.design_save", body.name, sdn.save_design(db, body, d))
    return sdn.design_out(d)


@router.delete("/api/sdn/designs/{design_id}", status_code=204)
async def delete_design(design_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    d = await read(sdn.get_design(db, design_id))
    await db.delete(d)
    await audit(db, "sdn.design_delete", user=admin, target=d.name, ip=client_ip(request))
    await db.commit()


class PlanIn(BaseModel):
    node_id: str
    spec: sdn.DesignSpec


@router.post("/api/sdn/plan")
async def plan(body: PlanIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    n = await pve_node(db, body.node_id)
    return await read(sdn.plan(db, n.id, body.spec))


class DesignApplyIn(BaseModel):
    dry_run: bool = True
    confirm: str | None = None  # the zone id


@router.post("/api/sdn/designs/{design_id}/apply")
async def apply_design(design_id: str, body: DesignApplyIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    d = await read(sdn.get_design(db, design_id))
    await pve_node(db, d.node_id)
    zone = (d.spec or {}).get("zone", {}).get("id")
    if body.dry_run:
        return await read(sdn.apply(db, d, dry_run=True))
    if body.confirm != zone:
        raise _err(nodes.NodeError(f"type the zone name ({zone}) to apply"))
    return await run(db, request, admin, "sdn.apply", zone,
                     sdn.apply(db, d, dry_run=False, requested_by=admin.username), design=d.name)
