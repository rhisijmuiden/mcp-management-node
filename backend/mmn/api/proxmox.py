"""Environments, nodes, pairing codes and node actions for the web interface."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.config import get_settings
from mmn.db import get_db
from mmn.models import Node, NodeJob, PairingCode, User
from mmn.services import agent_hub, agent_packages, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(tags=["proxmox"])


def _err(e: Exception) -> HTTPException:
    if isinstance(e, agent_hub.AgentOffline):
        return HTTPException(status.HTTP_409_CONFLICT, detail="node_offline")
    if isinstance(e, AgentError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e))
    if isinstance(e, nodes.NodeError):
        code = status.HTTP_404_NOT_FOUND if "not found" in str(e) else status.HTTP_400_BAD_REQUEST
        return HTTPException(code, detail=str(e))
    raise e


def _base(request: Request) -> str:
    bp = get_settings().base_path
    return f"{request.url.scheme}://{request.headers.get('host', 'localhost')}{'' if bp == '/' else bp}"


async def _node(db: AsyncSession, node_id: str) -> Node:
    try:
        return await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e


# ------------------------------------------------------------------ overview
@router.get("/api/proxmox/overview")
async def overview(environment_id: str | None = None, db: AsyncSession = Depends(get_db),
                   _: User = Depends(current_user), kind: str | None = "proxmox"):
    """Proxmox VE nodes only (standalone servers are in /api/servers/overview); kind=None for all."""
    envs = await nodes.list_environments(db)
    all_nodes = [n for n in await nodes.list_nodes(db, environment_id) if kind is None or n.kind == kind]
    out_nodes = [nodes.node_summary(n) for n in all_nodes]
    return {
        "environments": [nodes.env_out(e) for e in envs],
        "nodes": out_nodes,
        "totals": {
            "nodes": len(out_nodes), "online": sum(1 for n in out_nodes if n["online"]),
            "updates": sum(n["updates"]["count"] for n in out_nodes),
            "nodes_with_updates": sum(1 for n in out_nodes if n["updates"]["count"]),
            "reboot_required": sum(1 for n in out_nodes if n["updates"]["reboot_required"]),
            "agents_outdated": sum(1 for n in out_nodes if n["agent_update_available"]),
            "vms_running": sum((n["metrics"].get("guests") or {}).get("vms_running", 0) for n in out_nodes),
            "vms_total": sum((n["metrics"].get("guests") or {}).get("vms_total", 0) for n in out_nodes),
            "cts_running": sum((n["metrics"].get("guests") or {}).get("cts_running", 0) for n in out_nodes),
            "cts_total": sum((n["metrics"].get("guests") or {}).get("cts_total", 0) for n in out_nodes),
        },
        "agent_version": agent_packages.bundled_version(),
    }


# ------------------------------------------------------------------ environments
@router.get("/api/environments")
async def list_envs(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return [nodes.env_out(e) for e in await nodes.list_environments(db)]


@router.post("/api/environments", status_code=201)
async def create_env(body: nodes.EnvironmentIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    try:
        e = await nodes.create_environment(db, body)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    await audit(db, "environment.create", user=admin, target=e.name, ip=client_ip(request))
    await db.commit()
    return nodes.env_out(e, [])


@router.get("/api/environments/{env_id}")
async def get_env(env_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        e = await nodes.get_environment(db, env_id)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    return nodes.env_out(e)


@router.put("/api/environments/{env_id}")
async def update_env(env_id: str, body: nodes.EnvironmentIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    try:
        e = await nodes.update_environment(db, await nodes.get_environment(db, env_id), body)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    await audit(db, "environment.update", user=admin, target=e.name, ip=client_ip(request))
    await db.commit()
    return nodes.env_out(e)


@router.delete("/api/environments/{env_id}", status_code=204)
async def delete_env(env_id: str, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    try:
        e = await nodes.get_environment(db, env_id)
        name = e.name
        await nodes.delete_environment(db, e)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    await audit(db, "environment.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.get("/api/environments/{env_id}/notifications")
async def get_notify(env_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        e = await nodes.get_environment(db, env_id)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    return {**nodes.notify_public(e), "types_available": nodes.NOTIFY_TYPES, "severities_available": nodes.SEVERITIES}


@router.put("/api/environments/{env_id}/notifications")
async def put_notify(env_id: str, body: nodes.NotifyIn, request: Request, push: bool = True,
                     db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    try:
        e = await nodes.get_environment(db, env_id)
        out = await nodes.set_notify(db, e, body)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    await audit(db, "environment.notifications", user=admin, target=e.name, ip=client_ip(request),
                enabled=body.enabled)
    await db.commit()
    results = await nodes.push_notify_to_environment(e.id) if push else []
    return {**out, "push": results}


@router.post("/api/environments/{env_id}/notifications/push")
async def push_notify(env_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        e = await nodes.get_environment(db, env_id)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    results = await nodes.push_notify_to_environment(e.id)
    await audit(db, "environment.notifications.push", user=admin, target=e.name, ip=client_ip(request),
                ok=sum(1 for r in results if r["ok"]), failed=sum(1 for r in results if not r["ok"]))
    await db.commit()
    return results


# ------------------------------------------------------------------ pairing / agent settings
class PairingIn(BaseModel):
    environment_id: str
    role: Literal["server", "workstation"] | None = None
    label: str | None = Field(default=None, max_length=100)
    node_id: str | None = None
    hours: int = Field(default=24, ge=1, le=168)


@router.get("/api/agents/connect")
async def connect(request: Request, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await nodes.connect_info(db, request.headers.get("host"), _base(request))


class AgentSettingsIn(BaseModel):
    address: str = Field(default="", max_length=255, pattern=r"^[A-Za-z0-9.:\[\]-]*$")
    port: int = Field(default=8443, ge=1, le=65535)
    auto_update: bool = True
    updates_check_hours: int = Field(default=6, ge=1, le=168)
    transfer_max_gb: int = Field(default=50, ge=1, le=2000)


@router.put("/api/agents/settings")
async def put_agent_settings(body: AgentSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    from mmn.services import proxy_config, scheduler

    old = await get_setting(db, "agents")
    await set_setting(db, "agents", body.model_dump())
    queued = False
    if int(old.get("port") or 8443) != body.port:
        proxy = await get_setting(db, "proxy")
        cfg = proxy_config.ProxyIn(**{**{k: v for k, v in proxy.items() if k in proxy_config.ProxyIn.model_fields},
                                      "agent_port": body.port, "base_path": get_settings().base_path})
        try:
            queued = (await proxy_config.save_and_apply(db, cfg)).get("queued", False)
        except proxy_config.ProxyConfigError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    await audit(db, "agents.settings", user=admin, ip=client_ip(request), port=body.port, address=body.address)
    await db.commit()
    scheduler.reload_node_jobs(body.updates_check_hours)
    return {**await nodes.connect_info(db, request.headers.get("host"), _base(request)), "nginx_queued": queued}


@router.get("/api/pairing")
async def list_pairing(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    from datetime import UTC, datetime

    rows = (await db.execute(select(PairingCode).where(PairingCode.used_at.is_(None),
                                                       PairingCode.expires_at > datetime.now(UTC))
                             .order_by(PairingCode.created_at.desc()))).scalars().all()
    return [nodes.pairing_out(p) for p in rows]


@router.post("/api/pairing", status_code=201)
async def create_pairing(body: PairingIn, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    try:
        env = await nodes.get_environment(db, body.environment_id)
        if body.node_id:
            await nodes.get_node(db, body.node_id)
        pc, code = await nodes.create_pairing_code(db, env, label=body.label, node_id=body.node_id,
                                                   hours=body.hours, created_by=admin.username, role=body.role)
    except nodes.NodeError as ex:
        raise _err(ex) from ex
    await audit(db, "pairing.create", user=admin, target=env.name, ip=client_ip(request), label=body.label)
    await db.commit()
    info = await nodes.connect_info(db, request.headers.get("host"), _base(request))
    return {**nodes.pairing_out(pc), "code": code, "command": nodes.install_command(info["app_url"], code),
            **nodes.windows_commands(info["app_url"], info["endpoint"], info["fingerprint"], code),
            "manual": f"mmn-agent enroll --server {info['endpoint']} --fingerprint {info['fingerprint']} --code {code}",
            "connect": info}


@router.delete("/api/pairing/{pid}", status_code=204)
async def delete_pairing(pid: str, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    pc = await db.get(PairingCode, pid)
    if pc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await db.delete(pc)
    await audit(db, "pairing.delete", user=admin, target=pc.label or pid, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ nodes
@router.get("/api/nodes")
async def list_nodes(environment_id: str | None = None, kind: Literal["proxmox", "linux", "windows", "servers"] | None = None,
                     db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = await nodes.list_nodes(db, environment_id)
    if kind == "servers":
        rows = [n for n in rows if n.kind != "proxmox"]
    elif kind:
        rows = [n for n in rows if n.kind == kind]
    return [nodes.node_summary(n) for n in rows]


@router.get("/api/nodes/{node_id}")
async def get_node(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return nodes.node_out(await _node(db, node_id))


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._ -]+$")
    environment_id: str | None = None
    role: Literal["server", "workstation"] | None = None


@router.patch("/api/nodes/{node_id}")
async def patch_node(node_id: str, body: NodePatch, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    if body.name:
        n.name = body.name.strip()
    if body.environment_id and body.environment_id != n.environment_id:
        try:
            env = await nodes.get_environment(db, body.environment_id)
        except nodes.NodeError as ex:
            raise _err(ex) from ex
        n.environment_id, n.notify_hash = env.id, None
    if body.role and n.kind != "proxmox":
        n.role = body.role
    await audit(db, "node.update", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    await db.refresh(n)
    if body.environment_id:
        agent_hub.spawn(nodes.reconcile(n.id))
    return nodes.node_out(n)


class ConfirmIn(BaseModel):
    confirm: str = ""


@router.delete("/api/nodes/{node_id}", status_code=204)
async def delete_node(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    name = n.name
    await nodes.remove_node(db, n)
    await audit(db, "node.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


class GovernorIn(BaseModel):
    governor: str = Field(min_length=2, max_length=32)


@router.put("/api/nodes/{node_id}/governor")
async def put_governor(node_id: str, body: GovernorIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    try:
        res = await nodes.set_governor(n.id, body.governor)
    except (nodes.NodeError, AgentError) as e:
        await audit(db, "node.governor", user=admin, target=n.name, ip=client_ip(request), success=False,
                    governor=body.governor, error=str(e))
        await db.commit()
        raise _err(e) from e
    await audit(db, "node.governor", user=admin, target=n.name, ip=client_ip(request), governor=body.governor)
    await db.commit()
    return res


@router.post("/api/nodes/{node_id}/updates/refresh")
async def refresh_updates(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    n = await _node(db, node_id)
    try:
        return await nodes.refresh_updates(n.id)
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e


async def _job_action(db, request, admin, node_id: str, action: str, fn):
    n = await _node(db, node_id)
    try:
        job = await fn(n.id, source="ui", requested_by=admin.username)
    except (nodes.NodeError, AgentError) as e:
        await audit(db, action, user=admin, target=n.name, ip=client_ip(request), success=False, error=str(e))
        await db.commit()
        raise _err(e) from e
    await audit(db, action, user=admin, target=n.name, ip=client_ip(request), job_id=job.id)
    await db.commit()
    return nodes.job_out(job)


@router.post("/api/nodes/{node_id}/updates/apply", status_code=202)
async def apply_updates(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    return await _job_action(db, request, admin, node_id, "node.updates.apply", nodes.apply_updates)


@router.post("/api/nodes/{node_id}/agent/update", status_code=202)
async def update_agent(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    return await _job_action(db, request, admin, node_id, "node.agent.update", nodes.update_agent)


@router.post("/api/nodes/{node_id}/reboot")
async def reboot(node_id: str, body: ConfirmIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    if body.confirm != n.name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="confirm_mismatch")
    try:
        res = await nodes.reboot(n.id)
    except AgentError as e:
        raise _err(e) from e
    await audit(db, "node.reboot", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    return res


@router.post("/api/nodes/{node_id}/notifications/test")
async def test_notify(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    try:
        res = await nodes.test_notify(n.id)
    except AgentError as e:
        await audit(db, "node.notifications.test", user=admin, target=n.name, ip=client_ip(request),
                    success=False, error=str(e))
        await db.commit()
        return {"ok": False, "error": str(e)}
    await audit(db, "node.notifications.test", user=admin, target=n.name, ip=client_ip(request))
    await db.commit()
    return res


@router.post("/api/nodes/{node_id}/notifications/push")
async def push_node_notify(node_id: str, request: Request, db: AsyncSession = Depends(get_db),
                           admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    try:
        res = await nodes.push_notify_to_node(n.id)
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e
    await audit(db, "node.notifications.push", user=admin, target=n.name, ip=client_ip(request), ok=res["ok"])
    await db.commit()
    return res


@router.get("/api/nodes/{node_id}/jobs")
async def node_jobs(node_id: str, limit: int = Query(20, ge=1, le=200), db: AsyncSession = Depends(get_db),
                    _: User = Depends(current_user)):
    n = await _node(db, node_id)
    rows = (await db.execute(select(NodeJob).where(NodeJob.node_id == n.id).order_by(NodeJob.started_at.desc())
                             .limit(limit))).scalars().all()
    return [nodes.job_out(j, with_output=False) for j in rows]


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    j = await db.get(NodeJob, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return nodes.job_out(j)


# ------------------------------------------------------------------ logs, services, commands
class OpIn(BaseModel):
    action: Literal["logs.journal", "logs.events", "logs.search", "diagnose", "services.list", "services.status",
                    "services.action", "packages.query", "file.read", "exec"]
    params: dict = Field(default_factory=dict)


@router.post("/api/nodes/{node_id}/ops")
async def node_op(node_id: str, body: OpIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    n = await _node(db, node_id)
    detail = {k: v for k, v in body.params.items() if k in ("unit", "action", "pattern", "path", "since",
                                                               "priority", "grep", "command", "state", "log", "paths")}
    try:
        res = await nodes.node_call(n.id, body.action, body.params)
    except (nodes.NodeError, AgentError) as e:
        await audit(db, f"node.{body.action}", user=admin, target=n.name, ip=client_ip(request), success=False,
                    error=str(e), **detail)
        await db.commit()
        raise _err(e) from e
    extra = {}
    if isinstance(res, dict) and "exit_code" in res:
        extra["exit_code"] = res.get("exit_code")
        if body.action == "exec":
            extra["output"] = ((res.get("stdout") or "") + (res.get("stderr") or ""))[-2000:]
    await audit(db, f"node.{body.action}", user=admin, target=n.name, ip=client_ip(request), **detail, **extra)
    await db.commit()
    return res


@router.get("/api/packages/search")
async def package_search(pattern: str = Query(min_length=1, max_length=100), environment_id: str | None = None,
                         request: Request = None, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    res = await nodes.find_packages(pattern, environment_id)
    await audit(db, "packages.search", user=admin, target=pattern, ip=client_ip(request))
    await db.commit()
    return res
