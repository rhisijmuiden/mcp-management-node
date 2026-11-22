"""Built-in MCP server, so Claude (or another MCP client) can work with this app.

Endpoint: ``<base url>/mcp`` (Streamable HTTP). Authenticate with a personal API token:
``Authorization: Bearer mmn_...``. Every tool checks the token's scopes, and every call is
written to the audit log with source ``mcp``. Destructive tools (restore, install update)
require an explicit ``confirm`` argument that repeats the target.

Later phases register their own tools here (Proxmox nodes, VMs, containers, servers).
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp, Receive, Scope, Send

from mmn.db import sessionmaker
from mmn.version import get_build_info


@dataclass
class Principal:
    user_id: str
    username: str
    token_id: str
    token_name: str
    token_kind: str
    scopes: set[str]
    ip: str | None
    client: dict[str, Any]


_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("mmn_mcp_principal", default=None)

SECRET_ARGS = {"password", "passphrase", "token", "private_key", "secret", "ssh_key"}


class ToolDenied(ToolError):
    """A refusal or failure whose message is shown to the AI client."""


def _check(scope: str) -> Principal:
    p = _principal.get()
    if p is None:
        raise ToolDenied("not authenticated")
    if scope not in p.scopes:
        raise ToolDenied(f"this token lacks the '{scope}' scope")
    return p


def current_principal() -> Principal | None:
    return _principal.get()


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in args.items():
        if any(s in k.lower() for s in SECRET_ARGS):
            out[k] = "***"
        elif isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + "…"
        else:
            out[k] = v
    return out


def _summary(result: Any) -> Any:
    if isinstance(result, list):
        return {"items": len(result)}
    if isinstance(result, dict):
        keys = ("ok", "status", "state", "channel", "restored", "update_available", "error", "tag", "id", "exit_code")
        picked = {k: result[k] for k in keys if k in result and not isinstance(result[k], (dict, list))}
        return picked or {"keys": sorted(result)[:10]}
    return {"type": type(result).__name__}


async def _audit(p: Principal | None, tool_name: str, args: dict[str, Any], success: bool, *,
                 result: Any = None, error: str | None = None, ms: int = 0) -> None:
    from mmn.models import AuditLog

    detail = {"tool": tool_name, "args": _redact(args), "duration_ms": ms}
    if p is not None:
        detail.update(token=p.token_name, token_id=p.token_id, token_kind=p.token_kind, client=p.client)
    if success:
        detail["result"] = _summary(result)
    else:
        detail["error"] = (error or "")[:1000]
    async with sessionmaker()() as db:
        db.add(AuditLog(action=f"mcp.{tool_name}", user_id=p.user_id if p else None,
                        username=p.username if p else None, source="mcp", target=_target_of(args),
                        ip=p.ip if p else None, success=success, detail=_jsonable(detail)))
        await db.commit()


def _target_of(args: dict[str, Any]) -> str | None:
    for k in ("deployment", "design_id", "guest", "node", "environment", "slug", "template", "task_id", "run_id", "name", "backup_id",
              "target_id", "tag", "channel", "to", "pattern"):
        if args.get(k):
            return str(args[k])[:255]
    return None


def tool(scope: str, annotations: ToolAnnotations):
    """Register an MCP tool that checks ``scope`` and writes every call to the audit log."""

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            started = time.monotonic()
            bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
            p = _principal.get()
            try:
                _check(scope)
                result = await fn(*args, **kwargs)
            except Exception as e:
                msg = str(e) or type(e).__name__
                await _audit(p, fn.__name__, bound, False, error=msg, ms=int((time.monotonic() - started) * 1000))
                if isinstance(e, ToolError):
                    raise
                raise ToolDenied(f"{type(e).__name__}: {msg}") from e
            await _audit(p, fn.__name__, bound, True, result=result, ms=int((time.monotonic() - started) * 1000))
            return result

        mcp.tool(annotations=annotations)(wrapper)
        return fn

    return deco


INSTRUCTIONS = """MCP Management Node manages Proxmox VE environments and servers through an agent on
every machine, so you can do day-to-day server administration without a separate remote session.

Environments group nodes (Home, one per customer). Start with `get_overview` or `list_nodes`.
Per node you can read status and metrics, pending package updates, logs (`read_logs`, systemd
journal), service status (`list_services`, `get_service_status`), installed packages
(`find_packages` across all nodes, e.g. to check a CVE), files (`read_file`), and run commands
(`run_command`). You can also install updates, restart services, set the CPU governor, push the
notification mail settings to the nodes and update the agent.

VMs and containers ("guests"): `list_guests`, `get_guest`, power actions, snapshots (scheduled
tasks take one before updates and roll back on failure), migration inside a cluster, creating
containers from the template library (`list_templates`) and VMs from an ISO or a cloud image, and
apps from community-scripts.org (`list_community_scripts`, review with `get_community_script`,
then `install_community_script`). `suggest_node` picks the least loaded node. Scheduled tasks:
`list_scheduled_tasks`, `save_scheduled_task`, `run_scheduled_task`, `get_task_run`.

Network: `get_bridge_map` (which guests are on which bridge/VLAN), `get_node_network`, firewall
(`get_firewall`, `set_firewall`, `add_firewall_rule`), SDN (`get_sdn`, `save_sdn_design`,
`apply_sdn_design`). Network and SDN changes are staged and shown as a diff first; applying needs
dry_run=false plus `confirm`, saves a backup and rolls back automatically on failure
(`list_config_backups`, `restore_config_backup`). Cluster: `get_ha_status`, `set_ha_resource`,
replication, Proxmox backup jobs and storage. Monitoring: `get_monitoring`, `get_node_metrics`,
`list_alerts`, `get_disk_smart`. GitHub deployments into containers: `list_deployments`,
`list_github_repos`, `save_deployment`, `run_deployment`.

Nodes can be named by id or by name. Every call is written to the audit log with your token's name.
Destructive tools need `confirm` set to the exact node name, or are marked destructive: ask the user
before calling them, and give a short `reason` for commands and service actions."""

mcp = MCPServer(name="MCP Management Node", instructions=INSTRUCTIONS, version=get_build_info().version)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


# ------------------------------------------------------------------ system
@tool("read", RO)
async def get_system_info() -> dict:
    """Version, update channel (production/test), database and OS information of this app."""
    from mmn.api.system import system_info

    async with sessionmaker()() as db:
        info = await system_info(db)
    return _jsonable(info)


@tool("read", RO)
async def check_for_updates() -> dict:
    """Ask GitHub whether a newer release exists on the current channel."""
    from mmn.services.updates import check_for_updates as check

    async with sessionmaker()() as db:
        res = await check(db)
        await db.commit()
    return _jsonable(res)


@tool("updates", WRITE)
async def set_update_channel(channel: str) -> dict:
    """Switch between the 'production' (main branch) and 'test' (test branch) update channel.
    This does not install anything; use install_update afterwards."""
    from mmn.services.updates import UpdateError, set_channel

    async with sessionmaker()() as db:
        try:
            await set_channel(db, channel)
        except UpdateError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
    return {"channel": channel}


@tool("updates", DESTRUCTIVE)
async def install_update(tag: str, confirm: str) -> dict:
    """Install release `tag` (for example v0.2.0 or v0.2.0-test.5). The app restarts.
    Set `confirm` to the same tag to confirm. A database dump is made first and the update is
    rolled back automatically if the new version does not start."""
    if confirm != tag:
        raise ToolDenied("confirm must equal the tag")
    from mmn.services.settings_store import get_setting
    from mmn.services.updates import UpdateError, read_status, request_update

    async with sessionmaker()() as db:
        channel = (await get_setting(db, "updates"))["channel"]
    try:
        p = current_principal()
        request_update(tag, channel, f"{p.username if p else '?'} (mcp)")
    except UpdateError as e:
        raise ToolDenied(str(e)) from e
    return read_status()


@tool("read", RO)
async def get_update_status() -> dict:
    """Progress of a running or the last update."""
    from mmn.services.updates import read_status

    return read_status()


# ------------------------------------------------------------------ backups
@tool("read", RO)
async def list_backup_targets() -> list[dict]:
    """Remote backup locations (FTP, FTPS, SFTP, SCP) with their schedules. Secrets are never returned."""
    from mmn.services.backup.targets import list_targets, target_out

    async with sessionmaker()() as db:
        out = [target_out(t) for t in await list_targets(db)]
    return _jsonable(out)


@tool("read", RO)
async def list_backup_runs(limit: int = 20) -> list[dict]:
    """Recent backup and restore runs with status, size and errors."""
    from mmn.services.backup.targets import list_runs

    async with sessionmaker()() as db:
        runs = await list_runs(db, max(1, min(limit, 200)))
    for r in runs:
        r.pop("log", None)
    return _jsonable(runs)


@tool("read", RO)
async def list_remote_backups(target_id: str) -> list[dict]:
    """Backups that exist on a remote location (the source for restores)."""
    from mmn.models import BackupTarget
    from mmn.services.backup.engine import list_remote_backups as lst

    async with sessionmaker()() as db:
        t = await db.get(BackupTarget, target_id)
        if t is None:
            raise ToolDenied("target not found")
    res = await lst(t)
    return _jsonable(res)


@tool("backups", WRITE)
async def run_backup(target_id: str, kind: str = "auto") -> dict:
    """Start a backup now. kind: 'full', 'incremental' or 'auto' (incremental when a full exists).
    Returns when the backup has finished."""
    if kind not in ("auto", "full", "incremental"):
        raise ToolDenied("kind must be auto, full or incremental")
    from mmn.models import BackupRun
    from mmn.services.backup.engine import BackupError
    from mmn.services.backup.engine import run_backup as run
    from mmn.services.backup.targets import run_out

    try:
        run_id = await run(target_id, kind, trigger="mcp")
    except BackupError as e:
        raise ToolDenied(str(e)) from e
    async with sessionmaker()() as db:
        r = await db.get(BackupRun, run_id)
        out = run_out(r)
    return _jsonable(out)


@tool("restore", DESTRUCTIVE)
async def restore_backup(target_id: str, backup_id: str, confirm: str) -> dict:
    """Replace ALL current app data with backup `backup_id` from `target_id`. Set `confirm` to the
    backup_id to confirm. Always ask the user first."""
    if confirm != backup_id:
        raise ToolDenied("confirm must equal the backup_id")
    from mmn.services.backup.engine import BackupError
    from mmn.services.backup.engine import restore_backup as restore

    try:
        p = current_principal()
        res = await restore(target_id, backup_id, user_id=p.user_id if p else None,
                            username=p.username if p else None, source="mcp")
    except BackupError as e:
        raise ToolDenied(str(e)) from e
    from mmn.services.scheduler import reload_backup_jobs

    await reload_backup_jobs()
    return res


# ------------------------------------------------------------------ mail
@tool("read", RO)
async def get_mail_settings() -> dict:
    """The app's own SMTP server settings (passwords and keys are never returned)."""
    from mmn.services.mail_settings import get_mail_settings as get

    async with sessionmaker()() as db:
        out = await get(db)
    return out


@tool("settings", WRITE)
async def update_mail_settings(host: str | None = None, port: int | None = None, security: str | None = None,
                               username: str | None = None, from_address: str | None = None,
                               from_name: str | None = None, enabled: bool | None = None) -> dict:
    """Change SMTP settings. security: 'starttls', 'tls' (SMTPS) or 'none'.
    Passwords can only be set in the web interface."""
    from mmn.services.mail_settings import MailSettingsIn
    from mmn.services.mail_settings import update_mail_settings as upd

    body = MailSettingsIn(**{k: v for k, v in dict(host=host, port=port, security=security, username=username,
                                                    from_address=from_address, from_name=from_name,
                                                    enabled=enabled).items() if v is not None})
    async with sessionmaker()() as db:
        out = await upd(db, body)
        await db.commit()
    return out


@tool("settings", WRITE)
async def send_test_mail(to: str) -> dict:
    """Send a test email with the saved SMTP settings."""
    from mmn.services.mail_settings import send_test_mail as send

    async with sessionmaker()() as db:
        try:
            await send(db, to, "en")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


# ------------------------------------------------------------------ audit
@tool("read", RO)
async def get_audit_log(limit: int = 50) -> list[dict]:
    """Recent actions in the app (who did what, from the UI or via MCP)."""
    from sqlalchemy import select

    from mmn.models import AuditLog

    async with sessionmaker()() as db:
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500))))
                ).scalars().all()
    return _jsonable([{"at": r.at, "user": r.username, "source": r.source, "action": r.action,
                       "target": r.target, "success": r.success} for r in rows])


# ------------------------------------------------------------------ environments and nodes
async def _node_id(node: str) -> tuple[str, str]:
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            n = await nodes.get_node(db, node)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return n.id, n.name


def _agent_errors(e: Exception) -> ToolDenied:
    return ToolDenied(str(e) or type(e).__name__)


@tool("read", RO)
async def get_overview(environment: str | None = None) -> dict:
    """Environments, nodes (online, CPU/RAM/disk, pending updates, agent version) and totals.
    `environment` (name or id) limits the nodes to one environment."""
    from mmn.api.proxmox import overview
    from mmn.services import nodes

    async with sessionmaker()() as db:
        env_id = None
        if environment:
            try:
                env_id = (await nodes.get_environment(db, environment)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        out = await overview(env_id, db, None)
    return _jsonable(out)


@tool("read", RO)
async def list_environments() -> list[dict]:
    """Environments (Home, customers) with node counts and pending updates."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        return _jsonable([nodes.env_out(e) for e in await nodes.list_environments(db)])


@tool("nodes", WRITE)
async def create_environment(name: str, kind: str = "customer", description: str | None = None) -> dict:
    """Create an environment. kind: 'home' or 'customer'."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.create_environment(db, nodes.EnvironmentIn(name=name, kind=kind, description=description))
        except (nodes.NodeError, ValueError) as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        return _jsonable(nodes.env_out(e, []))


@tool("read", RO)
async def list_nodes(environment: str | None = None) -> list[dict]:
    """Nodes with status, metrics, cluster, agent version and number of pending updates."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        env_id = (await nodes.get_environment(db, environment)).id if environment else None
        return _jsonable([nodes.node_summary(n) for n in await nodes.list_nodes(db, env_id)])


@tool("read", RO)
async def get_node(node: str) -> dict:
    """Full details of one node, including the list of pending package updates."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            return _jsonable(nodes.node_out(await nodes.get_node(db, node)))
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("nodes", WRITE)
async def create_pairing_code(environment: str, label: str | None = None) -> dict:
    """Create a one-time pairing code (valid 24 hours) and the install command to run on a new node."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            env = await nodes.get_environment(db, environment)
            p = current_principal()
            pc, code = await nodes.create_pairing_code(db, env, label=label, created_by=p.username if p else None)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        info = await nodes.connect_info(db)
    return _jsonable({"code": code, "expires_at": pc.expires_at, "environment": env.name,
                      "command": nodes.install_command(info["app_url"] or "<app address>", code),
                      "agent_endpoint": info["endpoint"]})


@tool("nodes", WRITE)
async def refresh_node_updates(node: str) -> dict:
    """Run apt update on the node and return the list of upgradable packages."""
    from mmn.services import nodes

    nid, _ = await _node_id(node)
    try:
        return _jsonable(await nodes.refresh_updates(nid))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("node_updates", DESTRUCTIVE)
async def install_node_updates(node: str, confirm: str) -> dict:
    """Install all package updates on the node (apt dist-upgrade, as Proxmox VE requires). Set
    `confirm` to the node's name. Returns a job; follow it with get_node_job."""
    from mmn.services import nodes

    nid, name = await _node_id(node)
    if confirm != name:
        raise ToolDenied(f"confirm must equal the node name '{name}'")
    p = current_principal()
    try:
        job = await nodes.apply_updates(nid, source="mcp", requested_by=p.username if p else None)
    except Exception as e:
        raise _agent_errors(e) from e
    return _jsonable(nodes.job_out(job, with_output=False))


@tool("node_updates", DESTRUCTIVE)
async def reboot_node(node: str, confirm: str) -> dict:
    """Reboot the node. Running guests are handled by Proxmox VE's own shutdown policy. Set
    `confirm` to the node's name. Always ask the user first."""
    from mmn.services import nodes

    nid, name = await _node_id(node)
    if confirm != name:
        raise ToolDenied(f"confirm must equal the node name '{name}'")
    try:
        return _jsonable(await nodes.reboot(nid))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def get_node_job(job_id: str) -> dict:
    """Status and output of a node job (package upgrade, agent update)."""
    from mmn.models import NodeJob
    from mmn.services import nodes

    async with sessionmaker()() as db:
        j = await db.get(NodeJob, job_id)
        if j is None:
            raise ToolDenied("job not found")
        out = nodes.job_out(j)
    out["output"] = out["output"][-20000:]
    return _jsonable(out)


@tool("nodes", WRITE)
async def set_cpu_governor(node: str, governor: str) -> dict:
    """Set the CPU frequency governor (e.g. performance, schedutil, powersave). It is stored and
    applied again after every reboot."""
    from mmn.services import nodes

    nid, _ = await _node_id(node)
    try:
        return _jsonable(await nodes.set_governor(nid, governor))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def get_environment_notifications(environment: str) -> dict:
    """The Proxmox VE notification mail settings of an environment (password never returned)."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            return _jsonable(nodes.notify_public(await nodes.get_environment(db, environment)))
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("nodes", WRITE)
async def update_environment_notifications(environment: str, enabled: bool, mailto: list[str],
                                           types: list[str] | None = None, severities: list[str] | None = None,
                                           use_app_smtp: bool = True) -> dict:
    """Set who gets Proxmox VE notifications for an environment and push them to its nodes.
    types: vzdump, replication, fencing, package-updates, system-mail. severities: info, notice,
    warning, error, unknown. use_app_smtp sends through the app's own mail server; a different SMTP
    server can only be entered in the web interface."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, environment)
            cur = nodes.notify_public(e)
            body = nodes.NotifyIn(**{**{k: v for k, v in cur.items() if k in nodes.NotifyIn.model_fields},
                                     "enabled": enabled, "mailto": mailto, "use_app_smtp": use_app_smtp,
                                     **({"types": types} if types is not None else {}),
                                     **({"severities": severities} if severities is not None else {}),
                                     "password": None})
            out = await nodes.set_notify(db, e, body)
        except (nodes.NodeError, ValueError) as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        env_id = e.id
    out["push"] = await nodes.push_notify_to_environment(env_id)
    return _jsonable(out)


@tool("nodes", WRITE)
async def send_node_test_notification(node: str) -> dict:
    """Let Proxmox VE on this node send a test notification through the pushed mail settings."""
    from mmn.services import nodes

    nid, _ = await _node_id(node)
    try:
        return await nodes.test_notify(nid)
    except Exception as e:
        raise _agent_errors(e) from e


@tool("nodes", WRITE)
async def update_node_agent(node: str) -> dict:
    """Update the agent on the node to the version shipped with this app."""
    from mmn.services import nodes

    nid, _ = await _node_id(node)
    p = current_principal()
    try:
        job = await nodes.update_agent(nid, source="mcp", requested_by=p.username if p else None)
    except Exception as e:
        raise _agent_errors(e) from e
    return _jsonable(nodes.job_out(job, with_output=False))


# ------------------------------------------------------------------ server administration on nodes
async def _op(node: str, method: str, params: dict) -> Any:
    from mmn.services import nodes

    nid, _ = await _node_id(node)
    try:
        return await nodes.node_call(nid, method, params)
    except Exception as e:
        raise _agent_errors(e) from e


@tool("logs", RO)
async def read_logs(node: str, unit: str | None = None, since: str | None = None, until: str | None = None,
                    priority: str | None = None, grep: str | None = None, lines: int = 200,
                    this_boot: bool = False) -> dict:
    """Read the systemd journal of a node. unit: e.g. 'nginx' or 'pveproxy'. since/until: journalctl
    time like '1 hour ago' or '2026-11-22 09:00'. priority: emerg, alert, crit, err, warning, notice,
    info or debug (that level and worse). grep: case-insensitive text/regex filter. Max 5000 lines."""
    return _jsonable(await _op(node, "logs.journal", {"unit": unit or "", "since": since or "", "until": until or "",
                                                      "priority": priority or "", "grep": grep or "",
                                                      "lines": lines, "boot": this_boot}))


@tool("logs", RO)
async def list_services(node: str, state: str | None = None) -> list[dict]:
    """systemd services on a node. state: e.g. 'failed' or 'running' to filter."""
    return _jsonable(await _op(node, "services.list", {"state": state or ""}))


@tool("logs", RO)
async def get_service_status(node: str, unit: str) -> dict:
    """`systemctl status` of one service plus its properties (state, restarts, memory, start time)."""
    return _jsonable(await _op(node, "services.status", {"unit": unit}))


@tool("services", DESTRUCTIVE)
async def manage_service(node: str, unit: str, action: str, reason: str) -> dict:
    """start, stop, restart, reload, enable or disable a systemd service. `reason` is stored in the
    audit log. Ask the user before stopping or restarting production services."""
    if not reason.strip():
        raise ToolDenied("give a reason")
    return _jsonable(await _op(node, "services.action", {"unit": unit, "action": action}))


@tool("logs", RO)
async def find_packages(pattern: str, environment: str | None = None) -> list[dict]:
    """Which nodes have a package installed and which version, e.g. 'nginx*' or 'openssl' (useful
    for a CVE). Shows whether an update is pending for it. Offline nodes are listed without data."""
    from mmn.services import nodes

    env_id = None
    if environment:
        async with sessionmaker()() as db:
            try:
                env_id = (await nodes.get_environment(db, environment)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
    return _jsonable(await nodes.find_packages(pattern, env_id))


@tool("logs", RO)
async def read_file(node: str, path: str, max_bytes: int = 65536, tail: bool = False) -> dict:
    """Read a text file (or list a directory) on a node, e.g. /etc/nginx/nginx.conf or
    /var/log/syslog with tail=true. At most 1 MiB."""
    return _jsonable(await _op(node, "file.read", {"path": path, "max_bytes": max_bytes, "tail": tail}))


@tool("exec", DESTRUCTIVE)
async def run_command(node: str, command: str, reason: str, timeout: int = 60) -> dict:
    """Run a shell command as root on a node (/bin/sh -c) and return exit code, stdout and stderr
    (max 256 KiB each). `reason` is stored in the audit log with the command. Prefer the read-only
    tools when they suffice, and ask the user before changing anything. timeout: seconds, max 1800."""
    if not reason.strip():
        raise ToolDenied("give a reason")
    return _jsonable(await _op(node, "exec", {"command": command, "timeout": max(1, min(timeout, 1800))}))


# ------------------------------------------------------------------ VMs and containers
async def _guest(guest: str | int, node: str | None):
    from mmn.services import guests, nodes

    async with sessionmaker()() as db:
        try:
            return await guests.find_guest(db, guest, node)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


def _who() -> dict:
    p = current_principal()
    return {"source": "mcp", "requested_by": p.username if p else None}


async def _job(coro) -> dict:
    from mmn.services import nodes

    try:
        job = await coro
    except Exception as e:
        raise _agent_errors(e) from e
    return _jsonable({**nodes.job_out(job, with_output=False), "hint": "follow it with get_node_job"})


@tool("read", RO)
async def list_guests(environment: str | None = None, node: str | None = None, type: str | None = None,
                      query: str | None = None) -> list[dict]:
    """VMs (type 'qemu') and containers (type 'lxc') on all Proxmox VE nodes: VMID, name, status,
    node, CPU/memory/disk use, tags. Filter by environment, node, type or a text `query` (name,
    VMID or tag). Containers made by community-scripts.org have the tag 'community-script'."""
    from mmn.services import guests, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, environment)).id if environment else None
            node_id = (await nodes.get_node(db, node)).id if node else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable(await guests.list_guests(db, environment_id=env_id, node_id=node_id, type_=type,
                                                  query=query))


@tool("read", RO)
async def get_guest(guest: str, node: str | None = None) -> dict:
    """Details of one VM or container (by VMID or name): configuration, snapshots and, when
    running, its IP addresses. Give `node` when the VMID exists in more than one environment."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    try:
        return _jsonable(await guests.guest_detail(n, g))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("guests", WRITE)
async def guest_power(guest: str, action: str, node: str | None = None) -> dict:
    """start, stop (hard), shutdown (clean, falls back to stop after 3 minutes), reboot, suspend
    or resume a VM or container. Ask the user before stopping or rebooting production guests.
    Returns a job; follow it with get_node_job."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    return await _job(guests.power(n, g, action, **_who()))


@tool("read", RO)
async def list_snapshots(guest: str, node: str | None = None) -> list[dict]:
    """Snapshots of a VM or container."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    try:
        return _jsonable(await guests.list_snapshots(n, g))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("guests", WRITE)
async def create_snapshot(guest: str, name: str, description: str = "", include_ram: bool = False,
                          node: str | None = None) -> dict:
    """Take a snapshot (name: letters, digits, - and _). include_ram saves the RAM of a running VM."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    return await _job(guests.create_snapshot(n, g, name, description, include_ram, **_who()))


@tool("guests_admin", DESTRUCTIVE)
async def rollback_snapshot(guest: str, name: str, confirm: str, start_after: bool = True,
                            node: str | None = None) -> dict:
    """Roll a VM or container back to snapshot `name`: everything after it is lost. Set `confirm`
    to the snapshot name. Always ask the user first."""
    from mmn.services import guests

    if confirm != name:
        raise ToolDenied("confirm must equal the snapshot name")
    n, g = await _guest(guest, node)
    return await _job(guests.rollback_snapshot(n, g, name, start=start_after, **_who()))


@tool("guests_admin", DESTRUCTIVE)
async def delete_snapshot(guest: str, name: str, confirm: str, node: str | None = None) -> dict:
    """Delete a snapshot. Set `confirm` to the snapshot name."""
    from mmn.services import guests

    if confirm != name:
        raise ToolDenied("confirm must equal the snapshot name")
    n, g = await _guest(guest, node)
    return await _job(guests.delete_snapshot(n, g, name, **_who()))


@tool("guests_admin", DESTRUCTIVE)
async def migrate_guest(guest: str, target_node: str, confirm: str, online: bool = True,
                        node: str | None = None) -> dict:
    """Move a VM or container to another node of the same cluster. Running VMs move live;
    running containers restart on the target. Set `confirm` to the guest's VMID."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    if confirm != str(g["vmid"]):
        raise ToolDenied(f"confirm must equal the VMID {g['vmid']}")
    async with sessionmaker()() as db:
        return await _job(guests.migrate(db, n, g, target_node, online=online, **_who()))


@tool("guests", WRITE)
async def set_guest_resources(guest: str, cores: int | None = None, memory_mb: int | None = None,
                              onboot: bool | None = None, tags: list[str] | None = None,
                              node: str | None = None) -> dict:
    """Change CPU cores, memory (MiB), start-on-boot or tags. VMs need a reboot for CPU/memory."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    try:
        return _jsonable(await guests.set_resources(n, g, guests.ResourcesIn(cores=cores, memory=memory_mb,
                                                                             onboot=onboot, tags=tags)))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("guests_admin", DESTRUCTIVE)
async def delete_guest(guest: str, confirm: str, node: str | None = None) -> dict:
    """Delete a stopped VM or container including its disks. Cannot be undone. Set `confirm` to the
    guest's name. Always ask the user first."""
    from mmn.services import guests

    n, g = await _guest(guest, node)
    if confirm != g["name"]:
        raise ToolDenied(f"confirm must equal the name '{g['name']}'")
    return await _job(guests.destroy(n, g, **_who()))


@tool("read", RO)
async def suggest_node(environment: str | None = None) -> dict:
    """The least loaded online Proxmox VE node (memory 60 %, CPU 40 %), with the ranking."""
    from mmn.services import guests, nodes

    async with sessionmaker()() as db:
        env_id = None
        if environment:
            try:
                env_id = (await nodes.get_environment(db, environment)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        return _jsonable(await guests.suggest_node(db, env_id) or {"error": "no Proxmox VE node is online"})


@tool("read", RO)
async def get_node_resources(node: str) -> dict:
    """Storages (with free space), network bridges, ISO images, templates and the next free VMID on
    a node: what you need to create a guest."""
    from mmn.services import guests

    nid, _ = await _node_id(node)
    async with sessionmaker()() as db:
        from mmn.models import Node

        n = await db.get(Node, nid)
    try:
        return _jsonable(await guests.node_resources(n))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("guests_admin", WRITE)
async def create_container(name: str, template: str, node: str | None = None, cores: int = 1,
                           memory_mb: int = 1024, disk_gb: int = 8, storage: str = "local-lvm",
                           bridge: str = "vmbr0", vlan: int | None = None, ip: str = "dhcp",
                           gateway: str | None = None, password: str | None = None, ssh_key: str | None = None,
                           unprivileged: bool = True, start: bool = True, tags: list[str] | None = None,
                           vmid: int | None = None) -> dict:
    """Create an LXC container from the template library (`template`: library id, name or file
    name; see list_templates). Without `node` the least loaded node is used. ip: 'dhcp' or an
    address with prefix (10.0.0.20/24) plus `gateway`. A password or an SSH key is required. A
    template that is not on the node yet is downloaded first. Returns a job."""
    from mmn.services import guests, templates

    async with sessionmaker()() as db:
        try:
            tpl = await templates.get_template(db, template)
            if node:
                node_id = (await _node_id(node))[0]
            else:
                best = await guests.suggest_node(db)
                if not best:
                    raise ToolDenied("no Proxmox VE node is online")
                node_id = best["node_id"]
            body = guests.CreateCtIn(node_id=node_id, vmid=vmid, name=name, template_id=tpl.id, cores=cores,
                                     memory=memory_mb, disk_gb=disk_gb, storage=storage,
                                     net=guests.NetIn(bridge=bridge, vlan=vlan, ip=ip, gateway=gateway),
                                     password=password, ssh_keys=ssh_key, unprivileged=unprivileged, start=start,
                                     tags=tags or [])
        except (ValueError, guests.NodeError) as e:
            raise ToolDenied(str(e)) from e
        return await _job(guests.create_ct(db, body, **_who()))


@tool("guests_admin", WRITE)
async def create_vm(name: str, source: str, node: str | None = None, iso: str | None = None,
                    iso_url: str | None = None, cloud_image: str | None = None, cloud_url: str | None = None,
                    cores: int = 2, memory_mb: int = 2048, disk_gb: int = 32, storage: str = "local-lvm",
                    bridge: str = "vmbr0", vlan: int | None = None, ip: str = "dhcp", gateway: str | None = None,
                    ostype: str = "l26", ci_user: str = "admin", ci_password: str | None = None,
                    ssh_key: str | None = None, start: bool = True, vmid: int | None = None) -> dict:
    """Create a VM. source 'iso': install from `iso` (volid, see get_node_resources) or `iso_url`
    (downloaded first). source 'cloud': a ready cloud image with cloud-init: `cloud_image` is one of
    debian-13, debian-12, ubuntu-24.04, ubuntu-22.04, rocky-9 (or give `cloud_url`), with `ci_user`
    and `ci_password` or `ssh_key`. ostype: l26, win11, win10, other. Returns a job."""
    from mmn.services import guests

    async with sessionmaker()() as db:
        try:
            if node:
                node_id = (await _node_id(node))[0]
            else:
                best = await guests.suggest_node(db)
                if not best:
                    raise ToolDenied("no Proxmox VE node is online")
                node_id = best["node_id"]
            body = guests.CreateVmIn(node_id=node_id, vmid=vmid, name=name, source=source, iso=iso, iso_url=iso_url,
                                     cloud_image=cloud_image, cloud_url=cloud_url, cores=cores, memory=memory_mb,
                                     disk_gb=disk_gb, storage=storage, ostype=ostype, ci_user=ci_user,
                                     ci_password=ci_password, ssh_keys=ssh_key, start=start,
                                     net=guests.NetIn(bridge=bridge, vlan=vlan, ip=ip, gateway=gateway))
        except (ValueError, guests.NodeError) as e:
            raise ToolDenied(str(e)) from e
        return await _job(guests.create_vm(db, body, **_who()))


@tool("exec", DESTRUCTIVE)
async def run_in_container(guest: str, command: str, reason: str, timeout: int = 120,
                           node: str | None = None) -> dict:
    """Run a shell command as root inside a running container (pct exec ... sh -c) and return exit
    code and output. `reason` is stored in the audit log. Ask the user before changing anything."""
    import shlex

    from mmn.services import nodes

    if not reason.strip():
        raise ToolDenied("give a reason")
    n, g = await _guest(guest, node)
    if g["type"] != "lxc":
        raise ToolDenied("only containers; for a VM use run_command on a node with the agent")
    try:
        return _jsonable(await nodes.node_call(n.id, "exec", {
            "command": f"pct exec {int(g['vmid'])} -- sh -c {shlex.quote(command)}",
            "timeout": max(1, min(timeout, 1800))}))
    except Exception as e:
        raise _agent_errors(e) from e


# ------------------------------------------------------------------ templates and community scripts
@tool("read", RO)
async def list_templates() -> list[dict]:
    """The container template library (official Proxmox VE and custom templates) with the status per
    node: current, outdated or missing."""
    from mmn.services import templates

    async with sessionmaker()() as db:
        nl = await templates.pve_nodes(db)
        return _jsonable([templates.template_out(t, nl) for t in await templates.list_templates(db)])


@tool("read", RO)
async def list_template_catalog(query: str | None = None) -> list[dict]:
    """Official templates available from Proxmox VE (the pveam index) that can be added."""
    from mmn.services import templates

    async with sessionmaker()() as db:
        items = (await templates.catalog(db))["items"]
    if query:
        q = query.lower()
        items = [i for i in items if q in i["filename"].lower() or q in (i.get("headline") or "").lower()]
    return _jsonable(items[:200])


@tool("templates", WRITE)
async def add_official_template(filename: str, sync: bool = True) -> dict:
    """Add an official template (file name from list_template_catalog) to the library. With `sync`
    it is pushed to all nodes by the nightly sync (or run sync_templates now)."""
    from mmn.services import templates

    async with sessionmaker()() as db:
        p = current_principal()
        try:
            t = await templates.add_official(db, filename, created_by=p.username if p else None, sync=sync)
        except templates.NodeError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        return _jsonable(templates.template_out(t))


@tool("templates", WRITE)
async def sync_templates(template: str | None = None) -> dict:
    """Push the library to all online nodes now (or only one `template`). Returns a run id; follow
    it with get_task_run."""
    from mmn.services import templates

    p = current_principal()
    ids = None
    if template:
        async with sessionmaker()() as db:
            try:
                ids = [(await templates.get_template(db, template)).id]
            except templates.NodeError as e:
                raise ToolDenied(str(e)) from e
    run_id = await templates.sync(template_ids=ids, trigger="mcp", requested_by=p.username if p else None,
                                  refresh_index=ids is None)
    return {"run_id": run_id}


@tool("read", RO)
async def list_community_scripts(query: str | None = None, kind: str | None = None) -> list[dict]:
    """Scripts from community-scripts.org (synced nightly): slug, name, kind ('ct' container or
    'vm'), default CPU/RAM/disk and OS. Use `query` to search (e.g. 'jellyfin', 'home assistant')."""
    from mmn.services import community

    async with sessionmaker()() as db:
        return _jsonable((await community.list_scripts(db, query, kind))[:100])


@tool("read", RO)
async def get_community_script(slug: str) -> dict:
    """The current text of a community script from the official repository (and the install part it
    loads) with its sha256. Show the user what it does before install_community_script."""
    from mmn.services import community

    async with sessionmaker()() as db:
        try:
            info = await community.fetch_script(db, slug)
        except community.NodeError as e:
            raise ToolDenied(str(e)) from e
    return _jsonable({**info, "install_script": (info.get("install_script") or "")[:20000]})


@tool("guests_admin", DESTRUCTIVE)
async def install_community_script(slug: str, sha256: str, node: str | None = None, hostname: str | None = None,
                                   cpu: int | None = None, ram_mb: int | None = None, disk_gb: int | None = None,
                                   storage: str | None = None, bridge: str | None = None, vlan: int | None = None,
                                   ip: str | None = None, gateway: str | None = None) -> dict:
    """Run a community script unattended ('Default' mode) as root on a Proxmox VE node; it creates
    the container/VM and installs the app. `sha256` must be the value from get_community_script
    (proves the script was reviewed). Without `node` the least loaded node is used. Ask the user
    first. Returns a job with the full log."""
    from mmn.services import community, guests, nodes

    opts = {k: v for k, v in dict(hostname=hostname, cpu=cpu, ram=ram_mb, disk=disk_gb, storage=storage,
                                  bridge=bridge, vlan=vlan, ip=ip, gateway=gateway).items() if v is not None}
    async with sessionmaker()() as db:
        if node:
            node_id = (await _node_id(node))[0]
        else:
            best = await guests.suggest_node(db)
            if not best:
                raise ToolDenied("no Proxmox VE node is online")
            node_id = best["node_id"]
        try:
            job = await community.start_install(db, slug, node_id, sha256, opts, **_who())
        except (nodes.NodeError, Exception) as e:
            raise _agent_errors(e) from e
    return _jsonable(nodes.job_out(job, with_output=False))


# ------------------------------------------------------------------ scheduled tasks
@tool("read", RO)
async def list_scheduled_tasks() -> list[dict]:
    """Scheduled tasks (updates in containers, node updates, template sync, custom commands) with
    schedule, targets, options, last and next run."""
    from sqlalchemy import select

    from mmn.models import ScheduledTask
    from mmn.services import tasks

    async with sessionmaker()() as db:
        rows = (await db.execute(select(ScheduledTask).order_by(ScheduledTask.name))).scalars().all()
        return _jsonable([tasks.task_out(t) for t in rows])


@tool("tasks", WRITE)
async def save_scheduled_task(name: str, kind: str, cron: str, targets: dict | None = None,
                              options: dict | None = None, mail_to: list[str] | None = None,
                              mail_on: str = "always", enabled: bool = True, task_id: str | None = None) -> dict:
    """Create (or with `task_id` change) a scheduled task.
    kind: ct_community_update (the 'update' command of community-script containers),
    ct_apt_upgrade (apt-get update + full-upgrade, or apk/dnf, in containers), node_updates,
    template_sync, custom_command. cron: 5 fields in the app's time zone, e.g. '0 4 * * 0'.
    targets: {"scope": "all"|"environments"|"nodes"|"guests"|"tag", "environment_ids": [...],
    "node_ids": [...], "guests": [{"node_id": ..., "vmid": ...}], "tag": "...", "exclude_vmids": [...]}.
    options: {"snapshot": true, "rollback": true, "keep_snapshots": 3, "command": "...",
    "run_on": "ct"|"node", "timeout": 3600}. mail_on: always, failure or never."""
    from mmn.services import tasks
    from mmn.services.scheduler import reload_task_jobs

    try:
        body = tasks.TaskIn(name=name, kind=kind, cron=cron, targets=tasks.TargetsIn(**(targets or {})),
                            options=tasks.OptionsIn(**(options or {})), mail_to=mail_to or [], mail_on=mail_on,
                            enabled=enabled)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    async with sessionmaker()() as db:
        p = current_principal()
        try:
            t = await tasks.save_task(db, body, await tasks.get_task(db, task_id) if task_id else None,
                                      created_by=p.username if p else None)
        except tasks.NodeError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        out = tasks.task_out(t)
        preview = await tasks.resolve_targets(db, t.kind, t.targets, t.options)
    await reload_task_jobs()
    return _jsonable({**out, "targets_now": preview})


@tool("tasks", DESTRUCTIVE)
async def delete_scheduled_task(task_id: str, confirm: str) -> dict:
    """Delete a scheduled task. Set `confirm` to its name."""
    from mmn.services import tasks
    from mmn.services.scheduler import reload_task_jobs

    async with sessionmaker()() as db:
        try:
            t = await tasks.get_task(db, task_id)
        except tasks.NodeError as e:
            raise ToolDenied(str(e)) from e
        if confirm != t.name:
            raise ToolDenied(f"confirm must equal the task name '{t.name}'")
        await db.delete(t)
        await db.commit()
    await reload_task_jobs()
    return {"deleted": True}


@tool("tasks", WRITE)
async def run_scheduled_task(task_id: str) -> dict:
    """Run a scheduled task now. Returns a run id; follow it with get_task_run."""
    from mmn.services import tasks

    p = current_principal()
    try:
        return {"run_id": await tasks.run_task(task_id, trigger="mcp", requested_by=p.username if p else None)}
    except tasks.NodeError as e:
        raise ToolDenied(str(e)) from e


@tool("read", RO)
async def get_task_run(run_id: str | None = None, task_id: str | None = None) -> dict | list[dict]:
    """A run of a scheduled task or template sync with the result per target (snapshot, rolled back,
    error, last output lines). Without run_id: the last 10 runs (optionally of one task)."""
    from sqlalchemy import select

    from mmn.models import TaskRun
    from mmn.services import tasks

    async with sessionmaker()() as db:
        if run_id:
            r = await db.get(TaskRun, run_id)
            if r is None:
                raise ToolDenied("run not found")
            return _jsonable(tasks.run_out(r))
        q = select(TaskRun).order_by(TaskRun.started_at.desc()).limit(10)
        if task_id:
            q = q.where(TaskRun.task_id == task_id)
        return _jsonable([tasks.run_out(r) for r in (await db.execute(q)).scalars()])


# ------------------------------------------------------------------ network, firewall, SDN
async def _pve(node: str) -> tuple[str, str]:
    from mmn.services import agent_hub

    nid, name = await _node_id(node)
    if not agent_hub.is_online(nid):
        raise ToolDenied(f"the agent of {name} is not connected")
    return nid, name


async def _call(coro) -> Any:
    try:
        return _jsonable(await coro)
    except ToolDenied:
        raise
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def get_bridge_map(environment: str | None = None, node: str | None = None) -> dict:
    """Bridges (vmbr) and SDN VNets with the VMs and containers on them, grouped by VLAN tag:
    VMID, name, node, NIC, MAC, firewall flag, trunks. Use it to answer 'what is on VLAN 20' or
    'which guests use vmbr1'."""
    from mmn.services import network, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, environment)).id if environment else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        node_id = (await _node_id(node))[0] if node else None
        return await _call(network.bridge_map(db, environment_id=env_id, node_id=node_id))


@tool("read", RO)
async def get_node_network(node: str) -> dict:
    """Network interfaces of a Proxmox VE node (bridges, bonds, VLANs, NICs with addresses) and the
    staged change that is not applied yet, as a diff."""
    from mmn.services import network

    nid, _ = await _pve(node)
    return {"interfaces": await _call(network.interfaces(nid, fresh=True)), "pending": await _call(network.pending(nid))}


@tool("network", WRITE)
async def stage_bridge(node: str, iface: str, action: str = "create", ports: list[str] | None = None,
                       vlan_aware: bool = False, vids: str | None = None, cidr: str | None = None,
                       gateway: str | None = None, mtu: int | None = None, comments: str = "",
                       force: bool = False) -> dict:
    """Stage a bridge change on a node (action create, update or delete). Nothing is applied: the
    result is the pending diff. Review it with the user, then call apply_network_changes."""
    from mmn.services import network

    nid, _ = await _pve(node)
    if action == "delete":
        return await _call(network.stage_delete(nid, iface, force=force))
    if action not in ("create", "update"):
        raise ToolDenied("action must be create, update or delete")
    try:
        body = network.BridgeIn(iface=iface, ports=ports or [], vlan_aware=vlan_aware, vids=vids, cidr=cidr,
                                gateway=gateway, mtu=mtu, comments=comments)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(network.stage_bridge(nid, body, create=action == "create"))


@tool("network", DESTRUCTIVE)
async def apply_network_changes(node: str, dry_run: bool = True, confirm: str | None = None) -> dict:
    """Apply the staged network change of a node. Default dry_run=true only shows the diff and
    warnings. To apply, set dry_run=false and `confirm` to the node name, after the user agreed.
    The app saves /etc/network/interfaces first; if the node cannot be reached within 90 seconds
    after the change, the node restores the old configuration by itself."""
    from mmn.services import network

    nid, name = await _pve(node)
    if not dry_run and confirm != name:
        raise ToolDenied(f"confirm must equal the node name '{name}'")
    p = current_principal()
    return await _call(network.apply(nid, dry_run=dry_run, source="mcp", requested_by=p.username if p else None))


@tool("network", WRITE)
async def revert_network_changes(node: str) -> dict:
    """Throw away the staged (not applied) network change of a node."""
    from mmn.services import network

    nid, _ = await _pve(node)
    return await _call(network.revert_pending(nid))


@tool("read", RO)
async def list_config_backups(kind: str | None = None, node: str | None = None) -> list[dict]:
    """Configuration backups the app made before network or SDN changes (kind 'network' or 'sdn')."""
    from mmn.services import network

    async with sessionmaker()() as db:
        node_id = (await _node_id(node))[0] if node else None
        return _jsonable(await network.list_backups(db, kind=kind, node_id=node_id))


@tool("network", DESTRUCTIVE)
async def restore_config_backup(backup_id: str, confirm: str) -> dict:
    """Put a network or SDN configuration backup back (roll back a change). Set `confirm` to the
    node name of the backup. Ask the user first."""
    from mmn.models import ConfigBackup
    from mmn.services import network, sdn

    p = current_principal()
    who = p.username if p else None
    async with sessionmaker()() as db:
        b = await db.get(ConfigBackup, backup_id)
        if b is None:
            raise ToolDenied("backup not found")
        nid, name = await _pve(b.node_id)
        if confirm != name:
            raise ToolDenied(f"confirm must equal the node name '{name}'")
        if b.kind == "sdn":
            return await _call(sdn.rollback(db, b.id, source="mcp", requested_by=who))
    return await _call(network.restore_network_backup(backup_id, source="mcp", requested_by=who))


@tool("read", RO)
async def get_firewall(node: str, guest: str | None = None) -> dict:
    """Proxmox VE firewall: cluster and node options and rules, security groups, aliases, IP sets
    and which guest NICs have the firewall on. With `guest` (VMID or name): that guest's
    firewall options and rules."""
    from mmn.services import network

    if guest:
        n, g = await _guest(guest, node)
        return await _call(network.guest_firewall(n.id, g["type"], g["vmid"]))
    nid, _ = await _pve(node)
    return await _call(network.firewall_overview(nid))


async def _fw_target(node: str, level: str, guest: str | None) -> tuple[str, str | None, int | None]:
    if level == "guest":
        if not guest:
            raise ToolDenied("give the guest for a guest firewall")
        n, g = await _guest(guest, node)
        return n.id, g["type"], g["vmid"]
    nid, _ = await _pve(node)
    return nid, None, None


@tool("network", WRITE)
async def set_firewall(node: str, level: str, enable: bool | None = None, policy_in: str | None = None,
                       policy_out: str | None = None, guest: str | None = None, confirm: str | None = None) -> dict:
    """Switch the firewall on or off, or set the in/out policy, at level 'cluster', 'node' or
    'guest' (give `guest`). Switching the cluster firewall needs `confirm` set to the node name:
    a wrong policy can lock everyone out, so ask the user first."""
    from mmn.services import network

    nid, gtype, vmid = await _fw_target(node, level, guest)
    if level == "cluster" and enable is not None:
        _, name = await _node_id(node)
        if confirm != name:
            raise ToolDenied(f"confirm must equal the node name '{name}'")
    return await _call(network.set_firewall_options(nid, level, enable=enable, policy_in=policy_in,
                                                    policy_out=policy_out, gtype=gtype, vmid=vmid))


@tool("network", WRITE)
async def add_firewall_rule(node: str, level: str, action: str, type: str = "in", proto: str | None = None,
                            dport: str | None = None, source: str | None = None, dest: str | None = None,
                            macro: str | None = None, comment: str = "", enable: bool = True,
                            guest: str | None = None, group: str | None = None) -> list[dict]:
    """Add a firewall rule at level cluster, node, guest or group (security group). action ACCEPT,
    DROP or REJECT (or a group name with type 'group'). Returns the rules of that level."""
    from mmn.services import network

    nid, gtype, vmid = await _fw_target(node, level, guest)
    try:
        rule = network.RuleIn(type=type, action=action, proto=proto, dport=dport, source=source, dest=dest,
                              macro=macro, comment=comment, enable=enable)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(network.add_rule(nid, level, rule, gtype=gtype, vmid=vmid, group=group))


@tool("network", DESTRUCTIVE)
async def delete_firewall_rule(node: str, level: str, pos: int, confirm: int, guest: str | None = None,
                               group: str | None = None) -> list[dict]:
    """Delete the firewall rule at position `pos` (see get_firewall). Set `confirm` to the same position."""
    from mmn.services import network

    if confirm != pos:
        raise ToolDenied("confirm must equal pos")
    nid, gtype, vmid = await _fw_target(node, level, guest)
    return await _call(network.delete_rule(nid, level, pos, gtype=gtype, vmid=vmid, group=group))


@tool("read", RO)
async def get_sdn(node: str) -> dict:
    """SDN of the node's cluster: zones, VNets, subnets, controllers and their status per node."""
    from mmn.services import sdn

    nid, _ = await _pve(node)
    return await _call(sdn.state(nid))


@tool("read", RO)
async def list_sdn_designs() -> list[dict]:
    """Designs made in the SDN designer (one zone each) with their status."""
    from mmn.services import sdn

    async with sessionmaker()() as db:
        return _jsonable(await sdn.list_designs(db))


@tool("network", WRITE)
async def save_sdn_design(name: str, node: str, zone: dict, vnets: list[dict] | None = None,
                          subnets: list[dict] | None = None, design_id: str | None = None) -> dict:
    """Create or change an SDN design (nothing is applied). zone: {id (2-8 lowercase), type
    vlan|simple|vxlan|evpn|qinq, bridge, peers, controller, vrf_vxlan, mtu, nodes, ipam, dhcp};
    vnets: [{id, alias, tag, vlanaware}]; subnets: [{vnet, cidr, gateway, snat, dhcp_start, dhcp_end}].
    Returns the design and its plan (errors, warnings, list of changes)."""
    from mmn.services import nodes, sdn

    try:
        spec = sdn.DesignSpec(zone=zone, vnets=vnets or [], subnets=subnets or [])
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    nid, _ = await _pve(node)
    p = current_principal()
    async with sessionmaker()() as db:
        try:
            d = await sdn.get_design(db, design_id) if design_id else None
            d = await sdn.save_design(db, sdn.DesignIn(name=name, node_id=nid, spec=spec), d,
                                      user=p.username if p else None)
            await db.commit()
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        plan = await _call(sdn.plan(db, nid, spec))
        plan.pop("current", None)
        return _jsonable({"design": sdn.design_out(d), "plan": plan})


@tool("network", DESTRUCTIVE)
async def apply_sdn_design(design_id: str, dry_run: bool = True, confirm: str | None = None) -> dict:
    """Apply an SDN design to the cluster. Default dry_run=true returns the plan only. To apply,
    set dry_run=false and `confirm` to the zone id after the user agreed. The SDN configuration
    is saved first and put back automatically when a step fails."""
    from mmn.services import nodes, sdn

    p = current_principal()
    async with sessionmaker()() as db:
        try:
            d = await sdn.get_design(db, design_id)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        zone = (d.spec or {}).get("zone", {}).get("id")
        if not dry_run and confirm != zone:
            raise ToolDenied(f"confirm must equal the zone id '{zone}'")
        await _pve(d.node_id)
        res = await _call(sdn.apply(db, d, dry_run=dry_run, source="mcp", requested_by=p.username if p else None))
        await db.commit()
        res.pop("current", None)
        return res


# ------------------------------------------------------------------ HA, replication, backup jobs, storage
@tool("read", RO)
async def get_ha_status(node: str) -> dict:
    """High availability of the node's cluster: quorum and manager status, HA resources with their
    state, and groups (Proxmox VE 8) or rules (Proxmox VE 9)."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    return await _call(cluster.ha_overview(nid))


@tool("cluster", WRITE)
async def set_ha_resource(node: str, guest: str, state: str = "started", group: str | None = None,
                          max_restart: int = 1, max_relocate: int = 1, remove: bool = False) -> dict:
    """Put a VM or container under HA (or change it): state started, stopped, ignored or disabled.
    `remove` takes it out of HA."""
    from mmn.services import cluster

    n, g = await _guest(guest, node)
    sid = f"{'vm' if g['type'] == 'qemu' else 'ct'}:{g['vmid']}"
    if remove:
        return await _call(cluster.remove_ha_resource(n.id, sid))
    ha = await _call(cluster.ha_overview(n.id))
    exists = any(r.get("sid") == sid for r in ha.get("resources") or [])
    try:
        body = cluster.HaResourceIn(sid=sid, state=state, group=group, max_restart=max_restart,
                                    max_relocate=max_relocate)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(cluster.save_ha_resource(n.id, body, create=not exists))


@tool("cluster", WRITE)
async def create_ha_rule(node: str, id: str, resources: list[str], nodes: dict[str, int] | None = None,
                         type: str = "node-affinity", affinity: str = "positive", strict: bool = False) -> dict:
    """Proxmox VE 9: an HA rule. node-affinity: run `resources` (vm:100, ct:101) preferably on
    `nodes` {name: priority}. resource-affinity: keep resources together (positive) or apart
    (negative). On Proxmox VE 8 this creates an HA group from `nodes` instead."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    ha = await _call(cluster.ha_overview(nid))
    try:
        if ha["mode"] == "groups":
            return await _call(cluster.create_ha_group(nid, cluster.HaGroupIn(id=id, nodes=nodes or {})))
        return await _call(cluster.create_ha_rule(nid, cluster.HaRuleIn(id=id, type=type, resources=resources,
                                                                        nodes=nodes or {}, affinity=affinity,
                                                                        strict=strict)))
    except ValueError as e:
        raise ToolDenied(str(e)) from e


@tool("read", RO)
async def list_replication(node: str) -> list[dict]:
    """Storage replication jobs of the cluster with last/next sync, duration and errors."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    return await _call(cluster.replication(nid))


@tool("cluster", WRITE)
async def manage_replication(node: str, action: str, guest: str | None = None, target: str | None = None,
                             schedule: str = "*/15", job_id: str | None = None) -> Any:
    """action 'create' (guest + target node + schedule, e.g. */15), 'run' (job_id, sync now) or
    'delete' (job_id)."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    if action == "create":
        if not guest or not target:
            raise ToolDenied("give the guest and the target node")
        _, g = await _guest(guest, node)
        try:
            body = cluster.ReplicationIn(vmid=g["vmid"], target=target, schedule=schedule)
        except ValueError as e:
            raise ToolDenied(str(e)) from e
        return await _call(cluster.create_replication(nid, body))
    if not job_id:
        raise ToolDenied("give job_id")
    if action == "run":
        return await _call(cluster.run_replication(nid, job_id))
    if action == "delete":
        return await _call(cluster.delete_replication(nid, job_id))
    raise ToolDenied("action must be create, run or delete")


@tool("read", RO)
async def list_backup_jobs(node: str) -> dict:
    """Proxmox VE backup jobs (vzdump schedules) of the cluster and the guests that no job backs up."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    return await _call(cluster.backup_jobs(nid))


@tool("cluster", WRITE)
async def save_backup_job(node: str, storage: str, schedule: str = "sun 01:00", all: bool = False,
                          vmids: list[int] | None = None, mode: str = "snapshot", keep_last: int | None = None,
                          keep_daily: int | None = None, keep_weekly: int | None = None,
                          mailto: list[str] | None = None, enabled: bool = True, job_id: str | None = None) -> dict:
    """Create (or with job_id change) a Proxmox VE backup job: storage, schedule (systemd calendar,
    e.g. 'daily 02:00', 'sun 01:00'), all guests or vmids, mode snapshot/suspend/stop, retention."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    try:
        body = cluster.BackupJobIn(storage=storage, schedule=schedule, all=all, vmids=vmids or [], mode=mode,
                                   keep_last=keep_last, keep_daily=keep_daily, keep_weekly=keep_weekly,
                                   mailto=mailto or [], enabled=enabled)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(cluster.save_backup_job(nid, body, job_id))


@tool("cluster", WRITE)
async def run_backup_job(node: str, job_id: str) -> list[dict]:
    """Run a backup job now (a vzdump task per node). Follow the jobs with get_node_job."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    p = current_principal()
    async with sessionmaker()() as db:
        return await _call(cluster.run_backup_job(db, nid, job_id, source="mcp", requested_by=p.username if p else None))


@tool("cluster", DESTRUCTIVE)
async def delete_backup_job(node: str, job_id: str, confirm: str) -> dict:
    """Delete a backup job (existing backups stay). Set `confirm` to the job id."""
    from mmn.services import cluster

    if confirm != job_id:
        raise ToolDenied("confirm must equal the job id")
    nid, _ = await _pve(node)
    return await _call(cluster.delete_backup_job(nid, job_id))


@tool("read", RO)
async def list_storage(node: str) -> dict:
    """Storages of the cluster (type, content, shared, enabled) with use on this node."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    return await _call(cluster.storages(nid))


@tool("cluster", WRITE)
async def add_storage(node: str, storage: str, type: str, content: list[str] | None = None,
                      path: str | None = None, server: str | None = None, export: str | None = None,
                      share: str | None = None, username: str | None = None, password: str | None = None,
                      datastore: str | None = None, fingerprint: str | None = None) -> dict:
    """Add a storage to the cluster: dir (path), nfs (server, export), cifs (server, share,
    username, password) or pbs (server, datastore, username, password, fingerprint)."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    try:
        body = cluster.StorageIn(storage=storage, type=type, content=content or ["backup"], path=path, server=server,
                                 export=export, share=share, username=username, password=password,
                                 datastore=datastore, fingerprint=fingerprint)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(cluster.add_storage(nid, body))


@tool("cluster", WRITE)
async def set_storage_enabled(node: str, storage: str, enabled: bool) -> dict:
    """Enable or disable a storage in the cluster configuration."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    return await _call(cluster.set_storage_enabled(nid, storage, enabled))


# ------------------------------------------------------------------ monitoring
@tool("read", RO)
async def get_monitoring(range: str = "24h", environment: str | None = None) -> dict:
    """All nodes with CPU/RAM/disk now and as a trend (range 1h, 24h, 7d or 30d), storages, disk
    health (SMART) and the number of open alerts. Totals over all nodes."""
    from mmn.services import monitoring, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, environment)).id if environment else None
            return _jsonable(await monitoring.overview(db, range, env_id))
        except (ValueError, nodes.NodeError) as e:
            raise ToolDenied(str(e)) from e


@tool("read", RO)
async def get_node_metrics(node: str, range: str = "24h") -> dict:
    """Metric history of one node (CPU %, memory %, root disk %, load) for 1h, 24h, 7d or 30d,
    plus its storages and disks."""
    from mmn.services import monitoring

    nid, name = await _node_id(node)
    async with sessionmaker()() as db:
        try:
            h = await monitoring.history(db, [nid], range)
        except ValueError as e:
            raise ToolDenied(str(e)) from e
        from mmn.models import Node

        n = await db.get(Node, nid)
        return _jsonable({"node": name, "range": range, "points": h.get(nid, []), "health": n.health or {}})


@tool("read", RO)
async def list_alerts(open_only: bool = True, limit: int = 50) -> list[dict]:
    """Monitoring alerts (node offline, CPU, memory, disk, storage full, SMART) newest first."""
    from mmn.services import monitoring

    async with sessionmaker()() as db:
        return _jsonable(await monitoring.list_alerts(db, open_only=open_only, limit=limit))


@tool("read", RO)
async def get_disk_smart(node: str, disk: str) -> dict:
    """Full SMART data of a disk of a Proxmox VE node (disk: device path such as /dev/sda)."""
    from mmn.services import monitoring

    nid, _ = await _pve(node)
    return await _call(monitoring.smart(nid, disk))


# ------------------------------------------------------------------ GitHub deployments
@tool("read", RO)
async def list_deployments() -> list[dict]:
    """GitHub repositories deployed into containers: repo, channel (prod/test) and branch, target
    container, last commit and status, automatic deploys."""
    from mmn.services import deploy

    async with sessionmaker()() as db:
        return _jsonable([deploy.deployment_out(d) for d in await deploy.list_deployments(db)])


@tool("read", RO)
async def get_deployment(deployment: str) -> dict:
    """One deployment (by name or id) with its last runs. A run's job_id shows the log with get_node_job."""
    from mmn.services import deploy, nodes

    async with sessionmaker()() as db:
        try:
            d = await deploy.get_deployment(db, deployment)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable({**deploy.deployment_out(d), "runs": await deploy.list_runs(db, d.id, 10)})


@tool("deploy", RO)
async def list_github_repos(query: str | None = None) -> list[dict]:
    """Repositories the connected GitHub token can read (name, default branch, last push)."""
    from mmn.services import deploy

    async with sessionmaker()() as db:
        try:
            return _jsonable(await deploy.list_repos(db, query))
        except Exception as e:
            raise ToolDenied(str(e)) from e


@tool("deploy", WRITE)
async def save_deployment(name: str, repo: str, node: str, guest: str, deploy_cmd: str, channel: str = "prod",
                          branch_prod: str = "main", branch_test: str = "test", path: str | None = None,
                          setup_cmd: str = "", service: str | None = None, auto_deploy: str = "off",
                          snapshot: bool = True, rollback: bool = True) -> dict:
    """Create or change a deployment: GitHub `repo` (owner/name) into container `guest` on `node`.
    channel prod uses branch_prod, test uses branch_test. deploy_cmd runs in the checkout (path,
    default /opt/<name>) on every deploy, setup_cmd only on the first. service is restarted after.
    auto_deploy: off, webhook (GitHub push) or poll (checks every 5 minutes)."""
    from mmn.services import deploy, nodes

    n, g = await _guest(guest, node)
    p = current_principal()
    try:
        body = deploy.DeploymentIn(name=name, repo=repo, channel=channel, branch_prod=branch_prod,
                                   branch_test=branch_test, node_id=n.id, vmid=g["vmid"], path=path,
                                   preset="custom", setup_cmd=setup_cmd, deploy_cmd=deploy_cmd, service=service,
                                   auto_deploy=auto_deploy, snapshot=snapshot, rollback=rollback)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    async with sessionmaker()() as db:
        try:
            existing = None
            try:
                existing = await deploy.get_deployment(db, name)
            except nodes.NodeError:
                pass
            d = await deploy.save_deployment(db, body, existing, user=p.username if p else None)
            await db.commit()
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable(deploy.deployment_out(d))


@tool("deploy", WRITE)
async def run_deployment(deployment: str, run_setup: bool = False, channel: str | None = None) -> dict:
    """Deploy now: pull the branch of the deployment's channel into the container and run the deploy
    commands (snapshot first and roll back on failure when the deployment says so). `channel`
    prod/test switches the channel first. Follow the job with get_node_job."""
    from mmn.services import deploy, nodes

    p = current_principal()
    async with sessionmaker()() as db:
        try:
            d = await deploy.get_deployment(db, deployment)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        if channel:
            if channel not in ("prod", "test"):
                raise ToolDenied("channel must be prod or test")
            d.channel = channel
            await db.commit()
        dep_id = d.id
    try:
        r = await deploy.start_run(dep_id, trigger="mcp", source="mcp", requested_by=p.username if p else None,
                                   run_setup=run_setup)
    except Exception as e:
        raise _agent_errors(e) from e
    return _jsonable({**deploy.run_out(r), "hint": "follow job_id with get_node_job"})


# ------------------------------------------------------------------ ASGI wiring
def _base_url_from_scope(scope: Scope, headers: dict[str, str]) -> str:
    from mmn.config import get_settings

    bp = get_settings().base_path
    host = headers.get("host", "localhost")
    return f"{scope.get('scheme', 'http')}://{host}{'' if bp == '/' else bp}"


async def _read_body(receive: Receive, limit: int = 4 * 1024 * 1024) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if len(body) > limit or not msg.get("more_body"):
            return body


def _client_info(body: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and item.get("method") == "initialize":
            info = (item.get("params") or {}).get("clientInfo") or {}
            return {"name": str(info.get("name", ""))[:100], "version": str(info.get("version", ""))[:50]}
    return None


class BearerAuth:
    """Checks the API token before a request reaches the MCP transport, and remembers which AI
    client (name/version from MCP ``initialize``, plus User-Agent) uses the token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        body = await _read_body(receive) if scope.get("method") == "POST" else b""
        principal = None
        if raw:
            from datetime import UTC, datetime

            from mmn.api.tokens import effective_scopes, resolve_token
            from mmn.models import ApiToken

            async with sessionmaker()() as db:
                tok = await resolve_token(db, raw)
                if tok is not None:
                    ua = headers.get("user-agent", "")[:200]
                    client = dict(tok.last_client or {})
                    init = _client_info(body) if body else None
                    if init is not None or (ua and client.get("user_agent") != ua):
                        client.update(init or {})
                        client["user_agent"] = ua
                        client["seen_at"] = datetime.now(UTC).isoformat()
                        await db.execute(ApiToken.__table__.update().where(ApiToken.id == tok.id)
                                         .values(last_client=client))
                        await db.commit()
                    peer = scope.get("client")
                    principal = Principal(user_id=tok.user_id, username=tok.user.username, token_id=tok.id,
                                          token_name=tok.name, token_kind=tok.kind, scopes=effective_scopes(tok),
                                          ip=peer[0] if peer else None, client=client)
        if principal is None:
            meta = f"{_base_url_from_scope(scope, headers)}/.well-known/oauth-protected-resource/mcp"
            payload = json.dumps({"error": "invalid_token",
                                  "error_description": "Sign in with OAuth or use Authorization: Bearer <API token>"})
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate",
                 f'Bearer realm="mcp-management-node", error="invalid_token", resource_metadata="{meta}"'.encode())]})
            await send({"type": "http.response.body", "body": payload.encode()})
            return

        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        token = _principal.set(principal)
        try:
            await self.app(scope, replay if scope.get("method") == "POST" else receive, send)
        finally:
            _principal.reset(token)


def build_mcp_app() -> ASGIApp:
    from mcp.server.transport_security import TransportSecuritySettings

    # Every request needs a bearer token, which already rules out DNS rebinding; the public host name
    # behind Zoraxy is not known in advance.
    app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return BearerAuth(app)
