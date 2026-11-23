"""Backup management per customer (phase 11): coverage, policy (SLA) and alerts.

For every customer Stackpilot collects which Proxmox VE guests, servers and workstations are
backed up, by which job, when the last good backup was made, how old and how large it is, and
which runs failed. Sources:

* Proxmox VE (per cluster, through one online node): the backup jobs (``/cluster/backup``), the
  backups on every backup storage (``/nodes/<n>/storage/<s>/content``, PBS included, with the
  verification state) and the vzdump tasks of the last two weeks, whose logs say which guest failed.
* Windows servers and workstations: Windows Server Backup (policy, system state, last result) and
  known backup agents (Veeam with its last session, Acronis, Synology Active Backup, ...), read with a
  fixed read-only PowerShell script through the agent's command channel.
* Linux servers: backup tools, timers and cron entries (proxmox-backup-client, restic, borg, ...),
  and whether the machine is a Proxmox Backup Server (then its datastores and jobs, see ``pbs``).
* A machine that is itself a guest counts as covered by the guest's backup.

The raw facts are stored per customer (``BackupCoverage``); the status of every item (ok, stale,
failed, no job, never, none, excluded, ...) is derived when it is read, from the customer's backup
policy and the current time, so a policy change applies at once. The scheduler collects every 30
minutes; monitoring turns failed and missing backups into alerts (app and mail), and the monthly
report and the optimisation scan read the same data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import BackupCoverage, Environment, Node
from mmn.services import agent_hub, guests
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

log = logging.getLogger("mmn.protection")

DEFAULT_POLICY: dict[str, Any] = {"max_age_hours": 26, "alert_failed": True, "alert_missing": True,
                                  "workstations": False, "recipients": [], "exclude": []}
TASK_DAYS = 14
LOCAL_TTL = timedelta(hours=2)  # Windows/Linux checks on the machine itself are repeated at most this often
FRESH_FOR_ALERTS = timedelta(hours=6)  # older coverage is not used for alerts
SHARED_TYPES = {"pbs", "nfs", "cifs", "cephfs", "glusterfs", "rbd", "iscsi"}
KEY_RE = re.compile(r"^(guest:[A-Za-z0-9_.-]{1,100}:\d{3,9}|machine:[0-9a-f]{32})$")
UPID_RE = re.compile(r"^UPID:[A-Za-z0-9._@!:-]{10,300}$")
FAIL_RE = re.compile(r"ERROR: Backup of VM (\d+) failed - (.+)")
# Status order: problems first.
STATUSES = ("failed", "never", "no_job", "stale", "none", "unknown", "external", "backup_server", "ok",
            "excluded", "not_monitored")
PROBLEMS = {"failed", "never", "no_job", "stale", "none"}
MISSING = {"never", "no_job", "stale", "none"}

_locks: dict[str, asyncio.Lock] = {}


# ------------------------------------------------------------------ policy
class PolicyIn(BaseModel):
    max_age_hours: int = Field(default=26, ge=1, le=24 * 90)
    alert_failed: bool = True
    alert_missing: bool = True
    workstations: bool = False  # alert on workstations too (most have no backup by design)
    recipients: list[str] = Field(default_factory=list, max_length=10)
    exclude: list[str] = Field(default_factory=list, max_length=2000)

    @field_validator("recipients")
    @classmethod
    def _mails(cls, v: list[str]) -> list[str]:
        out = [x.strip() for x in v if x.strip()]
        bad = [x for x in out if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", x)]
        if bad:
            raise ValueError(f"invalid e-mail addresses: {', '.join(bad)}")
        return out

    @field_validator("exclude")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        bad = [k for k in v if not KEY_RE.match(k)]
        if bad:
            raise ValueError(f"invalid item keys: {', '.join(bad[:5])}")
        return sorted(set(v))


def policy_of(e: Environment) -> dict[str, Any]:
    p = {**DEFAULT_POLICY, **(e.backup or {})}
    p["exclude"] = list(p.get("exclude") or [])
    p["recipients"] = list(p.get("recipients") or [])
    return p


async def save_policy(db: AsyncSession, e: Environment, body: PolicyIn) -> dict[str, Any]:
    e.backup = body.model_dump()
    await db.flush()
    return policy_of(e)


async def set_excluded(db: AsyncSession, e: Environment, key: str, excluded: bool) -> dict[str, Any]:
    if not KEY_RE.match(key):
        raise NodeError("invalid item key")
    p = policy_of(e)
    keys = set(p["exclude"])
    (keys.add if excluded else keys.discard)(key)
    p["exclude"] = sorted(keys)
    e.backup = p
    await db.flush()
    return p


# ------------------------------------------------------------------ small helpers
def _iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), UTC).isoformat() if ts else None
    except (TypeError, ValueError, OSError):
        return None


def _parse(iso: Any) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ints(v: Any) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", str(v or "")) if x.strip().isdigit()}


def _enabled(job: dict[str, Any]) -> bool:
    return str(job.get("enabled", 1)).lower() not in ("0", "false", "")


def job_covers(job: dict[str, Any], vmid: int, pve_node: str | None, pools: dict[str, set[int]]) -> bool:
    """Whether an enabled Proxmox VE backup job selects this guest."""
    if not _enabled(job):
        return False
    if job.get("node") and pve_node and job["node"] != pve_node:
        return False
    if vmid in _ints(job.get("exclude")):
        return False
    if job.get("all") and str(job.get("all")) not in ("0", "false"):
        return True
    if job.get("pool"):
        return vmid in pools.get(str(job["pool"]), set())
    return vmid in _ints(job.get("vmid"))


def _cluster_key(members: list[Node]) -> str:
    raw = members[0].cluster_name or members[0].pve_name or members[0].name
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)[:100] or "pve"


def volume_out(v: dict[str, Any], storage: str, stype: str | None, node: str | None) -> dict[str, Any]:
    ver = v.get("verification") or {}
    subtype = v.get("subtype") or ("lxc" if "/ct/" in str(v.get("volid")) or "-lxc-" in str(v.get("volid"))
                                    else "qemu")
    return {"volid": v.get("volid"), "vmid": int(v.get("vmid") or 0), "ctime": _iso(v.get("ctime")),
            "size": int(v.get("size") or 0), "format": v.get("format"), "type": subtype, "storage": storage,
            "storage_type": stype, "node": node, "notes": (v.get("notes") or "")[:200],
            "protected": bool(v.get("protected")), "encrypted": bool(v.get("encrypted")),
            "verified": ver.get("state")}


# ------------------------------------------------------------------ Proxmox VE clusters
async def _get(node_id: str, path: str, args: dict[str, Any] | None = None, timeout: int = 60) -> Any:
    return await guests.api(node_id, "get", path, args, timeout=timeout)


async def backup_storages(anchor_id: str) -> list[dict[str, Any]]:
    cfg = await _get(anchor_id, "/storage") or []
    return [s for s in cfg if not s.get("disable")
            and ("backup" in str(s.get("content") or "").split(",") or s.get("type") == "pbs")]


async def list_volumes(anchor: Node, members: list[Node], *, vmid: int | None = None,
                       errors: list[str] | None = None) -> list[dict[str, Any]]:
    """Every backup on the backup storages of a cluster (optionally of one guest), newest first."""
    stores = await backup_storages(anchor.id)
    out: dict[str, dict[str, Any]] = {}
    for s in stores:
        allowed = {x for x in str(s.get("nodes") or "").split(",") if x}
        shared = bool(s.get("shared")) or s.get("type") in SHARED_TYPES
        targets = [m for m in members if agent_hub.is_online(m.id) or m.id == anchor.id]
        targets = [m for m in targets if not allowed or (m.pve_name or m.name) in allowed]
        if shared:
            targets = targets[:1]
        for m in targets:
            pve = m.pve_name or m.name
            args: dict[str, Any] = {"content": "backup"}
            if vmid:
                args["vmid"] = vmid
            try:
                rows = await _get(anchor.id, f"/nodes/{pve}/storage/{s['storage']}/content", args, timeout=90)
            except AgentError as e:
                if errors is not None:
                    errors.append(f"{s['storage']}@{pve}: {str(e)[:200]}")
                continue
            for v in rows or []:
                if not v.get("volid") or (vmid and int(v.get("vmid") or 0) != vmid):
                    continue
                out[v["volid"]] = volume_out(v, s["storage"], s.get("type"), None if shared else pve)
    return sorted(out.values(), key=lambda v: v["ctime"] or "", reverse=True)


async def _failures(anchor: Node, members: list[Node], since: datetime) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Failed backups per VMID (latest first) and run totals, from the vzdump tasks of the period."""
    fails: dict[int, dict[str, Any]] = {}
    runs = {"ok": 0, "failed": 0, "running": 0, "last_ok": None, "job_errors": []}
    failed_tasks: list[tuple[str, dict[str, Any]]] = []
    for m in members:
        if not agent_hub.is_online(m.id) and m.id != anchor.id:
            continue
        pve = m.pve_name or m.name
        try:
            tasks = await _get(anchor.id, f"/nodes/{pve}/tasks", {"typefilter": "vzdump", "since": int(since.timestamp()),
                                                                 "limit": 500}) or []
        except AgentError:
            continue
        for t in tasks:
            if not t.get("endtime"):
                runs["running"] += 1
                continue
            st = str(t.get("status") or "")
            if st == "OK" or st.startswith("WARNINGS"):
                runs["ok"] += 1
                end = _iso(t["endtime"])
                if end and (runs["last_ok"] is None or end > runs["last_ok"]):
                    runs["last_ok"] = end
            else:
                runs["failed"] += 1
                failed_tasks.append((pve, t))
    failed_tasks.sort(key=lambda x: int(x[1].get("endtime") or 0), reverse=True)
    for pve, t in failed_tasks[:15]:
        at = _iso(t.get("endtime"))
        status = str(t.get("status") or "")[:300]
        tid = str(t.get("id") or "")
        if tid.isdigit():
            fails.setdefault(int(tid), {"at": at, "error": status, "node": pve})
            continue
        upid = str(t.get("upid") or "")
        found = False
        if UPID_RE.match(upid):
            try:
                lines = await _get(anchor.id, f"/nodes/{pve}/tasks/{upid}/log", {"limit": 5000}) or []
            except AgentError:
                lines = []
            for ln in lines:
                mt = FAIL_RE.search(str(ln.get("t") if isinstance(ln, dict) else ln))
                if mt:
                    found = True
                    fails.setdefault(int(mt.group(1)), {"at": at, "error": mt.group(2)[:300], "node": pve})
        if not found:
            runs["job_errors"].append({"at": at, "node": pve, "error": status})
    runs["job_errors"] = runs["job_errors"][:10]
    return fails, runs


async def _pools(anchor_id: str, jobs: list[dict[str, Any]]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for p in {str(j["pool"]) for j in jobs if j.get("pool")}:
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", p):
            continue
        try:
            d = await _get(anchor_id, f"/pools/{p}") or {}
        except AgentError:
            d = {}
        out[p] = {int(x.get("vmid")) for x in d.get("members") or [] if str(x.get("vmid") or "").isdigit()}
    return out


def _job_out(j: dict[str, Any]) -> dict[str, Any]:
    return {"id": j.get("id"), "schedule": j.get("schedule"), "storage": j.get("storage"), "enabled": _enabled(j),
            "all": bool(j.get("all")), "vmid": j.get("vmid"), "pool": j.get("pool"), "node": j.get("node"),
            "exclude": j.get("exclude"), "mode": j.get("mode") or "snapshot", "next_run": _iso(j.get("next-run")),
            "prune": j.get("prune-backups"), "comment": j.get("comment")}


async def _collect_cluster(members: list[Node], now: datetime) -> dict[str, Any]:
    anchor = next((n for n in members if agent_hub.is_online(n.id)), None)
    key = _cluster_key(members)
    label = members[0].cluster_name or members[0].name
    base: dict[str, Any] = {"key": key, "name": label, "nodes": [m.name for m in members], "guests": []}
    if anchor is None:
        base["error"] = "offline"
        return base
    errors: list[str] = []
    jobs = await _get(anchor.id, "/cluster/backup") or []
    pools = await _pools(anchor.id, jobs)
    vols = await list_volumes(anchor, members, errors=errors)
    fails, runs = await _failures(anchor, members, now - timedelta(days=TASK_DAYS))
    by_vmid: dict[int, list[dict[str, Any]]] = {}
    for v in vols:
        by_vmid.setdefault(v["vmid"], []).append(v)
    seen: dict[tuple[str, int], tuple[Node, dict[str, Any]]] = {}
    for m in members:
        for g in guests.guests_of(m):
            if not g["template"]:
                seen[(g["type"], g["vmid"])] = (m, g)
    stores = {s["storage"]: s for s in await backup_storages(anchor.id)}
    rows = []
    for (_t, vmid), (m, g) in sorted(seen.items(), key=lambda x: x[0][1]):
        pve = g.get("pve_node") or m.pve_name or m.name
        node_obj = next((x for x in members if (x.pve_name or x.name) == pve), m)
        cov = [j for j in jobs if job_covers(j, vmid, pve, pools)]
        mine = by_vmid.get(vmid, [])
        last = mine[0] if mine else None
        nxt = sorted(x for x in (_iso(j.get("next-run")) for j in cov) if x)
        rows.append({
            "key": f"guest:{key}:{vmid}", "kind": "guest", "type": g["type"], "vmid": vmid, "name": g["name"],
            "node": pve, "node_id": node_obj.id, "cluster": label, "power": g["status"],
            "jobs": [j.get("id") for j in cov], "next_run": nxt[0] if nxt else None,
            "last_backup": last["ctime"] if last else None, "size": last["size"] if last else None,
            "total_size": sum(v["size"] for v in mine), "count": len(mine),
            "storages": sorted({v["storage"] for v in mine}), "last_volid": last["volid"] if last else None,
            "verified": last["verified"] if last else None,
            "offsite": any((stores.get(v["storage"]) or {}).get("type") in SHARED_TYPES for v in mine),
            "failure": fails.get(vmid),
        })
    base.update({
        "anchor_id": anchor.id, "anchor": anchor.name, "jobs": [_job_out(j) for j in jobs],
        "storages": [{"storage": s["storage"], "type": s.get("type"), "shared": bool(s.get("shared")) or s.get("type") in SHARED_TYPES,
                      "server": s.get("server"), "datastore": s.get("datastore"),
                      "backups": sum(1 for v in vols if v["storage"] == s["storage"]),
                      "size": sum(v["size"] for v in vols if v["storage"] == s["storage"]),
                      **_usage(anchor, s["storage"])} for s in stores.values()],
        "runs": runs, "guests": rows, "errors": errors[:20],
    })
    return base


def _usage(anchor: Node, storage: str) -> dict[str, Any]:
    for s in (anchor.health or {}).get("storages") or []:
        if s.get("storage") == storage:
            return {"used": s.get("used"), "total": s.get("total"), "used_pct": s.get("used_pct")}
    return {}


# ------------------------------------------------------------------ servers and workstations
# Windows PowerShell 5.1, read-only: Windows Server Backup (policy, system state, last result) and
# services of known backup agents, plus the last Veeam Agent session.
WINDOWS_SCRIPT = r"""$ErrorActionPreference='Stop'
$r=[ordered]@{v=1;errors=@()}
function D($d){if($d -and ([datetime]$d).Year -gt 1901){([datetime]$d).ToUniversalTime().ToString('o')}}
try{if(Get-Command Get-WBSummary -ErrorAction SilentlyContinue){$s=Get-WBSummary;$p=Get-WBPolicy -ErrorAction SilentlyContinue
$w=[ordered]@{installed=$true;last_success=(D $s.LastSuccessfulBackupTime);last_backup=(D $s.LastBackupTime);last_result=[int64]$s.LastBackupResultHR;next=(D $s.NextBackupTime);versions=[int]$s.NumberOfVersions;target=[string]$s.LastBackupTarget;policy=($null -ne $p)}
if($p){$w.schedule=@(Get-WBSchedule -Policy $p|%{$_.ToString('HH:mm')});$w.system_state=[bool](Get-WBSystemState -Policy $p);$w.bare_metal=[bool](Get-WBBareMetalRecovery -Policy $p);$w.volumes=@(Get-WBVolume -Policy $p|%{[string]$_.MountPath})}
$r.wsb=$w}else{$r.wsb=[ordered]@{installed=$false}}}catch{$r.errors+=("wsb: "+$_.Exception.Message)}
try{$r.agents=@(Get-Service -ErrorAction SilentlyContinue|?{$_.Name -match '^(VeeamEndpointBackupSvc|VeeamAgent.*|VeeamBackupSvc|AcronisCyberProtectService|MMS|ABService|ActiveBackupService|SynologyActiveBackup.*|Backup Service Controller|CrashPlanService|Code42Service|DattoBackupAgent|CloudBerryBackupService.*|UrBackupClientBackend|Arcserve.*|NakivoBackup.*|CohesityAgent|CommVault.*)$'}|%{[pscustomobject]@{name=$_.Name;display=$_.DisplayName;status=[string]$_.Status}})}catch{$r.errors+=("agents: "+$_.Exception.Message)}
try{$e=Get-WinEvent -FilterHashtable @{LogName='Veeam Agent';Id=190} -MaxEvents 1 -ErrorAction SilentlyContinue
if($e){$m=[string]$e.Message;$r.veeam=[ordered]@{at=(D $e.TimeCreated);level=[int]$e.Level;message=$m.Substring(0,[Math]::Min(300,$m.Length))}}}catch{}
$r|ConvertTo-Json -Depth 5 -Compress"""

LINUX_SCRIPT = r"""export LC_ALL=C
s(){ echo "@@@$1"; }
s tools; for c in proxmox-backup-client proxmox-backup-manager restic borg borgmatic rsnapshot duplicity duplicati-cli urbackupclientctl veeamconfig bacula-fd bareos-fd; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done
s timers; systemctl list-timers --all --no-legend 2>/dev/null | grep -Ei 'backup|restic|borg|rsnapshot|duplicity|pbs' | head -20
s cron; grep -hsEi 'backup|restic|borg|rsnapshot|duplicity|proxmox-backup-client' /etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/* 2>/dev/null | grep -v '^[[:space:]]*#' | head -20
s end
"""


def _sections(stdout: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in (stdout or "").splitlines():
        if line.startswith("@@@"):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None and line.strip():
            out[cur].append(line.rstrip())
    return out


def parse_linux(stdout: str) -> dict[str, Any]:
    sec = _sections(stdout)
    if "tools" not in sec:
        raise NodeError("the backup check returned no data: " + (stdout or "")[:200])
    tools = [t.strip() for t in sec.get("tools", []) if t.strip()]
    return {"tools": tools, "timers": sec.get("timers", [])[:20], "cron": sec.get("cron", [])[:20],
            "pbs_server": "proxmox-backup-manager" in tools}


def parse_windows(stdout: str) -> dict[str, Any]:
    raw = (stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        raise NodeError("the backup check returned no data: " + raw[:200])
    try:
        d = json.loads(raw[start:])
    except ValueError as e:
        raise NodeError(f"the backup check returned invalid data: {e}") from e
    agents = d.get("agents") or []
    if isinstance(agents, dict):
        agents = [agents]
    w = d.get("wsb") or {}
    for k in ("schedule", "volumes"):
        if isinstance(w.get(k), str):
            w[k] = [w[k]]
    return {"wsb": w, "agents": agents, "veeam": d.get("veeam"), "errors": d.get("errors") or []}


async def check_machine(n: Node) -> dict[str, Any]:
    """Run the backup check on a Linux or Windows machine (read-only)."""
    if n.kind not in ("linux", "windows"):
        raise NodeError("only for Linux and Windows machines")
    if not agent_hub.is_online(n.id):
        raise NodeError("the machine is offline")
    script = WINDOWS_SCRIPT if n.kind == "windows" else LINUX_SCRIPT
    res = await agent_hub.request(n.id, "exec", {"command": script, "timeout": 120}, timeout=150) or {}
    if res.get("timed_out"):
        raise NodeError("the backup check timed out")
    out = parse_windows(res.get("stdout") or "") if n.kind == "windows" else parse_linux(res.get("stdout") or "")
    out["checked_at"] = datetime.now(UTC).isoformat()
    return out


def _short(name: str | None) -> str:
    return (name or "").split(".")[0].strip().lower()


async def _collect_machines(ns: list[Node], prev: dict[str, Any], guest_rows: list[dict[str, Any]], *,
                            force: bool) -> tuple[list[dict[str, Any]], list[str]]:
    by_name: dict[str, dict[str, Any]] = {}
    for g in guest_rows:
        if g["name"]:
            by_name.setdefault(g["name"].lower(), g)
    old = {m["key"]: m for m in prev.get("machines") or []}
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    pbs_nodes: list[str] = []

    async def one(n: Node) -> None:
        key = f"machine:{n.id}"
        local = (old.get(key) or {}).get("local")
        error = None
        checked = _parse((local or {}).get("checked_at"))
        if agent_hub.is_online(n.id) and (force or not checked or now - checked > LOCAL_TTL):
            try:
                local = await check_machine(n)
            except (AgentError, NodeError) as e:
                error = str(e)[:300]
        guest = by_name.get(_short(n.name)) or by_name.get(_short(n.hostname))
        if local and local.get("pbs_server"):
            pbs_nodes.append(n.id)
        rows.append({"key": key, "kind": "machine", "machine_id": n.id, "name": n.name, "os": n.kind,
                     "role": n.role or "server", "online": agent_hub.is_online(n.id), "local": local,
                     "error": error, "guest": {"key": guest["key"], "vmid": guest["vmid"], "cluster": guest["cluster"]}
                     if guest else None})

    sem = asyncio.Semaphore(6)

    async def guarded(n: Node) -> None:
        async with sem:
            await one(n)

    await asyncio.gather(*(guarded(n) for n in ns if n.kind in ("linux", "windows")))
    rows.sort(key=lambda r: (r["role"] != "server", r["name"].lower()))
    return rows, pbs_nodes


# ------------------------------------------------------------------ collecting
async def collect(environment_id: str, *, force: bool = False) -> dict[str, Any]:
    """Collect the backup coverage of one customer and store it. Returns the classified view."""
    from mmn.services import pbs

    lock = _locks.setdefault(environment_id, asyncio.Lock())
    async with lock, sessionmaker()() as db:
        e = await db.get(Environment, environment_id)
        if e is None:
            raise NodeError("customer not found")
        prev_row = await db.get(BackupCoverage, environment_id)
        prev = prev_row.data if prev_row else {}
        ns = list(e.nodes)
        now = datetime.now(UTC)
        groups: dict[str, list[Node]] = {}
        for n in ns:
            if n.kind == "proxmox":
                groups.setdefault(n.cluster_name or n.id, []).append(n)
        clusters = []
        for members in groups.values():
            try:
                clusters.append(await _collect_cluster(members, now))
            except AgentError as ex:
                clusters.append({"key": _cluster_key(members), "name": members[0].cluster_name or members[0].name,
                                 "nodes": [m.name for m in members], "guests": [], "error": str(ex)[:300]})
        all_guests = [g for c in clusters for g in c.get("guests") or []]
        machines, pbs_ids = await _collect_machines(ns, prev, all_guests, force=force)
        pbs_list = []
        for nid in pbs_ids:
            n = next(x for x in ns if x.id == nid)
            try:
                pbs_list.append({"node_id": n.id, "name": n.name, **await pbs.overview(n.id)})
            except (AgentError, NodeError) as ex:
                pbs_list.append({"node_id": n.id, "name": n.name, "error": str(ex)[:300]})
        data = {"version": 1, "collected_at": now.isoformat(), "clusters": clusters, "machines": machines,
                "pbs": pbs_list}
        errors = [f"{c['name']}: {c['error']}" for c in clusters if c.get("error")]
        if prev_row is None:
            prev_row = BackupCoverage(environment_id=e.id)
            db.add(prev_row)
        prev_row.data = data
        prev_row.collected_at = now
        prev_row.error = "; ".join(errors)[:2000] or None
        await db.commit()
        return view(e, prev_row)


async def collect_all() -> int:
    """Scheduler: collect every customer that has Proxmox VE nodes or servers."""
    async with sessionmaker()() as db:
        envs = [e.id for e in (await db.execute(select(Environment))).scalars() if e.nodes]
    for eid in envs:
        try:
            await collect(eid)
        except Exception:  # noqa: BLE001
            log.exception("backup coverage of %s failed", eid)
    return len(envs)


async def stored(db: AsyncSession, e: Environment) -> BackupCoverage | None:
    return await db.get(BackupCoverage, e.id)


# ------------------------------------------------------------------ classification
def _age_h(iso: str | None, now: datetime) -> float | None:
    dt = _parse(iso)
    return round((now - dt).total_seconds() / 3600, 1) if dt else None


def classify_guest(g: dict[str, Any], policy: dict[str, Any], now: datetime) -> dict[str, Any]:
    r = dict(g)
    r["age_hours"] = _age_h(g.get("last_backup"), now)
    fail = g.get("failure") or {}
    if g["key"] in policy["exclude"]:
        st = "excluded"
    elif fail and (not g.get("last_backup") or (fail.get("at") or "") > g["last_backup"]):
        st = "failed"
    elif not g.get("jobs"):
        st = "no_job"
    elif not g.get("last_backup"):
        st = "never"
    elif r["age_hours"] is not None and r["age_hours"] > policy["max_age_hours"]:
        st = "stale"
    else:
        st = "ok"
    r["status"] = st
    return r


def classify_machine(m: dict[str, Any], policy: dict[str, Any], now: datetime,
                     guests_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    r = dict(m)
    local = m.get("local") or {}
    max_h = policy["max_age_hours"]
    method, st, last, detail = None, None, None, None
    wsb = local.get("wsb") or {}
    veeam = local.get("veeam") or {}
    if m["key"] in policy["exclude"]:
        st = "excluded"
    elif local.get("pbs_server"):
        method, st = "pbs", "backup_server"
    elif wsb.get("policy"):
        method, last = "wsb", wsb.get("last_success")
        age = _age_h(last, now)
        failed_after = wsb.get("last_result") not in (0, None) and (wsb.get("last_backup") or "") > (last or "")
        st = "failed" if failed_after else "never" if not last else "stale" if age is not None and age > max_h else "ok"
        if failed_after:
            detail = f"0x{int(wsb['last_result']) & 0xFFFFFFFF:08X}"
    elif veeam:
        method, last = "veeam", veeam.get("at")
        age = _age_h(last, now)
        st = "failed" if int(veeam.get("level") or 4) <= 2 else "stale" if age is not None and age > max_h else "ok"
        detail = veeam.get("message")
    if st is None and m.get("guest") and guests_by_key.get(m["guest"]["key"]):
        g = guests_by_key[m["guest"]["key"]]
        method, last = "guest", g.get("last_backup")
        st = g["status"] if g["status"] != "excluded" else "ok"
    if st is None and (local.get("agents") or local.get("tools") or local.get("timers") or local.get("cron")):
        method, st = "external", "external"
        names = [a.get("display") or a.get("name") for a in local.get("agents") or []] + list(local.get("tools") or [])
        detail = ", ".join(x for x in names if x)[:200] or None
    if st is None:
        st = "unknown" if not local else "none"
    if m.get("role") == "workstation" and not policy.get("workstations") and st in PROBLEMS:
        st = "not_monitored"
    r.update({"status": st, "method": method, "last_backup": last, "age_hours": _age_h(last, now), "detail": detail,
              "system_state": wsb.get("system_state") if wsb.get("policy") else None})
    return r


def _summary(guests_: list[dict[str, Any]], machines: list[dict[str, Any]]) -> dict[str, Any]:
    def count(rows: list[dict[str, Any]]) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for r in rows:
            out[r["status"]] = out.get(r["status"], 0) + 1
        out["total"] = len(rows)
        return out

    g, m = count(guests_), count(machines)
    considered = [r for r in guests_ + machines if r["status"] not in ("excluded", "not_monitored")]
    good = sum(1 for r in considered if r["status"] in ("ok", "external", "backup_server"))
    lasts = [r["last_backup"] for r in guests_ + machines if r.get("last_backup")]
    return {"guests": g, "machines": m, "protected": good, "considered": len(considered),
            "protected_pct": round(good * 100 / len(considered)) if considered else None,
            "problems": sum(1 for r in considered if r["status"] in PROBLEMS),
            "failed": sum(1 for r in considered if r["status"] == "failed"),
            "missing": sum(1 for r in considered if r["status"] in MISSING),
            "last_backup": max(lasts) if lasts else None,
            "size": sum(r.get("total_size") or 0 for r in guests_)}


def view(e: Environment, row: BackupCoverage | None, *, now: datetime | None = None) -> dict[str, Any]:
    """The coverage of a customer with every item classified under the current policy."""
    now = now or datetime.now(UTC)
    policy = policy_of(e)
    data = (row.data if row else None) or {}
    order = {s: i for i, s in enumerate(STATUSES)}
    clusters = []
    all_guests: list[dict[str, Any]] = []
    for c in data.get("clusters") or []:
        gs = [classify_guest(g, policy, now) for g in c.get("guests") or []]
        all_guests += gs
        clusters.append({**{k: v for k, v in c.items() if k != "guests"}, "guest_count": len(gs)})
    by_key = {g["key"]: g for g in all_guests}
    machines = [classify_machine(m, policy, now, by_key) for m in data.get("machines") or []]
    all_guests.sort(key=lambda r: (order.get(r["status"], 99), r["cluster"], r["vmid"]))
    machines.sort(key=lambda r: (order.get(r["status"], 99), r["role"] != "server", r["name"].lower()))
    return {"customer": {"id": e.id, "name": e.name, "sla": e.sla}, "policy": policy,
            "collected_at": row.collected_at.isoformat() if row and row.collected_at else None,
            "error": row.error if row else None, "summary": _summary(all_guests, machines),
            "clusters": clusters, "guests": all_guests, "machines": machines, "pbs": data.get("pbs") or []}


def problems(v: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in v["guests"] + v["machines"] if r["status"] in PROBLEMS]


def label_of(r: dict[str, Any]) -> str:
    if r["kind"] == "guest":
        return f"{'CT' if r.get('type') == 'lxc' else 'VM'} {r['vmid']} {r.get('name') or ''}".strip()
    return r["name"]


async def overview_all(db: AsyncSession) -> list[dict[str, Any]]:
    """One line per customer: summary of the stored coverage."""
    envs = list((await db.execute(select(Environment).order_by(Environment.name))).scalars())
    rows = {r.environment_id: r for r in (await db.execute(select(BackupCoverage))).scalars()}
    out = []
    for e in envs:
        if not e.nodes:
            continue
        v = view(e, rows.get(e.id))
        out.append({"customer": v["customer"], "collected_at": v["collected_at"], "error": v["error"],
                    "summary": v["summary"], "policy": {"max_age_hours": v["policy"]["max_age_hours"]},
                    "pbs": len(v["pbs"]), "jobs": sum(len(c.get("jobs") or []) for c in v["clusters"])})
    return out


# ------------------------------------------------------------------ alerts (read by monitoring)
async def alert_conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """One alert per customer for failed backups (critical) and one for missing or too old backups
    (warning), following each customer's policy. Only recent coverage counts."""
    want: dict[str, dict[str, Any]] = {}
    rows = {r.environment_id: r for r in (await db.execute(select(BackupCoverage))).scalars()}
    for e in (await db.execute(select(Environment))).scalars():
        row = rows.get(e.id)
        if row is None or not row.collected_at or now - row.collected_at > FRESH_FOR_ALERTS:
            continue
        v = view(e, row, now=now)
        p = v["policy"]
        anchor = next((n.id for n in e.nodes if n.kind == "proxmox"), None) or (e.nodes[0].id if e.nodes else None)
        src = {"source": f"{e.name} backups", "customer_id": e.id, "customer": e.name}

        def node_of(items: list[dict[str, Any]], anchor: str | None = anchor) -> str | None:
            first = items[0]
            return first.get("node_id") or first.get("machine_id") or anchor

        failed = [r for r in problems(v) if r["status"] == "failed"]
        missing = [r for r in problems(v) if r["status"] in MISSING]
        if p["alert_failed"] and failed:
            names = ", ".join(label_of(r) for r in failed[:8]) + (" …" if len(failed) > 8 else "")
            want[f"backup_failed:{e.id}"] = {
                "node_id": node_of(failed), "kind": "backup", "severity": "critical", "value": len(failed),
                "detail": {**src, "items": [r["key"] for r in failed[:50]]},
                "message": f"Backup failed at {e.name} for {len(failed)} item(s): {names}"}
        if p["alert_missing"] and missing:
            names = ", ".join(label_of(r) for r in missing[:8]) + (" …" if len(missing) > 8 else "")
            want[f"backup_missing:{e.id}"] = {
                "node_id": node_of(missing), "kind": "backup", "severity": "warning", "value": len(missing),
                "detail": {**src, "items": [r["key"] for r in missing[:50]]},
                "message": f"No backup within {p['max_age_hours']} h at {e.name} for {len(missing)} item(s): {names}"}
    return want


async def extra_recipients(db: AsyncSession, customer_ids: set[str]) -> dict[str, list[str]]:
    out = {}
    for cid in customer_ids:
        e = await db.get(Environment, cid)
        if e is not None and policy_of(e)["recipients"]:
            out[cid] = policy_of(e)["recipients"]
    return out


# ------------------------------------------------------------------ optimisation scan findings
def scan_findings(v: dict[str, Any] | None, finding) -> list[dict[str, Any]]:
    """Findings for customers.optimisation_scan from the stored coverage (``finding`` = customers._finding)."""
    if v is None or not v.get("collected_at"):
        return [finding("info", "backups", "backups", "Backup coverage has not been collected yet.",
                        "Open Backups in Stackpilot or call list_backup_coverage with refresh=true.",
                        "list_backup_coverage", code="backup_unknown")]
    out: list[dict[str, Any]] = []
    max_h = v["policy"]["max_age_hours"]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for g in v["guests"]:
        if g["status"] in PROBLEMS:
            groups.setdefault((g["cluster"], g["status"]), []).append(g)
    texts = {
        "failed": ("high", "The last backup failed for {count} guest(s): {names}.",
                   "Read the error in the backup log, fix it and run the job again.", "run_backup_job"),
        "no_job": ("high", "{count} guest(s) are not in any backup job: {names}.",
                   "Add them to a backup job, or exclude them on the Backups page if they need no backup.",
                   "save_backup_job"),
        "never": ("high", "{count} guest(s) are in a backup job but have no backup yet: {names}.",
                  "Run the backup job now and check that the storage is reachable.", "run_backup_job"),
        "stale": ("medium", "The last backup of {count} guest(s) is older than {max} hours: {names}.",
                  "Check the job schedule and the last backup runs.", "list_backup_coverage"),
    }
    for (cl, st), rows in groups.items():
        sev, f, s, tool = texts[st]
        names = ", ".join(label_of(r) for r in rows[:10]) + (" …" if len(rows) > 10 else "")
        out.append(finding(sev, "backups", f"cluster {cl}", f.format(count=len(rows), names=names, max=max_h), s, tool,
                           code=f"backup_{st}", count=len(rows), names=names, max=max_h))
    for c in v["clusters"]:
        stores = c.get("storages") or []
        if stores and c.get("guest_count") and not any(s.get("shared") for s in stores):
            names = ", ".join(s["storage"] for s in stores)
            out.append(finding("medium", "backups", f"cluster {c['name']}",
                               f"All backups are on local storage of the node itself ({names}).",
                               "Store backups on a separate machine (Proxmox Backup Server or NFS) and keep an "
                               "offsite copy.", "add_storage", code="backup_local_only", storages=names))
    for m in v["machines"]:
        if m["role"] != "server":
            continue
        if m["status"] == "failed":
            out.append(finding("high", "backups", m["name"], f"The last backup failed ({m.get('detail') or m.get('method')}).",
                               "Check the backup on the machine and run it again.", "diagnose_server",
                               code="machine_backup_failed", detail=m.get("detail") or m.get("method") or "-"))
        elif m["status"] in ("none", "never"):
            out.append(finding("medium", "backups", m["name"],
                               "No backup found: no Windows Server Backup, no backup agent and not a backed-up guest.",
                               "Set up a backup (for Windows: Windows Server Backup with system state), or back the "
                               "machine up as a Proxmox VE guest.", "list_backup_coverage", code="machine_backup_none"))
        elif m["status"] == "stale":
            out.append(finding("medium", "backups", m["name"], f"The last backup is older than {max_h} hours.",
                               "Check the backup schedule on the machine.", "list_backup_coverage",
                               code="machine_backup_stale", max=max_h))
        if m.get("method") == "wsb" and m.get("system_state") is False:
            out.append(finding("low", "backups", m["name"], "Windows Server Backup does not include the system state.",
                               "Add the system state to the backup (needed to restore a domain controller or its "
                               "roles).", code="wsb_no_system_state"))
    for p in v["pbs"]:
        if p.get("error"):
            continue
        verify = {j.get("store") for j in p.get("verify_jobs") or []}
        synced = {j.get("store") for j in p.get("sync_jobs") or []}
        for d in p.get("datastores") or []:
            tgt = f"{p['name']}:{d['name']}"
            if d["name"] not in verify:
                out.append(finding("medium", "backups", tgt, f"Datastore {d['name']} has no verify job.",
                                   "Add a verify job (for example weekly) so damaged backups are found in time.",
                                   "save_pbs_job", code="pbs_no_verify", store=d["name"]))
            if d["name"] not in synced:
                out.append(finding("medium", "backups", tgt, f"Datastore {d['name']} has no offsite copy.",
                                   "Add a remote and a sync job to a second Proxmox Backup Server (3-2-1 rule).",
                                   "save_pbs_job", code="pbs_no_sync", store=d["name"]))
            pct = d.get("used_pct")
            if pct is not None and pct >= 80:
                out.append(finding("high" if pct >= 92 else "medium", "backups", tgt,
                                   f"Datastore {d['name']} is {pct:.0f}% full.",
                                   "Tighten the retention (prune job) or extend the datastore.", "save_pbs_job",
                                   code="pbs_full", store=d["name"], pct=round(pct)))
    return out
