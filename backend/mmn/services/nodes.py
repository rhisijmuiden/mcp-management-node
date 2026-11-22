"""Customers (stored as environments), nodes and the actions on them. Used by the REST API and the MCP server alike."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Environment, Node, NodeJob, PairingCode
from mmn.security import decrypt_secret, encrypt_secret, hash_token
from mmn.services import agent_ca, agent_hub, agent_packages, events
from mmn.services.agent_hub import AgentError, AgentOffline
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.nodes")

NOTIFY_TYPES = ["vzdump", "replication", "fencing", "package-updates", "system-mail"]
SEVERITIES = ["info", "notice", "warning", "error", "unknown"]
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")


SECRET_ARGS = {"password", "cipassword", "ssh-public-keys", "sshkeys"}


class NodeError(Exception):
    """A refusal with a message for the user (HTTP 400/409, MCP error)."""


# ------------------------------------------------------------------ serialisation
def node_out(n: Node, bundled: str | None = None) -> dict[str, Any]:
    bundled = bundled if bundled is not None else agent_packages.bundled_version()
    upd = n.updates or {}
    m = n.metrics or {}
    return {
        "id": n.id, "name": n.name, "kind": n.kind, "role": n.role or "server", "hostname": n.hostname,
        "environment_id": n.environment_id, "environment_name": n.environment.name if n.environment else None,
        "customer_id": n.environment_id, "customer": n.environment.name if n.environment else None,
        "cluster_name": n.cluster_name, "online": agent_hub.is_online(n.id),
        "agent_version": n.agent_version, "agent_update_available": bool(bundled and n.agent_version
                                                                          and n.agent_version != bundled),
        "os": n.os, "arch": n.arch, "install_kind": n.install_kind, "capabilities": n.capabilities or [],
        "info": n.info or {}, "metrics": m,
        "updates": {"count": len(upd.get("packages") or []), "checked_at": upd.get("checked_at"),
                    "reboot_required": bool(upd.get("reboot_required")), "error": upd.get("error"),
                    "packages": upd.get("packages") or []},
        "governor": {**(n.governor or {}), "desired": n.governor_desired},
        "notify": {"applied_at": n.notify_applied_at, "error": n.notify_error, "hash": n.notify_hash},
        "remote_ip": n.remote_ip, "enrolled_at": n.enrolled_at, "last_seen_at": n.last_seen_at,
        "connected_at": n.connected_at, "cert_expires_at": n.cert_expires_at,
    }


def node_summary(n: Node) -> dict[str, Any]:
    """Compact form for lists and MCP (no package list)."""
    d = node_out(n)
    d["updates"] = {k: v for k, v in d["updates"].items() if k != "packages"}
    d["info"] = {k: d["info"].get(k) for k in ("pve_version", "kernel", "os_pretty_name", "cpu_model") if k in d["info"]}
    return d


def env_out(e: Environment, nodes: list[Node] | None = None) -> dict[str, Any]:
    nodes = nodes if nodes is not None else list(e.nodes)
    online = sum(1 for n in nodes if agent_hub.is_online(n.id))
    return {"id": e.id, "name": e.name, "kind": e.kind, "description": e.description, "color": e.color,
            "created_at": e.created_at, "node_count": len(nodes), "nodes_online": online,
            "contact_name": e.contact_name, "contact_email": e.contact_email, "contact_phone": e.contact_phone,
            "contacts": e.contacts or [], "notes": e.notes, "maintenance_window": e.maintenance_window,
            "sla": e.sla,
            "counts": {"proxmox": sum(1 for n in nodes if n.kind == "proxmox"),
                       "servers": sum(1 for n in nodes if n.kind != "proxmox" and (n.role or "server") == "server"),
                       "workstations": sum(1 for n in nodes if n.kind != "proxmox" and n.role == "workstation")},
            "updates": sum(len((n.updates or {}).get("packages") or []) for n in nodes),
            "notify_enabled": bool((e.notify or {}).get("enabled")),
            "clusters": sorted({n.cluster_name for n in nodes if n.cluster_name})}


def job_out(j: NodeJob, with_output: bool = True) -> dict[str, Any]:
    d = {"id": j.id, "node_id": j.node_id, "kind": j.kind, "status": j.status, "source": j.source,
         "requested_by": j.requested_by, "params": j.params, "result": j.result, "error": j.error,
         "started_at": j.started_at, "finished_at": j.finished_at}
    if with_output:
        d["output"] = j.output
    return d


# ------------------------------------------------------------------ environments
class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)


class EnvironmentIn(BaseModel):
    """A customer. ``kind`` home is your own environment, customer is someone you manage for."""

    name: str = Field(min_length=1, max_length=100)
    kind: Literal["home", "customer"] = "customer"
    description: str | None = Field(default=None, max_length=500)
    color: str = Field(default="lucht", pattern=r"^[a-z]{3,16}$")
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=50)
    contacts: list[ContactIn] = Field(default_factory=list, max_length=50)
    notes: str | None = Field(default=None, max_length=20000)
    maintenance_window: str | None = Field(default=None, max_length=200)
    sla: str | None = Field(default=None, max_length=200)

    def details(self) -> dict[str, Any]:
        def clean(v: str | None) -> str | None:
            return v.strip() or None if v else None

        return {"contact_name": clean(self.contact_name), "contact_email": clean(self.contact_email),
                "contact_phone": clean(self.contact_phone), "notes": clean(self.notes),
                "maintenance_window": clean(self.maintenance_window), "sla": clean(self.sla),
                "contacts": [c.model_dump() for c in self.contacts]}


async def list_environments(db: AsyncSession) -> list[Environment]:
    return list((await db.execute(select(Environment).order_by(Environment.kind.desc(), Environment.name))).scalars())


async def get_environment(db: AsyncSession, env_id: str) -> Environment:
    e = await db.get(Environment, env_id)
    if e is None:
        e = (await db.execute(select(Environment).where(func.lower(Environment.name) == env_id.lower()))).scalar_one_or_none()
    if e is None:
        raise NodeError("customer not found")
    return e


async def create_environment(db: AsyncSession, body: EnvironmentIn) -> Environment:
    if (await db.execute(select(Environment).where(func.lower(Environment.name) == body.name.lower()))).first():
        raise NodeError("a customer with this name exists already")
    e = Environment(name=body.name.strip(), kind=body.kind, description=body.description, color=body.color, notify={},
                    **body.details())
    db.add(e)
    await db.flush()
    return e


async def update_environment(db: AsyncSession, e: Environment, body: EnvironmentIn) -> Environment:
    other = (await db.execute(select(Environment).where(func.lower(Environment.name) == body.name.lower(),
                                                        Environment.id != e.id))).first()
    if other:
        raise NodeError("a customer with this name exists already")
    e.name, e.kind, e.description, e.color = body.name.strip(), body.kind, body.description, body.color
    for k, v in body.details().items():
        setattr(e, k, v)
    await db.flush()
    return e


async def delete_environment(db: AsyncSession, e: Environment) -> None:
    if e.nodes:
        raise NodeError("move or remove the machines of this customer first")
    from mmn.models import UnifiConnection

    if (await db.execute(select(UnifiConnection.id).where(UnifiConnection.environment_id == e.id))).first():
        raise NodeError("remove the UniFi connections of this customer first")
    await db.delete(e)
    await db.flush()


# ------------------------------------------------------------------ notifications per environment
class NotifyIn(BaseModel):
    enabled: bool = False
    use_app_smtp: bool = True
    server: str = Field(default="", max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    mode: Literal["starttls", "tls", "insecure"] = "starttls"
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=500)  # None = keep
    from_address: EmailStr | Literal[""] = ""
    author: str = Field(default="", max_length=100)
    mailto: list[EmailStr] = Field(default_factory=list, max_length=20)
    types: list[str] = Field(default_factory=lambda: ["vzdump", "replication", "fencing", "package-updates"])
    severities: list[str] = Field(default_factory=lambda: ["warning", "error"])

    @field_validator("types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(NOTIFY_TYPES)
        if bad:
            raise ValueError(f"unknown notification types: {', '.join(sorted(bad))}")
        return [t for t in NOTIFY_TYPES if t in v]

    @field_validator("severities")
    @classmethod
    def _sev(cls, v: list[str]) -> list[str]:
        bad = set(v) - set(SEVERITIES)
        if bad:
            raise ValueError(f"unknown severities: {', '.join(sorted(bad))}")
        return [s for s in SEVERITIES if s in v]


def notify_public(e: Environment) -> dict[str, Any]:
    cfg = {**NotifyIn().model_dump(exclude={"password"}), **{k: v for k, v in (e.notify or {}).items()
                                                            if k != "password_enc"}}
    cfg["password_set"] = bool((e.notify or {}).get("password_enc"))
    return cfg


async def set_notify(db: AsyncSession, e: Environment, body: NotifyIn) -> dict[str, Any]:
    cur = dict(e.notify or {})
    data = body.model_dump(exclude={"password"})
    data["mailto"] = [str(m) for m in body.mailto]
    data["from_address"] = str(body.from_address)
    if body.password is not None:
        data["password_enc"] = encrypt_secret(body.password) if body.password else None
    else:
        data["password_enc"] = cur.get("password_enc")
    if body.enabled:
        if not body.mailto:
            raise NodeError("add at least one recipient")
        if not body.use_app_smtp and (not body.server or not body.from_address):
            raise NodeError("SMTP server and from address are required")
        if body.use_app_smtp:
            mail = await get_setting(db, "mail")
            if not mail.get("host") or not mail.get("from_address"):
                raise NodeError("the app's own mail server is not set up yet (Administration > Mail server)")
    e.notify = data
    await db.flush()
    return notify_public(e)


async def effective_notify(db: AsyncSession, e: Environment) -> dict[str, Any]:
    """The payload sent to the agents (with the password)."""
    n = e.notify or {}
    out: dict[str, Any] = {"name": "mmn-smtp", "enabled": bool(n.get("enabled")), "mailto": n.get("mailto", []),
                           "types": n.get("types", []), "severities": n.get("severities", []),
                           "author": n.get("author") or f"Proxmox VE · {e.name}"}
    if n.get("use_app_smtp", True):
        mail = await get_setting(db, "mail")
        mode = {"none": "insecure", "starttls": "starttls", "tls": "tls"}.get(mail.get("security"), "starttls")
        out.update(server=mail.get("host", ""), port=int(mail.get("port") or 587), mode=mode,
                   username=mail.get("username", ""), password=decrypt_secret(mail.get("password_enc")) or "",
                   from_address=mail.get("from_address", ""))
    else:
        out.update(server=n.get("server", ""), port=int(n.get("port") or 587), mode=n.get("mode", "starttls"),
                   username=n.get("username", ""), password=decrypt_secret(n.get("password_enc")) or "",
                   from_address=n.get("from_address", ""))
    return out


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def push_notify_to_node(node_id: str) -> dict[str, Any]:
    async with sessionmaker()() as db:
        node = await db.get(Node, node_id)
        if node is None:
            raise NodeError("node not found")
        payload = await effective_notify(db, node.environment)
    if "pve-notifications" not in (node.capabilities or []):
        raise NodeError("this node has no Proxmox VE notification system")
    h = _hash(payload)
    error = None
    try:
        await agent_hub.request(node_id, "notifications.apply", payload, timeout=90)
    except AgentError as e:
        error = str(e)
    async with sessionmaker()() as db:
        node = await db.get(Node, node_id)
        node.notify_error = error
        if error is None:
            node.notify_hash, node.notify_applied_at = h, datetime.now(UTC)
        await db.commit()
    events.publish("node.status", {"node_id": node_id, "online": True, "type": "notify"})
    return {"node_id": node_id, "ok": error is None, "error": error}


async def push_notify_to_environment(env_id: str) -> list[dict[str, Any]]:
    async with sessionmaker()() as db:
        e = await get_environment(db, env_id)
        ids = [n.id for n in e.nodes if "pve-notifications" in (n.capabilities or [])]
    results = []
    for nid in ids:
        if not agent_hub.is_online(nid):
            results.append({"node_id": nid, "ok": False, "error": "offline (applied when it reconnects)"})
            continue
        try:
            results.append(await push_notify_to_node(nid))
        except (NodeError, AgentError) as ex:
            results.append({"node_id": nid, "ok": False, "error": str(ex)})
    # In a cluster /etc/pve is shared: one node is enough, but pushing to all keeps the status per node.
    return results


async def test_notify(node_id: str) -> dict[str, Any]:
    await agent_hub.request(node_id, "notifications.test", {"target": "mmn-smtp"}, timeout=90)
    return {"ok": True}


# ------------------------------------------------------------------ pairing
def _new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def normalize_code(code: str) -> str:
    c = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return f"{c[:4]}-{c[4:8]}-{c[8:12]}" if len(c) == 12 else c


async def create_pairing_code(db: AsyncSession, env: Environment, *, label: str | None = None,
                              node_id: str | None = None, hours: int = 24, created_by: str | None = None,
                              role: str | None = None) -> tuple[PairingCode, str]:
    code = _new_code()
    pc = PairingCode(environment_id=env.id, code_hash=hash_token(code), code_enc=encrypt_secret(code),
                     label=label, node_id=node_id, created_by=created_by,
                     role=role if role in ("server", "workstation") else None,
                     expires_at=datetime.now(UTC) + timedelta(hours=max(1, min(hours, 24 * 7))))
    db.add(pc)
    await db.flush()
    return pc, code


def pairing_out(pc: PairingCode) -> dict[str, Any]:
    return {"id": pc.id, "environment_id": pc.environment_id, "environment_name": pc.environment.name,
            "customer_id": pc.environment_id, "customer": pc.environment.name, "role": pc.role,
            "label": pc.label, "node_id": pc.node_id, "created_by": pc.created_by, "created_at": pc.created_at,
            "expires_at": pc.expires_at, "used_at": pc.used_at, "code": decrypt_secret(pc.code_enc)}


async def connect_info(db: AsyncSession, request_host: str | None = None,
                       request_base: str | None = None) -> dict[str, Any]:
    """Everything the UI needs to show the install command."""
    agents = await get_setting(db, "agents")
    general = await get_setting(db, "general")
    proxy = await get_setting(db, "proxy")
    ca = await agent_ca.get_ca(db)
    app_url = (general.get("base_url") or request_base or "").rstrip("/")
    endpoint, source = agent_packages.agent_endpoint_info(agents, general, request_host, proxy)
    return {"endpoint": endpoint, "endpoint_source": source, "proxy_mode": proxy.get("mode") == "proxy",
            "app_url": app_url,
            "fingerprint": ca.fingerprint, "fingerprint_pretty": agent_ca.pretty_fingerprint(ca.fingerprint),
            "agent_version": agent_packages.bundled_version(), "packages_available": bool(agent_packages.manifest()),
            "port": int(agents.get("port") or 8443), "address": agents.get("address", ""),
            "auto_update": bool(agents.get("auto_update", True)),
            "updates_check_hours": agents.get("updates_check_hours", 6),
            "transfer_max_gb": agents.get("transfer_max_gb", 50)}


def install_command(app_url: str, code: str) -> str:
    return f"curl -fsSL {app_url}/api/agent/install.sh | bash -s -- --code {code}"


def windows_commands(app_url: str, endpoint: str, fingerprint: str, code: str) -> dict[str, str]:
    """PowerShell one-liner (checks the SHA-256 of the installer) and the plain msiexec form."""
    ps = (f"powershell -NoProfile -ExecutionPolicy Bypass -Command \"& ([scriptblock]::Create((irm '{app_url}/api/agent/install.ps1'))) "
          f"-Code {code}\"")
    msi = (f"msiexec /i {app_url}/api/agent/download/mmn-agent-windows-amd64.msi /qn "
           f"SERVER={endpoint} FINGERPRINT={fingerprint} CODE={code}")
    return {"windows_command": ps, "msiexec": msi}


class EnrollIn(BaseModel):
    code: str = Field(max_length=40)
    csr: str = Field(max_length=10_000)
    hostname: str = Field(default="", max_length=255)
    os: str = Field(default="linux", max_length=32)
    arch: str = Field(default="amd64", max_length=16)
    agent_version: str = Field(default="", max_length=64)
    kind: Literal["proxmox", "linux", "windows"] = "proxmox"


async def enroll(db: AsyncSession, body: EnrollIn, remote_ip: str | None,
                 request_host: str | None = None) -> dict[str, Any]:
    """Pair a node. ``request_host`` is the host the agent reached us on: the fallback for the address it
    keeps using when neither the agent address nor the public address is configured."""
    code = normalize_code(body.code)
    if not CODE_RE.match(code):
        raise NodeError("invalid pairing code")
    pc = (await db.execute(select(PairingCode).where(PairingCode.code_hash == hash_token(code))
                           .with_for_update(of=PairingCode))).scalar_one_or_none()
    now = datetime.now(UTC)
    if pc is None or pc.used_at is not None or pc.expires_at < now:
        raise NodeError("this pairing code is unknown, used or expired: create a new one in the app")
    ca = await agent_ca.get_ca(db)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", (body.hostname or "node").split(".")[0])[:100] or "node"
    node = await db.get(Node, pc.node_id) if pc.node_id else None
    if node is None:
        node = Node(environment_id=pc.environment_id, name=name, kind=body.kind, hostname=body.hostname or None,
                    role="workstation" if pc.role == "workstation" and body.kind != "proxmox" else "server",
                    capabilities=[], info={}, metrics={}, updates={}, governor={})
        db.add(node)
        await db.flush()
    cert = ca.sign_client(body.csr, node.id)
    node.cert_fingerprint = agent_ca.fingerprint(cert)
    node.cert_serial = format(cert.serial_number, "x")
    node.cert_expires_at = cert.not_valid_after_utc
    node.enrolled_at = now
    node.agent_version, node.os, node.arch = body.agent_version or None, body.os, body.arch
    node.remote_ip = remote_ip
    node.notify_hash = None  # push the environment's settings on first connect
    pc.used_at, pc.used_by_node_id = now, node.id
    await db.flush()
    from cryptography.hazmat.primitives import serialization

    agents = await get_setting(db, "agents")
    general = await get_setting(db, "general")
    proxy = await get_setting(db, "proxy")
    return {"node_id": node.id, "node_name": node.name, "environment": pc.environment.name,
            "certificate": cert.public_bytes(serialization.Encoding.PEM).decode(), "ca": ca.cert_pem,
            "server": _enrolled_server(agents, general, request_host, proxy)}


def _enrolled_server(agents: dict[str, Any], general: dict[str, Any], request_host: str | None,
                     proxy: dict[str, Any]) -> str:
    """The address the agent keeps using. Without an explicit agent address, keep the one it just
    enrolled through: that one demonstrably reaches this app."""
    if not (agents.get("address") or "").strip() and request_host:
        try:
            u = urlparse("//" + request_host)
            if u.hostname and u.port:
                return request_host
        except ValueError:
            pass
    return agent_packages.agent_endpoint(agents, general, request_host, proxy)


# ------------------------------------------------------------------ node actions
async def get_node(db: AsyncSession, node_id: str) -> Node:
    n = await db.get(Node, node_id)
    if n is None:
        n = (await db.execute(select(Node).where(func.lower(Node.name) == node_id.lower()))).scalars().first()
    if n is None:
        raise NodeError("node not found")
    return n


async def list_nodes(db: AsyncSession, environment_id: str | None = None) -> list[Node]:
    q = select(Node).order_by(Node.name)
    if environment_id:
        q = q.where(Node.environment_id == environment_id)
    return list((await db.execute(q)).scalars())


async def set_governor(node_id: str, governor: str) -> dict[str, Any]:
    if not re.match(r"^[a-z_-]{2,32}$", governor):
        raise NodeError("invalid governor name")
    async with sessionmaker()() as db:
        node = await get_node(db, node_id)
        avail = (node.governor or {}).get("available") or []
        if avail and governor not in avail:
            raise NodeError(f"'{governor}' is not available on this node ({', '.join(avail)})")
        node.governor_desired = governor
        await db.commit()
        nid = node.id
    if not agent_hub.is_online(nid):
        return {"queued": True, "desired": governor}
    res = await agent_hub.request(nid, "governor.set", {"governor": governor})
    async with sessionmaker()() as db:
        node = await db.get(Node, nid)
        node.governor = {k: v for k, v in (res or {}).items() if k != "desired"}
        await db.commit()
    events.publish("node.status", {"node_id": nid, "online": True, "type": "governor"})
    return {"queued": False, **(res or {})}


async def refresh_updates(node_id: str) -> dict[str, Any]:
    res = await agent_hub.request(node_id, "updates.check", timeout=300)
    return res or {}


async def create_job(node_id: str, kind: str, params: dict[str, Any], *, source: str, requested_by: str | None,
                     exclusive: bool = True, busy_key: str | None = None) -> NodeJob:
    """Store a running job. ``exclusive`` refuses a second running job of the same kind on the node;
    ``busy_key`` (e.g. ``guest:101``) refuses a second job for the same guest."""
    async with sessionmaker()() as db:
        running = (await db.execute(select(NodeJob).where(NodeJob.node_id == node_id,
                                                          NodeJob.status == "running"))).scalars().all()
        if exclusive and any(j.kind == kind for j in running):
            raise NodeError("this task is already running on the node")
        if busy_key and any((j.params or {}).get("busy") == busy_key for j in running):
            raise NodeError("another task is still running for this guest")
        stored = {k: v for k, v in params.items() if k not in ("url", "script", "password", "env")}
        if isinstance(stored.get("args"), dict):
            stored["args"] = {k: ("***" if k in SECRET_ARGS else v) for k, v in stored["args"].items()}
        if busy_key:
            stored["busy"] = busy_key
        job = NodeJob(node_id=node_id, kind=kind, source=source, requested_by=requested_by,
                      params=stored, result={}, output="")
        db.add(job)
        await db.commit()
    events.publish("node.job", {"node_id": node_id, "job_id": job.id, "started": True})
    return job


async def start_job(node_id: str, kind: str, method: str, params: dict[str, Any], *, source: str,
                    requested_by: str | None, exclusive: bool = True, busy_key: str | None = None,
                    parent: str | None = None, label: dict[str, Any] | None = None) -> NodeJob:
    """Start a long task on the agent; its output and result arrive as job messages."""
    job = await create_job(node_id, kind, {**params, **(label or {})}, source=source, requested_by=requested_by,
                           exclusive=exclusive, busy_key=busy_key)
    if parent:
        agent_hub.forward(job.id, parent)
    try:
        await agent_hub.request(node_id, method, {**params, "job_id": job.id}, timeout=60)
    except AgentError as e:
        async with sessionmaker()() as db:
            j = await db.get(NodeJob, job.id)
            agent_hub._finish(j, "failed", {}, str(e))
            await db.commit()
        agent_hub._resolve(job.id, "failed", {}, str(e))
        raise
    return job


async def apply_updates(node_id: str, *, source: str = "ui", requested_by: str | None = None) -> NodeJob:
    agent_hub.get(node_id)
    return await start_job(node_id, "updates.apply", "updates.apply", {}, source=source, requested_by=requested_by)


async def update_agent(node_id: str, *, source: str = "ui", requested_by: str | None = None) -> NodeJob:
    async with sessionmaker()() as db:
        node = await get_node(db, node_id)
    agent_hub.get(node.id)
    pkg = agent_packages.package_for(node.os, node.arch, node.install_kind)
    if pkg is None:
        raise NodeError("no agent build for this platform in this release")
    name, meta = pkg
    version = agent_packages.bundled_version()
    params = {"url": f"/agent/download/{name}", "sha256": meta["sha256"], "version": version,
              "format": "deb" if name.endswith(".deb") else "binary"}
    return await start_job(node.id, "agent.update", "agent.update", params, source=source, requested_by=requested_by)


async def reboot(node_id: str) -> dict[str, Any]:
    return await agent_hub.request(node_id, "reboot", {}) or {}


async def remove_node(db: AsyncSession, node: Node) -> None:
    nid = node.id
    if agent_hub.is_online(nid):
        try:
            await agent_hub.request(nid, "unenroll", {}, timeout=10)
        except AgentError:
            pass
        await agent_hub.disconnect(nid)
    await db.delete(node)
    await db.flush()


async def reconcile(node_id: str) -> None:
    """After an agent connects: apply the desired governor and notification settings, and update
    the agent itself when auto-update is on."""
    try:
        async with sessionmaker()() as db:
            node = await db.get(Node, node_id)
            if node is None:
                return
            desired = node.governor_desired
            current = (node.governor or {}).get("current")
            env_notify_on = bool((node.environment.notify or {}).get("enabled"))
            payload_hash = _hash(await effective_notify(db, node.environment))
            stored = node.notify_hash
            agents = await get_setting(db, "agents")
            bundled = agent_packages.bundled_version()
            outdated = bool(bundled and node.agent_version and node.agent_version != bundled)
            caps = node.capabilities or []
        if desired and desired != current and "governor" in caps:
            try:
                await set_governor(node_id, desired)
            except (AgentError, NodeError) as e:
                log.warning("governor for %s: %s", node_id, e)
        if "pve-notifications" in caps and stored != payload_hash and (env_notify_on or stored):
            try:
                await push_notify_to_node(node_id)
            except (AgentError, NodeError) as e:
                log.warning("notifications for %s: %s", node_id, e)
        if outdated and agents.get("auto_update", True):
            try:
                await update_agent(node_id, source="system", requested_by="auto-update")
            except (AgentError, NodeError) as e:
                log.info("agent auto-update for %s skipped: %s", node_id, e)
    except AgentOffline:
        pass
    except Exception:  # noqa: BLE001
        log.exception("reconcile %s failed", node_id)


async def check_all_updates() -> None:
    """Scheduled: ask every connected agent to refresh its package list."""
    for nid in list(agent_hub.online_ids()):
        try:
            await refresh_updates(nid)
        except AgentError as e:
            log.info("update check on %s: %s", nid, e)


async def job_finished_mail(job_id: str) -> None:
    from mmn.i18n import t
    from mmn.services.mailer import MailContent, try_send_mail

    async with sessionmaker()() as db:
        job = await db.get(NodeJob, job_id)
        notif = await get_setting(db, "notifications")
        to = notif.get("node_update_to")
        if job is None or not to:
            return
        node = await db.get(Node, job.node_id)
        general = await get_setting(db, "general")
        lang = general["default_language"]
        ok = job.status == "success"
        key = "node_update.ok" if ok else "node_update.failed"
        tail = "\n".join((job.output or "").strip().splitlines()[-15:])
        await try_send_mail(db, to, MailContent(
            subject=t(lang, f"{key}_subject", node=node.name if node else "?"),
            title=t(lang, f"{key}_title"),
            paragraphs=[t(lang, "node_update.body", node=node.name if node else "?",
                          env=node.environment.name if node else "?",
                          remaining=job.result.get("remaining", "?")),
                        *(["" + (job.error or "")] if job.error else [])],
            muted=[tail] if tail else []), lang)


# ------------------------------------------------------------------ server-admin tools (logs, services, ...)
OPS_TIMEOUTS = {"exec": 1900, "logs.journal": 150, "services.list": 90, "services.status": 90,
                "services.action": 180, "packages.query": 120, "file.read": 30, "logs.events": 150,
                "logs.search": 120, "diagnose": 120}


async def node_call(node_id: str, method: str, params: dict[str, Any]) -> Any:
    if method not in OPS_TIMEOUTS:
        raise NodeError("unknown action")
    timeout = OPS_TIMEOUTS[method]
    if method == "exec":
        timeout = min(int(params.get("timeout") or 60), 1800) + 30
    return await agent_hub.request(node_id, method, params, timeout=timeout)


async def find_packages(pattern: str, environment_id: str | None = None) -> list[dict[str, Any]]:
    """Which nodes have a package installed (e.g. after a CVE), with the version per node."""
    async with sessionmaker()() as db:
        nodes = [n for n in await list_nodes(db, environment_id)]
    out = []
    for n in nodes:
        row: dict[str, Any] = {"node_id": n.id, "node": n.name, "environment": n.environment.name,
                               "customer": n.environment.name,
                               "online": agent_hub.is_online(n.id)}
        if row["online"]:
            try:
                row["packages"] = await node_call(n.id, "packages.query", {"pattern": pattern})
                pending = {p["name"]: p["new"] for p in (n.updates or {}).get("packages") or []}
                for p in row["packages"] or []:
                    if p["name"] in pending:
                        p["update_available"] = pending[p["name"]]
            except AgentError as e:
                row["error"] = str(e)
        out.append(row)
    return out
