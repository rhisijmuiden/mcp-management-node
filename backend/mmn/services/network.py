"""Network of Proxmox VE nodes: interfaces and bridges, which guests use which bridge and VLAN,
changing bridges safely (staged, reviewed as a diff, applied with an automatic roll back when
the node stops answering) and the Proxmox VE firewall.

All calls go through the node's agent (``pve.api`` for the Proxmox API, ``exec`` for small reads,
``script.run`` for the apply). The app keeps a copy of ``/etc/network/interfaces`` before every
apply, so a change can be undone with one click.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
import shlex
import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import ConfigBackup, Node
from mmn.services import agent_hub, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

log = logging.getLogger("mmn.network")

IFACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,14}$")
PORTS_RE = re.compile(r"^[A-Za-z0-9_.\- ]{0,200}$")
VIDS_RE = re.compile(r"^[0-9]{1,4}(-[0-9]{1,4})?( [0-9]{1,4}(-[0-9]{1,4})?)*$")
INTERFACES = "/etc/network/interfaces"
CONFIRM_SECONDS = 90

_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 20.0


def _cached(key: str) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def invalidate(node_id: str) -> None:
    for k in [k for k in _cache if k.endswith(node_id)]:
        _cache.pop(k, None)


async def _exec(node_id: str, command: str, timeout: int = 30) -> dict[str, Any]:
    return await agent_hub.request(node_id, "exec", {"command": command, "timeout": timeout}, timeout=timeout + 15) or {}


# ------------------------------------------------------------------ interfaces
def _iface_out(i: dict[str, Any]) -> dict[str, Any]:
    ports = (i.get("bridge_ports") or i.get("slaves") or "").split()
    return {
        "iface": i.get("iface"), "type": i.get("type"), "active": bool(i.get("active")),
        "autostart": bool(i.get("autostart")), "method": i.get("method"), "cidr": i.get("cidr"),
        "address": i.get("address"), "gateway": i.get("gateway"), "cidr6": i.get("cidr6"),
        "ports": ports, "vlan_aware": bool(i.get("bridge_vlan_aware")), "vids": i.get("bridge_vids"),
        "bond_mode": i.get("bond_mode"), "vlan_id": i.get("vlan-id"), "vlan_raw_device": i.get("vlan-raw-device"),
        "mtu": i.get("mtu"), "comments": (i.get("comments") or "").strip(), "pending": bool(i.get("pending")),
    }


async def interfaces(node_id: str, *, fresh: bool = False) -> list[dict[str, Any]]:
    key = f"if:{node_id}"
    if not fresh and (hit := _cached(key)) is not None:
        return hit
    res = await guests.api(node_id, "get", "/nodes/localhost/network", timeout=30)
    out = sorted((_iface_out(i) for i in (res or []) if isinstance(i, dict)),
                 key=lambda x: (x["type"] != "bridge", x["iface"] or ""))
    _cache[key] = (time.monotonic(), out)
    return out


# ------------------------------------------------------------------ guest NICs
NIC_SCRIPT = (
    "for f in /etc/pve/local/qemu-server/*.conf /etc/pve/local/lxc/*.conf; do "
    "[ -f \"$f\" ] || continue; "
    "awk -v F=\"$f\" '/^\\[/{exit} /^(net[0-9]+|name|hostname):/{print F\"\\t\"$0}' \"$f\"; done"
)


def parse_nic(gtype: str, value: str) -> dict[str, Any]:
    """``virtio=BC:24:11:..,bridge=vmbr0,firewall=1,tag=20`` (VM) or
    ``name=eth0,bridge=vmbr0,hwaddr=..,ip=dhcp,tag=20,type=veth`` (container)."""
    out: dict[str, Any] = {"bridge": None, "tag": None, "trunks": [], "firewall": False, "mac": None, "model": None,
                           "ip": None, "link_down": False, "name": None}
    for part in value.split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        if k == "bridge":
            out["bridge"] = v
        elif k == "tag":
            out["tag"] = int(v) if v.isdigit() else None
        elif k == "trunks":
            out["trunks"] = [t for t in re.split(r"[;,]", v) if t]
        elif k == "firewall":
            out["firewall"] = v == "1"
        elif k == "link_down":
            out["link_down"] = v == "1"
        elif k == "hwaddr":
            out["mac"] = v
        elif k == "ip":
            out["ip"] = v
        elif k == "name" and gtype == "lxc":
            out["name"] = v
        elif gtype == "qemu" and k in ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3") and v:
            out["model"], out["mac"] = k, v
    return out


def parse_nic_dump(text: str) -> dict[tuple[str, int], dict[str, Any]]:
    guests_: dict[tuple[str, int], dict[str, Any]] = {}
    for line in text.splitlines():
        path, _, rest = line.partition("\t")
        m = re.search(r"/(qemu-server|lxc)/(\d+)\.conf$", path)
        if not m or ":" not in rest:
            continue
        gtype = "qemu" if m.group(1) == "qemu-server" else "lxc"
        vmid = int(m.group(2))
        key, _, value = rest.partition(":")
        g = guests_.setdefault((gtype, vmid), {"type": gtype, "vmid": vmid, "name": "", "nics": []})
        value = value.strip()
        if key in ("name", "hostname"):
            g["name"] = value
        elif key.startswith("net"):
            g["nics"].append({"nic": key, **parse_nic(gtype, value)})
    return guests_


async def guest_nics(node_id: str) -> dict[tuple[str, int], dict[str, Any]]:
    key = f"nics:{node_id}"
    if (hit := _cached(key)) is not None:
        return hit
    res = await _exec(node_id, NIC_SCRIPT, 30)
    out = parse_nic_dump(res.get("stdout") or "")
    _cache[key] = (time.monotonic(), out)
    return out


async def bridge_map(db: AsyncSession, *, environment_id: str | None = None, node_id: str | None = None) -> dict[str, Any]:
    """Which VMs and containers use which bridge, grouped by VLAN tag, over the chosen nodes."""
    nlist = [n for n in await nodes.list_nodes(db, environment_id)
             if "pve" in (n.capabilities or []) and (not node_id or n.id == node_id)]
    errors: list[dict[str, str]] = []
    vnet_tags: dict[str, int | None] = {}

    async def per_node(n: Node) -> tuple[Node, list[dict], dict]:
        if not agent_hub.is_online(n.id):
            return n, [], {}
        try:
            ifs, nics = await asyncio.gather(interfaces(n.id), guest_nics(n.id))
            return n, ifs, nics
        except AgentError as e:
            errors.append({"node_id": n.id, "node": n.name, "error": str(e)})
            return n, [], {}

    results = await asyncio.gather(*(per_node(n) for n in nlist))
    # SDN VNets look like bridges to the guests; know their tag.
    for n in nlist:
        if agent_hub.is_online(n.id):
            try:
                for v in await guests.api(n.id, "get", "/cluster/sdn/vnets", timeout=20) or []:
                    vnet_tags[v.get("vnet")] = v.get("tag")
                break
            except AgentError:
                continue
    bridges: dict[str, dict[str, Any]] = {}
    tags: dict[int, int] = {}
    for n, ifs, nics in results:
        status = {(g["type"], int(g["vmid"])): g for g in guests.guests_of(n)}
        for i in ifs:
            if i["type"] not in ("bridge", "OVSBridge"):
                continue
            b = bridges.setdefault(i["iface"], {"name": i["iface"], "kind": "bridge", "vlan_aware": False,
                                                 "nodes": [], "lanes": {}, "interfaces": 0, "comments": ""})
            b["vlan_aware"] = b["vlan_aware"] or i["vlan_aware"]
            b["comments"] = b["comments"] or i["comments"]
            b["nodes"].append({"node_id": n.id, "node": n.name, "ports": i["ports"], "cidr": i["cidr"],
                               "gateway": i["gateway"], "vlan_aware": i["vlan_aware"], "active": i["active"],
                               "vids": i["vids"], "pending": i["pending"]})
        for (gtype, vmid), g in nics.items():
            st = status.get((gtype, vmid), {})
            for nic in g["nics"]:
                br = nic["bridge"] or "?"
                if br not in bridges:
                    kind = "vnet" if br in vnet_tags else "missing"
                    bridges[br] = {"name": br, "kind": kind, "vlan_aware": False, "nodes": [], "lanes": {},
                                   "interfaces": 0, "comments": "", "vnet_tag": vnet_tags.get(br)}
                tag = nic["tag"] or 0
                if bridges[br]["kind"] == "vnet" and not nic["tag"]:
                    tag = vnet_tags.get(br) or 0
                bridges[br]["lanes"].setdefault(tag, []).append({
                    "vmid": vmid, "type": gtype, "name": g["name"] or st.get("name") or "", "node_id": n.id,
                    "node": n.name, "status": st.get("status") or "unknown", "nic": nic["nic"], "mac": nic["mac"],
                    "firewall": nic["firewall"], "trunks": nic["trunks"], "ip": nic["ip"],
                    "link_down": nic["link_down"], "model": nic["model"]})
                bridges[br]["interfaces"] += 1
                tags[tag] = tags.get(tag, 0) + 1
    out = []
    for b in sorted(bridges.values(), key=lambda x: (x["kind"] != "bridge", x["name"])):
        lanes = [{"tag": t, "guests": sorted(gl, key=lambda g: (g["vmid"], g["nic"]))}
                 for t, gl in sorted(b["lanes"].items())]
        out.append({**b, "lanes": lanes})
    return {"bridges": out, "tags": [{"tag": t, "count": c} for t, c in sorted(tags.items())],
            "nodes": [{"node_id": n.id, "node": n.name, "online": agent_hub.is_online(n.id)} for n in nlist],
            "errors": errors}


# ------------------------------------------------------------------ changing bridges
class BridgeIn(BaseModel):
    iface: str = Field(pattern=IFACE_RE.pattern)
    ports: list[str] = Field(default_factory=list, max_length=8)
    vlan_aware: bool = False
    vids: str | None = Field(default=None, max_length=100)
    cidr: str | None = Field(default=None, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    mtu: int | None = Field(default=None, ge=576, le=65520)
    autostart: bool = True
    comments: str = Field(default="", max_length=200)

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not IFACE_RE.match(p)]
        if bad:
            raise ValueError(f"invalid port names: {', '.join(bad)}")
        return v

    @field_validator("vids")
    @classmethod
    def _vids(cls, v: str | None) -> str | None:
        if v and not VIDS_RE.match(v.strip()):
            raise ValueError("VLAN IDs look like '2-4094' or '10 20 30-40'")
        return v.strip() if v else None

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, v: str | None) -> str | None:
        if not v:
            return None
        import ipaddress

        try:
            ipaddress.ip_interface(v)
        except ValueError as e:
            raise ValueError("cidr must be an address with prefix, e.g. 10.0.30.1/24") from e
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


def _bridge_args(b: BridgeIn, create: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"type": "bridge", "autostart": b.autostart,
                            "bridge_ports": " ".join(b.ports), "bridge_vlan_aware": b.vlan_aware}
    if create:
        args["iface"] = b.iface
    if b.vids and b.vlan_aware:
        args["bridge_vids"] = b.vids
    if b.cidr:
        args["cidr"] = b.cidr
    if b.gateway:
        args["gateway"] = b.gateway
    if b.mtu:
        args["mtu"] = b.mtu
    if b.comments:
        args["comments"] = b.comments
    if not create:
        drop = [k for k, v in (("cidr", b.cidr), ("gateway", b.gateway), ("mtu", b.mtu),
                               ("bridge_vids", b.vids if b.vlan_aware else None), ("comments", b.comments)) if not v]
        if drop:
            args["delete"] = ",".join(drop)
    return args


async def _users_of(node_id: str, iface: str) -> list[str]:
    nics = await guest_nics(node_id)
    return [f"{g['vmid']} {g['name']}".strip() for g in nics.values() if any(n["bridge"] == iface for n in g["nics"])]


async def stage_bridge(node_id: str, body: BridgeIn, *, create: bool) -> dict[str, Any]:
    """Write the bridge into /etc/network/interfaces.new on the node (nothing is applied yet)."""
    ifs = await interfaces(node_id, fresh=True)
    exists = any(i["iface"] == body.iface for i in ifs)
    if create and exists:
        raise NodeError(f"{body.iface} already exists on this node")
    if not create and not exists:
        raise NodeError(f"{body.iface} not found on this node")
    known = {i["iface"] for i in ifs}
    missing = [p for p in body.ports if p not in known]
    if missing:
        raise NodeError(f"unknown ports on this node: {', '.join(missing)}")
    path = "/nodes/localhost/network" if create else f"/nodes/localhost/network/{body.iface}"
    await guests.api(node_id, "create" if create else "set", path, _bridge_args(body, create))
    invalidate(node_id)
    return await pending(node_id)


async def stage_delete(node_id: str, iface: str, *, force: bool = False) -> dict[str, Any]:
    if not IFACE_RE.match(iface):
        raise NodeError("invalid interface name")
    users = await _users_of(node_id, iface)
    if users and not force:
        raise NodeError(f"{iface} is still used by {', '.join(users[:8])}; move those guests first")
    await guests.api(node_id, "delete", f"/nodes/localhost/network/{iface}")
    invalidate(node_id)
    return await pending(node_id)


async def pending(node_id: str) -> dict[str, Any]:
    """The staged change as a unified diff, plus warnings about the node's own address."""
    q = shlex.quote
    cmd = (f"if [ -f {q(INTERFACES + '.new')} ]; then diff -u --label current --label pending "
           f"{q(INTERFACES)} {q(INTERFACES + '.new')}; true; fi")
    res = await _exec(node_id, cmd, 20)
    diff = res.get("stdout") or ""
    warnings: list[str] = []
    if diff:
        ifs = await interfaces(node_id, fresh=True)
        mgmt = [i for i in ifs if i["cidr"] and i["gateway"]]
        for i in mgmt:
            if re.search(rf"^[-+].*\b{re.escape(i['iface'])}\b", diff, re.M):
                warnings.append(f"{i['iface']} carries this node's address {i['cidr']}: a mistake can cut the "
                                f"connection. The app rolls back automatically after {CONFIRM_SECONDS} seconds "
                                "without contact.")
    return {"node_id": node_id, "has_changes": bool(diff.strip()), "diff": diff, "warnings": warnings}


async def revert_pending(node_id: str) -> dict[str, Any]:
    await guests.api(node_id, "delete", "/nodes/localhost/network")
    invalidate(node_id)
    return await pending(node_id)


def apply_script(token: str, seconds: int = CONFIRM_SECONDS) -> str:
    return f"""set -u
TOKEN={shlex.quote(token)}
WAIT={int(seconds)}
B=/var/lib/mmn-agent/network-backups
mkdir -p "$B"
TS=$(date +%Y%m%d-%H%M%S)
if [ ! -f /etc/network/interfaces.new ]; then echo "No pending network changes"; exit 0; fi
cp -a /etc/network/interfaces "$B/interfaces.$TS"
echo "Backup of the current configuration: $B/interfaces.$TS"
restore() {{
  echo "$1"
  cp -a "$B/interfaces.$TS" /etc/network/interfaces
  rm -f /etc/network/interfaces.new
  ifreload -a || true
  rm -f /run/mmn-net-applied-$TOKEN /run/mmn-net-confirm-$TOKEN
}}
echo "Applying the network configuration (ifreload)"
if ! pvesh set /nodes/localhost/network; then restore "Applying failed: the previous configuration is back"; exit 1; fi
touch /run/mmn-net-applied-$TOKEN
echo "Applied. Waiting up to $WAIT seconds for the app to confirm the node is still reachable"
i=0
while [ $i -lt $WAIT ]; do
  if [ -f /run/mmn-net-confirm-$TOKEN ]; then
    rm -f /run/mmn-net-applied-$TOKEN /run/mmn-net-confirm-$TOKEN
    echo "Confirmed by the app: the new configuration stays"
    exit 0
  fi
  sleep 1
  i=$((i+1))
done
restore "No contact with the app within $WAIT seconds: the previous configuration is back"
exit 3
"""


async def _read_files(node_id: str, paths: list[str]) -> dict[str, str | None]:
    """Read config files from the node (base64, so any content survives)."""
    out: dict[str, str | None] = {}
    for p in paths:
        res = await _exec(node_id, f"if [ -f {shlex.quote(p)} ]; then base64 -w0 {shlex.quote(p)}; else echo MISSING; fi", 30)
        raw = (res.get("stdout") or "").strip()
        out[p] = None if raw == "MISSING" else base64.b64decode(raw).decode("utf-8", "replace")
    return out


async def apply(node_id: str, *, dry_run: bool = True, source: str = "ui", requested_by: str | None = None) -> dict[str, Any]:
    """Apply the staged change. With ``dry_run`` only the diff and warnings are returned. Otherwise
    the current file is stored in the app, the change is applied and the app confirms contact; if
    it cannot, the node restores the previous configuration by itself."""
    plan = await pending(node_id)
    if dry_run or not plan["has_changes"]:
        return {**plan, "dry_run": dry_run, "applied": False}
    files = await _read_files(node_id, [INTERFACES])
    async with sessionmaker()() as db:
        b = ConfigBackup(kind="network", node_id=node_id, reason="before applying a network change",
                         files=files, created_by=requested_by)
        db.add(b)
        await db.commit()
        backup_id = b.id
    token = secrets.token_hex(8)
    job = await guests.script(node_id, "network.apply", apply_script(token), timeout=CONFIRM_SECONDS + 120,
                              source=source, requested_by=requested_by, label={"backup_id": backup_id})
    agent_hub.spawn(_confirm(node_id, job.id, token))
    return {**plan, "dry_run": False, "applied": True, "job": nodes.job_out(job, with_output=False),
            "backup_id": backup_id}


async def _confirm(node_id: str, job_id: str, token: str) -> None:
    """Keep trying to reach the node and confirm; the node rolls back if this never gets through."""
    deadline = time.monotonic() + CONFIRM_SECONDS - 10
    cmd = (f"if [ -f /run/mmn-net-applied-{token} ]; then touch /run/mmn-net-confirm-{token}; echo confirmed; fi")
    await asyncio.sleep(2)
    while time.monotonic() < deadline:
        try:
            res = await _exec(node_id, cmd, 10)
            if "confirmed" in (res.get("stdout") or ""):
                agent_hub.add_line(node_id, job_id, "App: the node answered after the change")
                invalidate(node_id)
                return
        except AgentError:
            pass
        async with sessionmaker()() as db:
            from mmn.models import NodeJob

            j = await db.get(NodeJob, job_id)
            if j is None or j.status != "running":
                return
        await asyncio.sleep(2)
    agent_hub.add_line(node_id, job_id, "App: could not reach the node; it restores the previous configuration")


def backup_out(b: ConfigBackup, with_files: bool = False) -> dict[str, Any]:
    out = {"id": b.id, "kind": b.kind, "node_id": b.node_id, "design_id": b.design_id, "reason": b.reason,
           "created_by": b.created_by, "created_at": b.created_at.isoformat(),
           "restored_at": b.restored_at.isoformat() if b.restored_at else None, "files": sorted(b.files or {})}
    if with_files:
        out["content"] = b.files or {}
    return out


async def list_backups(db: AsyncSession, *, kind: str | None = None, node_id: str | None = None,
                       design_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    q = select(ConfigBackup).order_by(ConfigBackup.created_at.desc()).limit(limit)
    if kind:
        q = q.where(ConfigBackup.kind == kind)
    if node_id:
        q = q.where(ConfigBackup.node_id == node_id)
    if design_id:
        q = q.where(ConfigBackup.design_id == design_id)
    return [backup_out(b) for b in (await db.execute(q)).scalars()]


async def restore_network_backup(backup_id: str, *, source: str = "ui", requested_by: str | None = None) -> dict[str, Any]:
    """Put a saved /etc/network/interfaces back as a pending change and apply it the safe way."""
    async with sessionmaker()() as db:
        b = await db.get(ConfigBackup, backup_id)
        if b is None or b.kind != "network":
            raise NodeError("network backup not found")
        content = (b.files or {}).get(INTERFACES)
        node_id = b.node_id
    if not content:
        raise NodeError("this backup holds no interfaces file")
    data = base64.b64encode(content.encode()).decode()
    res = await _exec(node_id, f"echo {shlex.quote(data)} | base64 -d > {INTERFACES}.new && echo staged", 30)
    if "staged" not in (res.get("stdout") or ""):
        raise NodeError(f"could not stage the backup: {res.get('stderr') or 'unknown error'}")
    out = await apply(node_id, dry_run=False, source=source, requested_by=requested_by)
    async with sessionmaker()() as db:
        b = await db.get(ConfigBackup, backup_id)
        b.restored_at = datetime.now(UTC)
        await db.commit()
    return out


# ------------------------------------------------------------------ firewall
FwLevel = Literal["cluster", "node", "guest", "group"]
RULE_ACTIONS = ("ACCEPT", "DROP", "REJECT")


def fw_base(level: str, *, gtype: str | None = None, vmid: int | None = None, group: str | None = None) -> str:
    if level == "cluster":
        return "/cluster/firewall"
    if level == "node":
        return "/nodes/localhost/firewall"
    if level == "guest":
        if gtype not in ("qemu", "lxc") or not vmid:
            raise NodeError("a guest firewall needs the guest type and VMID")
        return f"/nodes/localhost/{gtype}/{int(vmid)}/firewall"
    if level == "group":
        if not group or not re.match(r"^[A-Za-z][A-Za-z0-9_-]{1,17}$", group):
            raise NodeError("invalid security group name")
        return f"/cluster/firewall/groups/{group}"
    raise NodeError("level must be cluster, node, guest or group")


class RuleIn(BaseModel):
    type: Literal["in", "out", "group"] = "in"
    action: str = Field(default="ACCEPT", max_length=20)  # ACCEPT/DROP/REJECT, or a group name for type=group
    enable: bool = True
    proto: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,16}$")
    dport: str | None = Field(default=None, pattern=r"^[0-9:,a-z-]{1,100}$")
    sport: str | None = Field(default=None, pattern=r"^[0-9:,a-z-]{1,100}$")
    source: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.:/,+_!-]{1,200}$")
    dest: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.:/,+_!-]{1,200}$")
    macro: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,40}$")
    iface: str | None = Field(default=None, pattern=IFACE_RE.pattern)
    log: Literal["nolog", "info", "warning", "err", "crit", "alert", "emerg", "debug"] | None = None
    comment: str = Field(default="", max_length=200)

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v.upper() in RULE_ACTIONS:
            return v.upper()
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]{1,17}$", v):
            return v
        raise ValueError("action must be ACCEPT, DROP, REJECT or a security group")


def _rule_args(r: RuleIn) -> dict[str, Any]:
    args: dict[str, Any] = {"type": r.type, "action": r.action, "enable": 1 if r.enable else 0}
    for k in ("proto", "dport", "sport", "source", "dest", "macro", "iface", "log"):
        v = getattr(r, k)
        if v:
            args[k] = v
    if r.comment:
        args["comment"] = r.comment
    return args


async def firewall_overview(node_id: str) -> dict[str, Any]:
    async def get(path: str) -> Any:
        try:
            return await guests.api(node_id, "get", path, timeout=20)
        except AgentError as e:
            return {"error": str(e)}

    keys = ("cluster_options", "cluster_rules", "groups", "aliases", "ipsets", "node_options", "node_rules")
    paths = ("/cluster/firewall/options", "/cluster/firewall/rules", "/cluster/firewall/groups",
             "/cluster/firewall/aliases", "/cluster/firewall/ipset", "/nodes/localhost/firewall/options",
             "/nodes/localhost/firewall/rules")
    vals = await asyncio.gather(*(get(p) for p in paths))
    out = dict(zip(keys, vals, strict=True))
    nics = {}
    try:
        nics = await guest_nics(node_id)
    except AgentError:
        pass
    out["guests"] = sorted(({"vmid": g["vmid"], "type": g["type"], "name": g["name"],
                             "nics": [{"nic": n["nic"], "bridge": n["bridge"], "firewall": n["firewall"]}
                                      for n in g["nics"]]} for g in nics.values()), key=lambda g: g["vmid"])
    return out


async def guest_firewall(node_id: str, gtype: str, vmid: int) -> dict[str, Any]:
    base = fw_base("guest", gtype=gtype, vmid=vmid)
    opts, rules = await asyncio.gather(guests.api(node_id, "get", base + "/options"),
                                       guests.api(node_id, "get", base + "/rules"))
    return {"options": opts or {}, "rules": rules or []}


async def group_rules(node_id: str, group: str) -> list[dict[str, Any]]:
    return await guests.api(node_id, "get", fw_base("group", group=group)) or []


async def set_firewall_options(node_id: str, level: str, *, enable: bool | None = None,
                               policy_in: str | None = None, policy_out: str | None = None,
                               gtype: str | None = None, vmid: int | None = None) -> dict[str, Any]:
    base = fw_base(level, gtype=gtype, vmid=vmid)
    args: dict[str, Any] = {}
    if enable is not None:
        args["enable"] = 1 if enable else 0
    for k, v in (("policy_in", policy_in), ("policy_out", policy_out)):
        if v:
            if v.upper() not in RULE_ACTIONS:
                raise NodeError("policy must be ACCEPT, DROP or REJECT")
            args[k] = v.upper()
    if level == "node" and any(k.startswith("policy") for k in args):
        raise NodeError("the node firewall has no policy of its own (set it on the cluster)")
    if not args:
        raise NodeError("nothing to change")
    await guests.api(node_id, "set", base + "/options", args)
    return await guests.api(node_id, "get", base + "/options") or {}


async def add_rule(node_id: str, level: str, rule: RuleIn, *, gtype: str | None = None, vmid: int | None = None,
                   group: str | None = None) -> list[dict[str, Any]]:
    base = fw_base(level, gtype=gtype, vmid=vmid, group=group)
    path = base if level == "group" else base + "/rules"
    await guests.api(node_id, "create", path, _rule_args(rule))
    return await guests.api(node_id, "get", path) or []


async def delete_rule(node_id: str, level: str, pos: int, *, gtype: str | None = None, vmid: int | None = None,
                      group: str | None = None) -> list[dict[str, Any]]:
    base = fw_base(level, gtype=gtype, vmid=vmid, group=group)
    path = base if level == "group" else base + "/rules"
    await guests.api(node_id, "delete", f"{path}/{int(pos)}")
    return await guests.api(node_id, "get", path) or []
