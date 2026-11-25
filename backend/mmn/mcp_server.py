"""Built-in MCP server, so Claude (or another MCP client) can work with this app.

Endpoint: ``<base url>/mcp`` (Streamable HTTP). Authenticate with a personal API token:
``Authorization: Bearer mmn_...``. Every tool checks the token's scopes, and every call is
written to the audit log with source ``mcp``. Destructive tools (restore, install update)
require an explicit ``confirm`` argument that repeats the target.

Later phases register their own tools here (Proxmox nodes, VMs, containers, servers).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import re
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
    # Customers this token may see; None = all customers.
    customers: frozenset[str] | None = None


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
        keys = ("ok", "status", "state", "channel", "restored", "update_available", "error", "tag", "id", "exit_code",
                "stage", "timed_out", "approval_id")
        picked = {k: result[k] for k in keys if k in result and not isinstance(result[k], (dict, list))}
        if "exit_code" in result and ("stdout" in result or "stderr" in result):
            # Commands: keep the end of the output, so the audit log shows what happened.
            picked["output"] = (str(result.get("stdout") or "") + str(result.get("stderr") or ""))[-2000:]
        return picked or {"keys": sorted(result)[:10]}
    return {"type": type(result).__name__}


async def _audit(p: Principal | None, tool_name: str, args: dict[str, Any], success: bool, *,
                 result: Any = None, error: str | None = None, ms: int = 0, approval_id: str | None = None,
                 target: str | None = None, extra: dict[str, Any] | None = None) -> None:
    from mmn.models import AuditLog

    detail = {"tool": tool_name, "args": _redact(args), "duration_ms": ms, **(extra or {})}
    if approval_id:
        detail["approval_id"] = approval_id
    if p is not None:
        detail.update(token=p.token_name, token_id=p.token_id, token_kind=p.token_kind, client=p.client)
    if success:
        detail["result"] = _summary(result)
    else:
        detail["error"] = (error or "")[:1000]
    async with sessionmaker()() as db:
        db.add(AuditLog(action=f"mcp.{tool_name}", user_id=p.user_id if p else None,
                        username=p.username if p else None, source="mcp", target=target or _target_of(args),
                        ip=p.ip if p else None, success=success, detail=_jsonable(detail)))
        await db.commit()


def _target_of(args: dict[str, Any]) -> str | None:
    if args.get("target_path") and args.get("source") and args.get("target"):
        return f"{args['source']} → {args['target']}"[:255]
    if isinstance(args.get("servers"), list) and args["servers"]:
        return ", ".join(map(str, args["servers"]))[:255]
    if args.get("door"):
        return f"door {args['door']}"[:255]
    for k in ("credential", "document", "wifi", "client", "device", "connection", "deployment", "design_id", "guest", "node", "server", "workstation", "customer", "slug", "template", "task_id", "run_id", "name", "backup_id",
              "target_id", "tag", "channel", "to", "package", "transfer_id", "pattern"):
        if args.get(k):
            return str(args[k])[:255]
    return None


# Tools the AI may always call, also while AI actions are paused: they only report on approvals
# and guardrails.
GUARD_EXEMPT = {"get_approval", "get_guardrails"}
# Tools that apply the policy themselves (reveal_credential first looks for an approved grant).
SELF_GUARDED = {"reveal_credential"}


async def _guard(p: Principal | None, name: str, risk: str, args: dict[str, Any]) -> Any:
    """Apply the AI gateway policy. Returns None when the call may run now, otherwise what the tool
    answers instead: the result of an approval that was decided while waiting, or a pending notice."""
    from mmn.services import ai_notify, gateway

    clean = _jsonable(args)
    async with sessionmaker()() as db:
        d = await gateway.decide(db, tool=name, risk=risk, args=clean, token_id=p.token_id if p else None,
                                 token_customers=p.customers if p else None)
        if d.action == "allow":
            return None
        texts = "; ".join(gateway.reason_text(r) for r in d.reasons)
        if d.action == "paused":
            raise ToolDenied(f"Refused: {texts}. Nothing was changed. Tell the user; an admin can resume AI actions "
                             "in Stackpilot (Command center).")
        if d.action == "block":
            raise ToolDenied(f"Blocked by policy: {texts}. Nothing was changed; this cannot be approved. Tell the user.")
        a, created = await gateway.create_approval(
            db, tool=name, risk=risk, args=clean, args_view=_redact(clean), target=_target_of(clean), decision=d,
            token_id=p.token_id if p else None, token_name=p.token_name if p else None,
            user_id=p.user_id if p else None, username=p.username if p else None, client=p.client if p else {})
        await db.commit()
        if created:
            await ai_notify.notify(db, a)
        cfg = await gateway.gateway_settings(db)
        url = await ai_notify.approval_url(db, a.id)
        wait = max(0, min(int(cfg.get("approval_wait_seconds") or 0), 120))
    final = await gateway.wait_for(a.id, wait) or a
    if final.status == "executed":
        return final.result
    if final.status == "failed":
        raise ToolDenied(f"Approved by {final.decided_by}, but the call failed: {final.error}")
    if final.status == "rejected":
        note = f": {final.decision_note}" if final.decision_note else ""
        raise ToolDenied(f"Rejected by {final.decided_by}{note}. Nothing was changed. Do not retry; ask the user.")
    if final.status == "expired":
        raise ToolDenied("The approval request expired. Nothing was changed.")
    return gateway.pending_response(final, url)


def tool(scope: str, annotations: ToolAnnotations, *, all_customers: bool = False):
    """Register an MCP tool that checks ``scope``, applies the AI gateway policy (allow, approve,
    block, paused) and writes every call to the audit log.

    ``all_customers``: the tool works on the app itself or across customers (app updates, app
    backups, mail server, audit log, template library, scheduled tasks), so a token that is tied
    to some customers may not use it. Every other tool only sees the token's customers.
    """
    from mmn.services import gateway

    def deco(fn):
        name = fn.__name__
        risk = gateway.risk_from_annotations(name, bool(annotations.read_only_hint), bool(annotations.destructive_hint))
        sig = inspect.signature(fn)
        dry_default = sig.parameters["dry_run"].default if "dry_run" in sig.parameters else None

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            started = time.monotonic()
            bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
            p = _principal.get()
            approval_id = gateway.approved_call.get()
            rb_call = gateway.runbook_call.get()
            extra: dict[str, Any] = {"approval_id": approval_id} if approval_id else {}
            if rb_call:
                extra["extra"] = {"runbook_run": rb_call}
            try:
                _check(scope)
                if all_customers and p is not None and p.customers is not None:
                    raise ToolDenied("this tool works on the whole app or across customers; this token is tied to "
                                     "specific customers. Use a token for all customers.")
                if approval_id is None and rb_call is None and name not in GUARD_EXEMPT and name not in SELF_GUARDED:
                    # A dry run only shows what would change.
                    call_risk = "read" if bound.get("dry_run", dry_default) is True else risk
                    outcome = await _guard(p, name, call_risk, bound)
                    if outcome is not None:
                        await _audit(p, name, bound, True, result=outcome, ms=int((time.monotonic() - started) * 1000))
                        return outcome
                result = await fn(*args, **kwargs)
            except Exception as e:
                msg = str(e) or type(e).__name__
                await _audit(p, name, bound, False, error=msg, ms=int((time.monotonic() - started) * 1000), **extra)
                if isinstance(e, ToolError):
                    raise
                raise ToolDenied(f"{type(e).__name__}: {msg}") from e
            await _audit(p, name, bound, True, result=result, ms=int((time.monotonic() - started) * 1000), **extra)
            return result

        mcp.tool(annotations=annotations)(wrapper)
        doc = inspect.getdoc(fn) or ""
        gateway.register(gateway.ToolSpec(name=name, scope=scope, risk=risk, all_customers=all_customers,
                                          description=" ".join(doc.split("\n\n")[0].split()), call=wrapper))
        return fn

    return deco


INSTRUCTIONS = """Stackpilot manages the IT of several customers through an agent on every machine (Proxmox
VE nodes, Linux and Windows servers, workstations) plus their UniFi networks, so you can do
day-to-day administration without a separate remote session.

Customers are the top layer: everything hangs under a customer ("Home" is a customer too). Start with
`list_customers`; for "check customer X" use `get_customer_overview` and, to find improvements,
`optimisation_scan`. "What happened last night" / "what needs attention today": `get_morning_summary`
(the nightly checks of every customer, with what is new and proposals an admin approves with one
click). Every customer has notes for you, a maintenance window and an SLA
(`get_customer_context`, or the resource stackpilot://customers/<id>): read and follow them before
changing anything, and plan restarts and updates inside the maintenance window. Most list tools take
`customer` (name or id) to limit them to one customer. A token can be tied to some customers: it then
only sees their machines. Remember facts the user tells you about a customer with `update_customer`
(append_note). `get_overview` and `list_nodes` list all machines.
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

Backups per customer: `list_backup_coverage` (every guest, server and workstation with its last backup,
age, failures and status against the customer's SLA; start here for "is everything backed up"), then
`save_backup_job` / `run_backup_job` (Proxmox VE jobs), `backup_guest_now`, `set_backup_policy` (SLA,
alerts, exclusions), `list_guest_backups`, `restore_guest` (a new guest by default; overwriting needs the
user's go-ahead), `list_backup_files` + `restore_files` (file level from a Proxmox Backup Server), and
for a PBS `get_pbs`, `save_pbs_job` (verify, offsite sync, prune), `run_pbs_job`, `add_pbs_datastore`,
`add_pbs_remote`; connect a cluster to a PBS datastore with `add_storage` type pbs.

Standalone servers ("Servers" in the app, Linux and Windows with the same agent): `list_servers`,
`diagnose_server` (one-call health check: start here), `find_errors` and `search_logs` across all
machines (journal, Windows Event Log or log files), `read_event_log` (Windows), `check_cve` (installed
version vs the fixed version on every machine), services, `install_node_updates` (apt, dnf or Windows
Update), `run_command` (sh on Linux, PowerShell on Windows) and `copy_data` between machines
(`get_transfer`).

UniFi (Network, Site Manager cloud and Access, per customer): start with `get_unifi_overview` (all
gateways, switches and APs with status, uplink, firmware and clients, WiFi with VLAN, events) or
`list_unifi_connections`. Troubleshooting: `diagnose_wifi_client` ("why can't this laptop get on WiFi"),
`find_firmware_updates`, `check_guest_isolation` ("is guest isolated from the LAN"), `list_unifi_clients`,
`get_unifi_device`, `list_unifi_events` (offline, WAN failover). Doors: `list_doors`, `get_access_log`
("who opened the server room last night"), `list_door_schedules`. Actions (restart a device,
power-cycle a PoE port, guest access, switch an SSID, unlock a door, lock rules) need a key with write
rights, `confirm` and the user's go-ahead; unlocking a door is physical security, always ask.

Workstations (PCs and laptops per customer): `list_workstations` (security status, user, encryption, updates),
`get_workstation` (full inventory: hardware, OS, disks, Defender, BitLocker, firewall, update history),
`diagnose_workstation` ("my PC is slow": start here), `find_software` ("who has TeamViewer installed"),
`refresh_workstation_inventory`, `workstation_action` (Defender scan or signature update, Group Policy refresh,
message to the user) and `log_off_workstation_user`. Workstations are often asleep; offline is normal.
Monthly reports: `get_customer_report` (latest report, or generate=true for a fresh one) and
`send_customer_report` (mails the PDF to the customer's contacts: ask first).

Guardrails (AI gateway): every tool has a risk level (read, operate, change, destructive) and the
admin's policies decide per customer and per token whether a call runs, needs approval or is blocked
(`get_guardrails` shows them; changes outside a customer's maintenance window often need approval).
A call that needs approval answers status "pending_approval" with an `approval_id`: it has NOT run.
Tell the user, do not retry, and check later with `get_approval`; once an admin approves it, the app
runs it and `get_approval` returns the result. An admin can pause all AI actions; calls are then
refused until they resume.

MCP hub: other MCP servers (for example Keystone VoIP or GitHub) are registered in Stackpilot; their tools
appear here as `<prefix>__<tool>` (for example `github__create_issue`, `keystone__list_calls`) and follow
the same guardrails, approvals and audit log. `list_mcp_servers` shows them with their status; a token needs
the `hub` scope. With GitHub you can run a project from A to Z: create the repository, issues, pull
requests, releases and Actions through `github__*`, then deploy it into a container with `save_deployment`
and `run_deployment`. New or changed tools of a server only appear after an admin accepted them.

Runbooks: fixed procedures per customer or for everyone ("patch a server", "new employee", "new project via
GitHub"). `list_runbooks`, then `run_runbook` (with the customer, parameters and a reason) instead of doing the
steps yourself: Stackpilot runs them in order with your token's rights, asks approval per step where the policy or
the runbook says so, waits for the maintenance window and logs everything. Follow it with `get_runbook_run`;
a manual step for the AI waits for `complete_runbook_step`. `save_runbook` creates or changes one (show the user
the steps first).

Automatic recovery (self-healing): rules link a known problem (an alert such as a stopped service, a full
disk, an offline agent or a network adapter without link, or a finding of the nightly checks) to a runbook that Stackpilot runs by itself,
within the policy, with loop protection and an emergency stop. `get_remediation` shows the rules and what
was fixed; `save_remediation_rule` adds one, `pause_remediation` is the emergency stop. Repair tools:
`cleanup_disk`, `recover_agent` and `restart_network_adapter` (see `list_network_adapters`). A Proxmox VE node is never rebooted automatically.

Uptime and SLA: external checks per customer (websites with status, keyword and response time, TCP ports,
ping, DNS records, VPN endpoints), run from Stackpilot or from a machine at the customer. "Is the website of X
up", "what was down last night": `list_uptime_checks` (status=down), `get_uptime_check` (history and incidents),
`run_uptime_check` (check now). `get_sla_report` gives the measured availability per customer and month against
its uptime SLA target (`set_uptime_sla`). `save_uptime_check` adds or changes a check (show the user first),
`delete_uptime_check` removes one.

Tickets (customer portal): customers report problems and questions in their own portal and link them to their
machines; Stackpilot runs a first diagnosis on those machines right away. "What tickets are open", "look at ticket
1042": `list_tickets` (needs_reply = the customer waits for us), `get_ticket` (conversation, customer context and the
diagnosis), `diagnose_ticket` (run it again). `reply_ticket` answers the customer (mailed to them: show the user the
text first and write in the customer's language) or adds an internal note (internal=true); `update_ticket` changes
status, priority, assignee or machines; `create_ticket` opens one for a customer.

Documentation and password vault: every customer has documentation pages (network plans, procedures, notes,
contacts, hardware) and a password vault. Before working on a customer, look for relevant pages with
`list_documents` (query, machine) and read them with `get_document`; keep them up to date with `save_document`
(append=true for a dated change line). Passwords are never in documents: `list_credentials` shows what is in the
vault without secrets. A secret is only used after an admin approved that use: `run_with_credential` (preferred:
the script gets it in $SP_PASSWORD etc. on the machine and it never passes through you) or `reveal_credential`
(answers pending_approval first; call it again once approved). Every request and use is in the vault's access log.
Never repeat a secret to the user or write it into documents, tickets or notes. Store new or changed passwords
with `save_credential`.

Microsoft 365 / Entra ID: customers can have their tenant connected (`list_m365_tenants`). Read users with their
licences, admin roles, MFA and risk (`list_m365_users`, filter admins / no_mfa / risky / stale / disabled_licensed),
one user with recent sign-ins that stood out (`get_m365_user`), licences (`list_m365_licences`) and sign-ins that
stood out (`list_m365_signins`: risky, failed, foreign country, legacy authentication). `refresh_m365` reads the
tenant again. `m365_user_action` blocks or unblocks a user, signs them out everywhere, resets MFA or the password
(the temporary password goes into the customer's vault, never to you) or confirms or dismisses their risk: every
action needs an admin's approval, so give a clear `reason`.

Nodes and servers can be named by id or by name. Every call is written to the audit log with your token's name.
Destructive tools need `confirm` set to the exact node name, or are marked destructive: ask the user
before calling them, and give a short `reason` for commands and service actions."""

mcp = MCPServer(name="Stackpilot", instructions=INSTRUCTIONS, version=get_build_info().version)

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


@tool("updates", WRITE, all_customers=True)
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


@tool("updates", DESTRUCTIVE, all_customers=True)
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
@tool("read", RO, all_customers=True)
async def list_backup_targets() -> list[dict]:
    """Remote backup locations (FTP, FTPS, SFTP, SCP) with their schedules. Secrets are never returned."""
    from mmn.services.backup.targets import list_targets, target_out

    async with sessionmaker()() as db:
        out = [target_out(t) for t in await list_targets(db)]
    return _jsonable(out)


@tool("read", RO, all_customers=True)
async def list_backup_runs(limit: int = 20) -> list[dict]:
    """Recent backup and restore runs with status, size and errors."""
    from mmn.services.backup.targets import list_runs

    async with sessionmaker()() as db:
        runs = await list_runs(db, max(1, min(limit, 200)))
    for r in runs:
        r.pop("log", None)
    return _jsonable(runs)


@tool("read", RO, all_customers=True)
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


@tool("backups", WRITE, all_customers=True)
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


@tool("restore", DESTRUCTIVE, all_customers=True)
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


@tool("settings", WRITE, all_customers=True)
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


@tool("settings", WRITE, all_customers=True)
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
@tool("read", RO, all_customers=True)
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
async def get_overview(customer: str | None = None) -> dict:
    """Environments, all machines with an agent (Proxmox VE nodes and standalone Linux/Windows
    servers: online, CPU/RAM/disk, pending updates, agent version) and totals. `customer` (name
    or id) limits it to one customer. `list_servers` gives the standalone servers in more detail."""
    from mmn.api.proxmox import overview
    from mmn.services import nodes

    async with sessionmaker()() as db:
        env_id = None
        if customer:
            try:
                env_id = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        out = await overview(env_id, db, None, kind=None)
    return _jsonable(out)


@tool("read", RO)
async def list_customers() -> list[dict]:
    """All customers you may see (Home is a customer too): status (ok, attention, empty), machines
    online, Proxmox VE nodes, servers and workstations, pending updates, open alerts, UniFi devices
    and firmware updates, maintenance window and SLA. Start here when the user names a customer."""
    from mmn.services import customers

    async with sessionmaker()() as db:
        rows = await customers.summaries(db)
    keep = ("id", "name", "kind", "description", "status", "machines", "online", "counts", "updates", "alerts",
            "unifi", "clusters", "maintenance_window", "sla", "contact_name")
    return _jsonable([{k: r.get(k) for k in keep} | {"has_notes": bool(r.get("notes"))} for r in rows])


@tool("read", RO)
async def get_customer_overview(customer: str) -> dict:
    """Everything at one customer in one call: the customer context (notes for you, maintenance
    window, SLA, contacts), every machine (Proxmox VE nodes, servers, workstations) with status,
    OS, CPU/RAM, pending updates and what needs attention, UniFi connections, open alerts and totals.
    Read the notes and respect the maintenance window before changing anything."""
    from mmn.services import customers, nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        out = await customers.overview(db, e)
        out["context"] = customers.context_dict(e)
        out.pop("ai_access", None)
    return _jsonable(out)


@tool("read", RO)
async def optimisation_scan(customer: str, live_network_checks: bool = True) -> dict:
    """"What can be optimised at customer X": one call that checks capacity (memory per node and the
    balance inside a cluster, with a guest to move), storage and SMART health, disks of servers and
    containers, pending updates and reboots, old agents, failed services, stopped guests, UniFi
    devices offline, firmware updates and (live) whether the guest WiFi is isolated from the LAN.
    Returns findings by severity with a suggestion and the tool to use next. Present them to the
    user and ask before acting; plan changes in the maintenance window."""
    from mmn.services import customers, nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        return _jsonable(await customers.optimisation_scan(db, e, live=live_network_checks))


@tool("read", RO)
async def get_morning_summary(customer: str | None = None, only_new: bool = False) -> dict:
    """The morning summary of the nightly checks: per customer the findings of last night (updates,
    security updates with the CVEs they fix, full disks, certificates that expire, failed or missing
    backups and the optimisation scan), what is new since the night before, what was resolved, and
    the proposals that wait for one click in Stackpilot (approval requests; changes wait for the
    customer's maintenance window). Tell the user what needs attention; an admin approves the
    proposals in the app (Morning summary or Approvals), not you."""
    from mmn.services import nightly, nodes

    async with sessionmaker()() as db:
        cid = None
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
        r = await nightly.latest(db, finished=True)
        if r is None:
            return {"run": None, "running": nightly.running(),
                    "note": "The nightly checks have not run yet. Use run_nightly_checks to run them now."}
        d = await nightly.detail(db, r)
        url = await nightly.summary_url(db)
    if cid:
        d["customers"] = [c for c in d["customers"] if c.get("id") == cid]
        d["findings"] = [f for f in d["findings"] if f["customer_id"] == cid]
    if only_new:
        d["findings"] = [f for f in d["findings"] if f["is_new"]]
    names = {c["id"]: c.get("name") for c in d["customers"]}
    for f in d["findings"]:
        f["customer"] = names.get(f["customer_id"])
    d["url"] = url
    d["running"] = nightly.running()
    return _jsonable(d)


@tool("read", WRITE)
async def run_nightly_checks(customer: str | None = None) -> dict:
    """Run the nightly checks now (normally they run every night), for all customers or one. They
    refresh the update lists and the backup coverage, check certificates and look up CVEs, and
    create proposals that an admin approves. Takes a few minutes; then read get_morning_summary."""
    from mmn.services import nightly, nodes, tenancy

    ids = None
    async with sessionmaker()() as db:
        if customer:
            try:
                ids = [(await nodes.get_environment(db, customer)).id]
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
    allowed = tenancy.allowed()
    if ids is None and allowed is not None:
        ids = sorted(allowed)
    p = current_principal()
    try:
        rid = await nightly.start(trigger="mcp", requested_by=p.username if p else None, customer_ids=ids)
    except nightly.NightlyBusy as ex:
        raise ToolDenied(str(ex)) from ex
    return {"run_id": rid, "status": "running",
            "message": "The checks run in the background; call get_morning_summary in a few minutes."}


@tool("read", RO)
async def get_customer_context(customer: str) -> dict:
    """The customer's notes for you, maintenance window, SLA and contacts (also available as the MCP
    resource stackpilot://customers/<id>). Follow the notes, e.g. "never restart X during office hours"."""
    from mmn.services import customers, nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        from mmn.services import docs, m365

        pages = await docs.search(db, customer_id=e.id, ai_only=True, limit=50)
        return _jsonable({"id": e.id, **customers.context_dict(e), "markdown": customers.context_markdown(e),
                          "documentation": [{"id": d.id, "title": d.title, "category": d.category, "pinned": d.pinned}
                                            for d in pages],
                          "microsoft_365": [m365.overview(t) for t in await m365.tenants_of(db, e.id)]})


@tool("nodes", WRITE, all_customers=True)
async def create_customer(name: str, kind: str = "customer", description: str | None = None,
                          contact_name: str | None = None, contact_email: str | None = None,
                          contact_phone: str | None = None, maintenance_window: str | None = None,
                          sla: str | None = None, notes: str | None = None) -> dict:
    """Create a customer. kind: 'home' (your own environment) or 'customer'. maintenance_window
    e.g. 'Tue 22:00-02:00'. notes: guidance for the AI and the team."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.create_environment(db, nodes.EnvironmentIn(
                name=name, kind=kind, description=description, contact_name=contact_name,
                contact_email=contact_email, contact_phone=contact_phone, maintenance_window=maintenance_window,
                sla=sla, notes=notes))
        except (nodes.NodeError, ValueError) as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        return _jsonable(nodes.env_out(e, []))


@tool("nodes", WRITE)
async def update_customer(customer: str, description: str | None = None, contact_name: str | None = None,
                          contact_email: str | None = None, contact_phone: str | None = None,
                          maintenance_window: str | None = None, sla: str | None = None,
                          notes: str | None = None, append_note: str | None = None) -> dict:
    """Change a customer's details. Only the fields you pass change. `append_note` adds a line to the
    notes (e.g. something the user asked you to remember about this customer); `notes` replaces them."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
            cur = nodes.env_out(e)
            new_notes = notes if notes is not None else cur["notes"]
            if append_note and append_note.strip():
                new_notes = f"{new_notes.rstrip()}\n{append_note.strip()}" if new_notes else append_note.strip()
            body = nodes.EnvironmentIn(**{
                "name": e.name, "kind": e.kind, "color": e.color, "contacts": cur["contacts"],
                "description": description if description is not None else cur["description"],
                "contact_name": contact_name if contact_name is not None else cur["contact_name"],
                "contact_email": contact_email if contact_email is not None else cur["contact_email"],
                "contact_phone": contact_phone if contact_phone is not None else cur["contact_phone"],
                "maintenance_window": maintenance_window if maintenance_window is not None else cur["maintenance_window"],
                "sla": sla if sla is not None else cur["sla"], "notes": new_notes})
            await nodes.update_environment(db, e, body)
        except (nodes.NodeError, ValueError) as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        return _jsonable(nodes.env_out(e))


@tool("read", RO)
async def list_nodes(customer: str | None = None) -> list[dict]:
    """Nodes with status, metrics, cluster, agent version and number of pending updates."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        env_id = (await nodes.get_environment(db, customer)).id if customer else None
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
async def create_pairing_code(customer: str, label: str | None = None, role: str = "server") -> dict:
    """Create a one-time pairing code (valid 24 hours) for a customer and the install command to run on
    a new machine. role: 'server' or 'workstation' (a PC or laptop) for Linux/Windows machines."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            env = await nodes.get_environment(db, customer)
            p = current_principal()
            pc, code = await nodes.create_pairing_code(db, env, label=label, created_by=p.username if p else None,
                                                       role=role)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        info = await nodes.connect_info(db)
    return _jsonable({"code": code, "expires_at": pc.expires_at, "customer": env.name,
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
    """Install all pending updates on a node or server: apt dist-upgrade (Debian, Ubuntu, Proxmox
    VE), dnf upgrade (RHEL family) or Windows Update. Set `confirm` to the machine's name. Returns a
    job; follow it with get_node_job. Windows may need a reboot afterwards (reboot_node)."""
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
    """Reboot a node or server (Linux or Windows). On Proxmox VE running guests are handled by its
    own shutdown policy. Set `confirm` to the machine's name. Always ask the user first."""
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
async def get_customer_notifications(customer: str) -> dict:
    """The Proxmox VE notification mail settings of a customer (password never returned)."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            return _jsonable(nodes.notify_public(await nodes.get_environment(db, customer)))
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("nodes", WRITE)
async def update_customer_notifications(customer: str, enabled: bool, mailto: list[str],
                                        types: list[str] | None = None, severities: list[str] | None = None,
                                           use_app_smtp: bool = True) -> dict:
    """Set who gets Proxmox VE notifications for a customer and push them to its nodes.
    types: vzdump, replication, fencing, package-updates, system-mail. severities: info, notice,
    warning, error, unknown. use_app_smtp sends through the app's own mail server; a different SMTP
    server can only be entered in the web interface."""
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
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
    """Read the systemd journal of a Linux machine, or the Windows Event Log (System + Application)
    of a Windows server. unit: e.g. 'nginx' or 'pveproxy' (on Windows: the event source, e.g.
    'Service Control Manager'). since/until: '1 hour ago', '2 days ago', 'today' or
    '2026-11-22 09:00'. priority: emerg, alert, crit, err, warning, notice, info or debug (that level
    and worse). grep: case-insensitive text/regex filter. Max 5000 lines."""
    return _jsonable(await _op(node, "logs.journal", {"unit": unit or "", "since": since or "", "until": until or "",
                                                      "priority": priority or "", "grep": grep or "",
                                                      "lines": lines, "boot": this_boot}))


@tool("logs", RO)
async def list_services(node: str, state: str | None = None) -> list[dict]:
    """Services on a node or server (systemd, or Windows services). state: 'failed' or 'running' to
    filter. On Windows 'failed' means an automatic service that stopped with an error."""
    return _jsonable(await _op(node, "services.list", {"state": state or ""}))


@tool("logs", RO)
async def get_service_status(node: str, unit: str) -> dict:
    """Status of one service: `systemctl status` plus properties on Linux; state, start mode, account,
    path and recent Service Control Manager events on Windows."""
    return _jsonable(await _op(node, "services.status", {"unit": unit}))


@tool("services", DESTRUCTIVE)
async def manage_service(node: str, unit: str, action: str, reason: str) -> dict:
    """start, stop, restart, reload, enable or disable a service (systemd or Windows; on Windows
    enable = automatic start, no reload). `reason` is stored in the audit log. Ask the user before
    stopping or restarting production services."""
    if not reason.strip():
        raise ToolDenied("give a reason")
    return _jsonable(await _op(node, "services.action", {"unit": unit, "action": action}))


@tool("logs", RO)
async def find_packages(pattern: str, customer: str | None = None) -> list[dict]:
    """Which machines have a package installed and which version, e.g. 'nginx*' or 'openssl':
    dpkg/rpm/apk packages on Linux, installed programs on Windows ('*sql server*'), hotfixes with
    'KB5*'. Shows whether an update is pending for it. Offline machines are listed without data.
    To mark versions as vulnerable or fixed for a CVE use `check_cve`."""
    from mmn.services import nodes

    env_id = None
    if customer:
        async with sessionmaker()() as db:
            try:
                env_id = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
    return _jsonable(await nodes.find_packages(pattern, env_id))


@tool("logs", RO)
async def read_file(node: str, path: str, max_bytes: int = 65536, tail: bool = False) -> dict:
    """Read a text file (or list a directory) on a machine, e.g. /etc/nginx/nginx.conf,
    /var/log/syslog with tail=true, or C:\\inetpub\\logs\\LogFiles on Windows. At most 1 MiB."""
    return _jsonable(await _op(node, "file.read", {"path": path, "max_bytes": max_bytes, "tail": tail}))


@tool("exec", DESTRUCTIVE)
async def run_command(node: str, command: str, reason: str, timeout: int = 60) -> dict:
    """Run a command on a machine and return exit code, stdout and stderr (max 256 KiB each): /bin/sh
    as root on Linux, Windows PowerShell 5.1 as SYSTEM on Windows. `reason` is stored in the audit
    log with the command and its output. Prefer the read-only tools when they suffice, and ask the
    user before changing anything. timeout: seconds, max 1800."""
    if not reason.strip():
        raise ToolDenied("give a reason")
    return _jsonable(await _op(node, "exec", {"command": command, "timeout": max(1, min(timeout, 1800))}))


# ------------------------------------------------------------------ standalone servers + troubleshooting
async def _targets(servers: list[str] | None, customer: str | None, os: str | None):
    from mmn.services import nodes
    from mmn.services import servers as srv

    async with sessionmaker()() as db:
        try:
            return await srv.resolve_targets(db, servers, customer, os)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("read", RO)
async def list_servers(customer: str | None = None, os: str | None = None) -> dict:
    """Standalone Linux and Windows servers with an agent (not the Proxmox VE nodes): OS and version,
    online, CPU/RAM/disks, failed services, pending updates and what needs attention (offline,
    failed_services, disk_full, memory, reboot_required). os: 'linux' or 'windows'."""
    from mmn.services import nodes
    from mmn.services import servers as srv

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable(await srv.overview(db, env_id, os))


@tool("logs", RO)
async def diagnose_server(server: str) -> dict:
    """Health check of one machine in a single call: findings (full disks, memory, load, failed
    services, clock, pending updates/reboot), CPU/RAM/disks, failed services, error log lines of the
    last hour, busiest processes (CPU and memory), listening ports and time sync. Start here when
    something "doesn't work" on a server, then dig deeper with read_logs, get_service_status or
    search_logs."""
    from mmn.services import servers as srv

    nid, _ = await _node_id(server)
    try:
        return _jsonable(await srv.diagnose(nid))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("logs", RO)
async def search_logs(pattern: str, servers: list[str] | None = None, customer: str | None = None,
                      os: str | None = None, since: str = "24 hours ago", source: str = "auto",
                      unit: str | None = None, priority: str | None = None, paths: list[str] | None = None,
                      lines: int = 100, log: str | None = None) -> list[dict]:
    """Search the logs of many machines at once (all machines when `servers` and `customer` are
    empty; os: linux, windows or proxmox). pattern: case-insensitive regex, e.g. 'postfix.*reject' or
    'disk full|No space left'. source: auto (systemd journal on Linux, Event Log on Windows),
    journal, eventlog or files (text log files: `paths` such as /var/log/nginx/*.log or
    C:\\inetpub\\logs\\LogFiles; /var/log when empty). since: '1 hour ago', '2 days ago', 'today'.
    unit: systemd unit or Windows event source. priority: err, warning, ... log: Windows log names
    (e.g. 'System,Application,Security'). lines: max results per machine."""
    from mmn.services import nodes
    from mmn.services import servers as srv

    targets = await _targets(servers, customer, os)
    try:
        res = await srv.search_logs(targets, pattern, since=since, source=source, unit=unit, priority=priority,
                                    paths=paths, lines=lines, log_name=log)
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    return _jsonable(res)


@tool("logs", RO)
async def read_event_log(server: str, log: str = "System,Application", level: str | None = None,
                         since: str = "24 hours ago", source: str | None = None, grep: str | None = None,
                         max_events: int = 100) -> list[dict]:
    """Windows Event Log entries of one Windows server as structured data (time, log, level,
    provider, event id, message). log: comma separated, e.g. 'System', 'Application', 'Security' or
    'Microsoft-Windows-TaskScheduler/Operational'. level: critical, error, warning or information
    (that level and worse). source: event provider, e.g. 'Service Control Manager' or 'MSSQLSERVER'."""
    return _jsonable(await _op(server, "logs.events", {"log": log, "priority": level or "", "since": since,
                                                       "unit": source or "", "grep": grep or "",
                                                       "lines": max(1, min(max_events, 2000))}))


@tool("logs", RO)
async def find_errors(servers: list[str] | None = None, customer: str | None = None, os: str | None = None,
                      since: str = "1 hour ago") -> list[dict]:
    """What is wrong where: per machine the failed services, the number of error log lines since
    `since` with the most recent ones, full disks and other findings, most broken machines first.
    Covers all machines when `servers` and `customer` are empty."""
    from mmn.services import servers as srv

    return _jsonable(await srv.find_errors(await _targets(servers, customer, os), since))


@tool("logs", RO)
async def check_cve(package: str, fixed_version: str | None = None, servers: list[str] | None = None,
                    customer: str | None = None, os: str | None = None) -> list[dict]:
    """Check a vulnerable package on all machines (or the given ones): the installed version per
    machine and, with `fixed_version` (the first fixed version from the advisory or the
    distribution's security tracker, e.g. '1.26.3-3' for Debian's nginx), whether it is still
    vulnerable and whether a pending update fixes it. package: name or glob ('nginx*', 'openssl',
    '*7-zip*' on Windows). Most affected machines first. Install fixes with install_node_updates."""
    from mmn.services import servers as srv

    return _jsonable(await srv.check_cve(await _targets(servers, customer, os), package, fixed_version))


@tool("copy", DESTRUCTIVE)
async def copy_data(source: str, paths: list[str], target: str, target_path: str, reason: str,
                    overwrite: bool = False) -> dict:
    """Copy files or folders from one machine to another (Linux and Windows, also between them),
    relayed through the app over the agents' encrypted connections; nothing is opened between the
    servers. paths: absolute paths on the source (max 20); each one is placed inside `target_path`
    (created when missing), like `cp -a <path> <target_path>/`. Owners, modes and times are kept on
    Linux. overwrite=false stops at the first existing file. `reason` is stored in the audit log.
    Returns a transfer; follow it with get_transfer. Ask the user before overwriting anything."""
    from mmn.services import nodes
    from mmn.services import servers as srv

    if not reason.strip():
        raise ToolDenied("give a reason")
    p = current_principal()
    try:
        t = await srv.start_transfer(source, paths, target, target_path, overwrite=overwrite, reason=reason,
                                     origin="mcp", requested_by=p.username if p else None)
    except (nodes.NodeError, Exception) as e:
        raise _agent_errors(e) from e
    return _jsonable(srv.transfer_out(t))


@tool("read", RO)
async def get_transfer(transfer_id: str) -> dict:
    """Status of a data copy (stage send/receive/done, files, bytes, error) and the output of its
    two jobs (source and target)."""
    from mmn.models import NodeJob, Transfer
    from mmn.services import servers as srv

    async with sessionmaker()() as db:
        t = await db.get(Transfer, transfer_id)
        if t is None:
            raise ToolDenied("transfer not found")
        out = srv.transfer_out(t)
        for key in ("send_job_id", "receive_job_id"):
            if t and getattr(t, key):
                j = await db.get(NodeJob, getattr(t, key))
                if j is not None:
                    out[key.replace("_id", "_output")] = (j.output or "")[-4000:]
    return _jsonable(out)


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
async def list_guests(customer: str | None = None, node: str | None = None, type: str | None = None,
                      query: str | None = None) -> list[dict]:
    """VMs (type 'qemu') and containers (type 'lxc') on all Proxmox VE nodes: VMID, name, status,
    node, CPU/memory/disk use, tags. Filter by customer, node, type or a text `query` (name,
    VMID or tag). Containers made by community-scripts.org have the tag 'community-script'."""
    from mmn.services import guests, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
            node_id = (await nodes.get_node(db, node)).id if node else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable(await guests.list_guests(db, environment_id=env_id, node_id=node_id, type_=type,
                                                  query=query))


@tool("read", RO)
async def get_guest(guest: str, node: str | None = None) -> dict:
    """Details of one VM or container (by VMID or name): configuration, snapshots and, when
    running, its IP addresses. Give `node` when the VMID exists in more than one customer."""
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
async def suggest_node(customer: str | None = None) -> dict:
    """The least loaded online Proxmox VE node (memory 60 %, CPU 40 %), with the ranking."""
    from mmn.services import guests, nodes

    async with sessionmaker()() as db:
        env_id = None
        if customer:
            try:
                env_id = (await nodes.get_environment(db, customer)).id
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


@tool("templates", WRITE, all_customers=True)
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


@tool("templates", WRITE, all_customers=True)
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
@tool("read", RO, all_customers=True)
async def list_scheduled_tasks() -> list[dict]:
    """Scheduled tasks (updates in containers, node updates, template sync, custom commands) with
    schedule, targets, options, last and next run."""
    from sqlalchemy import select

    from mmn.models import ScheduledTask
    from mmn.services import tasks

    async with sessionmaker()() as db:
        rows = (await db.execute(select(ScheduledTask).order_by(ScheduledTask.name))).scalars().all()
        return _jsonable([tasks.task_out(t) for t in rows])


@tool("tasks", WRITE, all_customers=True)
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
    "command_windows": "...", "run_on": "ct"|"node", "timeout": 3600}. For custom_command on
    machines (run_on "node"), `command` is a bash script for Linux machines and `command_windows` a
    PowerShell script for Windows servers and workstations (it runs as LocalSystem); a machine only
    runs the script of its own OS. mail_on: always, failure or never."""
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


@tool("tasks", DESTRUCTIVE, all_customers=True)
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


@tool("tasks", WRITE, all_customers=True)
async def run_scheduled_task(task_id: str) -> dict:
    """Run a scheduled task now. Returns a run id; follow it with get_task_run."""
    from mmn.services import tasks

    p = current_principal()
    try:
        return {"run_id": await tasks.run_task(task_id, trigger="mcp", requested_by=p.username if p else None)}
    except tasks.NodeError as e:
        raise ToolDenied(str(e)) from e


@tool("read", RO, all_customers=True)
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
async def get_bridge_map(customer: str | None = None, node: str | None = None) -> dict:
    """Bridges (vmbr) and SDN VNets with the VMs and containers on them, grouped by VLAN tag:
    VMID, name, node, NIC, MAC, firewall flag, trunks. Use it to answer 'what is on VLAN 20' or
    'which guests use vmbr1'."""
    from mmn.services import network, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
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
                            guest: str | None = None, group: str | None = None) -> list[dict] | dict:
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
                               group: str | None = None) -> list[dict] | dict:
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
                          mailto: list[str] | None = None, enabled: bool = True, job_id: str | None = None,
                          keep_monthly: int | None = None, comment: str = "") -> dict:
    """Create (or with job_id change) a Proxmox VE backup job: storage, schedule (systemd calendar,
    e.g. 'daily 02:00', 'sun 01:00'), all guests or vmids, mode snapshot/suspend/stop, retention."""
    from mmn.services import cluster

    nid, _ = await _pve(node)
    try:
        body = cluster.BackupJobIn(storage=storage, schedule=schedule, all=all, vmids=vmids or [], mode=mode,
                                   keep_last=keep_last, keep_daily=keep_daily, keep_weekly=keep_weekly,
                                   keep_monthly=keep_monthly, mailto=mailto or [], enabled=enabled, comment=comment)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(cluster.save_backup_job(nid, body, job_id))


@tool("cluster", WRITE)
async def run_backup_job(node: str, job_id: str) -> list[dict] | dict:
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


# ------------------------------------------------------------------ backup management per customer (phase 11)
async def _customer_env(customer: str):
    from mmn.services import nodes

    async with sessionmaker()() as db:
        try:
            return await nodes.get_environment(db, customer)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("read", RO)
async def list_backup_coverage(customer: str, refresh: bool = False, problems_only: bool = False) -> dict:
    """Backup coverage of a customer: every Proxmox VE guest, server and workstation with its status
    (ok, stale, failed, no_job, never, none, external, backup_server, excluded, not_monitored), last
    backup, age in hours, size, the jobs that cover it, the storages and the last error; plus the
    backup jobs, backup storages, Proxmox Backup Servers and the customer's backup policy (SLA).
    Items carry a `key` for set_backup_policy (exclude). `refresh` collects it again now (up to a
    minute); otherwise it is at most 30 minutes old. `problems_only` leaves out the good items."""
    from mmn.services import nodes, protection

    e = await _customer_env(customer)
    if refresh:
        v = await _call(protection.collect(e.id, force=True))
    else:
        async with sessionmaker()() as db:
            e = await nodes.get_environment(db, e.id)
            v = protection.view(e, await protection.stored(db, e))
    if problems_only:
        v["guests"] = [g for g in v["guests"] if g["status"] in protection.PROBLEMS]
        v["machines"] = [m for m in v["machines"] if m["status"] in protection.PROBLEMS]
    if not v["collected_at"]:
        v["note"] = "Not collected yet: call again with refresh=true."
    return _jsonable(v)


@tool("cluster", WRITE)
async def set_backup_policy(customer: str, max_age_hours: int | None = None, alert_failed: bool | None = None,
                            alert_missing: bool | None = None, workstations: bool | None = None,
                            recipients: list[str] | None = None, exclude: list[str] | None = None,
                            include: list[str] | None = None) -> dict:
    """Change a customer's backup policy: the SLA as the maximum age of the last good backup in hours
    (default 26), alerts for failed and for missing backups, whether workstations count, extra alert
    recipients (besides the monitoring recipients), and items to `exclude` from (or `include` again
    in) the coverage and alerts, by their key from list_backup_coverage (e.g. guest:pve-cl:105)."""
    from mmn.services import nodes, protection

    e = await _customer_env(customer)
    async with sessionmaker()() as db:
        e = await nodes.get_environment(db, e.id)
        cur = protection.policy_of(e)
        keys = (set(cur["exclude"]) | set(exclude or [])) - set(include or [])
        try:
            body = protection.PolicyIn(
                max_age_hours=max_age_hours if max_age_hours is not None else cur["max_age_hours"],
                alert_failed=cur["alert_failed"] if alert_failed is None else alert_failed,
                alert_missing=cur["alert_missing"] if alert_missing is None else alert_missing,
                workstations=cur["workstations"] if workstations is None else workstations,
                recipients=cur["recipients"] if recipients is None else recipients, exclude=sorted(keys))
        except ValueError as ex:
            raise ToolDenied(str(ex)) from ex
        res = await protection.save_policy(db, e, body)
        await db.commit()
    return _jsonable(res)


@tool("read", RO)
async def list_guest_backups(guest: str, node: str | None = None) -> list[dict]:
    """All backups of one VM or container on every backup storage of its cluster (Proxmox Backup
    Server included), newest first: volume id, time, size, storage, verification state, notes.
    Use the volume id with restore_guest or list_backup_files."""
    from mmn.services import restore

    n, g = await _guest(guest, node)
    async with sessionmaker()() as db:
        from mmn.services import nodes

        nn = await nodes.get_node(db, n.id)
        return await _call(restore.guest_backups(db, nn, g["vmid"]))


@tool("cluster", WRITE)
async def backup_guest_now(guest: str, storage: str, node: str | None = None, mode: str = "snapshot",
                           protected: bool = False) -> dict:
    """Make a one-off backup of a VM or container now (vzdump to `storage`, a backup storage of its
    cluster), outside the backup jobs; `protected` keeps it from being pruned. Follow the job with
    get_node_job."""
    from mmn.services import nodes, restore

    n, g = await _guest(guest, node)
    try:
        body = restore.BackupNowIn(vmid=g["vmid"], storage=storage, mode=mode, protected=protected)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    p = current_principal()
    async with sessionmaker()() as db:
        nn = await nodes.get_node(db, n.id)
        job = await _call(restore.backup_now(db, nn, body, source="mcp", requested_by=p.username if p else None))
    return job if isinstance(job, dict) else _jsonable(nodes.job_out(job, with_output=False))


@tool("restore_guests", DESTRUCTIVE)
async def restore_guest(node: str, volume: str, confirm: str, vmid: int | None = None, storage: str | None = None,
                        overwrite: bool = False, start: bool = False, unique: bool = False) -> dict:
    """Restore a backup (volume id from list_guest_backups or list_backup_coverage) as a VM or
    container on `node`. By default as a NEW guest with a free VMID (or `vmid`) next to the original;
    `overwrite=true` replaces the existing guest with that VMID (it must be stopped). A container
    needs `storage` for its disks unless it is overwritten; `unique` gives a copy new MAC addresses.
    Set `confirm` to the VMID that is overwritten, or to 'new' for a new guest. Always ask the user
    first; the AI gateway asks an admin's approval by default. Follow the job with get_node_job."""
    from mmn.services import nodes, restore

    try:
        body = restore.RestoreIn(volume=volume, vmid=vmid, storage=storage, overwrite=overwrite, start=start,
                                 unique=unique)
        expected = str(vmid or restore.volume_vmid(volume) or "") if overwrite else "new"
    except (ValueError, nodes.NodeError) as e:
        raise ToolDenied(str(e)) from e
    if str(confirm).strip().lower() != expected.lower():
        raise ToolDenied(f"confirm must be '{expected}'")
    nid, _ = await _pve(node)
    p = current_principal()
    async with sessionmaker()() as db:
        n = await nodes.get_node(db, nid)
        try:
            job, info = await restore.restore_guest(db, n, body, source="mcp", requested_by=p.username if p else None)
        except Exception as e:  # noqa: BLE001
            raise _agent_errors(e) from e
    return _jsonable({"job": nodes.job_out(job, with_output=False), **info})


@tool("restore_guests", RO)
async def list_backup_files(node: str, volume: str, path: str = "/") -> dict:
    """Browse the files inside a backup on a Proxmox Backup Server (file-level restore), through the
    Proxmox VE `node`. Start at '/': a VM backup shows its disks and partitions first, a container
    backup its file system. Then restore_files with the path of a file or folder."""
    from mmn.services import nodes, restore

    nid, _ = await _pve(node)
    async with sessionmaker()() as db:
        n = await nodes.get_node(db, nid)
    return await _call(restore.list_files(n, volume, path))


@tool("restore_guests", WRITE)
async def restore_files(node: str, volume: str, path: str, target_dir: str | None = None) -> dict:
    """Extract a file or folder from a Proxmox Backup Server backup (path from list_backup_files)
    into a new folder on the Proxmox VE `node` (default /var/lib/stackpilot-restore/<time>); nothing
    existing is overwritten. Move it to the machine that needs it with copy_data. Follow the job
    with get_node_job."""
    from mmn.services import nodes, restore

    try:
        body = restore.FileRestoreIn(volume=volume, path=path, target_dir=target_dir)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    nid, _ = await _pve(node)
    p = current_principal()
    async with sessionmaker()() as db:
        n = await nodes.get_node(db, nid)
    try:
        job, target = await restore.restore_files(n, body, source="mcp", requested_by=p.username if p else None)
    except Exception as e:  # noqa: BLE001
        raise _agent_errors(e) from e
    return _jsonable({"job": nodes.job_out(job, with_output=False), "target": target})


async def _pbs_server(server: str) -> str:
    from mmn.services import agent_hub, nodes

    async with sessionmaker()() as db:
        try:
            n = await nodes.get_node(db, server)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        if n.kind != "linux":
            raise ToolDenied("a Proxmox Backup Server is a Linux server with the agent")
        if not agent_hub.is_online(n.id):
            raise ToolDenied(f"the agent of {n.name} is not connected")
        return n.id


@tool("read", RO)
async def get_pbs(server: str) -> dict:
    """A Proxmox Backup Server (a Linux server with the agent): version, certificate fingerprint (for
    add_storage type pbs), datastores with usage and garbage collection, verify jobs, sync jobs
    (offsite copies), prune jobs (retention) and remotes."""
    from mmn.services import pbs

    return await _call(pbs.overview(await _pbs_server(server)))


@tool("cluster", WRITE)
async def save_pbs_job(server: str, kind: str, job_id: str, store: str, schedule: str | None = None,
                       remote: str | None = None, remote_store: str | None = None, direction: str = "pull",
                       remove_vanished: bool = False, ignore_verified: bool = True, outdated_after: int | None = 30,
                       keep_last: int | None = None, keep_daily: int | None = None, keep_weekly: int | None = None,
                       keep_monthly: int | None = None, keep_yearly: int | None = None, ns: str | None = None,
                       comment: str | None = None) -> dict:
    """Create or change a job on a Proxmox Backup Server. kind 'verify' (check backups, e.g.
    schedule 'sat 03:00', re-verify after `outdated_after` days), 'sync' (offsite copy: pull
    `remote_store` of `remote` into `store`, or direction 'push' on PBS 3.3+) or 'prune'
    (retention: keep_last/daily/weekly/monthly/yearly, needs a schedule)."""
    from mmn.services import pbs

    try:
        body = pbs.PbsJobIn(kind=kind, id=job_id, store=store, schedule=schedule, remote=remote,
                            remote_store=remote_store, direction=direction, remove_vanished=remove_vanished,
                            ignore_verified=ignore_verified, outdated_after=outdated_after, keep_last=keep_last,
                            keep_daily=keep_daily, keep_weekly=keep_weekly, keep_monthly=keep_monthly,
                            keep_yearly=keep_yearly, ns=ns, comment=comment)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(pbs.save_job(await _pbs_server(server), body))


@tool("cluster", WRITE)
async def run_pbs_job(server: str, kind: str, job_id: str) -> dict:
    """Run a verify, sync or prune job of a Proxmox Backup Server now (it runs as a PBS task)."""
    from mmn.services import pbs

    return await _call(pbs.run_job(await _pbs_server(server), kind, job_id))


@tool("cluster", DESTRUCTIVE)
async def delete_pbs_job(server: str, kind: str, job_id: str, confirm: str) -> dict:
    """Remove a verify, sync or prune job from a Proxmox Backup Server (backups stay). Set `confirm`
    to the job id."""
    from mmn.services import pbs

    if confirm != job_id:
        raise ToolDenied("confirm must equal the job id")
    return await _call(pbs.delete_job(await _pbs_server(server), kind, job_id))


@tool("cluster", WRITE)
async def add_pbs_datastore(server: str, name: str, path: str, gc_schedule: str = "daily") -> dict:
    """Create a datastore on a Proxmox Backup Server in an existing, dedicated directory or mount
    point `path` (for example /mnt/datastore/backup)."""
    from mmn.services import pbs

    try:
        body = pbs.DatastoreIn(name=name, path=path, gc_schedule=gc_schedule)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(pbs.add_datastore(await _pbs_server(server), body))


@tool("cluster", WRITE)
async def add_pbs_remote(server: str, name: str, host: str, auth_id: str, password: str,
                         fingerprint: str | None = None, port: int | None = None) -> dict:
    """Add a remote (another Proxmox Backup Server, usually offsite) to a PBS, for sync jobs.
    `auth_id` like sync@pbs or sync@pbs!token, `password` the password or token secret."""
    from mmn.services import pbs

    try:
        body = pbs.RemoteIn(name=name, host=host, auth_id=auth_id, password=password, fingerprint=fingerprint,
                            port=port)
    except ValueError as e:
        raise ToolDenied(str(e)) from e
    return await _call(pbs.add_remote(await _pbs_server(server), body))


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
async def get_monitoring(range: str = "24h", customer: str | None = None) -> dict:
    """All nodes with CPU/RAM/disk now and as a trend (range 1h, 24h, 7d or 30d), storages, disk
    health (SMART) and the number of open alerts. Totals over all nodes."""
    from mmn.services import monitoring, nodes

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
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
async def list_alerts(open_only: bool = True, limit: int = 50, customer: str | None = None) -> list[dict]:
    """Monitoring alerts (node offline, CPU, memory, disk, storage full, SMART) newest first.
    `customer` limits them to one customer's machines."""
    from mmn.services import monitoring, nodes

    async with sessionmaker()() as db:
        rows = await monitoring.list_alerts(db, open_only=open_only, limit=limit if not customer else 500)
        if customer:
            try:
                ids = {n.id for n in (await nodes.get_environment(db, customer)).nodes}
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
            rows = [a for a in rows if a["node_id"] in ids][:limit]
        return _jsonable(rows)


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


@tool("deploy", RO, all_customers=True)
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
# ------------------------------------------------------------------ UniFi (phase 6)
async def _uconn(connection: str | None, kind: str | tuple[str, ...]):
    """The connection named (id or name), or the only one of that kind."""
    from mmn.services.unifi import service as uf
    from mmn.services.unifi.transport import UnifiError

    kinds = (kind,) if isinstance(kind, str) else kind
    async with sessionmaker()() as db:
        try:
            if connection:
                return await uf.get_connection(db, connection, kinds)
            rows = [c for c in await uf.list_connections(db) if c.kind in kinds]
        except UnifiError as e:
            raise ToolDenied(str(e)) from e
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ToolDenied("no UniFi connection of this kind; add one under UniFi → Connections")
    raise ToolDenied("several UniFi connections, name one: " + ", ".join(c.name for c in rows))


def _uerr(e: Exception) -> ToolDenied:
    return ToolDenied(str(e))


async def _usite(connection: str | None, site: str | None):
    from mmn.services.unifi import service as uf
    from mmn.services.unifi.transport import UnifiError

    c = await _uconn(connection, "network")
    try:
        return c, uf.site_of(c, site)
    except UnifiError as e:
        raise ToolDenied(str(e)) from e


async def _with_db(fn):
    from mmn.services.unifi.transport import UnifiError

    async with sessionmaker()() as db:
        try:
            res = await fn(db)
            await db.commit()
            return _jsonable(res)
        except UnifiError as e:
            raise _uerr(e) from e


@tool("unifi", RO)
async def list_unifi_connections(customer: str | None = None) -> list[dict]:
    """UniFi connections per customer/customer: kind (network = a console's local Integration API,
    site_manager = the cloud API with all consoles of an account, access = UniFi Access doors), how they
    are reached (direct, through an agent in the customer's network, or through the cloud connector),
    status, read-only key, sites and totals of the last poll (every 5 minutes)."""
    from mmn.services import nodes
    from mmn.services.unifi import service as uf

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable([uf.connection_out(c) for c in await uf.list_connections(db, env_id)])


@tool("unifi", RO)
async def get_unifi_overview(customer: str | None = None, connection: str | None = None) -> dict:
    """All UniFi Network sites with their gateways, switches and access points: status (online/offline),
    model, IP, uplink device, firmware and whether an update is available, clients per device and per
    site (wired, WiFi, guests), WiFi networks with VLAN and security, WAN state and the latest events
    (device offline/online, WAN change/failover, new firmware). Data from the last poll; use
    refresh_unifi first when it must be live. Offline devices are listed first."""
    from mmn.services import nodes
    from mmn.services.unifi import service as uf

    async def run(db):
        env_id = (await nodes.get_environment(db, customer)).id if customer else None
        cid = (await _uconn(connection, ("network", "site_manager"))).id if connection else None
        res = await uf.network_overview(db, env_id, cid)
        for d in res["devices"]:
            d.pop("radios", None)
        return res

    try:
        return await _with_db(run)
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e


@tool("unifi", RO)
async def refresh_unifi(connection: str) -> dict:
    """Poll one UniFi connection now (devices, clients, WiFi, WAN or doors) instead of waiting for the
    5-minute poll. Returns the totals."""
    from mmn.services.unifi import service as uf
    from mmn.services.unifi.transport import UnifiError

    c = await _uconn(connection, ("network", "site_manager", "access"))
    try:
        snap = await uf.poll(c.id)
    except UnifiError as e:
        raise _uerr(e) from e
    return _jsonable({"connection": c.name, "totals": snap.get("totals"), "polled_at": snap.get("polled_at")})


@tool("unifi", RO)
async def get_unifi_device(device: str, connection: str | None = None, site: str | None = None) -> dict:
    """Live details of one UniFi device (by name, id or MAC): state, firmware and update, uplink, ports
    (state, speed, PoE), radios, uptime, CPU/memory, uplink throughput, radio retries and the clients
    connected to it."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)

    async def run(db):
        d = uf.find_device(c, s["id"], device)
        return await uf.device_detail(db, c, s["id"], d["id"])

    return await _with_db(run)


@tool("unifi", RO)
async def list_unifi_clients(connection: str | None = None, site: str | None = None, query: str | None = None,
                             limit: int = 200) -> list[dict]:
    """Clients connected now (live): name, MAC, IP, wired/wireless/VPN, connected since, the device it
    hangs on (AP or switch) and guest access. query: part of a name, a MAC or an IP."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)
    return await _with_db(lambda db: uf.clients(db, c, s["id"], query, max(1, min(limit, 2000))))


@tool("unifi", RO)
async def diagnose_wifi_client(client: str, connection: str | None = None, site: str | None = None) -> dict:
    """Why can't a device (laptop, phone, printer) get on WiFi? client: its name, MAC or IP. Checks
    whether it is connected now and where, whether it has an IP (DHCP), guest portal authorization, its
    AP's state, and per SSID: disabled, hidden, WPA3-only, 802.1X, MAC allow/block lists that refuse
    this MAC, blackout schedules and AP filters; plus offline APs and recent events. Give the MAC when
    the device is not connected, so MAC filters can be checked."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)
    return await _with_db(lambda db: uf.diagnose_client(db, c, s["id"], client))


@tool("unifi", RO)
async def list_wifi_networks(connection: str | None = None, site: str | None = None) -> dict:
    """WiFi networks (SSIDs) of a site, live: enabled, security (WPA2/WPA3/enterprise/open), the network
    and VLAN they use, hidden, client isolation, MAC filtering, blackout schedule and whether it looks
    like a guest network."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)
    return await _with_db(lambda db: uf.live_wifi(db, c, s["id"]))


@tool("unifi", RO)
async def check_guest_isolation(connection: str | None = None, site: str | None = None) -> dict:
    """Is the guest network isolated from the LAN? Finds the guest networks (by name, zone or SSID) and,
    from the zone-based firewall policies, says per LAN network: isolated, partial (exceptions allow some
    traffic), open (same zone or an allow policy) or unknown, with the policy that decides it. Also shows
    client isolation of the guest SSIDs."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)
    return await _with_db(lambda db: uf.guest_isolation(db, c, s["id"]))


@tool("unifi", RO)
async def find_firmware_updates(customer: str | None = None, type: str | None = None) -> list[dict]:
    """Which UniFi devices have a firmware update available, across all connections and customers
    (from the last poll). type: gateway, switch or ap."""
    from mmn.services import nodes
    from mmn.services.unifi import service as uf

    async def run(db):
        env_id = (await nodes.get_environment(db, customer)).id if customer else None
        return uf.firmware_report(await uf.list_connections(db, env_id), type)

    try:
        return await _with_db(run)
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e


@tool("unifi", RO)
async def list_unifi_events(connection: str | None = None, since_hours: int = 24, kind: str | None = None,
                            limit: int = 100) -> list[dict]:
    """What changed in UniFi: device_offline, device_online, wan_change (WAN failover), uplink_changed,
    firmware_available, device_added, console_state, connection_error/restored. Detected by comparing the
    5-minute polls. kind filters on one of these."""
    from mmn.services.unifi import service as uf

    cid = (await _uconn(connection, ("network", "site_manager", "access"))).id if connection else None
    return await _with_db(lambda db: uf.list_events(db, cid, max(1, min(since_hours, 24 * 90)), kind,
                                                    max(1, min(limit, 1000))))


@tool("unifi", RO)
async def get_unifi_cloud(connection: str | None = None, isp_metrics: bool = False) -> dict:
    """UniFi Site Manager (cloud): all consoles (hosts) of the account with their state and version, all
    sites with device/offline/client counts, ISP and WAN uptime, and all devices with firmware status.
    isp_metrics=true adds hourly WAN latency, packet loss and downtime of the last 7 days."""
    from mmn.services.unifi import service as uf

    c = await _uconn(connection, "site_manager")

    async def run(db):
        out = {"connection": c.name, **(c.snapshot or {})}
        if isp_metrics:
            out["isp_metrics"] = await (await uf.cloud_api(db, c)).isp_metrics("1h", "7d")
        return out

    return await _with_db(run)


@tool("unifi_admin", DESTRUCTIVE)
async def restart_unifi_device(device: str, confirm: str, reason: str, connection: str | None = None,
                               site: str | None = None) -> dict:
    """Restart a UniFi device (AP, switch or gateway). Clients on it lose their connection for a few
    minutes; restarting the gateway takes the whole site offline. Set `confirm` to the device's exact
    name. Ask the user first."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)

    async def run(db):
        d = uf.find_device(c, s["id"], device)
        if confirm != d["name"]:
            raise ToolDenied(f"confirm must equal the device name '{d['name']}'")
        if not reason.strip():
            raise ToolDenied("give a reason")
        uf.require_writable(c)
        await (await uf.network_api(db, c)).device_action(s["id"], d["id"], "RESTART")
        return {"ok": True, "device": d["name"], "site": s["name"], "action": "restart"}

    return await _with_db(run)


@tool("unifi_admin", DESTRUCTIVE)
async def power_cycle_switch_port(device: str, port: int, confirm: str, reason: str, connection: str | None = None,
                                  site: str | None = None) -> dict:
    """Power-cycle the PoE of one switch port (e.g. to reboot a hung AP, camera or phone). `confirm`
    is '<switch name> port <n>'. Ask the user first."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)

    async def run(db):
        d = uf.find_device(c, s["id"], device)
        if confirm != f"{d['name']} port {port}":
            raise ToolDenied(f"confirm must be '{d['name']} port {port}'")
        if not reason.strip():
            raise ToolDenied("give a reason")
        uf.require_writable(c)
        await (await uf.network_api(db, c)).port_action(s["id"], d["id"], port, "POWER_CYCLE")
        return {"ok": True, "device": d["name"], "port": port, "action": "power_cycle"}

    return await _with_db(run)


@tool("unifi_admin", WRITE)
async def set_guest_access(client: str, authorize: bool, connection: str | None = None,
                           site: str | None = None) -> dict:
    """Authorize (or revoke) guest portal access for a connected client (name, MAC or IP)."""
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)

    async def run(db):
        rows = await uf.clients(db, c, s["id"], client)
        if len(rows) != 1:
            raise ToolDenied(f"{len(rows)} clients match '{client}'; be more specific (MAC)")
        uf.require_writable(c)
        await (await uf.network_api(db, c)).client_action(
            s["id"], rows[0]["id"], "AUTHORIZE_GUEST_ACCESS" if authorize else "UNAUTHORIZE_GUEST_ACCESS")
        return {"ok": True, "client": rows[0]["name"], "authorized": authorize}

    return await _with_db(run)


@tool("unifi_admin", DESTRUCTIVE)
async def set_wifi_enabled(wifi: str, enabled: bool, confirm: str, connection: str | None = None,
                           site: str | None = None) -> dict:
    """Switch a WiFi network (SSID) on or off. Turning it off disconnects all its clients. `confirm`
    is the exact SSID name. Ask the user first."""
    from mmn.api.unifi import set_wifi
    from mmn.services.unifi import service as uf

    c, s = await _usite(connection, site)

    async def run(db):
        rows = [w for w in await uf.live_wifi(db, c, s["id"]) or [] if wifi in (w["id"], w["name"])]
        if not rows:
            raise ToolDenied("WiFi network not found")
        if confirm != rows[0]["name"]:
            raise ToolDenied(f"confirm must equal the SSID '{rows[0]['name']}'")
        uf.require_writable(c)
        return await set_wifi(await uf.network_api(db, c), s["id"], rows[0]["id"], enabled)

    return await _with_db(run)


@tool("unifi", RO)
async def list_doors(connection: str | None = None) -> list[dict]:
    """UniFi Access doors, live: name, lock relay (lock/unlock), door position (open/close, when a door
    sensor is fitted) and whether a hub is bound. Without `connection` all Access connections."""
    from mmn.services.unifi import service as uf
    from mmn.services.unifi.transport import UnifiError

    async with sessionmaker()() as db:
        conns = [await _uconn(connection, "access")] if connection else await uf.list_connections(db, kind="access")
    out = []
    for c in conns:
        try:
            snap = await uf.poll(c.id)
            out += [{"connection": c.name, **d} for d in snap["doors"]]
        except UnifiError as e:
            out.append({"connection": c.name, "error": str(e)})
    return _jsonable(out)


@tool("unifi", RO)
async def get_access_log(connection: str | None = None, door: str | None = None, actor: str | None = None,
                         since: str | None = None, until: str | None = None, hours: float = 24,
                         topic: str = "door_openings", limit: int = 100) -> list[dict]:
    """UniFi Access log: who opened which door when, how (NFC card, PIN, mobile, remote) and whether it
    was granted or blocked. For "who opened the server room last night": door='server room',
    since='2026-11-21T18:00:00+01:00', until='2026-11-22T07:00:00+01:00' (ISO 8601 with time zone) or
    hours=12. topic: door_openings (default), critical, updates, device_events, admin_activity, visitor
    or all. actor: part of a person's name."""
    from mmn.services.unifi import service as uf

    c = await _uconn(connection, "access")

    def parse(v: str | None):
        if not v:
            return None
        from datetime import UTC, datetime

        try:
            d = datetime.fromisoformat(v)
        except ValueError as e:
            raise ToolDenied(f"invalid time '{v}', use ISO 8601 like 2026-11-21T22:00:00+01:00") from e
        return d if d.tzinfo else d.replace(tzinfo=UTC)

    return await _with_db(lambda db: uf.access_log(db, c, hours=hours, since=parse(since), until=parse(until),
                                                   topic=topic, door=door, actor=actor, limit=max(1, min(limit, 500))))


@tool("unifi", RO)
async def list_door_schedules(connection: str | None = None) -> dict:
    """UniFi Access schedules (when doors are unlocked or who may enter when) and access policies."""
    from mmn.services.unifi import service as uf

    c = await _uconn(connection, "access")

    async def run(db):
        api = await uf.access_api(db, c)
        return {"schedules": await api.schedules(), "policies": await uf._optional(api.policies()) or []}

    return await _with_db(run)


@tool("doors", DESTRUCTIVE)
async def unlock_door(door: str, confirm: str, reason: str, connection: str | None = None) -> dict:
    """Unlock a door remotely (one momentary unlock, like pressing the button in UniFi Access).
    Physical security: ALWAYS ask the user first. `confirm` is the exact door name; `reason` is
    logged here and the unlock shows in UniFi's own access log under this app's name."""
    from mmn.services.unifi import service as uf

    c = await _uconn(connection, "access")
    p = current_principal()

    async def run(db):
        d = await uf.find_door(db, c, door)
        if confirm != d["name"]:
            raise ToolDenied(f"confirm must equal the door name '{d['name']}'")
        if not reason.strip():
            raise ToolDenied("give a reason")
        uf.require_writable(c)
        await (await uf.access_api(db, c)).unlock(d["id"], f"{p.username if p else 'mcp'} via Claude (MCP)")
        return {"ok": True, "door": d["name"], "action": "unlock"}

    return await _with_db(run)


@tool("doors", DESTRUCTIVE)
async def set_door_lock_rule(door: str, rule: str, reason: str, confirm: str, minutes: int | None = None,
                             connection: str | None = None) -> dict:
    """Change a door's temporary lock rule: keep_lock (stay locked), keep_unlock (stay unlocked),
    custom (unlocked for `minutes`), lock_early (end an unlock schedule now) or reset (back to the
    normal schedule). `confirm` is the exact door name. Ask the user first."""
    from mmn.services.unifi import service as uf

    if rule not in uf.LOCK_RULES:
        raise ToolDenied("rule must be one of " + ", ".join(uf.LOCK_RULES))
    if rule == "custom" and not minutes:
        raise ToolDenied("custom needs minutes")
    c = await _uconn(connection, "access")

    async def run(db):
        d = await uf.find_door(db, c, door)
        if confirm != d["name"]:
            raise ToolDenied(f"confirm must equal the door name '{d['name']}'")
        if not reason.strip():
            raise ToolDenied("give a reason")
        uf.require_writable(c)
        await (await uf.access_api(db, c)).set_lock_rule(d["id"], rule, minutes)
        return {"ok": True, "door": d["name"], "rule": rule, "minutes": minutes}

    return await _with_db(run)


# ------------------------------------------------------------------ workstations (phase 10)
async def _ws(workstation: str):
    from mmn.services import nodes, workstations

    async with sessionmaker()() as db:
        try:
            return await workstations.get_workstation(db, workstation)
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("read", RO)
async def list_workstations(customer: str | None = None) -> dict:
    """Workstations (PCs and laptops with the agent), optionally of one customer: online and last
    seen, OS and build, model and serial, logged-in user, antivirus, disk encryption (BitLocker or
    LUKS), pending updates, last update installed, security status (ok, warning, critical) with issue
    codes, and totals. Workstations are often asleep: offline is normal, the data is from when it was
    last seen. Start here for "how are the PCs at customer X"."""
    from mmn.services import nodes, workstations

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e
        return _jsonable(await workstations.list_workstations(db, env_id))


@tool("read", RO)
async def get_workstation(workstation: str, include_software: bool = False) -> dict:
    """The full inventory of one workstation: hardware (manufacturer, model, serial, BIOS, CPU,
    memory), OS (version, build, install date, last boot, domain), logged-in users, disks with health
    and SSD wear, volumes and free space, Microsoft Defender (real-time protection, signature age,
    scans, threats) or another antivirus, BitLocker per volume, firewall profiles, TPM, Secure Boot,
    Windows Update history and hotfixes, pending updates and the security checks. include_software
    adds the installed programs (long); use find_software to search them instead."""
    from mmn.services import workstations

    n = await _ws(workstation)
    d = workstations.detail(n)
    if not include_software:
        inv = dict(d["inventory"])
        inv["software_count"] = len(inv.pop("software", None) or [])
        d["inventory"] = inv
    return _jsonable(d)


@tool("logs", RO)
async def diagnose_workstation(workstation: str) -> dict:
    """Health and security check of one workstation in a single call: refreshes the inventory when it
    is older than an hour, runs the agent's health check (CPU, memory, disks, failed services, recent
    errors, busiest processes) and returns the security checks (antivirus, signatures, threats,
    firewall, BitLocker/LUKS, updates, pending reboot, disk health and space) with findings and the
    next tools to use. Start here when a user says "my PC is slow" or "something is wrong"."""
    from mmn.services import workstations

    n = await _ws(workstation)
    try:
        return _jsonable(await workstations.diagnose(n))
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def refresh_workstation_inventory(workstation: str) -> dict:
    """Collect the inventory of a workstation now (read-only script through the agent, about 10-60
    seconds). The app also refreshes it every few hours while the machine is online."""
    from mmn.services import workstations

    n = await _ws(workstation)
    try:
        d = await workstations.collect(n.id)
    except Exception as e:
        raise _agent_errors(e) from e
    inv = dict(d["inventory"])
    inv["software_count"] = len(inv.pop("software", None) or [])
    d["inventory"] = inv
    return _jsonable(d)


@tool("read", RO)
async def find_software(name: str, customer: str | None = None, include_servers: bool = False) -> list[dict]:
    """Which workstations (and with include_servers also servers) have a program installed, with the
    version: e.g. 'teamviewer', 'office', 'chrome', '7-zip*'. Searches the stored inventories, so it
    also finds machines that are offline now. For a CVE on servers use check_cve."""
    from mmn.services import nodes, workstations

    async with sessionmaker()() as db:
        try:
            env_id = (await nodes.get_environment(db, customer)).id if customer else None
            return _jsonable(await workstations.find_software(db, name, env_id, include_servers=include_servers))
        except nodes.NodeError as e:
            raise ToolDenied(str(e)) from e


@tool("workstations", WRITE)
async def workstation_action(workstation: str, action: str, reason: str, message: str | None = None) -> dict:
    """Remote action on a workstation through the agent. action: defender_quick_scan or
    defender_full_scan (starts a Microsoft Defender scan in the background), defender_update (update
    the virus signatures), gpupdate (refresh Group Policy) or message (show `message` to the logged-in
    user(s); on Linux through wall). Windows only, except message. Give a short `reason`. Restarts go
    through reboot_node, updates through install_node_updates."""
    from mmn.services import nodes, workstations

    n = await _ws(workstation)
    try:
        return _jsonable(await workstations.run_action(n, action, message=message))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


@tool("workstations", DESTRUCTIVE)
async def log_off_workstation_user(workstation: str, user: str, confirm: str, reason: str) -> dict:
    """Log a user off a workstation (all their sessions; unsaved work is lost). confirm must be the
    workstation's exact name. Ask the user first."""
    from mmn.services import nodes, workstations

    n = await _ws(workstation)
    if confirm != n.name:
        raise ToolDenied(f"confirm must be the workstation name '{n.name}'")
    try:
        return _jsonable(await workstations.log_off(n, user))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


# ------------------------------------------------------------------ monthly customer reports (phase 10)
@tool("read", RO)
async def get_customer_report(customer: str, period: str | None = None, generate: bool = False,
                              lang: str | None = None) -> dict:
    """The monthly IT report of a customer: availability per server, incidents, updates installed and
    pending, Proxmox backups, workstations with their security status, security points, what the AI
    did (audit log, approvals) and recommendations. period: 'YYYY-MM' (default: the latest report).
    With generate=true a fresh report is made for the period (default: the current month so far),
    without mailing it. Returns the report data and the link to the PDF in Stackpilot."""
    from datetime import UTC, datetime

    from mmn.services import nodes, reports
    from mmn.services.settings_store import get_setting

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
            tz = await reports.timezone(db)
            r = None
            if not generate:
                r = await reports.latest(db, e.id, period)
            if r is None:
                if not generate and period is None:
                    raise ToolDenied("this customer has no report yet; call again with generate=true")
                p = period or reports.current_period(datetime.now(UTC), tz)
                r = await reports.generate(db, e, p, lang=lang, trigger="mcp",
                                           created_by=(current_principal().token_name if current_principal() else None))
                await db.commit()
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        out = reports.report_out(r, with_data=True)
        general = await get_setting(db, "general")
    base = (general.get("base_url") or "").rstrip("/")
    if base:
        out["pdf_url"] = f"{base}/api/customers/{r.environment_id}/reports/{r.id}/pdf"
        out["app_url"] = f"{base}/customers/{r.environment_id}?tab=reports"
    return _jsonable(out)


@tool("reports", WRITE)
async def send_customer_report(customer: str, period: str | None = None, to: list[str] | None = None) -> dict:
    """Mail the customer's report (PDF and HTML attached) to the report recipients of the customer,
    or to `to`. period 'YYYY-MM': the latest report of that month, made when there is none (default:
    last month). This sends mail outside Stackpilot: ask the user first."""
    from datetime import UTC, datetime

    from mmn.services import nodes, reports
    from mmn.services.mailer import MailNotConfigured

    async with sessionmaker()() as db:
        try:
            e = await nodes.get_environment(db, customer)
            tz = await reports.timezone(db)
            p = period or reports.previous_period(datetime.now(UTC), tz)
            r = await reports.latest(db, e.id, p)
            if r is None:
                r = await reports.generate(db, e, p, trigger="mcp",
                                           created_by=(current_principal().token_name if current_principal() else None))
                await db.commit()
            sent = await reports.send(db, r, e, to)
            await db.commit()
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        except MailNotConfigured as ex:
            await db.commit()
            raise ToolDenied("the mail server is not configured in Stackpilot (Settings > Mail)") from ex
        return _jsonable({"ok": True, "sent_to": sent, "period": r.period, "report_id": r.id})


# ------------------------------------------------------------------ runbooks (phase 13)
def _rb_err(e: Exception) -> ToolDenied:
    return ToolDenied(str(e))


@tool("read", RO)
async def list_runbooks(customer: str | None = None) -> list[dict]:
    """The runbooks (fixed procedures such as "patch a server", "new employee", "new project via
    GitHub"): name, category, customer (none = for every customer), parameters and steps. Run one
    with run_runbook instead of doing the steps yourself: Stackpilot runs them in order, asks
    approval where the policy or the runbook says so and keeps the audit trail."""
    from sqlalchemy import select

    from mmn.models import Environment, Runbook
    from mmn.services import nodes, runbooks

    async with sessionmaker()() as db:
        q = select(Runbook).where(Runbook.enabled.is_(True)).order_by(Runbook.name)
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
            q = q.where((Runbook.customer_id == cid) | Runbook.customer_id.is_(None))
        names = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
        out = []
        for rb in (await db.execute(q)).scalars():
            d = runbooks.runbook_out(rb, names.get(rb.customer_id))
            out.append({k: d[k] for k in ("id", "name", "description", "category", "customer_name", "params")}
                       | {"steps": [{"id": s["id"], "kind": s["kind"], "title": s.get("title"), "tool": s.get("tool"),
                                     "approval": s.get("approval")} for s in d["steps"]]})
    return _jsonable(out)


@tool("read", RO)
async def get_runbook(runbook: str) -> dict:
    """One runbook (id or exact name) with all steps, their arguments and placeholders."""
    from mmn.services import runbooks

    async with sessionmaker()() as db:
        try:
            rb = await runbooks.find_runbook(db, runbook)
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        return _jsonable(runbooks.runbook_out(rb))


@tool("runbooks", WRITE)
async def run_runbook(runbook: str, reason: str, customer: str | None = None,
                      params: dict[str, Any] | None = None) -> dict:
    """Start a runbook (id or exact name) for a customer with its parameters. Stackpilot runs the
    steps one by one with this token's rights; steps that need approval wait for an admin (app and
    mail), changes outside the maintenance window wait for it, and manual steps wait for a person.
    Returns the run; follow it with get_runbook_run. Ask the user before starting a runbook that
    changes something, and give a short `reason`."""
    from mmn.models import Environment
    from mmn.services import gateway, nodes, runbooks

    p = current_principal()
    if not reason.strip():
        raise ToolDenied("give a reason")
    async with sessionmaker()() as db:
        try:
            rb = await runbooks.find_runbook(db, runbook)
            env: Environment | None = None
            if customer:
                try:
                    env = await nodes.get_environment(db, customer)
                except nodes.NodeError as ex:
                    raise ToolDenied(str(ex)) from ex
            elif rb.customer_id is None and p is not None and p.customers is not None:
                if len(p.customers) != 1:
                    raise ToolDenied("name the customer this run is for")
                env = await db.get(Environment, next(iter(p.customers)))
            missing = sorted({(gateway.REGISTRY[s["tool"]].scope if s["tool"] in gateway.REGISTRY else "hub")
                              for s in rb.steps or [] if s.get("kind") == "tool"} - (p.scopes if p else set()))
            if missing:
                raise ToolDenied(f"this token lacks scope(s) the steps need: {', '.join(missing)}")
            run = await runbooks.start_run(db, rb, customer=env, params=params, trigger="mcp", reason=reason,
                                           user_id=p.user_id if p else None, username=p.username if p else None,
                                           token_id=p.token_id if p else None, token_name=p.token_name if p else None)
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        await db.commit()
        out = runbooks.run_out(run, full=False)
    runbooks.kick(run.id)
    await asyncio.sleep(0.3)  # the first (read) steps usually finish right away
    async with sessionmaker()() as db:
        from mmn.models import RunbookRun

        r = await db.get(RunbookRun, run.id)
        if r is not None:
            out = runbooks.run_out(r)
    out["message"] = ("The run has started. Steps that need approval wait for an admin in Stackpilot; tell the user. "
                      "Check progress with get_runbook_run(run_id); do not start it again.")
    out["run_id"] = run.id
    return _jsonable(out)


@tool("read", RO)
async def get_runbook_run(run_id: str) -> dict:
    """The state of a runbook run: status (running, waiting_approval, waiting_input, succeeded,
    failed, cancelled) and per step its status, approval request, result or error. A manual step for
    the AI (assignee "ai") waits for complete_runbook_step."""
    from mmn.models import RunbookRun
    from mmn.services import runbooks

    async with sessionmaker()() as db:
        r = await db.get(RunbookRun, run_id)
        if r is None:
            raise ToolDenied("run not found")
        return _jsonable(runbooks.run_out(r))


@tool("read", RO)
async def list_runbook_runs(customer: str | None = None, runbook: str | None = None, active_only: bool = False,
                            limit: int = 20) -> list[dict]:
    """Recent runbook runs (newest first), optionally of one customer or runbook, or only the ones that
    are not finished."""
    from sqlalchemy import select

    from mmn.models import RunbookRun
    from mmn.services import nodes, runbooks

    async with sessionmaker()() as db:
        q = select(RunbookRun).order_by(RunbookRun.created_at.desc()).limit(max(1, min(limit, 100)))
        if customer:
            try:
                q = q.where(RunbookRun.customer_id == (await nodes.get_environment(db, customer)).id)
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
        if runbook:
            try:
                q = q.where(RunbookRun.runbook_id == (await runbooks.find_runbook(db, runbook)).id)
            except runbooks.RunbookError as e:
                raise _rb_err(e) from e
        if active_only:
            q = q.where(RunbookRun.status.not_in(tuple(runbooks.FINAL)))
        return _jsonable([runbooks.run_out(r, full=False) for r in (await db.execute(q)).scalars()])


@tool("runbooks", WRITE)
async def complete_runbook_step(run_id: str, note: str, failed: bool = False) -> dict:
    """Confirm the manual step a run waits for, when that step is for the AI (assignee "ai"): you
    did what its instructions say (with other tools). `note` says what you did or found. failed=true
    stops the run (unless the step may fail). Steps for a person are confirmed in Stackpilot."""
    from mmn.models import RunbookRun
    from mmn.services import runbooks

    p = current_principal()
    if not note.strip():
        raise ToolDenied("say in `note` what you did")
    async with sessionmaker()() as db:
        r = await db.get(RunbookRun, run_id)
        if r is None:
            raise ToolDenied("run not found")
        try:
            await runbooks.complete_step(db, r, by=p.token_name if p else "AI", note=note, ok=not failed, via="mcp",
                                         ai=True)
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        await db.commit()
    runbooks.kick(run_id)
    return {"ok": True, "run_id": run_id, "message": "Confirmed; the run goes on. Follow it with get_runbook_run."}


@tool("runbooks", WRITE)
async def cancel_runbook_run(run_id: str, reason: str) -> dict:
    """Cancel a runbook run that is not finished: its open approval requests are withdrawn and no
    further steps run. Steps that already ran are not undone."""
    from mmn.models import RunbookRun
    from mmn.services import runbooks

    p = current_principal()
    async with sessionmaker()() as db:
        r = await db.get(RunbookRun, run_id)
        if r is None:
            raise ToolDenied("run not found")
        try:
            await runbooks.cancel(db, r, by=p.token_name if p else "AI", via="mcp")
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        r.error = f"{r.error}: {reason}"[:2000] if reason else r.error
        await db.commit()
        return _jsonable(runbooks.run_out(r, full=False))


@tool("runbooks", WRITE)
async def save_runbook(name: str, steps: list[dict[str, Any]], params: list[dict[str, Any]] | None = None,
                       description: str | None = None, category: str = "other", customer: str | None = None,
                       runbook: str | None = None, enabled: bool = True, use_window: bool = True) -> dict:
    """Create a runbook, or change one (`runbook` = its id or name). steps: [{"id", "title", "kind":
    "tool"|"manual"|"wait", "tool", "args", "approval": "policy"|"always", "on_failure":
    "stop"|"continue", "optional", "wait_for_job", "instructions", "assignee": "human"|"ai",
    "minutes"}]. params: [{"name", "label", "required", "default"}]. Arguments may use {{params.x}},
    {{customer.name}}, {{customer.id}}, {{run.id}} and {{steps.<id>.result.<path>}}. category:
    maintenance, onboarding, project, remediation or other. customer: none = for every customer.
    Show the user the steps before saving."""
    from mmn.services import nodes, runbooks

    p = current_principal()
    async with sessionmaker()() as db:
        cid = None
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
        elif p is not None and p.customers is not None:
            raise ToolDenied("this token is tied to customers: give the customer the runbook is for")
        try:
            rb = await runbooks.find_runbook(db, runbook) if runbook else None
            if rb is not None and rb.customer_id is None and p is not None and p.customers is not None:
                raise ToolDenied("this runbook is for every customer; a token tied to customers cannot change it")
            rb = await runbooks.save(db, rb, name=name, description=description, category=category, customer_id=cid,
                                     enabled=enabled, params=params or [], steps=steps, use_window=use_window,
                                     triggers=rb.triggers if rb else [], by=p.token_name if p else None)
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        await db.commit()
        return _jsonable(runbooks.runbook_out(rb))


@tool("runbooks", DESTRUCTIVE)
async def delete_runbook(runbook: str, confirm: str) -> dict:
    """Delete a runbook (its past runs stay in the history). Set `confirm` to the runbook's name."""
    from sqlalchemy import func, select

    from mmn.models import RunbookRun
    from mmn.services import runbooks

    p = current_principal()
    async with sessionmaker()() as db:
        try:
            rb = await runbooks.find_runbook(db, runbook)
        except runbooks.RunbookError as e:
            raise _rb_err(e) from e
        if confirm != rb.name:
            raise ToolDenied(f"confirm must equal the runbook name '{rb.name}'")
        if rb.customer_id is None and p is not None and p.customers is not None:
            raise ToolDenied("this runbook is for every customer; a token tied to customers cannot delete it")
        active = (await db.execute(select(func.count()).select_from(RunbookRun).where(
            RunbookRun.runbook_id == rb.id, RunbookRun.status.not_in(tuple(runbooks.FINAL))))).scalar_one()
        if active:
            raise ToolDenied("this runbook has runs that are not finished")
        await db.delete(rb)
        await db.commit()
    return {"ok": True, "deleted": runbook}


# ------------------------------------------------------------------ automatic recovery (phase 14)
@tool("services", WRITE)
async def cleanup_disk(node: str, reason: str) -> dict:
    """Free disk space on a machine, safely: package caches, journal entries older than 14 days and
    temp files older than 7 days (Linux); temp files, the Windows Update download cache and the
    component store (Windows). Shows the disk use before and after."""
    from mmn.services import nodes, remediation

    if not reason.strip():
        raise ToolDenied("give a reason")
    try:
        return _jsonable(await remediation.cleanup_disk(node))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


@tool("services", WRITE)
async def recover_agent(node: str, reason: str) -> dict:
    """Bring back the agent of a server that runs as a VM or container on a Proxmox VE node that
    Stackpilot reaches: starts the guest when it is stopped, otherwise restarts the agent service
    inside it (pct exec, or the QEMU guest agent for a VM). Never on a Proxmox VE node itself."""
    from mmn.services import nodes, remediation

    if not reason.strip():
        raise ToolDenied("give a reason")
    p = current_principal()
    try:
        return _jsonable(await remediation.recover_agent(node, requested_by=p.username if p else None))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def list_network_adapters(node: str) -> dict:
    """The physical network adapters of a machine (Windows or Linux) with their link state: up,
    down (no link), disabled (switched off) or absent, plus description, speed and MAC address."""
    from mmn.services import netwatch, nodes

    try:
        return _jsonable(await netwatch.adapters(node))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


@tool("services", WRITE)
async def restart_network_adapter(node: str, adapter: str, reason: str, force: bool = False,
                                  confirm: str | None = None) -> dict:
    """Restart a network adapter that has no link (for example an uplink or X710 port that stayed
    down after a switch reboot): Restart-NetAdapter on Windows, ip link down/up on Linux, then wait
    up to a minute for the link. `adapter` is the exact name from list_network_adapters. An
    adapter that is up, switched off or absent is left alone, unless `force` is true and `confirm`
    equals the machine name (restarting an adapter that is up interrupts its traffic)."""
    from mmn.services import netwatch, nodes

    if not reason.strip():
        raise ToolDenied("give a reason")
    p = current_principal()
    if force:
        async with sessionmaker()() as db:
            try:
                n = await nodes.get_node(db, node)
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        if confirm != n.name:
            raise ToolDenied(f"force restarts an adapter that may be in use: set confirm to the machine name '{n.name}'")
    try:
        return _jsonable(await netwatch.restart_adapter(node, adapter, force=force,
                                                        requested_by=p.username if p else None))
    except nodes.NodeError as e:
        raise ToolDenied(str(e)) from e
    except Exception as e:
        raise _agent_errors(e) from e


@tool("read", RO)
async def get_remediation(customer: str | None = None, limit: int = 30) -> dict:
    """Automatic recovery: whether it is on or stopped (emergency stop), its limits, the rules
    (problem -> runbook, mode auto/approve/notify) and the latest attempts with their outcome
    (succeeded = the problem is gone, failed, waiting_approval, skipped by the loop protection)."""
    from sqlalchemy import select

    from mmn.models import RemediationAttempt, RemediationRule
    from mmn.services import nodes, remediation

    async with sessionmaker()() as db:
        cid = None
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
        cfg = await remediation.settings(db)
        names, rbs = await remediation.lookups(db)
        rq = select(RemediationRule).order_by(RemediationRule.name)
        aq = select(RemediationAttempt).order_by(RemediationAttempt.created_at.desc()).limit(max(1, min(limit, 200)))
        if cid:
            rq = rq.where((RemediationRule.customer_id == cid) | RemediationRule.customer_id.is_(None))
            aq = aq.where(RemediationAttempt.customer_id == cid)
        rules = [remediation.rule_out(r, names, rbs) for r in (await db.execute(rq)).scalars()]
        attempts = [remediation.attempt_out(a) | {"reason_text": remediation.reason_text(a.reason) if a.reason else None}
                    for a in (await db.execute(aq)).scalars()]
    return _jsonable({"settings": cfg, "rules": rules, "attempts": attempts})


@tool("runbooks", WRITE)
async def save_remediation_rule(name: str, trigger: str, match: dict[str, Any], runbook: str,
                                params: dict[str, Any] | None = None, mode: str = "auto", customer: str | None = None,
                                rule: str | None = None, enabled: bool = True, delay_minutes: int = 5,
                                cooldown_minutes: int = 30, max_attempts: int = 3,
                                description: str | None = None) -> dict:
    """Create or change (`rule` = its id) an automatic recovery rule: when a problem shows up,
    Stackpilot runs `runbook` (id or exact name) within the policy. trigger "alert" with match
    {"kinds": [offline|service|network|disk|storage|memory|cpu|smart|unifi|backup], "services":
    [units to watch, for "service"], "adapters": [names or descriptions with * ?, for "network";
    empty = every wired physical adapter that was up], "local": true (network: the agent also
    restarts the adapter itself when it cannot reach Stackpilot), "machine": "web*",
    "machine_types": [server|workstation|proxmox]} or
    trigger "finding" with match {"codes": ["disk_full", "agent_old", ...]}. params fill in the
    runbook's parameters and may use {{machine.name}}, {{alert.detail.service}},
    {{alert.detail.adapter}}, {{finding.params.x}} and {{customer.name}}. mode: auto, approve
    (every change asks an admin) or notify (only report). Show the user the rule before saving."""
    from mmn.models import RemediationRule
    from mmn.services import nodes, remediation, runbooks

    p = current_principal()
    async with sessionmaker()() as db:
        cid = None
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as ex:
                raise ToolDenied(str(ex)) from ex
        elif p is not None and p.customers is not None:
            raise ToolDenied("this token is tied to customers: give the customer the rule is for")
        r = None
        if rule:
            r = await db.get(RemediationRule, rule)
            if r is None:
                raise ToolDenied("rule not found")
            if r.customer_id is None and p is not None and p.customers is not None:
                raise ToolDenied("this rule is for every customer; a token tied to customers cannot change it")
        try:
            rb = await runbooks.find_runbook(db, runbook)
            r = await remediation.save_rule(db, r, name=name, description=description, customer_id=cid,
                                            enabled=enabled, trigger=trigger, match=match, runbook_id=rb.id,
                                            params=params, mode=mode, delay_minutes=delay_minutes,
                                            cooldown_minutes=cooldown_minutes, max_attempts=max_attempts,
                                            by=p.token_name if p else None)
        except (runbooks.RunbookError, remediation.RemediationError) as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        names, rbs = await remediation.lookups(db)
        return _jsonable(remediation.rule_out(r, names, rbs))


@tool("runbooks", DESTRUCTIVE)
async def delete_remediation_rule(rule: str, confirm: str) -> dict:
    """Delete an automatic recovery rule (its past attempts stay). Set `confirm` to the rule's name."""
    from mmn.models import RemediationRule

    p = current_principal()
    async with sessionmaker()() as db:
        r = await db.get(RemediationRule, rule)
        if r is None:
            raise ToolDenied("rule not found")
        if confirm != r.name:
            raise ToolDenied(f"confirm must equal the rule name '{r.name}'")
        if r.customer_id is None and p is not None and p.customers is not None:
            raise ToolDenied("this rule is for every customer; a token tied to customers cannot delete it")
        await db.delete(r)
        await db.commit()
    return {"ok": True, "deleted": rule}


@tool("runbooks", WRITE, all_customers=True)
async def pause_remediation(paused: bool, reason: str) -> dict:
    """The emergency stop of automatic recovery: paused=true stops it for every customer and cancels
    the recoveries that are busy (steps that already ran are not undone); paused=false resumes it.
    Give a short `reason`."""
    from mmn.services import remediation

    p = current_principal()
    async with sessionmaker()() as db:
        cfg = await remediation.set_paused(db, paused, by=p.token_name if p else "AI", note=reason, source="mcp")
        await db.commit()
    if paused:
        await remediation.notify_paused()
    return _jsonable({"paused": cfg["paused"], "cancelled": cfg.get("cancelled", 0)})


# ------------------------------------------------------------------ uptime and SLA (phase 16)
async def _uptime_customer(db, customer: str | None):
    from mmn.services import nodes

    if not customer:
        return None
    try:
        return await nodes.get_environment(db, customer)
    except nodes.NodeError as ex:
        raise ToolDenied(str(ex)) from ex


async def _uptime_check(db, check: str):
    from mmn.services import uptime

    try:
        return await uptime.find_check(db, check)
    except uptime.UptimeError as e:
        raise ToolDenied(str(e)) from e


async def _uptime_machine(db, customer_id: str, ref: str | None, what: str) -> str | None:
    from mmn.services import nodes

    if not ref:
        return None
    try:
        n = await nodes.get_node(db, ref)
    except nodes.NodeError as ex:
        raise ToolDenied(f"{what}: {ex}") from ex
    if n.environment_id != customer_id:
        raise ToolDenied(f"{what} {n.name} does not belong to this customer")
    return n.id


@tool("read", RO)
async def list_uptime_checks(customer: str | None = None, status: str | None = None) -> dict:
    """The uptime checks (websites, TCP ports, ping, DNS records, VPN endpoints) with their status
    (up, down, degraded = slower than its limit, unknown, paused), last response time and error,
    availability over 24 hours, 7 and 30 days and the certificate expiry of HTTPS and SSL VPN checks.
    `customer` limits to one customer; `status` to one status (for "is everything up": status=down)."""
    from sqlalchemy import select

    from mmn.models import UptimeCheck
    from mmn.services import uptime

    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        q = select(UptimeCheck).order_by(UptimeCheck.name)
        if e is not None:
            q = q.where(UptimeCheck.customer_id == e.id)
        rows = list((await db.execute(q)).scalars())
        names = await uptime.names(db)
        st = await uptime.stats(db, rows)
        out = [uptime.check_out(c, names, st.get(c.id)) for c in rows]
    if status:
        out = [c for c in out if c["status"] == status]
    counts: dict[str, int] = {}
    for c in out:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return _jsonable({"counts": counts, "checks": out})


@tool("read", RO)
async def get_uptime_check(check: str, range: str = "24h") -> dict:
    """One uptime check (id or exact name) with its response times over `range` (24h, 7d, 30d), the
    last 30 results with their errors and the incidents (when it was down and why)."""
    from mmn.services import uptime

    async with sessionmaker()() as db:
        c = await _uptime_check(db, check)
        try:
            hist = await uptime.history(db, c, range)
        except uptime.UptimeError as e:
            raise ToolDenied(str(e)) from e
        st = await uptime.stats(db, [c])
        return _jsonable({**uptime.check_out(c, await uptime.names(db), st.get(c.id)), "history": hist})


@tool("monitoring", WRITE)
async def save_uptime_check(customer: str, name: str, kind: str, target: str, port: int | None = None,
                            config: dict[str, Any] | None = None, check: str | None = None,
                            probe: str | None = None, machine: str | None = None, enabled: bool = True,
                            interval_seconds: int = 60, timeout_seconds: int = 10, fail_after: int = 2,
                            slow_ms: int | None = None, cert_warn_days: int = 14, severity: str = "critical",
                            in_sla: bool = True) -> dict:
    """Create or change (`check` = its id or name) an uptime check for a customer. kind: http (target
    a URL; config {"method": GET|HEAD, "expect_status": "200-399", "keyword": "text that must be on
    the page", "keyword_absent": false, "verify_tls": true, "follow_redirects": true}), tcp (target a
    host, `port`), ping (a host), dns (target the name; config {"record_type": A|AAAA|CNAME|MX|TXT|NS|
    SOA|CAA|SRV|PTR, "expect": "value that must be in the answer", "server": "resolver IP"}) or vpn
    (config {"mode": "ssl"} = the VPN portal answers TLS on `port` (443), {"mode": "tunnel"} = a host
    behind the tunnel answers ping). `probe`: run the check from this machine's agent (name or id, at
    the same customer) instead of from Stackpilot, for internal addresses and tunnels. `machine`: the
    machine the service runs on, so alerts belong to it and automatic recovery can act on them. Down
    after `fail_after` failed runs in a row; `slow_ms` makes it "degraded" (warning) above that time.
    `in_sla`: counts towards the customer's uptime SLA. Show the user the check before saving."""
    from mmn.models import UptimeCheck
    from mmn.services import agent_hub, uptime

    p = current_principal()
    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        c: UptimeCheck | None = await _uptime_check(db, check) if check else None
        if c is not None and c.customer_id != e.id:
            raise ToolDenied("this check belongs to another customer")
        try:
            c = await uptime.save_check(
                db, c, by=p.token_name if p else None, customer_id=e.id, name=name, kind=kind, target=target, port=port,
                config=config, probe_node_id=await _uptime_machine(db, e.id, probe, "probe"),
                node_id=await _uptime_machine(db, e.id, machine, "machine"), enabled=enabled,
                interval_seconds=interval_seconds, timeout_seconds=timeout_seconds, fail_after=fail_after,
                slow_ms=slow_ms, cert_warn_days=cert_warn_days, severity=severity, in_sla=in_sla)
        except uptime.UptimeError as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        out = uptime.check_out(c, await uptime.names(db))
    if out["enabled"]:
        agent_hub.spawn(uptime.run_check(out["id"]))
    return _jsonable(out)


@tool("monitoring", DESTRUCTIVE)
async def delete_uptime_check(check: str, confirm: str) -> dict:
    """Delete an uptime check with its history and incidents (they no longer count in the SLA). Set
    `confirm` to the check's name. To stop checking for a while, save it with enabled=false instead."""
    async with sessionmaker()() as db:
        c = await _uptime_check(db, check)
        if confirm != c.name:
            raise ToolDenied(f"confirm must equal the check name '{c.name}'")
        await db.delete(c)
        await db.commit()
    return {"ok": True, "deleted": confirm}


@tool("read", RO)
async def run_uptime_check(check: str) -> dict:
    """Run an uptime check now (instead of waiting for its interval) and return the result: ok, the
    response time, HTTP status, error and the new status of the check."""
    from mmn.services import uptime

    async with sessionmaker()() as db:
        c = await _uptime_check(db, check)
        cid = c.id
    res = await uptime.run_check(cid)
    if res is None:
        raise ToolDenied("uptime check not found")
    return _jsonable(res)


@tool("read", RO)
async def get_sla_report(customer: str | None = None, period: str | None = None) -> dict:
    """The uptime SLA per customer for a month (period 'YYYY-MM', default: this month so far): the
    measured availability of the checks that count in the SLA, the target (the customer's uptime SLA
    setting, or a percentage in its SLA text), met or not, the downtime against the allowed downtime,
    and per check its availability and incidents. Downtime inside the maintenance window does not
    count unless the customer's setting says so. With `customer` also the incidents."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from mmn.models import Environment
    from mmn.services import nodes, reports, uptime

    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        tz = await reports.timezone(db)
        now = datetime.now(UTC)
        p = period or reports.current_period(now, tz)
        try:
            start, end = reports.month_bounds(p, tz)
        except nodes.NodeError as ex:
            raise ToolDenied(str(ex)) from ex
        envs = [e] if e is not None else list((await db.execute(select(Environment).order_by(Environment.name))).scalars())
        out = []
        for env in envs:
            s = await uptime.customer_sla(db, env, start, end, now=now, with_incidents=e is not None)
            if e is None and not s["checks"]:
                continue
            out.append(s)
    return _jsonable({"period": p, "customers": out})


@tool("monitoring", WRITE)
async def set_uptime_sla(customer: str, target_pct: float | None = None, exclude_maintenance: bool | None = None,
                         alert_breach: bool | None = None, recipients: list[str] | None = None,
                         clear_target: bool = False) -> dict:
    """Set the uptime SLA of a customer: `target_pct` (for example 99.9; clear_target=true removes it,
    then a percentage in the customer's SLA text is used), `exclude_maintenance` (downtime inside the
    maintenance window does not count, default true), `alert_breach` (an alert when this month falls
    below the target, default true) and `recipients` (extra addresses for this customer's uptime
    alerts). Leave a field out to keep it."""
    from mmn.services import uptime

    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        try:
            cfg = uptime.save_sla(e, target_pct=target_pct, exclude_maintenance=exclude_maintenance,
                                  alert_breach=alert_breach, recipients=recipients, clear_target=clear_target)
        except uptime.UptimeError as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
    return _jsonable({"customer": e.name, **cfg})


# ------------------------------------------------------------------ AI gateway: approvals and guardrails
@tool("read", RO)
async def get_approval(approval_id: str) -> dict:
    """The state of a call that waited for approval: pending, approved, running, executed (with the
    tool's result), failed (with the error), rejected (with the admin's note) or expired. Only
    approvals of this token. Do not retry a pending call; check again later."""
    from mmn.models import AiApproval
    from mmn.services import gateway

    p = current_principal()
    async with sessionmaker()() as db:
        await gateway.expire_old(db)
        await db.commit()
        a = await db.get(AiApproval, approval_id)
        if a is None or p is None or a.token_id != p.token_id:
            raise ToolDenied("approval not found")
        out = gateway.approval_out(a)
    keep = ("id", "status", "tool", "target", "customer_names", "reason_texts", "created_at", "expires_at",
            "decided_at", "decided_by", "decision_note", "executed_at", "error", "result")
    return _jsonable({k: out[k] for k in keep})


@tool("read", RO)
async def get_guardrails(customer: str | None = None) -> dict:
    """What this token may do without asking: per risk level (read, operate, change, destructive)
    allow, approve or block, for one customer (name or id) or in general, plus whether AI actions are
    paused and whether the customer is inside its maintenance window now. Also lists the tools with
    their risk level when called without a customer."""
    from mmn.services import gateway, nodes

    p = current_principal()
    async with sessionmaker()() as db:
        cid = None
        if customer:
            try:
                cid = (await nodes.get_environment(db, customer)).id
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        out = await gateway.effective_for(db, cid, p.token_id if p else None)
    if not customer:
        out["tools"] = {t["name"]: t["risk"] for t in gateway.catalog()}
    return _jsonable(out)


# ------------------------------------------------------------------ tickets and customer portal (phase 17)
async def _ticket(db, ticket: str):
    from mmn.services import tickets

    try:
        return await tickets.find_ticket(db, ticket)
    except tickets.TicketError as e:
        raise ToolDenied(str(e)) from e


async def _ticket_detail(db, tk) -> dict:
    from mmn.services import tickets

    out = tickets.ticket_out(tk, await tickets.names(db))
    out["thread"] = await tickets.messages(db, tk)
    out["diagnosis"] = tk.diagnosis or {}
    env = tk.customer
    if env is not None:
        out["customer_context"] = {"notes": env.notes, "maintenance_window": env.maintenance_window, "sla": env.sla}
    return _jsonable(out)


def _ai_author() -> str:
    p = current_principal()
    return f"AI ({p.token_name})" if p else "AI"


@tool("read", RO)
async def list_tickets(customer: str | None = None, status: str = "active", assignee: str | None = None,
                       query: str | None = None) -> dict:
    """Tickets that customers reported in the customer portal (or staff on their behalf), newest
    activity first, with status (new, open, waiting_customer, on_hold, resolved, closed), priority,
    linked machines, assignee, whether the customer waits for a reply (needs_reply) and the verdict of
    the first automatic diagnosis. status: active (the default: everything not resolved or closed),
    one status, or empty for all. assignee: a username, "me" (the token's user) or "none". query
    searches subject, reporter, messages and the ticket number."""
    from sqlalchemy import select

    from mmn.models import User
    from mmn.services import tickets

    p = current_principal()
    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        aid = None
        if assignee == "me":
            aid = p.user_id if p else None
        elif assignee == "none":
            aid = "none"
        elif assignee:
            u = (await db.execute(select(User).where(User.username == assignee.lower()))).scalar_one_or_none()
            if u is None:
                raise ToolDenied("assignee not found")
            aid = u.id
        rows = await tickets.search(db, customer_id=e.id if e else None, status=status or None, assignee_id=aid,
                                    q=query, limit=200)
        nm = await tickets.names(db)
        cnt = await tickets.message_counts(db, [r.id for r in rows])
        return _jsonable({"counts": await tickets.counts(db),
                          "tickets": [tickets.ticket_out(r, nm, counts=cnt) for r in rows]})


@tool("read", RO)
async def get_ticket(ticket: str) -> dict:
    """One ticket (id, number or "#1042") with the whole conversation (customer messages, replies,
    internal notes and status changes), the linked machines, the customer's notes, maintenance window
    and SLA, and the first automatic diagnosis of the linked machines (findings per machine, failed
    services, recent errors, open alerts, uptime checks that are down, suggested next steps). Start
    here when asked to look at a ticket; dig deeper with diagnose_server, diagnose_workstation,
    search_logs or find_errors."""
    async with sessionmaker()() as db:
        return await _ticket_detail(db, await _ticket(db, ticket))


@tool("logs", RO)
async def diagnose_ticket(ticket: str) -> dict:
    """Run the first diagnosis of a ticket again, now, and return it: diagnose_server or
    diagnose_workstation on every linked machine (find_errors on all servers of the customer when no
    machine is linked), plus open alerts and uptime checks that are down. Takes up to 90 seconds."""
    from mmn.services import tickets

    async with sessionmaker()() as db:
        tk = await _ticket(db, ticket)
        tid = tk.id
    diag = await tickets.run_diagnosis(tid)
    if diag is None:
        raise ToolDenied("the diagnosis failed; see the app log")
    return _jsonable(diag)


@tool("tickets", WRITE)
async def reply_ticket(ticket: str, message: str, internal: bool = False, status: str | None = None) -> dict:
    """Reply on a ticket. internal=false: the customer sees the reply in the portal and gets it by
    mail, so show the user the text first and write it in the customer's language. internal=true: a
    note for staff only (findings, what you tried). status: change it together with the reply, e.g.
    waiting_customer (you asked the customer something) or resolved."""
    from mmn.services import events, tickets
    from mmn.services.links import public_base_url

    async with sessionmaker()() as db:
        tk = await _ticket(db, ticket)
        old = tk.status
        try:
            m = await tickets.add_message(db, tk, kind="ai", body=message, internal=internal,
                                          author_name=_ai_author(), status=status)
        except tickets.TicketError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        base = await public_base_url(db)
        out = await _ticket_detail(db, tk)
    if not internal:
        tickets.notify_later(tk.id, "staff_reply", base_url=base, message_id=m.id)
    elif out["status"] != old:
        tickets.notify_later(tk.id, "status", base_url=base)
    events.publish("ticket.updated", {"id": tk.id})
    return out


@tool("tickets", WRITE)
async def update_ticket(ticket: str, status: str | None = None, priority: str | None = None,
                        assignee: str | None = None, machines: list[str] | None = None,
                        category: str | None = None) -> dict:
    """Change a ticket: status (new, open, waiting_customer, on_hold, resolved, closed; the customer
    gets a mail for waiting_customer, resolved and closed), priority (low, normal, high, urgent),
    assignee (a staff username, or "none"), the linked machines (names or ids of this customer's
    machines; replaces the list) or category (problem, question, request)."""
    from sqlalchemy import select

    from mmn.models import User
    from mmn.services import events, tickets
    from mmn.services.links import public_base_url

    async with sessionmaker()() as db:
        tk = await _ticket(db, ticket)
        aid: str | None | bool = False
        if assignee == "none":
            aid = None
        elif assignee:
            u = (await db.execute(select(User).where(User.username == assignee.lower()))).scalar_one_or_none()
            if u is None:
                raise ToolDenied("assignee not found")
            aid = u.id
        try:
            changed = await tickets.update_ticket(db, tk, by=_ai_author(), status=status, priority=priority,
                                                  assignee_id=aid, node_ids=machines, category=category)
        except tickets.TicketError as e:
            raise ToolDenied(str(e)) from e
        await db.commit()
        base = await public_base_url(db)
        out = await _ticket_detail(db, tk)
    if "status" in changed:
        tickets.notify_later(tk.id, "status", base_url=base)
    if changed.get("assigned"):
        tickets.notify_later(tk.id, "assigned", base_url=base, by=_ai_author())
    if changed:
        events.publish("ticket.updated", {"id": tk.id})
    return {"changed": sorted(changed), **out}


@tool("tickets", WRITE)
async def create_ticket(customer: str, subject: str, description: str, priority: str = "normal",
                        category: str = "problem", machines: list[str] | None = None,
                        diagnose: bool = True) -> dict:
    """Open a ticket for a customer (for example for a problem you found yourself, or one a user
    reported by phone), linked to machines of that customer (names or ids). The first diagnosis runs
    on them in the background unless diagnose=false."""
    from mmn.services import events, tickets

    p = current_principal()
    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        try:
            tk = await tickets.create_ticket(db, customer_id=e.id, subject=subject, body=description,
                                             category=category, priority=priority, node_ids=machines,
                                             by_name=p.token_name if p else "AI")
        except tickets.TicketError as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        out = tickets.ticket_out(tk, await tickets.names(db))
    if diagnose:
        tickets.start_diagnosis(tk.id)
    events.publish("ticket.created", {"id": tk.id, "number": tk.number, "customer_id": tk.customer_id})
    return _jsonable(out)


# ------------------------------------------------------------------ documentation and password vault (phase 18)
async def _doc_customer(db, customer: str | None):
    return await _uptime_customer(db, customer)


async def _document(db, document: str, customer: str | None):
    from mmn.services import docs

    e = await _doc_customer(db, customer)
    try:
        d = await docs.find(db, document, e.id if e else None)
    except docs.DocError as ex:
        raise ToolDenied(str(ex)) from ex
    if not d.ai_visible:
        raise ToolDenied(f"document {document!r} not found")
    return d


async def _machine_ids(db, customer_id: str, machines: list[str] | None) -> list[str] | None:
    if machines is None:
        return None
    return [await _uptime_machine(db, customer_id, m, "machine") for m in machines]


@tool("read", RO)
async def list_documents(customer: str | None = None, query: str | None = None, category: str | None = None,
                         machine: str | None = None, tag: str | None = None) -> dict:
    """The documentation of the customers: network plans, procedures, notes, contacts, hardware and software
    pages (Markdown), newest first with pinned pages on top. query searches title and text and returns the
    matching lines. category: network, procedure, notes, contacts, howto, hardware, software, security or other.
    machine: only pages linked to that machine. Read a page with get_document. Pages an admin hid from the AI are
    left out. Passwords are never in the documentation: they are in the vault (list_credentials)."""
    from mmn.services import docs, vault

    async with sessionmaker()() as db:
        e = await _doc_customer(db, customer)
        node_id = await _uptime_machine(db, e.id, machine, "machine") if (machine and e) else None
        if machine and not e:
            node_id, _ = await _node_id(machine)
        rows = await docs.search(db, customer_id=e.id if e else None, q=query, category=category, tag=tag,
                                 node_id=node_id, ai_only=True, limit=200)
        names = await vault.machine_names(db)
        out = []
        for d in rows:
            item = docs.doc_out(d, names=names, body=False)
            if query:
                item["matches"] = docs.snippets(d.body, query)
            out.append(item)
        return _jsonable({"documents": out})


@tool("read", RO)
async def get_document(document: str, customer: str | None = None) -> dict:
    """One documentation page (id or exact title; give the customer when titles repeat) with its full Markdown
    text, linked machines, tags, version and its attachments (names and sizes)."""
    from mmn.services import docs, vault

    async with sessionmaker()() as db:
        d = await _document(db, document, customer)
        return _jsonable(docs.doc_out(d, names=await vault.machine_names(db),
                                      attachments=await docs.attachments_of(db, d.id)))


@tool("docs", WRITE)
async def save_document(customer: str, title: str, body: str | None = None, document: str | None = None,
                        category: str | None = None, tags: list[str] | None = None,
                        machines: list[str] | None = None, append: bool = False) -> dict:
    """Write documentation for a customer: create a page, or change one (`document`: id or title). body is
    Markdown; with append=true it is added at the end of the existing text (for example a dated line under
    "Changes"). Every change keeps the previous version. Never put passwords or keys in documentation: store them
    with save_credential. category: network, procedure, notes, contacts, howto, hardware, software, security or
    other. machines: names or ids of the customer's machines this page is about."""
    from mmn.services import docs, vault

    p = current_principal()
    by = f"AI ({p.token_name})" if p else "AI"
    async with sessionmaker()() as db:
        e = await _doc_customer(db, customer)
        try:
            if document:
                d = await _document(db, document, e.id)
                data: dict[str, Any] = {"title": title}
                if body is not None:
                    data["body"] = (d.body.rstrip() + "\n\n" + body.strip() + "\n") if append else body
                if category:
                    data["category"] = category
                if tags is not None:
                    data["tags"] = tags
                if machines is not None:
                    data["node_ids"] = await _machine_ids(db, e.id, machines)
                changed = await docs.update(db, d, data, by=by)
            else:
                d = await docs.create(db, customer_id=e.id, title=title, body=body or "", category=category or "notes",
                                      tags=tags, node_ids=await _machine_ids(db, e.id, machines) or [], by=by)
                changed = ["created"]
        except docs.DocError as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        from mmn.services import events

        events.publish("docs.changed", {"id": d.id})
        return _jsonable({**docs.doc_out(d, names=await vault.machine_names(db), body=False), "changed": changed})


async def _credential(db, credential: str, customer: str | None):
    from mmn.services import vault

    e = await _doc_customer(db, customer)
    try:
        return await vault.find_item(db, credential, e.id if e else None)
    except vault.VaultError as ex:
        raise ToolDenied(str(ex)) from ex


def _vault_actor() -> dict:
    p = current_principal()
    return {"actor_kind": "ai", "username": p.username if p else None, "token_id": p.token_id if p else None,
            "token_name": p.token_name if p else None}


def _no_runbook(name: str) -> None:
    from mmn.services import gateway

    if gateway.runbook_call.get():
        raise ToolDenied(f"{name} cannot run as a runbook step: every use of a secret needs its own approval")


@tool("vault", RO)
async def list_credentials(customer: str | None = None, query: str | None = None, machine: str | None = None,
                           kind: str | None = None) -> dict:
    """The credentials in the password vault (names, kind, username, URL, linked machines, whether you may use it
    and whether it is due for rotation) WITHOUT the secrets. To use one, call reveal_credential (needs an admin's
    approval for every use) or, better, run_with_credential, which hands the secret to a script on the machine
    without it passing through you. kind: login, server, network, database, api_key, wifi, certificate, license,
    note or other."""
    from mmn.services import vault

    async with sessionmaker()() as db:
        e = await _doc_customer(db, customer)
        node_id = None
        if machine:
            node_id = await _uptime_machine(db, e.id, machine, "machine") if e else (await _node_id(machine))[0]
        rows = await vault.search_items(db, customer_id=e.id if e else None, q=query, kind=kind, node_id=node_id,
                                        limit=300)
        names = await vault.machine_names(db)
        out = []
        for r in rows:
            try:
                o = vault.item_out(r, names=names)
            except vault.VaultError:
                continue
            o["ai_may_use"] = r.ai_access == "approval"
            for k in ("created_by", "updated_by"):
                o.pop(k, None)
            out.append(o)
        return _jsonable({"credentials": out})


@tool("vault", DESTRUCTIVE)
async def reveal_credential(credential: str, reason: str, customer: str | None = None) -> dict:
    """Read the secret of a vault credential (password, username, notes and extra fields). Every use needs an
    admin's approval, whatever the policy says, and is written to the vault's access log with your `reason`.
    The first call answers pending_approval: tell the user and stop. Once approved, call reveal_credential again
    with the same arguments within the time the approval allows (15 minutes and one read by default) to get it.
    Never repeat the secret to the user, write it into documents or tickets, or keep it longer than the task
    needs. Prefer run_with_credential when the secret is only needed on a machine."""
    from mmn.services import gateway, vault

    _no_runbook("reveal_credential")
    if not reason or not reason.strip():
        raise ToolDenied("give a reason: it is shown to the admin who approves it and kept in the access log")
    p = current_principal()
    approval_id = gateway.approved_call.get()
    async with sessionmaker()() as db:
        item = await _credential(db, credential, customer)
        if item.ai_access != "approval":
            await vault.log_access(db, item, "ai_denied", reason=reason, success=False, error="not for the AI",
                                   **_vault_actor())
            await db.commit()
            raise ToolDenied(f"credential {item.name!r} is not available to the AI (an admin set it to never)")
        if approval_id:
            # The admin approved: give this token a short-lived grant (the secret itself is not stored with the
            # approval result).
            a = await db.get(gateway.AiApproval, approval_id)
            g = await vault.create_grant(db, item, token_id=p.token_id if p else None, approval_id=approval_id,
                                         approved_by=a.decided_by if a else None)
            await vault.log_access(db, item, "ai_approved", reason=reason, approval_id=approval_id,
                                   approved_by=a.decided_by if a else None, **_vault_actor())
            await db.commit()
            return {"status": "approved", "credential": item.name, "expires_at": g.expires_at.isoformat(),
                    "reads": g.uses_left,
                    "message": "Approved. Call reveal_credential again with the same arguments to read the secret "
                               f"before {g.expires_at.isoformat()}."}
        args = {"credential": credential, "reason": reason, "customer": customer}
        d = await gateway.decide(db, tool="reveal_credential", risk="destructive", args=_jsonable(args),
                                 token_id=p.token_id if p else None, token_customers=p.customers if p else None)
        if d.action not in ("paused", "block"):
            secret = await _consume_and_read(db, item, reason, p)
            if secret is not None:
                return secret
        item_id = item.id
    outcome = await _guard(p, "reveal_credential", "destructive", args)  # raises when paused or blocked
    async with sessionmaker()() as db:
        item = await db.get(vault.VaultItem, item_id)
        if isinstance(outcome, dict) and outcome.get("status") == "pending_approval":
            await vault.log_access(db, item, "ai_request", reason=reason, approval_id=outcome.get("approval_id"),
                                   **_vault_actor())
            await db.commit()
            return outcome
        # Approved (and the grant made) while the call waited.
        secret = await _consume_and_read(db, item, reason, p)
        if secret is not None:
            return secret
    raise ToolDenied("the approval did not give access; ask the user")


async def _consume_and_read(db, item, reason: str, p) -> dict | None:
    from mmn.services import vault

    g = await vault.consume_grant(db, item, p.token_id if p else None)
    if g is None:
        return None
    out = vault.item_out(item, reveal=True)
    await vault.log_access(db, item, "ai_reveal", reason=reason, approval_id=g.approval_id,
                           approved_by=g.approved_by, reads_left=g.uses_left, **_vault_actor())
    await vault.announce_ai_use(db, {"item": item.name, "token": p.token_name if p else None, "action": "reveal"})
    await db.commit()
    return _jsonable({"credential": item.name, "kind": item.kind, "url": item.url, "username": out["username"],
                      "password": out["password"], "notes": out["notes"], "fields": out["fields"],
                      "reads_left": g.uses_left,
                      "note": "Use it only for this task. Do not repeat it to the user or write it down."})


@tool("vault", DESTRUCTIVE)
async def run_with_credential(server: str, credential: str, script: str, reason: str, customer: str | None = None,
                              timeout: int = 300) -> dict:
    """Run a script on a machine with a vault credential, without the secret passing through you. The script
    gets it in environment variables: SP_USERNAME, SP_PASSWORD, SP_URL and SP_FIELD_<NAME> for extra fields
    (bash on Linux: "$SP_PASSWORD"; PowerShell on Windows: $env:SP_PASSWORD). The secret is wiped from the output.
    Every run needs an admin's approval (the admin sees the script and your reason) and the token also needs the
    `exec` scope; the credential and the machine must belong to the same customer. timeout: seconds, max 1800."""
    from mmn.models import Node, NodeJob
    from mmn.services import agent_hub, gateway, nodes, vault

    _no_runbook("run_with_credential")
    p = _check("exec")
    approval_id = gateway.approved_call.get()
    if not approval_id:
        raise ToolDenied("run_with_credential only runs after an admin approved it")
    if not reason.strip() or not script.strip():
        raise ToolDenied("give a script and a reason")
    nid, nname = await _node_id(server)
    async with sessionmaker()() as db:
        item = await _credential(db, credential, customer)
        n = await db.get(Node, nid)
        if item.ai_access != "approval":
            await vault.log_access(db, item, "ai_denied", reason=reason, success=False, error="not for the AI",
                                   **_vault_actor())
            await db.commit()
            raise ToolDenied(f"credential {item.name!r} is not available to the AI (an admin set it to never)")
        if n is None or n.environment_id != item.customer_id:
            raise ToolDenied(f"{item.name} does not belong to the customer of {nname}")
        sec = vault.read_secret(item)
        a = await db.get(gateway.AiApproval, approval_id)
        approved_by = a.decided_by if a else None
        item_name, item_url = item.name, item.url or ""
    env = {"SP_USERNAME": sec["username"], "SP_PASSWORD": sec["password"], "SP_URL": item_url}
    # Wiped from the output: the password and the secret extra fields (not the username or URL; a short
    # username would wipe half the output).
    secrets_ = [sec["password"]] if len(sec["password"]) >= 3 else []
    for f in sec["fields"]:
        key = "SP_FIELD_" + re.sub(r"[^A-Za-z0-9]+", "_", f["name"]).strip("_").upper()[:50]
        if key != "SP_FIELD_":
            env[key] = f["value"]
            if f["secret"] and len(f["value"]) >= 3:
                secrets_.append(f["value"])
    try:
        t = max(1, min(timeout, 1800))
        # Same as guests.script, but the secrets are registered for scrubbing before the agent starts.
        job = await nodes.create_job(nid, "vault.run", {"credential": item_name, "reason": reason[:300]}, source="mcp",
                                     requested_by=f"{p.username} (AI: {p.token_name})", exclusive=False)
        agent_hub.scrub(job.id, secrets_)
        try:
            await agent_hub.request(nid, "script.run", {"script": script, "env": env, "timeout": t, "job_id": job.id},
                                    timeout=60)
        except Exception as e:  # noqa: BLE001
            await agent_hub.finish_job(job.id, "failed", {}, str(e))
            raise
        status, result, error = await agent_hub.wait_job(job.id, t + 30)
    except Exception as e:  # noqa: BLE001
        async with sessionmaker()() as db:
            item = await db.get(vault.VaultItem, item.id)
            await vault.log_access(db, item, "ai_use", reason=reason, approval_id=approval_id, success=False,
                                   machine=nname, error=str(e)[:500], **_vault_actor())
            await db.commit()
        raise _agent_errors(e) from e
    finally:
        env.clear()
    async with sessionmaker()() as db:
        j = await db.get(NodeJob, job.id)
        output = vault.scrub(j.output if j else "", secrets_)
        if j is not None:
            j.output = output
        item = await db.get(vault.VaultItem, item.id)
        await vault.log_access(db, item, "ai_use", reason=reason, approval_id=approval_id, approved_by=approved_by,
                               success=status == "success", machine=nname, job_id=job.id,
                               exit_code=(result or {}).get("exit_code"), **_vault_actor())
        await vault.announce_ai_use(db, {"item": item_name, "token": p.token_name, "action": "use", "machine": nname})
        await db.commit()
    agent_hub.scrub(job.id, None)
    return _jsonable({"status": status, "exit_code": (result or {}).get("exit_code"), "machine": nname,
                      "credential": item_name, "output": output[-60000:], "error": vault.scrub(error or "", secrets_)
                      or None, "job_id": job.id})


@tool("vault", WRITE)
async def save_credential(customer: str, name: str, credential: str | None = None, kind: str | None = None,
                          username: str | None = None, password: str | None = None, url: str | None = None,
                          notes: str | None = None, machines: list[str] | None = None,
                          tags: list[str] | None = None) -> dict:
    """Store a credential in the customer's vault (encrypted), or change one (`credential`: id or name), for
    example after you created an account or changed a password with run_command. A changed password keeps the old
    one in the item's history. kind: login, server, network, database, api_key, wifi, certificate, license, note or
    other. machines: names or ids of the customer's machines it belongs to. Returns the item without the secret.
    New items may be used by the AI (with approval); an admin can change that."""
    from mmn.services import events, vault

    p = current_principal()
    by = f"AI ({p.token_name})" if p else "AI"
    async with sessionmaker()() as db:
        e = await _doc_customer(db, customer)
        try:
            if credential:
                item = await _credential(db, credential, e.id)
                data: dict[str, Any] = {"name": name}
                for k, v in (("kind", kind), ("username", username), ("password", password), ("url", url),
                             ("notes", notes), ("tags", tags)):
                    if v is not None:
                        data[k] = v
                if machines is not None:
                    data["node_ids"] = await _machine_ids(db, e.id, machines)
                changed = await vault.update_item(db, item, data, by=by)
                await vault.log_access(db, item, "update", changed=changed, **_vault_actor())
            else:
                item = await vault.create_item(db, customer_id=e.id, name=name, kind=kind or "login", url=url,
                                               payload={"username": username or "", "password": password or "",
                                                        "notes": notes or ""},
                                               tags=tags, node_ids=await _machine_ids(db, e.id, machines) or [], by=by)
                changed = ["created"]
                await vault.log_access(db, item, "create", **_vault_actor())
        except vault.VaultError as ex:
            raise ToolDenied(str(ex)) from ex
        await db.commit()
        events.publish("vault.changed", {"id": item.id})
        return _jsonable({**vault.item_out(item, names=await vault.machine_names(db)), "changed": changed})


# ------------------------------------------------------------------ Microsoft 365 / Entra ID (phase 19)
async def _m365_tenant(db, customer: str, tenant: str | None):
    from mmn.services import m365

    e = await _uptime_customer(db, customer)
    try:
        return e, await m365.tenant_for_customer(db, e.id, tenant)
    except m365.M365Error as ex:
        raise ToolDenied(str(ex)) from ex


def _m365_user_view(u: dict) -> dict:
    mfa = u.get("mfa")
    return {"id": u.get("id"), "name": u.get("name"), "upn": u.get("upn"), "enabled": u.get("enabled"),
            "type": u.get("type"), "licences": [m365_sku(x) for x in u.get("licences") or []], "admin": u.get("admin"),
            "roles": u.get("roles") or [], "mfa": None if mfa is None else {"registered": mfa.get("registered"),
                                                                            "methods": mfa.get("methods"),
                                                                            "default": mfa.get("default")},
            "risk": u.get("risk"), "last_sign_in": u.get("last_sign_in"), "last_activity": u.get("last_activity"),
            "stale": u.get("stale"), "synced_from_ad": u.get("synced"), "department": u.get("department"),
            "job": u.get("job")}


def m365_sku(part: str) -> str:
    from mmn.services import m365

    return m365.sku_name(part)


@tool("m365", RO)
async def list_m365_tenants(customer: str | None = None) -> dict:
    """The Microsoft 365 / Entra ID tenants connected to customers: status of the last sync, organisation and
    domains, counts (users, admins, users and admins without MFA, risky users, licences bought and used),
    security defaults and Conditional Access, when the app's secret or certificate expires, which parts
    Stackpilot can read (features) and which write actions the app has permission for."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        e = await _uptime_customer(db, customer)
        rows = await m365.tenants_of(db, e.id if e else None)
        out = []
        for t in rows:
            x = m365.tenant_out(t)
            out.append({k: x[k] for k in ("id", "customer_name", "name", "tenant_id", "status", "last_sync_at",
                                          "last_error", "organization", "counts", "security", "mfa_source",
                                          "app_credential", "credential_days", "features", "actions", "settings")})
    return _jsonable({"tenants": out})


@tool("m365", RO)
async def list_m365_users(customer: str, filter: str = "all", query: str | None = None, tenant: str | None = None,
                          limit: int = 200) -> dict:
    """Users of the customer's Microsoft 365 tenant with licences, admin roles, MFA registration, Entra ID risk
    and last sign-in. `filter`: all, admins, no_mfa (enabled members without MFA), risky, disabled, guests, stale
    (licensed but no sign-in for the tenant's stale_days), unlicensed, licensed, disabled_licensed (blocked but
    still licensed: costs money). `query` searches name, sign-in name, mail, department and job title."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        try:
            users = m365.filter_users(t, filter, query)
        except m365.M365Error as ex:
            raise ToolDenied(str(ex)) from ex
        return _jsonable({"tenant": t.name, "synced_at": t.last_sync_at, "total": len(users),
                          "users": [_m365_user_view(u) for u in users[:max(1, min(limit, 1000))]]})


@tool("m365", RO)
async def get_m365_user(customer: str, user: str, tenant: str | None = None) -> dict:
    """One Microsoft 365 user (sign-in name, mail, id or exact display name) with licences, roles, MFA methods,
    risk, the sign-ins of the last 30 days that stood out (risky, failed, foreign country, legacy
    authentication), their risk detections and which actions are possible with `m365_user_action`."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        u = m365.find_user(t, user)
        if u is None:
            raise ToolDenied(f"user {user} not found in {t.name}")
        snap = t.snapshot or {}
        return _jsonable({**_m365_user_view(u), "tenant": t.name,
                          "signins": await m365.signins(db, tenant_id=t.id, user=u.get("upn") or u["id"],
                                                        hours=24 * 30, limit=50),
                          "risk_detections": [d for d in snap.get("risk_detections") or [] if d.get("user_id") == u["id"]],
                          "possible_actions": [a for a, ok in m365.permissions_view(snap.get("granted") or [])["actions"].items()
                                               if ok]})


@tool("m365", RO)
async def list_m365_licences(customer: str, tenant: str | None = None) -> dict:
    """The licences (subscriptions) of the customer's tenant: bought, assigned, free, status (Enabled, Warning
    = expired, Suspended), plus who is blocked but still licensed."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        snap = t.snapshot or {}
        return _jsonable({"tenant": t.name, "licences": snap.get("licences") or [],
                          "disabled_licensed": [{"upn": u.get("upn"), "licences": [m365_sku(x) for x in u["licences"]]}
                                                for u in m365.filter_users(t, "disabled_licensed")]})


@tool("m365", RO)
async def list_m365_signins(customer: str, flag: str | None = None, user: str | None = None, hours: int = 168,
                            tenant: str | None = None, limit: int = 100) -> dict:
    """Sign-ins of the customer's tenant that stood out in the last `hours` (default a week): `flag` risky (Entra
    ID Protection), failed, foreign (outside the tenant's allowed countries) or legacy (old protocols such as
    IMAP, POP, SMTP AUTH). Ordinary successful sign-ins are not stored. Includes the failed sign-ins per user in
    the last 24 hours."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        rows = await m365.signins(db, tenant_id=t.id, user=user, flag=flag, hours=hours, limit=max(1, min(limit, 500)))
        failed = await m365.failed_counts(db, [t.id])
        return _jsonable({"tenant": t.name, "signins": rows,
                          "failed_24h": sorted(({"user": u, "count": n} for (_, u), n in failed.items()),
                                               key=lambda x: -x["count"])[:50]})


@tool("m365", WRITE)
async def refresh_m365(customer: str, tenant: str | None = None) -> dict:
    """Read the customer's Microsoft 365 tenant again now (normally every 15 minutes): users, licences, MFA,
    risk and new sign-ins."""
    from mmn.services import m365

    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        tid = t.id
    try:
        res = await m365.sync(tid)
    except m365.M365Error as ex:
        raise ToolDenied(str(ex)) from ex
    return _jsonable(res)


@tool("m365_admin", DESTRUCTIVE)
async def m365_user_action(customer: str, user: str, action: str, reason: str, confirm: str,
                           tenant: str | None = None) -> dict:
    """Change a Microsoft 365 user. `action`: block (sign-in blocked and signed out everywhere), unblock,
    revoke_sessions (sign out everywhere), reset_mfa (removes the MFA methods: the user registers again at the
    next sign-in), reset_password (a temporary password that must be changed; it is stored in the customer's
    password vault, never returned), confirm_compromised or dismiss_risk (Entra ID Protection). Always needs an
    admin's approval: the call answers pending_approval and runs once approved. Set `confirm` to the user's
    sign-in name (UPN) and give a `reason`."""
    from mmn.services import m365

    p = current_principal()
    async with sessionmaker()() as db:
        _, t = await _m365_tenant(db, customer, tenant)
        u = m365.find_user(t, user)
    upn = (u or {}).get("upn") or user
    if confirm.strip().lower() != str(upn).lower():
        raise ToolDenied(f"confirm must equal the user's sign-in name '{upn}'")
    if action not in m365.ACTIONS:
        raise ToolDenied(f"action must be one of {', '.join(m365.ACTIONS)}")
    try:
        res = await m365.user_action(t, upn, action, by=p.token_name if p else "ai", reason=reason)
    except m365.M365Error as ex:
        raise ToolDenied(str(ex)) from ex
    async with sessionmaker()() as db:
        from mmn.services.audit import audit

        await audit(db, f"m365.user.{action}", source="mcp", target=res.get("user") or upn, tenant=t.name,
                    reason=reason, token=p.token_name if p else None)
        await db.commit()
    if "vault_item" in res:
        res["vault_item"] = {"name": res["vault_item"]["name"],
                             "note": "the temporary password is in the customer's vault; an admin can show it there"}
    return _jsonable(res)


# ------------------------------------------------------------------ MCP hub
@tool("hub", RO)
async def list_mcp_servers() -> list[dict]:
    """The other MCP servers registered in the MCP hub that this token can use: prefix of their tools,
    customer, status (online, error, auth_required), number of tools offered and waiting for an admin."""
    from mmn.models import McpUpstream
    from mmn.services import hub

    p = current_principal()
    async with sessionmaker()() as db:
        names = await hub.env_names(db)
        with tenancy_unrestricted():
            rows = list((await db.execute(select_upstreams(McpUpstream))).scalars())
    out = []
    for u in rows:
        if p is not None and p.customers is not None and (u.environment_id is None or u.environment_id not in p.customers):
            continue
        o = hub.upstream_out(u, names)
        out.append({k: o[k] for k in ("name", "namespace", "description", "customer_name", "transport", "enabled",
                                      "status", "last_error", "last_checked_at", "active_count", "pending_count")}
                   | {"tool_prefix": f"{u.namespace}__"})
    return _jsonable(out)


@tool("hub", WRITE)
async def refresh_mcp_server(server: str) -> dict:
    """Reconnect to one MCP hub server (prefix or name) and read its tools again. Tools that are new or
    changed still wait for an admin to accept them in Stackpilot (MCP hub)."""
    from mmn.models import McpUpstream
    from mmn.services import hub

    p = current_principal()
    async with sessionmaker()() as db:
        with tenancy_unrestricted():
            rows = list((await db.execute(select_upstreams(McpUpstream))).scalars())
        u = next((r for r in rows if server.lower() in (r.namespace, r.name.lower(), r.id)), None)
        if u is None or (p is not None and p.customers is not None
                         and (u.environment_id is None or u.environment_id not in p.customers)):
            raise ToolDenied("MCP server not found")
        try:
            diff = await hub.refresh(db, u)
        except hub.HubError as e:
            await db.commit()
            raise ToolDenied(f"{u.name}: {e}") from e
        await db.commit()
        await hub.sync(db)
    return {"ok": True, "server": u.name, "status": u.status, **diff}


def tenancy_unrestricted():
    from mmn.services import tenancy

    return tenancy.unrestricted()


def select_upstreams(model):
    from sqlalchemy import select

    return select(model).order_by(model.name)


def _hub_result_summary(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {"type": type(res).__name__}
    text = " ".join(str(c.get("text") or "") for c in res.get("content") or [] if isinstance(c, dict))
    return {"is_error": bool(res.get("isError")), "output": text[:1500]}


async def run_hub_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool of an MCP hub server, with the same checks as built-in tools: the ``hub`` scope, the
    token's customers, the guardrails (allow, approve, block, paused) and the audit log."""
    from mmn.services import gateway, hub

    started = time.monotonic()
    p = _principal.get()
    approval_id = gateway.approved_call.get()
    rb_call = gateway.runbook_call.get()
    extra_kw = {"approval_id": approval_id} if approval_id else {}
    spec = gateway.REGISTRY.get(name)
    h = hub.HUB_TOOLS.get(name)
    args = dict(arguments or {})
    target = h.upstream_name if h else gateway.namespace_of(name)
    info = {"upstream": h.upstream_name if h else None, "upstream_tool": h.name if h else None}
    if rb_call:
        info["runbook_run"] = rb_call

    def ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        _check("hub")
        if spec is None or spec.source != "hub" or h is None:
            raise ToolDenied(f"unknown tool {name}; the MCP hub may not offer it (anymore)")
        if p is not None and p.customers is not None and (spec.customer_id is None or spec.customer_id not in p.customers):
            raise ToolDenied("this MCP server is not available to this token (it belongs to another customer, or "
                             "is shared and this token is tied to specific customers)")
        if approval_id is None and rb_call is None:
            outcome = await _guard(p, name, spec.risk, args)
            if outcome is not None:
                await _audit(p, name, args, True, result=outcome, ms=ms(), target=target, extra=info)
                return outcome
        result = await hub.call_tool(name, args)
    except Exception as e:
        msg = str(e) or type(e).__name__
        await _audit(p, name, args, False, error=msg, ms=ms(), target=target, extra=info, **extra_kw)
        if isinstance(e, ToolError):
            raise
        raise ToolDenied(msg if isinstance(e, hub.HubError) else f"{type(e).__name__}: {msg}") from e
    summary = _hub_result_summary(result)
    if summary.get("is_error"):
        await _audit(p, name, args, False, error=summary.get("output") or "the tool reported an error", ms=ms(),
                     target=target, extra=info, **extra_kw)
    else:
        await _audit(p, name, args, True, result=summary, ms=ms(), target=target, extra=info, **extra_kw)
    return result


async def _rules(p: Principal | None) -> tuple[dict[str, str], dict[str, str]]:
    from mmn.services import gateway

    async with sessionmaker()() as db:
        g = await gateway.get_policy(db, "global")
        t = await gateway.get_policy(db, "token", p.token_id) if p is not None else None
    return dict(g.tools or {}) if g else {}, dict(t.tools or {}) if t else {}


def _blocked(name: str, *layers: dict[str, str]) -> bool:
    from mmn.services import gateway

    ns = gateway.namespace_of(name)
    for rules in layers:
        if rules.get(name) == "block" or (ns and rules.get(f"{ns}__*") == "block"):
            return True
    return False


_builtin_list_tools = mcp.list_tools
_builtin_call_tool = mcp.call_tool


async def _list_tools():
    """Built-in tools plus the MCP hub tools this token may use. Tools that the tool catalog switched off
    (a "block" rule for the tool in the defaults or for this token) are left out."""
    from mcp.types import Tool as MCPTool

    from mmn.services import hub

    tools = await _builtin_list_tools()
    p = _principal.get()
    if p is None:
        return tools
    g_rules, t_rules = await _rules(p)
    out = [t for t in tools if not _blocked(t.name, g_rules, t_rules)]
    if "hub" not in p.scopes:
        return out
    await hub.ensure_synced()
    for h in sorted(hub.HUB_TOOLS.values(), key=lambda h: h.exposed):
        if p.customers is not None and (h.customer_id is None or h.customer_id not in p.customers):
            continue
        if _blocked(h.exposed, g_rules, t_rules):
            continue
        ann = {k: v for k, v in h.annotations.items() if k != "title"}
        ann.setdefault("readOnlyHint", h.risk == "read")
        ann["destructiveHint"] = h.risk == "destructive"
        out.append(MCPTool(name=h.exposed, title=h.title, description=f"[{h.upstream_name}] {h.description}".strip(),
                           input_schema=h.input_schema or {"type": "object", "properties": {}},
                           annotations=ToolAnnotations(**ann)))
    return out


async def _call_tool(name: str, arguments: dict[str, Any], context=None):
    from mcp.types import CallToolResult, TextContent

    from mmn.services import gateway, hub

    spec = gateway.REGISTRY.get(name)
    if gateway.namespace_of(name) is None or (spec is not None and spec.source is None):
        return await _builtin_call_tool(name, arguments, context)
    await hub.ensure_synced()
    try:
        res = await run_hub_tool(name, arguments)
    except ToolError as e:
        return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
    if isinstance(res, dict) and isinstance(res.get("content"), list) and "status" not in res:
        try:
            return CallToolResult.model_validate(res)
        except Exception:  # noqa: BLE001 - fall back to plain JSON
            pass
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(res, default=str))],
                          structured_content=res if isinstance(res, dict) else None)


mcp.list_tools = _list_tools  # type: ignore[method-assign]
mcp.call_tool = _call_tool  # type: ignore[method-assign]


# ------------------------------------------------------------------ resources: customer context
async def _read_context(customer: str | None) -> str:
    from mmn.services import customers, nodes

    p = _principal.get()
    if p is None or "read" not in p.scopes:
        raise ToolDenied("not authenticated")
    async with sessionmaker()() as db:
        if customer:
            try:
                return customers.context_markdown(await nodes.get_environment(db, customer))
            except nodes.NodeError as e:
                raise ToolDenied(str(e)) from e
        envs = await nodes.list_environments(db)
        if not envs:
            return "No customers."
        return "\n\n---\n\n".join(f"{customers.context_markdown(e)}\n\n(id: {e.id})" for e in envs)


@mcp.resource("stackpilot://customers", name="customers", title="Customer notes and maintenance windows",
              description="Notes for the AI, maintenance window, SLA and contacts of every customer this token "
                          "may see. Follow them before changing anything.", mime_type="text/markdown")
async def customers_resource() -> str:
    return await _read_context(None)


@mcp.resource("stackpilot://customers/{customer}", name="customer", title="Customer context",
              description="Notes for the AI, maintenance window, SLA and contacts of one customer (id or name).",
              mime_type="text/markdown")
async def customer_resource(customer: str) -> str:
    return await _read_context(customer)


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
                                          ip=peer[0] if peer else None, client=client,
                                          customers=frozenset(tok.customer_ids) if tok.customer_ids is not None
                                          else None)
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

        from mmn.services import tenancy

        token = _principal.set(principal)
        limit = tenancy.set_allowed(principal.customers)
        try:
            await self.app(scope, replay if scope.get("method") == "POST" else receive, send)
        finally:
            tenancy.reset(limit)
            _principal.reset(token)


def build_mcp_app() -> ASGIApp:
    from mcp.server.transport_security import TransportSecuritySettings

    # Every request needs a bearer token, which already rules out DNS rebinding; the public host name
    # behind Zoraxy is not known in advance.
    app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return BearerAuth(app)
