"""Proxmox Backup Server management (phase 11).

A Proxmox Backup Server with the Stackpilot agent is a Linux server on which
``proxmox-backup-manager`` exists. Stackpilot reads its datastores (with usage and garbage
collection), verify, sync (offsite) and prune jobs, remotes and certificate fingerprint, and
creates, changes, runs and removes jobs, datastores and remotes with fixed commands through the
agent's command channel (``proxmox-backup-manager`` and ``proxmox-backup-debug api``). Every
argument is validated and quoted; the app decides what is allowed, the agent only executes.

Connecting a Proxmox VE cluster to a PBS datastore is a Proxmox VE storage of type ``pbs``
(``cluster.add_storage``); the fingerprint for it is in ``overview``.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from mmn.services import agent_hub
from mmn.services.nodes import NodeError

ID_RE = r"^[A-Za-z0-9_][A-Za-z0-9._-]{1,31}$"
SCHEDULE_RE = r"^[A-Za-z0-9*/:,. -]{1,60}$"
NS_RE = r"^[A-Za-z0-9_/-]{0,200}$"
KINDS = ("verify", "sync", "prune")
KEEP = ("keep_last", "keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")

READ_SCRIPT = r"""export LC_ALL=C
s(){ echo "@@@$1"; }
if ! command -v proxmox-backup-manager >/dev/null 2>&1; then echo "@@@nopbs"; exit 0; fi
api(){ proxmox-backup-debug api get "$1" --output-format json 2>/dev/null; }
s version; proxmox-backup-manager versions 2>/dev/null | head -1
s datastores; proxmox-backup-manager datastore list --output-format json 2>/dev/null
s usage; api /status/datastore-usage
s verify; api /admin/verify || proxmox-backup-manager verify-job list --output-format json 2>/dev/null
s sync; api /admin/sync || proxmox-backup-manager sync-job list --output-format json 2>/dev/null
s prune; api /admin/prune || proxmox-backup-manager prune-job list --output-format json 2>/dev/null
s remotes; proxmox-backup-manager remote list --output-format json 2>/dev/null
s gc; for d in $(proxmox-backup-manager datastore list --output-format json 2>/dev/null | grep -o '"name":"[^"]*"' | cut -d'"' -f4); do printf '%s\t' "$d"; api "/admin/datastore/$d/gc" | tr -d '\n'; echo; done
s fingerprint; proxmox-backup-manager cert info 2>/dev/null | grep -i fingerprint
s end
"""


def _sections(stdout: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in (stdout or "").splitlines():
        if line.startswith("@@@"):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _json(text: str, default: Any) -> Any:
    if not text:
        return default
    try:
        v = json.loads(text)
    except ValueError:
        return default
    if isinstance(v, dict) and "data" in v and len(v) <= 3:
        v = v["data"]
    return v


def _clean(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [{k: v for k, v in r.items() if "password" not in k.lower()} for r in rows if isinstance(r, dict)]


def parse_overview(stdout: str) -> dict[str, Any]:
    sec = _sections(stdout)
    if "nopbs" in sec:
        raise NodeError("this machine is not a Proxmox Backup Server (proxmox-backup-manager not found)")
    if "datastores" not in sec:
        raise NodeError("the PBS check returned no data: " + (stdout or "")[:200])
    usage = {u.get("store"): u for u in _clean(_json(sec.get("usage", ""), []))}
    gc: dict[str, dict[str, Any]] = {}
    for line in sec.get("gc", "").splitlines():
        if "\t" in line:
            name, raw = line.split("\t", 1)
            gc[name] = _json(raw, {}) if raw.strip() else {}
    stores = []
    for d in _clean(_json(sec.get("datastores", ""), [])):
        u = usage.get(d.get("name"), {})
        total, used = int(u.get("total") or 0), int(u.get("used") or 0)
        g = gc.get(d.get("name")) or {}
        stores.append({"name": d.get("name"), "path": d.get("path"), "comment": d.get("comment"),
                       "total": total or None, "used": used or None, "avail": u.get("avail"),
                       "used_pct": round(used * 100 / total, 1) if total else None,
                       "gc_schedule": d.get("gc-schedule"), "prune_schedule": d.get("prune-schedule"),
                       "gc_last": g.get("last-run-endtime"), "gc_state": g.get("last-run-state"),
                       "removed_bytes": g.get("removed-bytes")})
    fp = None
    m = re.search(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){31})", sec.get("fingerprint", ""))
    if m:
        fp = m.group(1)

    def jobs(name: str) -> list[dict[str, Any]]:
        out = []
        for j in _clean(_json(sec.get(name, ""), [])):
            out.append({**j, "last_state": j.get("last-run-state"), "last_run": j.get("last-run-endtime"),
                        "next_run": j.get("next-run")})
        return out

    return {"version": sec.get("version", "")[:100] or None, "fingerprint": fp, "datastores": stores,
            "verify_jobs": jobs("verify"), "sync_jobs": jobs("sync"), "prune_jobs": jobs("prune"),
            "remotes": _clean(_json(sec.get("remotes", ""), []))}


async def _exec(node_id: str, command: str, timeout: int = 120) -> dict[str, Any]:
    if not agent_hub.is_online(node_id):
        raise NodeError("the machine is offline")
    res = await agent_hub.request(node_id, "exec", {"command": command, "timeout": timeout}, timeout=timeout + 30) or {}
    if res.get("timed_out"):
        raise NodeError("the command timed out")
    return res


async def overview(node_id: str) -> dict[str, Any]:
    res = await _exec(node_id, READ_SCRIPT, 120)
    return parse_overview(res.get("stdout") or "")


async def _run(node_id: str, args: list[str], *, timeout: int = 120, env: dict[str, str] | None = None) -> dict[str, Any]:
    # Secrets go into a shell variable first, so they are not part of the logged argument list.
    prefix = "".join(f"{k}={shlex.quote(v)}; " for k, v in (env or {}).items())
    cmd = prefix + " ".join(shlex.quote(a) if not a.startswith("$") else f'"{a}"' for a in args)
    res = await _exec(node_id, cmd + " 2>&1", timeout)
    if res.get("exit_code") not in (0, None):
        out = ((res.get("stdout") or "") + (res.get("stderr") or "")).strip()
        raise NodeError(f"PBS: {out[-500:] or 'command failed'}")
    return {"ok": True, "output": (res.get("stdout") or "").strip()[-2000:]}


# ------------------------------------------------------------------ jobs
class PbsJobIn(BaseModel):
    kind: Literal["verify", "sync", "prune"]
    id: str = Field(pattern=ID_RE)
    store: str = Field(pattern=ID_RE)
    schedule: str | None = Field(default=None, pattern=SCHEDULE_RE)
    ns: str | None = Field(default=None, pattern=NS_RE)
    comment: str | None = Field(default=None, max_length=200)
    # verify
    ignore_verified: bool = True
    outdated_after: int | None = Field(default=30, ge=1, le=3650)
    # sync (pull from a remote, or push to it on PBS 3.3+)
    remote: str | None = Field(default=None, pattern=ID_RE)
    remote_store: str | None = Field(default=None, pattern=ID_RE)
    direction: Literal["pull", "push"] = "pull"
    remove_vanished: bool = False
    # prune (retention)
    keep_last: int | None = Field(default=None, ge=0, le=10000)
    keep_hourly: int | None = Field(default=None, ge=0, le=10000)
    keep_daily: int | None = Field(default=None, ge=0, le=10000)
    keep_weekly: int | None = Field(default=None, ge=0, le=10000)
    keep_monthly: int | None = Field(default=None, ge=0, le=10000)
    keep_yearly: int | None = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def _check(self) -> PbsJobIn:
        if self.kind == "sync" and not (self.remote and self.remote_store):
            raise ValueError("a sync job needs remote and remote_store")
        if self.kind == "prune":
            if not self.schedule:
                raise ValueError("a prune job needs a schedule")
            if not any(getattr(self, k) for k in KEEP):
                raise ValueError("a prune job needs at least one keep option")
        return self


def job_args(j: PbsJobIn, *, create: bool) -> list[str]:
    a = ["proxmox-backup-manager", f"{j.kind}-job", "create" if create else "update", j.id]
    if create or j.kind != "sync":
        a += ["--store", j.store]
    if j.schedule:
        a += ["--schedule", j.schedule]
    if j.ns:
        a += ["--ns", j.ns]
    if j.comment:
        a += ["--comment", j.comment]
    if j.kind == "verify":
        a += ["--ignore-verified", "true" if j.ignore_verified else "false"]
        if j.outdated_after:
            a += ["--outdated-after", str(j.outdated_after)]
    elif j.kind == "sync":
        a += ["--remote", j.remote or "", "--remote-store", j.remote_store or "",
              "--remove-vanished", "true" if j.remove_vanished else "false"]
        if j.direction == "push":
            a += ["--sync-direction", "push"]
    else:
        for k in KEEP:
            v = getattr(j, k)
            if v:
                a += [f"--{k.replace('_', '-')}", str(v)]
    return a


async def save_job(node_id: str, j: PbsJobIn) -> dict[str, Any]:
    ov = await overview(node_id)
    if j.store not in {d["name"] for d in ov["datastores"]}:
        raise NodeError(f"datastore {j.store} does not exist on this PBS")
    exists = any(x.get("id") == j.id for x in ov[f"{j.kind}_jobs"])
    await _run(node_id, job_args(j, create=not exists))
    return await overview(node_id)


def _check_kind(kind: str, job_id: str) -> None:
    if kind not in KINDS:
        raise NodeError("kind must be verify, sync or prune")
    if not re.match(ID_RE, job_id):
        raise NodeError("invalid job id")


async def delete_job(node_id: str, kind: str, job_id: str) -> dict[str, Any]:
    _check_kind(kind, job_id)
    await _run(node_id, ["proxmox-backup-manager", f"{kind}-job", "remove", job_id])
    return await overview(node_id)


async def run_job(node_id: str, kind: str, job_id: str) -> dict[str, Any]:
    """Start a job now (it runs on the PBS as a task)."""
    _check_kind(kind, job_id)
    res = await _run(node_id, ["proxmox-backup-debug", "api", "create", f"/admin/{kind}/{job_id}/run"])
    return {"ok": True, "kind": kind, "id": job_id, "task": res["output"][-300:]}


# ------------------------------------------------------------------ datastores and remotes
class DatastoreIn(BaseModel):
    name: str = Field(pattern=ID_RE)
    path: str = Field(pattern=r"^/[A-Za-z0-9_./-]{1,200}$")
    gc_schedule: str | None = Field(default="daily", pattern=SCHEDULE_RE)
    comment: str | None = Field(default=None, max_length=200)


async def add_datastore(node_id: str, d: DatastoreIn) -> dict[str, Any]:
    if d.path in ("/", "/etc", "/usr", "/var", "/root", "/boot") or ".." in d.path:
        raise NodeError("choose a dedicated directory or mount point for the datastore")
    a = ["proxmox-backup-manager", "datastore", "create", d.name, d.path]
    if d.gc_schedule:
        a += ["--gc-schedule", d.gc_schedule]
    if d.comment:
        a += ["--comment", d.comment]
    await _run(node_id, a, timeout=900)
    return await overview(node_id)


class RemoteIn(BaseModel):
    name: str = Field(pattern=ID_RE)
    host: str = Field(pattern=r"^[A-Za-z0-9.:\[\]-]{1,200}$")
    port: int | None = Field(default=None, ge=1, le=65535)
    auth_id: str = Field(pattern=r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+(![A-Za-z0-9._-]+)?$")
    password: str = Field(min_length=1, max_length=500)
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f:]{20,200}$")
    comment: str | None = Field(default=None, max_length=200)


async def add_remote(node_id: str, r: RemoteIn) -> dict[str, Any]:
    a = ["proxmox-backup-manager", "remote", "create", r.name, "--host", r.host, "--auth-id", r.auth_id,
         "--password", "$MMN_PBS_SECRET"]
    if r.port:
        a += ["--port", str(r.port)]
    if r.fingerprint:
        a += ["--fingerprint", r.fingerprint]
    if r.comment:
        a += ["--comment", r.comment]
    await _run(node_id, a, env={"MMN_PBS_SECRET": r.password})
    return await overview(node_id)
