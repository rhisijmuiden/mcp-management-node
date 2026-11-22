"""Cluster features of Proxmox VE: high availability (resources, groups or rules), storage
replication, backup jobs (vzdump) and storage. All calls go to one node of the cluster (the
"anchor"); pvesh forwards node-specific calls inside the cluster by itself.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from mmn.services import agent_hub, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

SID_RE = re.compile(r"^(vm|ct):\d{3,9}$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")
CONTENT = {"images", "rootdir", "backup", "iso", "vztmpl", "snippets", "import"}


async def _get(node_id: str, path: str, args: dict[str, Any] | None = None, default: Any = None) -> Any:
    try:
        return await guests.api(node_id, "get", path, args, timeout=30)
    except AgentError:
        if default is not None:
            return default
        raise


# ------------------------------------------------------------------ high availability
async def ha_overview(node_id: str) -> dict[str, Any]:
    status, resources = await asyncio.gather(_get(node_id, "/cluster/ha/status/current", default=[]),
                                             _get(node_id, "/cluster/ha/resources", default=[]))
    rules: list[dict[str, Any]] | None
    try:
        rules = await guests.api(node_id, "get", "/cluster/ha/rules", timeout=20) or []
    except AgentError:
        rules = None
    groups = [] if rules is not None else await _get(node_id, "/cluster/ha/groups", default=[])
    return {"mode": "rules" if rules is not None else "groups", "status": status or [], "resources": resources or [],
            "groups": groups or [], "rules": rules or []}


class HaResourceIn(BaseModel):
    sid: str = Field(pattern=SID_RE.pattern)
    state: Literal["started", "stopped", "ignored", "disabled"] = "started"
    group: str | None = Field(default=None, pattern=ID_RE.pattern)
    max_restart: int = Field(default=1, ge=0, le=10)
    max_relocate: int = Field(default=1, ge=0, le=10)
    comment: str = Field(default="", max_length=200)


def _ha_args(r: HaResourceIn, create: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"state": r.state, "max_restart": r.max_restart, "max_relocate": r.max_relocate}
    if create:
        args["sid"] = r.sid
    if r.group:
        args["group"] = r.group
    if r.comment:
        args["comment"] = r.comment
    return args


async def save_ha_resource(node_id: str, r: HaResourceIn, *, create: bool) -> dict[str, Any]:
    if create:
        await guests.api(node_id, "create", "/cluster/ha/resources", _ha_args(r, True))
    else:
        await guests.api(node_id, "set", f"/cluster/ha/resources/{r.sid}", _ha_args(r, False))
    return await ha_overview(node_id)


async def remove_ha_resource(node_id: str, sid: str) -> dict[str, Any]:
    if not SID_RE.match(sid):
        raise NodeError("sid looks like vm:100 or ct:101")
    await guests.api(node_id, "delete", f"/cluster/ha/resources/{sid}")
    return await ha_overview(node_id)


class HaGroupIn(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    nodes: dict[str, int] = Field(min_length=1)  # node name -> priority
    restricted: bool = False
    nofailback: bool = False
    comment: str = Field(default="", max_length=200)

    @field_validator("nodes")
    @classmethod
    def _nodes(cls, v: dict[str, int]) -> dict[str, int]:
        bad = [n for n in v if not NODE_RE.match(n)]
        if bad:
            raise ValueError(f"invalid node names: {', '.join(bad)}")
        return v


class HaRuleIn(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    type: Literal["node-affinity", "resource-affinity"] = "node-affinity"
    resources: list[str] = Field(min_length=1, max_length=100)
    nodes: dict[str, int] = Field(default_factory=dict)  # node-affinity: node -> priority
    affinity: Literal["positive", "negative"] = "positive"  # resource-affinity
    strict: bool = False
    comment: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _check(self) -> HaRuleIn:
        bad = [s for s in self.resources if not SID_RE.match(s)]
        if bad:
            raise ValueError(f"invalid resources: {', '.join(bad)}")
        if self.type == "node-affinity" and not self.nodes:
            raise ValueError("a node affinity rule needs at least one node")
        if self.type == "resource-affinity" and len(self.resources) < 2:
            raise ValueError("a resource affinity rule needs at least two resources")
        return self


def _prio(nodes_: dict[str, int]) -> str:
    return ",".join(f"{n}:{int(p)}" if p else n for n, p in nodes_.items())


async def create_ha_group(node_id: str, g: HaGroupIn) -> dict[str, Any]:
    args: dict[str, Any] = {"group": g.id, "nodes": _prio(g.nodes), "restricted": g.restricted,
                            "nofailback": g.nofailback}
    if g.comment:
        args["comment"] = g.comment
    await guests.api(node_id, "create", "/cluster/ha/groups", args)
    return await ha_overview(node_id)


async def create_ha_rule(node_id: str, r: HaRuleIn) -> dict[str, Any]:
    args: dict[str, Any] = {"rule": r.id, "type": r.type, "resources": ",".join(r.resources)}
    if r.type == "node-affinity":
        args["nodes"] = _prio(r.nodes)
        args["strict"] = r.strict
    else:
        args["affinity"] = r.affinity
    if r.comment:
        args["comment"] = r.comment
    await guests.api(node_id, "create", "/cluster/ha/rules", args)
    return await ha_overview(node_id)


async def delete_ha_group_or_rule(node_id: str, kind: Literal["group", "rule"], ident: str) -> dict[str, Any]:
    if not ID_RE.match(ident):
        raise NodeError("invalid id")
    await guests.api(node_id, "delete", f"/cluster/ha/{'groups' if kind == 'group' else 'rules'}/{ident}")
    return await ha_overview(node_id)


# ------------------------------------------------------------------ replication
async def replication(node_id: str) -> list[dict[str, Any]]:
    jobs = await _get(node_id, "/cluster/replication", default=[]) or []
    # Status lives on the source node of every job; pvesh forwards to it.
    sources = sorted({j.get("source") for j in jobs if j.get("source")})
    status: dict[str, dict[str, Any]] = {}
    for src in sources:
        for s in await _get(node_id, f"/nodes/{src}/replication", default=[]) or []:
            status[s.get("id")] = s
    out = []
    for j in jobs:
        s = status.get(j.get("id"), {})
        out.append({**j, "last_sync": s.get("last_sync"), "next_sync": s.get("next_sync"),
                    "duration": s.get("duration"), "fail_count": s.get("fail_count"), "error": s.get("error"),
                    "state": s.get("state")})
    return out


class ReplicationIn(BaseModel):
    vmid: int = Field(ge=100, le=999_999_999)
    target: str = Field(pattern=NODE_RE.pattern)
    schedule: str = Field(default="*/15", pattern=r"^[A-Za-z0-9*/:,. -]{1,60}$")
    rate: float | None = Field(default=None, gt=0, le=10000)  # MB/s
    comment: str = Field(default="", max_length=200)
    disable: bool = False


async def create_replication(node_id: str, r: ReplicationIn) -> list[dict[str, Any]]:
    existing = {j.get("id") for j in await _get(node_id, "/cluster/replication", default=[]) or []}
    n = 0
    while f"{r.vmid}-{n}" in existing:
        n += 1
    args: dict[str, Any] = {"id": f"{r.vmid}-{n}", "type": "local", "target": r.target, "schedule": r.schedule}
    if r.rate:
        args["rate"] = r.rate
    if r.comment:
        args["comment"] = r.comment
    if r.disable:
        args["disable"] = True
    await guests.api(node_id, "create", "/cluster/replication", args)
    return await replication(node_id)


async def delete_replication(node_id: str, job_id: str) -> list[dict[str, Any]]:
    if not re.match(r"^\d{3,9}-\d{1,3}$", job_id):
        raise NodeError("replication ids look like 100-0")
    await guests.api(node_id, "delete", f"/cluster/replication/{job_id}")
    return await replication(node_id)


async def run_replication(node_id: str, job_id: str) -> dict[str, Any]:
    jobs = await _get(node_id, "/cluster/replication", default=[]) or []
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if job is None:
        raise NodeError("replication job not found")
    await guests.api(node_id, "create", f"/nodes/{job.get('source')}/replication/{job_id}/schedule_now")
    return {"ok": True, "id": job_id}


# ------------------------------------------------------------------ backup jobs (vzdump)
async def backup_jobs(node_id: str) -> dict[str, Any]:
    jobs, missing = await asyncio.gather(_get(node_id, "/cluster/backup", default=[]),
                                         _get(node_id, "/cluster/backup-info/not-backed-up", default=[]))
    return {"jobs": jobs or [], "not_backed_up": missing or []}


class BackupJobIn(BaseModel):
    schedule: str = Field(default="sun 01:00", pattern=r"^[A-Za-z0-9*/:,. -]{1,60}$")
    storage: str = Field(pattern=guests.STORAGE_RE.pattern)
    all: bool = False
    vmids: list[int] = Field(default_factory=list, max_length=500)
    node: str | None = Field(default=None, pattern=NODE_RE.pattern)
    mode: Literal["snapshot", "suspend", "stop"] = "snapshot"
    compress: Literal["zstd", "lzo", "gzip", "0"] = "zstd"
    enabled: bool = True
    mailto: list[str] = Field(default_factory=list, max_length=10)
    mailnotification: Literal["always", "failure"] = "failure"
    keep_last: int | None = Field(default=None, ge=0, le=1000)
    keep_daily: int | None = Field(default=None, ge=0, le=1000)
    keep_weekly: int | None = Field(default=None, ge=0, le=1000)
    keep_monthly: int | None = Field(default=None, ge=0, le=1000)
    notes_template: str = Field(default="{{guestname}}", max_length=200)
    comment: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _sel(self) -> BackupJobIn:
        if not self.all and not self.vmids:
            raise ValueError("choose all guests or a list of VMIDs")
        return self


def _backup_args(b: BackupJobIn) -> dict[str, Any]:
    args: dict[str, Any] = {"schedule": b.schedule, "storage": b.storage, "mode": b.mode, "compress": b.compress,
                            "enabled": b.enabled, "notes-template": b.notes_template}
    if b.all:
        args["all"] = True
    else:
        args["vmid"] = ",".join(str(v) for v in b.vmids)
    if b.node:
        args["node"] = b.node
    if b.mailto:
        args["mailto"] = ",".join(b.mailto)
        args["mailnotification"] = b.mailnotification
    keep = [f"keep-{k}={v}" for k, v in (("last", b.keep_last), ("daily", b.keep_daily), ("weekly", b.keep_weekly),
                                         ("monthly", b.keep_monthly)) if v]
    if keep:
        args["prune-backups"] = ",".join(keep)
    if b.comment:
        args["comment"] = b.comment
    return args


async def save_backup_job(node_id: str, b: BackupJobIn, job_id: str | None = None) -> dict[str, Any]:
    if job_id:
        if not re.match(r"^[A-Za-z0-9_-]{1,64}$", job_id):
            raise NodeError("invalid backup job id")
        args = _backup_args(b)
        if not b.all:
            args["delete"] = "all"
        await guests.api(node_id, "set", f"/cluster/backup/{job_id}", args)
    else:
        await guests.api(node_id, "create", "/cluster/backup", _backup_args(b))
    return await backup_jobs(node_id)


async def delete_backup_job(node_id: str, job_id: str) -> dict[str, Any]:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", job_id):
        raise NodeError("invalid backup job id")
    await guests.api(node_id, "delete", f"/cluster/backup/{job_id}")
    return await backup_jobs(node_id)


async def run_backup_job(db, node_id: str, job_id: str, *, source: str = "ui",
                         requested_by: str | None = None) -> list[dict[str, Any]]:
    """Run a backup job now: one vzdump task per node of the cluster that holds selected guests."""
    jobs = await _get(node_id, "/cluster/backup", default=[]) or []
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if job is None:
        raise NodeError("backup job not found")
    anchor = await nodes.get_node(db, node_id)
    members = [n for n in await nodes.list_nodes(db, anchor.environment_id)
               if (n.cluster_name == anchor.cluster_name or n.id == anchor.id) and agent_hub.is_online(n.id)]
    vmids = {int(v) for v in str(job.get("vmid") or "").split(",") if v.strip().isdigit()}
    started = []
    for n in members:
        if job.get("node") and job["node"] != (n.pve_name or n.name):
            continue
        local = [g["vmid"] for g in guests.guests_of(n) if not g["template"]]
        sel = local if job.get("all") else [v for v in local if v in vmids]
        if not sel:
            continue
        args: dict[str, Any] = {"storage": job.get("storage"), "mode": job.get("mode") or "snapshot",
                                "compress": job.get("compress") or "zstd"}
        if job.get("all"):
            args["all"] = True
        else:
            args["vmid"] = ",".join(str(v) for v in sel)
        for k in ("mailto", "mailnotification", "notes-template", "prune-backups"):
            if job.get(k):
                v = job[k]
                if isinstance(v, dict):
                    v = ",".join(f"{a}={b}" for a, b in v.items())
                args[k] = v
        j = await guests.task(n.id, "backup.run", "create", "/nodes/localhost/vzdump", args, source=source,
                              requested_by=requested_by, label={"backup_job": job_id})
        started.append(nodes.job_out(j, with_output=False))
    if not started:
        raise NodeError("no online node holds guests of this backup job")
    return started


# ------------------------------------------------------------------ storage
async def storages(node_id: str) -> dict[str, Any]:
    cfg, status = await asyncio.gather(_get(node_id, "/storage", default=[]),
                                       _get(node_id, "/nodes/localhost/storage", default=[]))
    st = {s.get("storage"): s for s in status or []}
    out = []
    for c in cfg or []:
        s = st.get(c.get("storage"), {})
        total, used = int(s.get("total") or 0), int(s.get("used") or 0)
        out.append({"storage": c.get("storage"), "type": c.get("type"), "content": c.get("content") or "",
                    "shared": bool(c.get("shared")), "disabled": bool(c.get("disable")),
                    "nodes": c.get("nodes") or "", "path": c.get("path"), "server": c.get("server"),
                    "export": c.get("export"), "share": c.get("share"), "datastore": c.get("datastore"),
                    "pool": c.get("pool") or c.get("vgname") or c.get("thinpool"),
                    "active": bool(s.get("active")), "total": total, "used": used, "avail": int(s.get("avail") or 0),
                    "used_pct": round(used * 100 / total, 1) if total else None})
    return {"storages": out}


class StorageIn(BaseModel):
    storage: str = Field(pattern=guests.STORAGE_RE.pattern)
    type: Literal["dir", "nfs", "cifs", "pbs"]
    content: list[str] = Field(default_factory=lambda: ["backup"])
    nodes: list[str] = Field(default_factory=list)
    path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_./-]{1,200}$")
    server: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.:\[\]-]{1,200}$")
    export: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_./-]{0,200}$")
    share: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.$ -]{1,100}$")
    username: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=100)
    datastore: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    namespace: str | None = Field(default=None, max_length=200)
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f:]{20,200}$")
    shared: bool = False

    @model_validator(mode="after")
    def _check(self) -> StorageIn:
        bad = set(self.content) - CONTENT
        if bad:
            raise ValueError(f"unknown content types: {', '.join(sorted(bad))}")
        need = {"dir": ["path"], "nfs": ["server", "export"], "cifs": ["server", "share"],
                "pbs": ["server", "datastore", "username", "password"]}[self.type]
        missing = [k for k in need if not getattr(self, k)]
        if missing:
            raise ValueError(f"{self.type} storage needs: {', '.join(missing)}")
        return self


def _storage_args(s: StorageIn) -> dict[str, Any]:
    args: dict[str, Any] = {"storage": s.storage, "type": s.type}
    if s.type != "pbs":
        args["content"] = ",".join(s.content)
    for k in ("path", "server", "export", "share", "username", "password", "domain", "datastore", "namespace",
              "fingerprint"):
        v = getattr(s, k)
        if v:
            args[k] = v
    if s.nodes:
        args["nodes"] = ",".join(s.nodes)
    if s.type == "dir" and s.shared:
        args["shared"] = True
    return args


async def add_storage(node_id: str, s: StorageIn) -> dict[str, Any]:
    await guests.api(node_id, "create", "/storage", _storage_args(s), timeout=120)
    return await storages(node_id)


async def set_storage_enabled(node_id: str, storage: str, enabled: bool) -> dict[str, Any]:
    if not guests.STORAGE_RE.match(storage):
        raise NodeError("invalid storage name")
    await guests.api(node_id, "set", f"/storage/{storage}", {"disable": not enabled})
    return await storages(node_id)


async def remove_storage(node_id: str, storage: str) -> dict[str, Any]:
    if not guests.STORAGE_RE.match(storage):
        raise NodeError("invalid storage name")
    await guests.api(node_id, "delete", f"/storage/{storage}")
    return await storages(node_id)
