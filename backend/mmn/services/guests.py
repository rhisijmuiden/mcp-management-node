"""VMs and containers on Proxmox VE nodes: listing, power actions, snapshots, migration,
creating new guests. Used by the REST API, the MCP server and scheduled tasks.

Everything goes through the agent of the node that runs the guest: short calls with
``pve.api`` (pvesh), long ones as jobs (``pve.task`` follows the Proxmox VE task log,
``script.run`` runs a shell script). The app decides what is allowed; the agent only executes.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Node, NodeJob
from mmn.services import agent_hub, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

GuestType = Literal["qemu", "lxc"]
POWER_ACTIONS = ("start", "stop", "shutdown", "reboot", "suspend", "resume")
NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,61}[A-Za-z0-9])?$")
SNAP_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
STORAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
BRIDGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,15}$")
VOLID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_./+-]{1,250}$")
TAG_RE = re.compile(r"^[a-z0-9_][a-z0-9_.+-]{0,63}$")


# ------------------------------------------------------------------ calls to the agent
async def api(node_id: str, method: str, path: str, args: dict[str, Any] | None = None, timeout: int = 60) -> Any:
    return await agent_hub.request(node_id, "pve.api", {"method": method, "path": path, "args": args or {},
                                                       "timeout": timeout}, timeout=timeout + 15)


async def task(node_id: str, kind: str, method: str, path: str, args: dict[str, Any] | None = None, *,
               source: str = "ui", requested_by: str | None = None, vmid: int | None = None,
               parent: str | None = None, label: dict[str, Any] | None = None) -> NodeJob:
    """Start a Proxmox VE task as a job (its log is streamed)."""
    return await nodes.start_job(node_id, kind, "pve.task", {"method": method, "path": path, "args": args or {}},
                                 source=source, requested_by=requested_by, exclusive=False,
                                 busy_key=f"guest:{vmid}" if vmid and not parent else None, parent=parent,
                                 label={"vmid": vmid, **(label or {})} if vmid else label)


async def script(node_id: str, kind: str, body: str, *, env: dict[str, str] | None = None, timeout: int = 3600,
                 source: str = "ui", requested_by: str | None = None, vmid: int | None = None,
                 parent: str | None = None, label: dict[str, Any] | None = None) -> NodeJob:
    return await nodes.start_job(node_id, kind, "script.run", {"script": body, "env": env or {}, "timeout": timeout},
                                 source=source, requested_by=requested_by, exclusive=False,
                                 busy_key=f"guest:{vmid}" if vmid and not parent else None, parent=parent,
                                 label={"vmid": vmid, **(label or {})} if vmid else label)


async def run_and_wait(job: NodeJob, timeout: float = 6 * 3600) -> tuple[str, dict[str, Any], str | None]:
    return await agent_hub.wait_job(job.id, timeout)


# ------------------------------------------------------------------ listing
def _guest_out(node: Node, g: dict[str, Any]) -> dict[str, Any]:
    tags = [t for t in re.split(r"[;, ]+", g.get("tags") or "") if t]
    return {
        "vmid": int(g.get("vmid") or 0), "type": g.get("type"), "name": g.get("name") or "",
        "status": g.get("status") or "unknown", "node_id": node.id, "node": node.name,
        "pve_node": g.get("node") or node.pve_name, "environment_id": node.environment_id,
        "environment": node.environment.name if node.environment else None, "cluster": node.cluster_name,
        "online": agent_hub.is_online(node.id),
        "cpu": g.get("cpu") or 0, "maxcpu": g.get("maxcpu") or 0, "mem": g.get("mem") or 0,
        "maxmem": g.get("maxmem") or 0, "disk": g.get("disk") or 0, "maxdisk": g.get("maxdisk") or 0,
        "uptime": g.get("uptime") or 0, "netin": g.get("netin") or 0, "netout": g.get("netout") or 0,
        "tags": tags, "template": bool(g.get("template")), "lock": g.get("lock") or None,
        "hastate": g.get("hastate") or None, "community_script": "community-script" in tags,
    }


def guests_of(node: Node) -> list[dict[str, Any]]:
    return [_guest_out(node, g) for g in (node.guests or {}).get("guests") or []]


async def list_guests(db: AsyncSession, *, environment_id: str | None = None, node_id: str | None = None,
                      type_: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in await nodes.list_nodes(db, environment_id):
        if node_id and n.id != node_id:
            continue
        for g in guests_of(n):
            if type_ and g["type"] != type_:
                continue
            if query:
                q = query.lower()
                if q not in g["name"].lower() and q != str(g["vmid"]) and not any(q == t for t in g["tags"]):
                    continue
            out.append(g)
    out.sort(key=lambda g: (g["environment"] or "", g["vmid"]))
    return out


async def find_guest(db: AsyncSession, ref: str | int, node: str | None = None) -> tuple[Node, dict[str, Any]]:
    """A guest by VMID or name, optionally on a given node (needed when the VMID exists in more
    than one environment)."""
    nlist = [await nodes.get_node(db, node)] if node else await nodes.list_nodes(db)
    ref_s = str(ref).strip()
    hits = []
    for n in nlist:
        for g in guests_of(n):
            if ref_s == str(g["vmid"]) or ref_s.lower() == g["name"].lower():
                hits.append((n, g))
    if not hits:
        raise NodeError("guest not found (it may be on a node whose agent is offline)")
    if len(hits) > 1:
        where = ", ".join(f"{g['name'] or g['vmid']} on {n.name}" for n, g in hits[:5])
        raise NodeError(f"more than one guest matches ({where}); name the node as well")
    return hits[0]


async def refresh(node_id: str) -> None:
    try:
        await agent_hub.request(node_id, "guests.list", {}, timeout=45)
    except AgentError:
        pass


def _base(g: dict[str, Any]) -> str:
    return f"/nodes/localhost/{g['type']}/{int(g['vmid'])}"


async def guest_detail(node: Node, g: dict[str, Any]) -> dict[str, Any]:
    cfg = await api(node.id, "get", _base(g) + "/config")
    snaps = await api(node.id, "get", _base(g) + "/snapshot")
    agent_ifaces = None
    if g["type"] == "lxc" and g["status"] == "running":
        try:
            agent_ifaces = await api(node.id, "get", _base(g) + "/interfaces", timeout=15)
        except AgentError:
            agent_ifaces = None
    elif g["type"] == "qemu" and g["status"] == "running" and str((cfg or {}).get("agent", "0")).startswith("1"):
        try:
            res = await api(node.id, "get", _base(g) + "/agent/network-get-interfaces", timeout=15)
            agent_ifaces = (res or {}).get("result")
        except AgentError:
            agent_ifaces = None
    return {**g, "config": _redact_config(cfg or {}), "snapshots": _snapshots_out(snaps or []),
            "interfaces": agent_ifaces}


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in ("cipassword",) else v) for k, v in cfg.items()}


def _snapshots_out(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for s in snaps:
        if s.get("name") == "current":
            continue
        out.append({"name": s.get("name"), "description": (s.get("description") or "").strip(),
                    "snaptime": s.get("snaptime"), "parent": s.get("parent"), "vmstate": bool(s.get("vmstate"))})
    out.sort(key=lambda s: s.get("snaptime") or 0)
    return out


async def list_snapshots(node: Node, g: dict[str, Any]) -> list[dict[str, Any]]:
    return _snapshots_out(await api(node.id, "get", _base(g) + "/snapshot") or [])


# ------------------------------------------------------------------ actions
def _who(source: str, requested_by: str | None) -> dict[str, Any]:
    return {"source": source, "requested_by": requested_by}


async def power(node: Node, g: dict[str, Any], action: str, *, source: str = "ui",
                requested_by: str | None = None) -> NodeJob:
    if action not in POWER_ACTIONS:
        raise NodeError(f"action must be one of {', '.join(POWER_ACTIONS)}")
    args: dict[str, Any] = {}
    if action == "shutdown":
        args["timeout"] = 180
        if g["type"] == "qemu":
            args["forceStop"] = True
    return await task(node.id, f"guest.{action}", "create", f"{_base(g)}/status/{action}", args,
                      vmid=g["vmid"], label={"name": g["name"], "type": g["type"]}, **_who(source, requested_by))


async def create_snapshot(node: Node, g: dict[str, Any], name: str, description: str = "", vmstate: bool = False,
                          *, source: str = "ui", requested_by: str | None = None, parent: str | None = None) -> NodeJob:
    if not SNAP_RE.match(name):
        raise NodeError("snapshot names start with a letter and use letters, digits, - and _ (2 to 40)")
    args: dict[str, Any] = {"snapname": name, "description": description[:500]}
    if vmstate and g["type"] == "qemu":
        args["vmstate"] = True
    return await task(node.id, "guest.snapshot", "create", f"{_base(g)}/snapshot", args, vmid=g["vmid"],
                      parent=parent, label={"name": g["name"], "snapshot": name}, **_who(source, requested_by))


async def rollback_snapshot(node: Node, g: dict[str, Any], name: str, *, start: bool = False, source: str = "ui",
                            requested_by: str | None = None, parent: str | None = None) -> NodeJob:
    if not SNAP_RE.match(name):
        raise NodeError("invalid snapshot name")
    args: dict[str, Any] = {"start": True} if start else {}
    return await task(node.id, "guest.rollback", "create", f"{_base(g)}/snapshot/{name}/rollback", args,
                      vmid=g["vmid"], parent=parent, label={"name": g["name"], "snapshot": name},
                      **_who(source, requested_by))


async def delete_snapshot(node: Node, g: dict[str, Any], name: str, *, source: str = "ui",
                          requested_by: str | None = None, parent: str | None = None) -> NodeJob:
    if not SNAP_RE.match(name):
        raise NodeError("invalid snapshot name")
    return await task(node.id, "guest.snapshot.delete", "delete", f"{_base(g)}/snapshot/{name}", {},
                      vmid=g["vmid"], parent=parent, label={"name": g["name"], "snapshot": name},
                      **_who(source, requested_by))


async def migrate(db: AsyncSession, node: Node, g: dict[str, Any], target: str, *, online: bool = True,
                  source: str = "ui", requested_by: str | None = None) -> NodeJob:
    """Move a guest to another node of the same cluster (live for running VMs; containers restart)."""
    members = [m.get("name") for m in ((node.info or {}).get("cluster") or {}).get("nodes") or []]
    tnode = None
    try:
        tnode = await nodes.get_node(db, target)
    except NodeError:
        pass
    target_name = (tnode.pve_name or tnode.name) if tnode else target
    if not node.cluster_name or target_name not in members:
        raise NodeError("the target must be another node of the same Proxmox VE cluster")
    if target_name == (g.get("pve_node") or node.pve_name):
        raise NodeError("the guest already runs on that node")
    args: dict[str, Any] = {"target": target_name}
    if g["status"] == "running":
        if g["type"] == "qemu":
            args["online"] = bool(online)
            args["with-local-disks"] = True
        else:
            args["restart"] = True
            args["timeout"] = 180
    return await task(node.id, "guest.migrate", "create", f"{_base(g)}/migrate", args, vmid=g["vmid"],
                      label={"name": g["name"], "target": target_name}, **_who(source, requested_by))


async def destroy(node: Node, g: dict[str, Any], *, source: str = "ui", requested_by: str | None = None) -> NodeJob:
    if g["status"] == "running":
        raise NodeError("stop the guest first")
    return await task(node.id, "guest.destroy", "delete", _base(g), {"purge": True, "destroy-unreferenced-disks": True},
                      vmid=g["vmid"], label={"name": g["name"]}, **_who(source, requested_by))


class ResourcesIn(BaseModel):
    cores: int | None = Field(default=None, ge=1, le=512)
    memory: int | None = Field(default=None, ge=16, le=4 * 1024 * 1024)  # MiB
    onboot: bool | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=8000)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = [t for t in v if not TAG_RE.match(t)]
        if bad:
            raise ValueError(f"invalid tags: {', '.join(bad)}")
        return v


async def set_resources(node: Node, g: dict[str, Any], body: ResourcesIn) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if body.cores is not None:
        args["cores"] = body.cores
    if body.memory is not None:
        args["memory"] = body.memory
    if body.onboot is not None:
        args["onboot"] = body.onboot
    if body.tags is not None:
        args["tags"] = ";".join(body.tags)
    if body.description is not None:
        args["description"] = body.description
    if not args:
        raise NodeError("nothing to change")
    await api(node.id, "set", _base(g) + "/config", args)
    agent_hub.spawn(refresh(node.id))
    return {"ok": True, "changed": sorted(args), "pending_reboot": g["status"] == "running"
            and any(k in args for k in ("cores", "memory")) and g["type"] == "qemu"}


# ------------------------------------------------------------------ choosing a node, resources on a node
def load_score(n: Node) -> float:
    m = n.metrics or {}
    mem = (m.get("mem_used") or 0) / (m.get("mem_total") or 1)
    cpu = (m.get("cpu_percent") or 0) / 100
    return round(0.6 * mem + 0.4 * cpu, 4)


async def pve_nodes(db: AsyncSession, environment_id: str | None = None) -> list[Node]:
    return [n for n in await nodes.list_nodes(db, environment_id) if "guests" in (n.capabilities or [])]


async def suggest_node(db: AsyncSession, environment_id: str | None = None) -> dict[str, Any] | None:
    """The least loaded online Proxmox VE node (60 % memory, 40 % CPU)."""
    cands = [n for n in await pve_nodes(db, environment_id) if agent_hub.is_online(n.id)]
    if not cands:
        return None
    best = min(cands, key=load_score)
    return {"node_id": best.id, "node": best.name, "score": load_score(best),
            "ranking": [{"node_id": n.id, "node": n.name, "score": load_score(n)}
                        for n in sorted(cands, key=load_score)]}


async def node_resources(node: Node) -> dict[str, Any]:
    """Storages, bridges, ISO images and templates on a node, plus the next free VMID."""
    storages = await api(node.id, "get", "/nodes/localhost/storage", {"enabled": True}) or []
    bridges = []
    try:
        net = await api(node.id, "get", "/nodes/localhost/network") or []
        bridges = sorted(i.get("iface") for i in net if i.get("type") in ("bridge", "OVSBridge") and i.get("iface"))
    except AgentError:
        pass
    try:
        for z in await api(node.id, "get", "/cluster/sdn/vnets") or []:
            if z.get("vnet"):
                bridges.append(z["vnet"])
    except AgentError:
        pass
    isos: list[dict[str, Any]] = []
    for s in storages:
        if "iso" in (s.get("content") or "").split(","):
            try:
                for c in await api(node.id, "get", f"/nodes/localhost/storage/{s['storage']}/content",
                                   {"content": "iso"}) or []:
                    isos.append({"volid": c.get("volid"), "size": c.get("size"), "storage": s["storage"]})
            except AgentError:
                pass
    nextid = await api(node.id, "get", "/cluster/nextid")
    return {
        "node_id": node.id, "node": node.name, "nextid": int(nextid) if nextid else None,
        "storages": [{"storage": s.get("storage"), "type": s.get("type"), "content": (s.get("content") or "").split(","),
                      "avail": s.get("avail"), "total": s.get("total"), "shared": bool(s.get("shared")),
                      "active": bool(s.get("active", 1))} for s in storages],
        "bridges": bridges, "isos": isos,
        "templates": [f for st in ((node.storage_scan or {}).get("storages") or {}).values()
                      for f in st.get("files") or []],
    }


# ------------------------------------------------------------------ creating guests
class NetIn(BaseModel):
    bridge: str = Field(default="vmbr0", pattern=BRIDGE_RE.pattern)
    vlan: int | None = Field(default=None, ge=1, le=4094)
    ip: str = Field(default="dhcp", max_length=64)  # dhcp | 10.0.0.5/24
    gateway: str | None = Field(default=None, max_length=64)
    firewall: bool = False

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        if v in ("dhcp", "manual"):
            return v
        import ipaddress

        try:
            ipaddress.ip_interface(v)
        except ValueError as e:
            raise ValueError("ip must be 'dhcp' or an address with prefix, e.g. 192.168.1.20/24") from e
        if "/" not in v:
            raise ValueError("add the prefix length, e.g. /24")
        return v

    @field_validator("gateway")
    @classmethod
    def _gw(cls, v: str | None) -> str | None:
        if not v:
            return None
        import ipaddress

        ipaddress.ip_address(v)
        return v


def _lxc_net(n: NetIn) -> str:
    parts = ["name=eth0", f"bridge={n.bridge}", f"ip={n.ip}"]
    if n.gateway and n.ip not in ("dhcp", "manual"):
        parts.append(f"gw={n.gateway}")
    if n.vlan:
        parts.append(f"tag={n.vlan}")
    if n.firewall:
        parts.append("firewall=1")
    return ",".join(parts)


def _qemu_net(n: NetIn) -> str:
    parts = ["virtio", f"bridge={n.bridge}"]
    if n.vlan:
        parts.append(f"tag={n.vlan}")
    if n.firewall:
        parts.append("firewall=1")
    return ",".join(parts)


class CreateBase(BaseModel):
    node_id: str
    vmid: int | None = Field(default=None, ge=100, le=999_999_999)
    name: str = Field(max_length=63)
    cores: int = Field(default=2, ge=1, le=128)
    memory: int = Field(default=2048, ge=64, le=1024 * 1024)
    disk_gb: int = Field(default=16, ge=1, le=64 * 1024)
    storage: str = Field(default="local-lvm", pattern=STORAGE_RE.pattern)
    net: NetIn = Field(default_factory=NetIn)
    start: bool = True
    onboot: bool = False
    tags: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("use a host name: letters, digits and '-' (max 63)")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if not TAG_RE.match(t)]
        if bad:
            raise ValueError(f"invalid tags: {', '.join(bad)}")
        return v


class CreateCtIn(CreateBase):
    template_id: str | None = None  # from the library
    ostemplate: str | None = Field(default=None, pattern=VOLID_RE.pattern)  # or a volid already on the node
    disk_gb: int = Field(default=8, ge=1, le=64 * 1024)
    memory: int = Field(default=1024, ge=64, le=1024 * 1024)
    swap: int = Field(default=512, ge=0, le=1024 * 1024)
    password: str | None = Field(default=None, min_length=5, max_length=200)
    ssh_keys: str | None = Field(default=None, max_length=20000)
    unprivileged: bool = True
    nesting: bool = True
    nameserver: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _source(self) -> CreateCtIn:
        if not self.template_id and not self.ostemplate:
            raise ValueError("choose a template")
        if not self.password and not self.ssh_keys:
            raise ValueError("set a root password or an SSH key")
        return self


class CreateVmIn(CreateBase):
    source: Literal["iso", "cloud"] = "iso"
    iso: str | None = Field(default=None, pattern=VOLID_RE.pattern)  # existing ISO volid
    iso_url: str | None = Field(default=None, max_length=2000)  # or download it first
    iso_storage: str = Field(default="local", pattern=STORAGE_RE.pattern)
    cloud_image: str | None = Field(default=None, max_length=64)  # preset key
    cloud_url: str | None = Field(default=None, max_length=2000)  # or own image URL
    ostype: Literal["l26", "win11", "win10", "other"] = "l26"
    bios: Literal["seabios", "ovmf"] = "ovmf"
    cpu: str = Field(default="x86-64-v2-AES", pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    # cloud-init
    ci_user: str = Field(default="admin", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    ci_password: str | None = Field(default=None, max_length=200)
    ssh_keys: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def _source(self) -> CreateVmIn:
        if self.source == "iso" and not self.iso and not self.iso_url:
            raise ValueError("choose an ISO image or give a download URL")
        if self.source == "cloud" and not (self.cloud_image or self.cloud_url):
            raise ValueError("choose a cloud image")
        if self.source == "cloud" and not (self.ci_password or self.ssh_keys):
            raise ValueError("set a password or an SSH key for the cloud-init user")
        for u in (self.iso_url, self.cloud_url):
            if u and not re.match(r"^https?://[^\s'\"]+$", u):
                raise ValueError("the download URL must start with http:// or https://")
        return self


CLOUD_IMAGES: dict[str, dict[str, str]] = {
    "debian-13": {"name": "Debian 13 (trixie)",
                  "url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"},
    "debian-12": {"name": "Debian 12 (bookworm)",
                  "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"},
    "ubuntu-24.04": {"name": "Ubuntu 24.04 LTS (noble)",
                     "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"},
    "ubuntu-22.04": {"name": "Ubuntu 22.04 LTS (jammy)",
                     "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"},
    "rocky-9": {"name": "Rocky Linux 9",
                "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2"},
}


async def _vmid(node: Node, wanted: int | None) -> int:
    if wanted:
        return wanted
    nextid = await api(node.id, "get", "/cluster/nextid")
    return int(nextid)


async def create_ct(db: AsyncSession, body: CreateCtIn, *, source: str = "ui",
                    requested_by: str | None = None) -> NodeJob:
    """Create a container from the template library. A library template that is missing on the
    node is downloaded to it first."""
    from mmn.services import templates

    node = await nodes.get_node(db, body.node_id)
    agent_hub.get(node.id)
    vmid = await _vmid(node, body.vmid)
    tpl = None
    if body.template_id:
        tpl = await templates.get_template(db, body.template_id)
    job = await nodes.create_job(node.id, "guest.create", {"vmid": vmid, "name": body.name, "type": "lxc",
                                                           "template": tpl.filename if tpl else body.ostemplate},
                                 source=source, requested_by=requested_by, exclusive=False,
                                 busy_key=f"guest:{vmid}")
    agent_hub.spawn(_create_ct_pipeline(job.id, node.id, vmid, body, tpl.id if tpl else None, source, requested_by))
    return job


async def _create_ct_pipeline(job_id: str, node_id: str, vmid: int, body: CreateCtIn, tpl_id: str | None,
                              source: str, requested_by: str | None) -> None:
    from mmn.services import templates

    say = lambda line: agent_hub.add_line(node_id, job_id, line)  # noqa: E731
    try:
        ostemplate = body.ostemplate
        if tpl_id:
            async with sessionmaker()() as db:
                tpl = await templates.get_template(db, tpl_id)
                node = await db.get(Node, node_id)
            volid = templates.volid_on_node(tpl, node)
            if volid is None:
                say(f"Template {tpl.filename} is not on this node yet: downloading it first")
                await templates.scan_node(node_id)
                async with sessionmaker()() as db:
                    node = await db.get(Node, node_id)
                volid = templates.volid_on_node(tpl, node)
            if volid is None:
                status, _, err = await templates.push_to_node(tpl_id, node_id, parent=job_id, source=source,
                                                              requested_by=requested_by)
                if status != "success":
                    raise NodeError(f"template download failed: {err}")
                await templates.scan_node(node_id)
                async with sessionmaker()() as db:
                    node = await db.get(Node, node_id)
                    tpl = await templates.get_template(db, tpl_id)
                volid = templates.volid_on_node(tpl, node) or f"{templates.target_storage_name(tpl, node)}:vztmpl/{tpl.filename}"
            ostemplate = volid
        args: dict[str, Any] = {
            "vmid": vmid, "hostname": body.name, "ostemplate": ostemplate, "cores": body.cores,
            "memory": body.memory, "swap": body.swap, "rootfs": f"{body.storage}:{body.disk_gb}",
            "net0": _lxc_net(body.net), "unprivileged": body.unprivileged, "start": body.start,
            "onboot": body.onboot,
        }
        if body.nesting:
            args["features"] = "nesting=1"
        if body.password:
            args["password"] = body.password
        if body.ssh_keys:
            args["ssh-public-keys"] = body.ssh_keys.strip()
        if body.tags:
            args["tags"] = ";".join(body.tags)
        if body.description:
            args["description"] = body.description
        if body.nameserver:
            args["nameserver"] = body.nameserver
        say(f"Creating container {vmid} ({body.name}) from {ostemplate}")
        sub = await task(node_id, "pve.create", "create", "/nodes/localhost/lxc", args, parent=job_id,
                         source=source, requested_by=requested_by)
        status, _, err = await run_and_wait(sub)
        if status != "success":
            raise NodeError(err or "creating the container failed")
        await refresh(node_id)
        await agent_hub.finish_job(job_id, "success", {"vmid": vmid, "type": "lxc", "name": body.name})
    except (NodeError, AgentError) as e:
        say(f"ERROR: {e}")
        await agent_hub.finish_job(job_id, "failed", {"vmid": vmid}, str(e))
    except Exception as e:  # noqa: BLE001
        say(f"ERROR: {type(e).__name__}: {e}")
        await agent_hub.finish_job(job_id, "failed", {"vmid": vmid}, f"{type(e).__name__}: {e}")


def _q(v: Any) -> str:
    return shlex.quote(str(v))


def cloud_vm_script(vmid: int, body: CreateVmIn, image_url: str) -> str:
    """A bash script for the node: download (cached) the cloud image, create the VM, import the
    disk, add cloud-init and resize. Values are shell-quoted; nothing comes from the browser
    unvalidated."""
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", image_url.rsplit("/", 1)[-1])[:120] or "image.img"
    cache = "/var/lib/vz/template/mmn-cloud"
    lines = [
        "set -euo pipefail",
        f"mkdir -p {cache}",
        f"IMG={cache}/{_q(fname)}",
        'echo "Downloading cloud image (cached for 7 days)"',
        f'if [ ! -s "$IMG" ] || [ -n "$(find "$IMG" -mtime +7 2>/dev/null)" ]; then curl -fL --retry 3 -o "$IMG.part" {_q(image_url)} && mv "$IMG.part" "$IMG"; fi',
        f"qm create {vmid} --name {_q(body.name)} --memory {body.memory} --cores {body.cores} --cpu {_q(body.cpu)}"
        f" --net0 {_q(_qemu_net(body.net))} --scsihw virtio-scsi-single --ostype l26 --agent enabled=1"
        f" --serial0 socket --vga serial0 --machine q35 --bios {body.bios}",
    ]
    if body.bios == "ovmf":
        lines.append(f"qm set {vmid} --efidisk0 {_q(body.storage + ':1,efitype=4m,pre-enrolled-keys=0')}")
    lines += [
        'echo "Importing disk"',
        f'qm set {vmid} --scsi0 {_q(body.storage)}:0,import-from="$IMG",discard=on,ssd=1,iothread=1',
        f"qm set {vmid} --boot order=scsi0 --ide2 {_q(body.storage + ':cloudinit')}",
        f"qm set {vmid} --ciuser {_q(body.ci_user)}",
        f"qm set {vmid} --ipconfig0 {_q(_ipconfig(body.net))}",
        f"qm resize {vmid} scsi0 {body.disk_gb}G || true",
    ]
    if body.ci_password:
        lines.append(f'qm set {vmid} --cipassword "$MMN_CI_PASSWORD"')
    if body.ssh_keys:
        lines += ['KEYS=$(mktemp)', 'printf "%s\\n" "$MMN_SSH_KEYS" > "$KEYS"',
                  f'qm set {vmid} --sshkeys "$KEYS"', 'rm -f "$KEYS"']
    if body.tags:
        lines.append(f"qm set {vmid} --tags {_q(';'.join(body.tags))}")
    if body.onboot:
        lines.append(f"qm set {vmid} --onboot 1")
    if body.description:
        lines.append(f"qm set {vmid} --description {_q(body.description)}")
    if body.start:
        lines += ['echo "Starting VM"', f"qm start {vmid}"]
    lines.append(f'echo "VM {vmid} is ready"')
    return "\n".join(lines) + "\n"


def _ipconfig(n: NetIn) -> str:
    if n.ip == "dhcp":
        return "ip=dhcp"
    if n.ip == "manual":
        return "ip=manual"
    return f"ip={n.ip}" + (f",gw={n.gateway}" if n.gateway else "")


def iso_vm_args(vmid: int, body: CreateVmIn, iso: str) -> dict[str, Any]:
    win = body.ostype.startswith("win")
    disk_opts = "discard=on,ssd=1,iothread=1"
    args: dict[str, Any] = {
        "vmid": vmid, "name": body.name, "cores": body.cores, "memory": body.memory, "cpu": body.cpu,
        "ostype": body.ostype, "machine": "q35", "bios": body.bios, "scsihw": "virtio-scsi-single",
        "scsi0": f"{body.storage}:{body.disk_gb},{disk_opts}", "ide2": f"{iso},media=cdrom",
        "net0": _qemu_net(body.net), "boot": "order=scsi0;ide2", "agent": "enabled=1",
        "start": body.start, "onboot": body.onboot,
    }
    if body.bios == "ovmf":
        args["efidisk0"] = f"{body.storage}:1,efitype=4m,pre-enrolled-keys={1 if win else 0}"
    if body.ostype == "win11":
        args["tpmstate0"] = f"{body.storage}:1,version=v2.0"
    if body.tags:
        args["tags"] = ";".join(body.tags)
    if body.description:
        args["description"] = body.description
    return args


async def create_vm(db: AsyncSession, body: CreateVmIn, *, source: str = "ui",
                    requested_by: str | None = None) -> NodeJob:
    node = await nodes.get_node(db, body.node_id)
    agent_hub.get(node.id)
    vmid = await _vmid(node, body.vmid)
    job = await nodes.create_job(node.id, "guest.create", {"vmid": vmid, "name": body.name, "type": "qemu",
                                                           "source": body.source},
                                 source=source, requested_by=requested_by, exclusive=False,
                                 busy_key=f"guest:{vmid}")
    agent_hub.spawn(_create_vm_pipeline(job.id, node.id, vmid, body, source, requested_by))
    return job


async def _create_vm_pipeline(job_id: str, node_id: str, vmid: int, body: CreateVmIn, source: str,
                              requested_by: str | None) -> None:
    say = lambda line: agent_hub.add_line(node_id, job_id, line)  # noqa: E731
    try:
        if body.source == "cloud":
            url = body.cloud_url or CLOUD_IMAGES.get(body.cloud_image or "", {}).get("url")
            if not url:
                raise NodeError("unknown cloud image")
            env = {}
            if body.ci_password:
                env["MMN_CI_PASSWORD"] = body.ci_password
            if body.ssh_keys:
                env["MMN_SSH_KEYS"] = body.ssh_keys.strip()
            sub = await script(node_id, "vm.cloud", cloud_vm_script(vmid, body, url), env=env, timeout=3600,
                               parent=job_id, source=source, requested_by=requested_by)
            status, _, err = await run_and_wait(sub)
            if status != "success":
                raise NodeError(err or "creating the VM failed")
        else:
            iso = body.iso
            if not iso and body.iso_url:
                fname = re.sub(r"[^A-Za-z0-9._-]", "_", body.iso_url.split("?")[0].rsplit("/", 1)[-1])[:200]
                if not fname.lower().endswith((".iso", ".img")):
                    fname += ".iso"
                say(f"Downloading {fname} to {body.iso_storage}")
                sub = await task(node_id, "pve.download", "create",
                                 f"/nodes/localhost/storage/{body.iso_storage}/download-url",
                                 {"url": body.iso_url, "content": "iso", "filename": fname}, parent=job_id,
                                 source=source, requested_by=requested_by)
                status, _, err = await run_and_wait(sub)
                if status != "success":
                    raise NodeError(f"ISO download failed: {err}")
                iso = f"{body.iso_storage}:iso/{fname}"
            say(f"Creating VM {vmid} ({body.name})")
            sub = await task(node_id, "pve.create", "create", "/nodes/localhost/qemu", iso_vm_args(vmid, body, iso),
                             parent=job_id, source=source, requested_by=requested_by)
            status, _, err = await run_and_wait(sub)
            if status != "success":
                raise NodeError(err or "creating the VM failed")
        await refresh(node_id)
        await agent_hub.finish_job(job_id, "success", {"vmid": vmid, "type": "qemu", "name": body.name})
    except (NodeError, AgentError) as e:
        say(f"ERROR: {e}")
        await agent_hub.finish_job(job_id, "failed", {"vmid": vmid}, str(e))
    except Exception as e:  # noqa: BLE001
        say(f"ERROR: {type(e).__name__}: {e}")
        await agent_hub.finish_job(job_id, "failed", {"vmid": vmid}, f"{type(e).__name__}: {e}")


def snapshot_name(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
