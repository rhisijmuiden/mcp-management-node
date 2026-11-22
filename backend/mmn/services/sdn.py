"""Proxmox VE SDN (software defined networking): overview, and the SDN designer.

A design describes one zone with its VNets and subnets. The app compares it with what the
cluster has now and shows the exact list of changes (the plan). Applying stores the cluster's
SDN configuration files first, makes the changes through the Proxmox API, and reloads SDN; if a
step fails the stored files are put back. Any later moment the backup can be restored with one
click.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import shlex
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import ConfigBackup, SdnDesign
from mmn.services import agent_hub, guests, network, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

ID_RE = re.compile(r"^[a-z][a-z0-9]{1,7}$")
SDN_FILES = [f"/etc/pve/sdn/{f}.cfg" for f in ("zones", "vnets", "subnets", "controllers", "ipams", "dns")]
ZoneType = Literal["vlan", "simple", "vxlan", "evpn", "qinq"]


class ZoneSpec(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    type: ZoneType = "vlan"
    bridge: str | None = Field(default=None, pattern=network.IFACE_RE.pattern)
    tag: int | None = Field(default=None, ge=1, le=4094)  # qinq service tag
    peers: list[str] = Field(default_factory=list, max_length=64)  # vxlan
    controller: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")  # evpn
    vrf_vxlan: int | None = Field(default=None, ge=1, le=16777215)  # evpn
    mtu: int | None = Field(default=None, ge=576, le=65520)
    nodes: list[str] = Field(default_factory=list, max_length=64)  # Proxmox node names; empty = all
    ipam: bool = True
    dhcp: bool = False  # simple zones: built-in DHCP (dnsmasq)


class VnetSpec(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    alias: str = Field(default="", max_length=60)
    tag: int | None = Field(default=None, ge=1, le=16777215)
    vlanaware: bool = False
    color: str | None = Field(default=None, max_length=16)  # only for the designer


class SubnetSpec(BaseModel):
    vnet: str = Field(pattern=ID_RE.pattern)
    cidr: str = Field(max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    snat: bool = False
    dhcp_start: str | None = Field(default=None, max_length=64)
    dhcp_end: str | None = Field(default=None, max_length=64)


class DesignSpec(BaseModel):
    zone: ZoneSpec
    vnets: list[VnetSpec] = Field(default_factory=list, max_length=200)
    subnets: list[SubnetSpec] = Field(default_factory=list, max_length=400)


class DesignIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    node_id: str
    spec: DesignSpec


# ------------------------------------------------------------------ reading the cluster
async def _api(node_id: str, path: str, args: dict[str, Any] | None = None) -> Any:
    return await guests.api(node_id, "get", path, args, timeout=30)


async def state(node_id: str) -> dict[str, Any]:
    """Zones, VNets (with subnets) and controllers as the cluster has them now (pending included)."""
    zones = await _api(node_id, "/cluster/sdn/zones") or []
    vnets = await _api(node_id, "/cluster/sdn/vnets") or []
    try:
        controllers = await _api(node_id, "/cluster/sdn/controllers") or []
    except AgentError:
        controllers = []
    subnets: list[dict[str, Any]] = []
    for v in vnets:
        try:
            for s in await _api(node_id, f"/cluster/sdn/vnets/{v['vnet']}/subnets") or []:
                subnets.append({**s, "vnet": v["vnet"]})
        except AgentError:
            continue
    try:
        status = [r for r in await _api(node_id, "/cluster/resources", {"type": "sdn"}) or []]
    except AgentError:
        status = []
    return {"zones": zones, "vnets": vnets, "subnets": subnets, "controllers": controllers, "status": status}


def _subnet_id(zone: str, cidr: str) -> str:
    net = ipaddress.ip_network(cidr, strict=False)
    return f"{zone}-{net.network_address}-{net.prefixlen}"


def spec_from_state(st: dict[str, Any], zone_id: str) -> DesignSpec:
    z = next((z for z in st["zones"] if z.get("zone") == zone_id), None)
    if z is None:
        raise NodeError("zone not found")
    zone = ZoneSpec(id=zone_id, type=z.get("type") if z.get("type") in ZoneType.__args__ else "vlan",
                    bridge=z.get("bridge"), tag=z.get("tag"), peers=[p for p in (z.get("peers") or "").split(",") if p],
                    controller=z.get("controller"), vrf_vxlan=z.get("vrf-vxlan"), mtu=z.get("mtu"),
                    nodes=[n for n in (z.get("nodes") or "").split(",") if n], ipam=bool(z.get("ipam")),
                    dhcp=z.get("dhcp") == "dnsmasq")
    vn = [v for v in st["vnets"] if v.get("zone") == zone_id]
    vnets = [VnetSpec(id=v["vnet"], alias=v.get("alias") or "", tag=v.get("tag"), vlanaware=bool(v.get("vlanaware")))
             for v in vn]
    ids = {v.id for v in vnets}
    subnets = []
    for s in st["subnets"]:
        if s.get("vnet") not in ids:
            continue
        start = end = None
        dr = s.get("dhcp-range")
        if isinstance(dr, list) and dr:
            first = dr[0] if isinstance(dr[0], dict) else {}
            start, end = first.get("start-address"), first.get("end-address")
        subnets.append(SubnetSpec(vnet=s["vnet"], cidr=s.get("cidr") or s.get("subnet") or "",
                                  gateway=s.get("gateway"), snat=bool(s.get("snat")), dhcp_start=start, dhcp_end=end))
    return DesignSpec(zone=zone, vnets=vnets, subnets=subnets)


# ------------------------------------------------------------------ validation and plan
def _zone_args(z: ZoneSpec, create: bool) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if create:
        args.update(zone=z.id, type=z.type)
    if z.type in ("vlan", "qinq"):
        args["bridge"] = z.bridge
    if z.type == "qinq" and z.tag:
        args["tag"] = z.tag
    if z.type == "vxlan":
        args["peers"] = ",".join(z.peers)
    if z.type == "evpn":
        args["controller"] = z.controller
        args["vrf-vxlan"] = z.vrf_vxlan
    if z.mtu:
        args["mtu"] = z.mtu
    if z.nodes:
        args["nodes"] = ",".join(z.nodes)
    if z.ipam:
        args["ipam"] = "pve"
    if z.type == "simple" and z.dhcp:
        args["dhcp"] = "dnsmasq"
    return args


def _vnet_args(v: VnetSpec, zone: ZoneSpec, create: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"zone": zone.id}
    if create:
        args["vnet"] = v.id
    if v.alias:
        args["alias"] = v.alias
    if zone.type != "simple" and v.tag:
        args["tag"] = v.tag
    if v.vlanaware:
        args["vlanaware"] = 1
    return args


def _subnet_args(s: SubnetSpec, zone: ZoneSpec, create: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"type": "subnet"}
    if create:
        args["subnet"] = str(ipaddress.ip_network(s.cidr, strict=False))
    if s.gateway:
        args["gateway"] = s.gateway
    args["snat"] = 1 if s.snat else 0
    if s.dhcp_start and s.dhcp_end:
        args["dhcp-range"] = f"start-address={s.dhcp_start},end-address={s.dhcp_end}"
    return args


def _norm(d: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    out = {}
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            v = int(v)
        if v in (None, "", 0) and k in ("snat", "vlanaware"):
            v = 0
        if v not in (None, ""):
            out[k] = str(v)
    return out


def validate(spec: DesignSpec, st: dict[str, Any], *, bridges: dict[str, dict[str, Any]] | None = None,
             cluster_nodes: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Errors (block applying) and warnings."""
    errors: list[str] = []
    warnings: list[str] = []
    z = spec.zone
    other_vnets = {v["vnet"]: v for v in st["vnets"] if v.get("zone") != z.id}
    if z.type in ("vlan", "qinq") and not z.bridge:
        errors.append("a VLAN or QinQ zone needs a bridge")
    if z.type == "vxlan" and not z.peers:
        errors.append("a VXLAN zone needs the peer addresses of all nodes")
    for p in z.peers:
        try:
            ipaddress.ip_address(p)
        except ValueError:
            errors.append(f"peer {p} is not an IP address")
    if z.type == "evpn" and (not z.controller or not z.vrf_vxlan):
        errors.append("an EVPN zone needs a controller and a VRF VXLAN id")
    if z.type == "evpn" and z.controller and not any(c.get("controller") == z.controller for c in st["controllers"]):
        errors.append(f"controller {z.controller} does not exist; create it first")
    if cluster_nodes:
        missing = [n for n in z.nodes if n not in cluster_nodes]
        if missing:
            errors.append(f"unknown nodes: {', '.join(missing)}")
    if bridges is not None and z.bridge and z.type in ("vlan", "qinq"):
        b = bridges.get(z.bridge)
        if b is None:
            errors.append(f"bridge {z.bridge} does not exist on the nodes")
        else:
            on = {n["node"] for n in b.get("nodes", [])}
            if cluster_nodes and z.nodes:
                lacking = [n for n in z.nodes if n not in on and n in cluster_nodes]
                if lacking:
                    warnings.append(f"bridge {z.bridge} is missing on: {', '.join(lacking)}")
            if not b.get("vlan_aware"):
                warnings.append(f"bridge {z.bridge} is not VLAN aware; Proxmox will create VLAN interfaces "
                                "on its ports instead")
    seen: set[str] = set()
    tags: dict[int, str] = {}
    for v in spec.vnets:
        if v.id in seen:
            errors.append(f"VNet {v.id} appears twice")
        seen.add(v.id)
        if v.id in other_vnets:
            errors.append(f"VNet {v.id} already exists in zone {other_vnets[v.id].get('zone')}")
        if bridges is not None and v.id in bridges and bridges[v.id].get("kind") == "bridge":
            errors.append(f"{v.id} is already the name of a bridge on a node")
        if v.id == z.id:
            warnings.append(f"VNet {v.id} has the same name as its zone")
        if z.type == "simple":
            if v.tag:
                warnings.append(f"VNet {v.id}: a simple zone ignores tags")
            continue
        if not v.tag:
            errors.append(f"VNet {v.id} needs a {'VNI' if z.type in ('vxlan', 'evpn') else 'VLAN tag'}")
            continue
        if z.type in ("vlan", "qinq") and v.tag > 4094:
            errors.append(f"VNet {v.id}: VLAN tags go from 1 to 4094")
        if v.tag in tags:
            errors.append(f"VNets {tags[v.tag]} and {v.id} use the same tag {v.tag}")
        tags[v.tag] = v.id
    nets: list[tuple[ipaddress._BaseNetwork, str]] = []
    for s in spec.subnets:
        if s.vnet not in seen:
            errors.append(f"subnet {s.cidr} belongs to unknown VNet {s.vnet}")
        try:
            net = ipaddress.ip_network(s.cidr, strict=False)
        except ValueError:
            errors.append(f"{s.cidr} is not a network such as 10.0.20.0/24")
            continue
        if str(net) != s.cidr and "/" in s.cidr:
            warnings.append(f"{s.cidr} is not a network address; it will be {net}")
        for addr, label in ((s.gateway, "gateway"), (s.dhcp_start, "DHCP start"), (s.dhcp_end, "DHCP end")):
            if not addr:
                continue
            try:
                if ipaddress.ip_address(addr) not in net:
                    errors.append(f"{label} {addr} is outside {net}")
            except ValueError:
                errors.append(f"{label} {addr} is not an IP address")
        if bool(s.dhcp_start) != bool(s.dhcp_end):
            errors.append(f"{net}: give both the start and the end of the DHCP range")
        if (s.dhcp_start or s.dhcp_end) and not (z.type == "simple" and z.dhcp):
            warnings.append(f"{net}: DHCP ranges only work in a simple zone with DHCP enabled")
        if s.snat and not s.gateway:
            errors.append(f"{net}: SNAT needs a gateway")
        for other, where in nets:
            if net.version == other.version and net.overlaps(other):
                errors.append(f"{net} overlaps {other} ({where})")
        nets.append((net, f"VNet {s.vnet}"))
    mine = {v.id for v in spec.vnets}
    for s in st["subnets"]:
        if s.get("vnet") in mine:
            continue
        try:
            other = ipaddress.ip_network(s.get("cidr") or s.get("subnet") or "", strict=False)
        except ValueError:
            continue
        for net, where in nets:
            if net.version == other.version and net.overlaps(other):
                warnings.append(f"{net} ({where}) overlaps {other} of VNet {s.get('vnet')}")
    return errors, warnings


def plan_ops(spec: DesignSpec, st: dict[str, Any], users: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """The list of API calls that turns the cluster's SDN into the design (for this zone only)."""
    z = spec.zone
    ops: list[dict[str, Any]] = []
    cur_zone = next((x for x in st["zones"] if x.get("zone") == z.id), None)
    zkeys = ["type", "bridge", "tag", "peers", "controller", "vrf-vxlan", "mtu", "nodes", "ipam", "dhcp"]
    want_zone = _zone_args(z, True)
    if cur_zone is None:
        ops.append({"op": "create", "kind": "zone", "id": z.id, "method": "create", "path": "/cluster/sdn/zones",
                    "args": want_zone, "before": None, "after": _norm(want_zone, zkeys)})
    else:
        before, after = _norm(cur_zone, zkeys), _norm(want_zone, zkeys)
        if before != after:
            args = _zone_args(z, False)
            drop = [k for k in zkeys if k in before and k not in after and k != "type"]
            if drop:
                args["delete"] = ",".join(drop)
            ops.append({"op": "update", "kind": "zone", "id": z.id, "method": "set",
                        "path": f"/cluster/sdn/zones/{z.id}", "args": args, "before": before, "after": after})
    cur_vnets = {v["vnet"]: v for v in st["vnets"] if v.get("zone") == z.id}
    vkeys = ["zone", "alias", "tag", "vlanaware"]
    for v in spec.vnets:
        want = _vnet_args(v, z, True)
        cur = cur_vnets.get(v.id)
        if cur is None:
            ops.append({"op": "create", "kind": "vnet", "id": v.id, "method": "create", "path": "/cluster/sdn/vnets",
                        "args": want, "before": None, "after": _norm(want, vkeys)})
            continue
        before, after = _norm(cur, vkeys), _norm(want, vkeys)
        if before != after:
            args = _vnet_args(v, z, False)
            drop = [k for k in ("alias", "tag", "vlanaware") if k in before and k not in after]
            if drop:
                args["delete"] = ",".join(drop)
            ops.append({"op": "update", "kind": "vnet", "id": v.id, "method": "set",
                        "path": f"/cluster/sdn/vnets/{v.id}", "args": args, "before": before, "after": after})
    want_ids = {v.id for v in spec.vnets}
    cur_subnets = {(s["vnet"], str(ipaddress.ip_network(s.get("cidr") or s.get("subnet"), strict=False))): s
                   for s in st["subnets"] if s.get("vnet") in cur_vnets and (s.get("cidr") or s.get("subnet"))}
    skeys = ["gateway", "snat", "dhcp-range"]
    want_subnets = set()
    for s in spec.subnets:
        try:
            net = str(ipaddress.ip_network(s.cidr, strict=False))
        except ValueError:
            continue
        want_subnets.add((s.vnet, net))
        want = _subnet_args(s, z, True)
        cur = cur_subnets.get((s.vnet, net))
        if cur is None:
            ops.append({"op": "create", "kind": "subnet", "id": f"{s.vnet}:{net}", "method": "create",
                        "path": f"/cluster/sdn/vnets/{s.vnet}/subnets", "args": want, "before": None,
                        "after": _norm(want, ["subnet", *skeys])})
            continue
        cdr = cur.get("dhcp-range")
        cur_flat = {**cur, "dhcp-range": (f"start-address={cdr[0].get('start-address')},end-address="
                                          f"{cdr[0].get('end-address')}") if isinstance(cdr, list) and cdr
                    and isinstance(cdr[0], dict) else None}
        before, after = _norm(cur_flat, skeys), _norm(want, skeys)
        if before != after:
            sid = cur.get("subnet") if "-" in str(cur.get("subnet", "")) else cur.get("id") or _subnet_id(z.id, net)
            args = _subnet_args(s, z, False)
            drop = [k for k in ("gateway", "dhcp-range") if k in before and k not in after]
            if drop:
                args["delete"] = ",".join(drop)
            ops.append({"op": "update", "kind": "subnet", "id": f"{s.vnet}:{net}", "method": "set",
                        "path": f"/cluster/sdn/vnets/{s.vnet}/subnets/{sid}", "args": args,
                        "before": before, "after": after})
    for (vnet, net), cur in cur_subnets.items():
        if (vnet, net) in want_subnets:
            continue
        sid = cur.get("subnet") if "-" in str(cur.get("subnet", "")) else cur.get("id") or _subnet_id(z.id, net)
        ops.append({"op": "delete", "kind": "subnet", "id": f"{vnet}:{net}", "method": "delete",
                    "path": f"/cluster/sdn/vnets/{vnet}/subnets/{sid}", "args": {}, "before": {"subnet": net},
                    "after": None})
    for vid in cur_vnets:
        if vid in want_ids:
            continue
        used = (users or {}).get(vid) or []
        ops.append({"op": "delete", "kind": "vnet", "id": vid, "method": "delete", "path": f"/cluster/sdn/vnets/{vid}",
                    "args": {}, "before": _norm(cur_vnets[vid], vkeys), "after": None, "used_by": used})
    return ops


def _summary(op: dict[str, Any]) -> str:
    verb = {"create": "Create", "update": "Change", "delete": "Remove"}[op["op"]]
    detail = ", ".join(f"{k}={v}" for k, v in (op.get("after") or {}).items() if k not in ("zone",))
    return f"{verb} {op['kind']} {op['id']}" + (f" ({detail})" if detail and op["op"] != "delete" else "")


async def plan(db: AsyncSession, node_id: str, spec: DesignSpec) -> dict[str, Any]:
    node = await nodes.get_node(db, node_id)
    agent_hub.get(node.id)
    st = await state(node.id)
    bmap = await network.bridge_map(db, environment_id=node.environment_id)
    bridges = {b["name"]: b for b in bmap["bridges"]}
    cluster_nodes = [n.get("name") for n in ((node.info or {}).get("cluster") or {}).get("nodes") or []] or \
        [node.pve_name or node.name]
    errors, warnings = validate(spec, st, bridges=bridges, cluster_nodes=cluster_nodes)
    users = {b["name"]: [f"{g['vmid']} {g['name']} ({g['node']})".strip() for ln in b["lanes"] for g in ln["guests"]]
             for b in bmap["bridges"]}
    ops = plan_ops(spec, st, users)
    for op in ops:
        op["summary"] = _summary(op)
        if op["op"] == "delete" and op["kind"] == "vnet" and op.get("used_by"):
            errors.append(f"VNet {op['id']} is still used by {', '.join(op['used_by'][:6])}")
    ops.append({"op": "reload", "kind": "sdn", "id": "cluster", "method": "set", "path": "/cluster/sdn", "args": {},
                "summary": f"Reload SDN on {', '.join(spec.zone.nodes) if spec.zone.nodes else 'all nodes'}"}
               if any(o["op"] != "reload" for o in ops) else
               {"op": "none", "kind": "sdn", "id": "cluster", "summary": "Nothing to change"})
    return {"node_id": node.id, "cluster": node.cluster_name, "cluster_nodes": cluster_nodes, "errors": errors,
            "warnings": warnings, "ops": ops, "current": st,
            "can_apply": not errors and any(o["op"] in ("create", "update", "delete") for o in ops)}


# ------------------------------------------------------------------ designs
def design_out(d: SdnDesign) -> dict[str, Any]:
    return {"id": d.id, "name": d.name, "node_id": d.node_id, "spec": d.spec, "status": d.status,
            "created_by": d.created_by, "created_at": d.created_at.isoformat(), "updated_at": d.updated_at.isoformat(),
            "applied_at": d.applied_at.isoformat() if d.applied_at else None, "last_job_id": d.last_job_id,
            "last_backup_id": d.last_backup_id}


async def list_designs(db: AsyncSession) -> list[dict[str, Any]]:
    return [design_out(d) for d in (await db.execute(select(SdnDesign).order_by(SdnDesign.updated_at.desc()))).scalars()]


async def get_design(db: AsyncSession, design_id: str) -> SdnDesign:
    d = await db.get(SdnDesign, design_id)
    if d is None:
        raise NodeError("design not found")
    return d


async def save_design(db: AsyncSession, body: DesignIn, design: SdnDesign | None = None,
                      user: str | None = None) -> SdnDesign:
    node = await nodes.get_node(db, body.node_id)
    if "pve" not in (node.capabilities or []):
        raise NodeError("choose a Proxmox VE node of the cluster")
    if design is None:
        design = SdnDesign(name=body.name, node_id=node.id, spec=body.spec.model_dump(), created_by=user)
        db.add(design)
    else:
        design.name, design.node_id, design.spec = body.name, node.id, body.spec.model_dump()
        if design.status != "draft":
            design.status = "draft"
    design.updated_at = datetime.now(UTC)
    await db.flush()
    return design


# ------------------------------------------------------------------ apply, backup, rollback
async def _backup(node_id: str, reason: str, design_id: str | None, user: str | None) -> str:
    files = await network._read_files(node_id, SDN_FILES)
    async with sessionmaker()() as db:
        b = ConfigBackup(kind="sdn", node_id=node_id, design_id=design_id, reason=reason, files=files, created_by=user)
        db.add(b)
        await db.commit()
        return b.id


def restore_script(files: dict[str, str | None]) -> str:
    lines = ["set -e"]
    for path, content in sorted(files.items()):
        if path not in SDN_FILES:
            continue
        if content is None:
            lines.append(f"rm -f {shlex.quote(path)}")
        else:
            data = base64.b64encode(content.encode()).decode()
            lines.append(f"echo {shlex.quote(data)} | base64 -d > {shlex.quote(path)}")
    lines.append('echo "SDN configuration files restored"')
    return "\n".join(lines) + "\n"


async def _restore_files(node_id: str, files: dict[str, str | None]) -> None:
    res = await network._exec(node_id, restore_script(files), 60)
    if res.get("exit_code") not in (0, None):
        raise NodeError(f"restoring the SDN files failed: {res.get('stderr') or res.get('stdout')}")


async def _reload(node_id: str, job_id: str, source: str, requested_by: str | None) -> tuple[str, str | None]:
    sub = await guests.task(node_id, "sdn.reload", "set", "/cluster/sdn", {}, parent=job_id, source=source,
                            requested_by=requested_by)
    status, _, err = await guests.run_and_wait(sub, 600)
    return status, err


async def apply(db: AsyncSession, design: SdnDesign, *, dry_run: bool = True, source: str = "ui",
                requested_by: str | None = None) -> dict[str, Any]:
    spec = DesignSpec(**design.spec)
    p = await plan(db, design.node_id, spec)
    if dry_run or not p["can_apply"]:
        return {**p, "dry_run": dry_run, "applied": False}
    job = await nodes.create_job(design.node_id, "sdn.apply", {"design": design.name}, source=source,
                                 requested_by=requested_by, exclusive=True)
    design.last_job_id = job.id
    await db.flush()
    agent_hub.spawn(_apply_pipeline(job.id, design.id, design.node_id, p["ops"], source, requested_by))
    return {**p, "dry_run": False, "applied": True, "job": nodes.job_out(job, with_output=False)}


async def _apply_pipeline(job_id: str, design_id: str, node_id: str, ops: list[dict[str, Any]], source: str,
                          requested_by: str | None) -> None:
    say = lambda line: agent_hub.add_line(node_id, job_id, line)  # noqa: E731
    backup_id = None
    files: dict[str, str | None] = {}
    status = "failed"
    try:
        say("Saving the current SDN configuration")
        backup_id = await _backup(node_id, "before applying an SDN design", design_id, requested_by)
        async with sessionmaker()() as db:
            files = (await db.get(ConfigBackup, backup_id)).files
            d = await db.get(SdnDesign, design_id)
            if d is not None:
                d.last_backup_id = backup_id
            await db.commit()
        say(f"Backup stored in the app ({backup_id})")
        order = {"zone": 0, "vnet": 1, "subnet": 2}
        creates = sorted([o for o in ops if o["op"] in ("create", "update")], key=lambda o: order[o["kind"]])
        deletes = sorted([o for o in ops if o["op"] == "delete"], key=lambda o: -order[o["kind"]])
        for op in creates + deletes:
            say(op.get("summary") or f"{op['op']} {op['kind']} {op['id']}")
            await guests.api(node_id, op["method"], op["path"], op.get("args") or {})
        say("Reloading SDN on the nodes")
        rstatus, err = await _reload(node_id, job_id, source, requested_by)
        if rstatus != "success":
            raise NodeError(f"reloading SDN failed: {err}")
        status = "applied"
        await agent_hub.finish_job(job_id, "success", {"backup_id": backup_id, "changes": len(creates + deletes)})
    except Exception as e:  # noqa: BLE001
        say(f"ERROR: {e}")
        if backup_id:
            say("Putting the previous SDN configuration back")
            try:
                await _restore_files(node_id, files)
                await _reload(node_id, job_id, source, requested_by)
                say("Previous configuration restored")
                status = "failed"
            except Exception as re_:  # noqa: BLE001
                say(f"ERROR while restoring: {re_}. Restore the backup from the SDN page.")
        await agent_hub.finish_job(job_id, "failed", {"backup_id": backup_id}, str(e))
    async with sessionmaker()() as db:
        d = await db.get(SdnDesign, design_id)
        if d is not None:
            d.status = status
            if status == "applied":
                d.applied_at = datetime.now(UTC)
            await db.commit()
    network.invalidate(node_id)


async def rollback(db: AsyncSession, backup_id: str, *, source: str = "ui", requested_by: str | None = None) -> dict[str, Any]:
    """Put the SDN configuration of a backup back and reload SDN (a new backup is made first)."""
    b = await db.get(ConfigBackup, backup_id)
    if b is None or b.kind != "sdn":
        raise NodeError("SDN backup not found")
    agent_hub.get(b.node_id)
    job = await nodes.create_job(b.node_id, "sdn.rollback", {"backup_id": b.id}, source=source,
                                 requested_by=requested_by, exclusive=True)
    agent_hub.spawn(_rollback_pipeline(job.id, b.id, b.node_id, source, requested_by))
    return {"job": nodes.job_out(job, with_output=False), "backup_id": b.id}


async def _rollback_pipeline(job_id: str, backup_id: str, node_id: str, source: str, requested_by: str | None) -> None:
    say = lambda line: agent_hub.add_line(node_id, job_id, line)  # noqa: E731
    try:
        async with sessionmaker()() as db:
            b = await db.get(ConfigBackup, backup_id)
            files, design_id = dict(b.files or {}), b.design_id
        say("Saving the current SDN configuration first")
        safety = await _backup(node_id, f"before restoring backup {backup_id}", design_id, requested_by)
        say(f"Writing the SDN configuration of {backup_id}")
        await _restore_files(node_id, files)
        say("Reloading SDN on the nodes")
        status, err = await _reload(node_id, job_id, source, requested_by)
        if status != "success":
            raise NodeError(f"reloading SDN failed: {err}")
        async with sessionmaker()() as db:
            b = await db.get(ConfigBackup, backup_id)
            b.restored_at = datetime.now(UTC)
            if design_id:
                d = await db.get(SdnDesign, design_id)
                if d is not None and d.last_backup_id == backup_id:
                    d.status = "rolled_back"
            await db.commit()
        await agent_hub.finish_job(job_id, "success", {"backup_id": backup_id, "safety_backup_id": safety})
    except Exception as e:  # noqa: BLE001
        say(f"ERROR: {e}")
        await agent_hub.finish_job(job_id, "failed", {"backup_id": backup_id}, str(e))
    network.invalidate(node_id)
