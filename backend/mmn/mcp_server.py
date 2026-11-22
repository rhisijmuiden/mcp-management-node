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

SECRET_ARGS = {"password", "passphrase", "token", "private_key", "secret"}


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
    for k in ("node", "environment", "backup_id", "target_id", "tag", "channel", "to", "pattern"):
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
