"""Standalone Linux and Windows servers (phase 5) and troubleshooting across many machines.

The same agent runs on Proxmox VE nodes and on any other server; ``Node.kind`` tells them apart
(``proxmox`` vs ``linux`` / ``windows``). The functions here work for every kind, so Claude can
search the logs of "all servers" or check a CVE everywhere in one call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.config import get_settings
from mmn.db import sessionmaker
from mmn.models import Node, Transfer
from mmn.services import agent_hub, events, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

log = logging.getLogger("mmn.servers")

SERVER_KINDS = ("linux", "windows")
KINDS = ("proxmox", "linux", "windows")


# ------------------------------------------------------------------ overview
def _pct(used: Any, total: Any) -> float:
    try:
        return float(used) / float(total) * 100 if total else 0.0
    except (TypeError, ValueError):
        return 0.0


def attention(n: Node) -> list[str]:
    """Short reasons why a machine needs a look (codes, translated in the UI)."""
    out: list[str] = []
    if not agent_hub.is_online(n.id):
        out.append("offline")
        return out
    m = n.metrics or {}
    if m.get("failed_services"):
        out.append("failed_services")
    disks = m.get("disks") or ([{"total": m.get("disk_total"), "used": m.get("disk_used")}] if m.get("disk_total") else [])
    if any(_pct(d.get("used"), d.get("total")) >= 90 for d in disks):
        out.append("disk_full")
    if _pct(m.get("mem_used"), m.get("mem_total")) >= 92:
        out.append("memory")
    if (n.updates or {}).get("reboot_required"):
        out.append("reboot_required")
    return out


def server_summary(n: Node) -> dict[str, Any]:
    d = nodes.node_summary(n)
    m = n.metrics or {}
    d["info"] = {k: (n.info or {}).get(k) for k in ("os_pretty_name", "os_version", "os_build", "kernel", "cpu_model",
                                                     "domain", "update_manager", "pve_version") if (n.info or {}).get(k)}
    d["failed_services"] = m.get("failed_services")
    d["disks"] = m.get("disks") or []
    d["attention"] = attention(n)
    return d


async def overview(db: AsyncSession, environment_id: str | None = None, os_filter: str | None = None) -> dict[str, Any]:
    q = select(Node).where(Node.kind.in_(SERVER_KINDS)).order_by(Node.name)
    if environment_id:
        q = q.where(Node.environment_id == environment_id)
    if os_filter in SERVER_KINDS:
        q = q.where(Node.kind == os_filter)
    rows = [server_summary(n) for n in (await db.execute(q)).scalars()]
    return {
        "servers": rows,
        "totals": {
            "servers": len(rows), "online": sum(1 for r in rows if r["online"]),
            "linux": sum(1 for r in rows if r["kind"] == "linux"), "windows": sum(1 for r in rows if r["kind"] == "windows"),
            "attention": sum(1 for r in rows if r["attention"]),
            "updates": sum(r["updates"]["count"] for r in rows),
            "servers_with_updates": sum(1 for r in rows if r["updates"]["count"]),
            "failed_services": sum(r["failed_services"] or 0 for r in rows),
        },
    }


# ------------------------------------------------------------------ choosing machines
async def resolve_targets(db: AsyncSession, servers: list[str] | None = None, environment: str | None = None,
                          os_filter: str | None = None) -> list[Node]:
    """Machines by name/id, or all of an environment, optionally only linux/windows/proxmox."""
    if servers:
        out, seen = [], set()
        for s in servers[:200]:
            n = await nodes.get_node(db, s)
            if n.id not in seen:
                seen.add(n.id)
                out.append(n)
        return out
    env_id = (await nodes.get_environment(db, environment)).id if environment else None
    rows = await nodes.list_nodes(db, env_id)
    if os_filter:
        if os_filter not in KINDS:
            raise NodeError("os must be linux, windows or proxmox")
        rows = [n for n in rows if n.kind == os_filter]
    return rows


def _row(n: Node) -> dict[str, Any]:
    return {"node_id": n.id, "server": n.name, "kind": n.kind, "environment": n.environment.name if n.environment else None,
            "online": agent_hub.is_online(n.id)}


async def fan_out(targets: list[Node], call, concurrency: int = 8) -> list[dict[str, Any]]:
    """Run ``call(node)`` on every online machine at once (max ``concurrency`` in parallel)."""
    sem = asyncio.Semaphore(concurrency)

    async def one(n: Node) -> dict[str, Any]:
        row = _row(n)
        if not row["online"]:
            row["error"] = "offline"
            return row
        async with sem:
            try:
                row["result"] = await call(n)
            except (AgentError, NodeError) as e:
                row["error"] = str(e)
        return row

    return list(await asyncio.gather(*(one(n) for n in targets)))


# ------------------------------------------------------------------ logs and errors
def _lines(text: str, keep: int) -> list[str]:
    out = [ln for ln in (text or "").splitlines() if ln.strip() and not ln.startswith("-- ")]
    return out[-keep:]


async def search_logs(targets: list[Node], pattern: str, *, since: str = "24 hours ago", source: str = "auto",
                      unit: str | None = None, priority: str | None = None, paths: list[str] | None = None,
                      lines: int = 100, log_name: str | None = None) -> list[dict[str, Any]]:
    """Search logs on many machines. source: auto (journal on Linux, Event Log on Windows), journal,
    eventlog or files (text log files such as /var/log/nginx/*.log or IIS logs)."""
    if source not in ("auto", "journal", "eventlog", "files"):
        raise NodeError("source must be auto, journal, eventlog or files")
    lines = max(1, min(lines, 2000))

    async def call(n: Node) -> dict[str, Any]:
        if source == "files":
            hours = _hours(since)
            r = await nodes.node_call(n.id, "logs.search", {"pattern": pattern, "paths": paths or [], "max_matches": lines,
                                                            "modified_within_hours": hours, "include_gz": hours > 48})
            return {"source": "files", "matches": r.get("matches", []), "files_scanned": r.get("files_scanned"),
                    "truncated": r.get("truncated"), "stopped": r.get("stopped"), "errors": r.get("errors")}
        if n.kind == "windows" and source in ("auto", "eventlog"):
            r = await nodes.node_call(n.id, "logs.events", {"grep": pattern, "since": since, "unit": unit or "",
                                                            "priority": priority or "", "lines": lines, "log": log_name or ""})
            return {"source": "eventlog", "events": r or [], "count": len(r or [])}
        if n.kind != "windows" and source == "eventlog":
            raise NodeError("no Event Log on Linux")
        r = await nodes.node_call(n.id, "logs.journal", {"grep": pattern, "since": since, "unit": unit or "",
                                                         "priority": priority or "", "lines": lines})
        found = _lines(r.get("stdout", ""), lines)
        return {"source": "journal", "lines": found, "count": len(found),
                **({"error": r.get("stderr", "").strip()[:500]} if r.get("exit_code") else {})}

    return await fan_out(targets, call)


def _hours(since: str) -> int:
    m = re.match(r"^\s*(\d+)\s*(m|min|minute|h|hour|d|day|w|week)s?\s+ago\s*$", since or "", re.I)
    if not m:
        return 24 * 7
    n, unit = int(m.group(1)), m.group(2)[0].lower()
    return max(1, {"m": n // 60 or 1, "h": n, "d": n * 24, "w": n * 24 * 7}[unit])


async def find_errors(targets: list[Node], since: str = "1 hour ago", per_server: int = 15) -> list[dict[str, Any]]:
    """Per machine: failed services and the most recent error lines (journal priority err or
    Windows events of level Error/Critical). The quickest way to see what is wrong where."""

    async def call(n: Node) -> dict[str, Any]:
        failed, logs = await asyncio.gather(
            nodes.node_call(n.id, "services.list", {"state": "failed"}),
            nodes.node_call(n.id, "logs.journal", {"since": since, "priority": "err", "lines": 200}),
            return_exceptions=True)
        out: dict[str, Any] = {}
        if isinstance(failed, BaseException):
            out["services_error"] = str(failed)
        else:
            out["failed_services"] = [s.get("name") for s in failed or []]
        if isinstance(logs, BaseException):
            out["logs_error"] = str(logs)
        else:
            all_lines = _lines(logs.get("stdout", ""), 200)
            out["error_count"] = len(all_lines)
            out["recent_errors"] = all_lines[-per_server:]
        m = n.metrics or {}
        out["findings"] = [a for a in attention(n) if a != "offline"]
        out["disks"] = [{"path": d.get("path"), "used_pct": round(_pct(d.get("used"), d.get("total")))} for d in m.get("disks") or []]
        return out

    rows = await fan_out(targets, call)
    # Most errors first, healthy machines last.
    rows.sort(key=lambda r: (-(len((r.get("result") or {}).get("failed_services") or []) * 100
                               + ((r.get("result") or {}).get("error_count") or 0)), r["server"]))
    return rows


async def diagnose(node_id: str) -> dict[str, Any]:
    return await nodes.node_call(node_id, "diagnose", {})


# ------------------------------------------------------------------ versions and CVEs
def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isalpha():
        return ord(c)
    return ord(c) + 256 if c else 0


def _cmp_part(a: str, b: str) -> int:
    """dpkg's comparison of an upstream or revision string."""
    ia = ib = 0
    while ia < len(a) or ib < len(b):
        first_diff = 0
        while (ia < len(a) and not a[ia].isdigit()) or (ib < len(b) and not b[ib].isdigit()):
            ac = _order(a[ia]) if ia < len(a) else 0
            bc = _order(b[ib]) if ib < len(b) else 0
            if ac != bc:
                return -1 if ac < bc else 1
            ia += 1
            ib += 1
        while ia < len(a) and a[ia] == "0":
            ia += 1
        while ib < len(b) and b[ib] == "0":
            ib += 1
        while ia < len(a) and a[ia].isdigit() and ib < len(b) and b[ib].isdigit():
            if not first_diff:
                first_diff = ord(a[ia]) - ord(b[ib])
            ia += 1
            ib += 1
        if ia < len(a) and a[ia].isdigit():
            return 1
        if ib < len(b) and b[ib].isdigit():
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


def compare_versions(a: str, b: str) -> int:
    """Compare two package versions like dpkg does (epoch:upstream-revision); also fine for rpm
    and plain Windows version numbers. Returns -1, 0 or 1."""
    def split(v: str) -> tuple[int, str, str]:
        v = (v or "").strip()
        epoch = 0
        if ":" in v:
            e, v = v.split(":", 1)
            epoch = int(e) if e.isdigit() else 0
        up, _, rev = v.rpartition("-") if "-" in v else (v, "", "0")
        return epoch, up, rev or "0"

    ea, ua, ra = split(a)
    eb, ub, rb = split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


async def check_cve(targets: list[Node], package: str, fixed_version: str | None = None) -> list[dict[str, Any]]:
    """Installed versions of a package on every machine; with ``fixed_version`` (the first version
    with the fix, from the CVE advisory or the distribution's tracker) each one is marked
    vulnerable or fixed. Pending updates for the package are shown too."""

    async def call(n: Node) -> dict[str, Any]:
        pkgs = await nodes.node_call(n.id, "packages.query", {"pattern": package})
        pending = {p["name"]: p.get("new") for p in (n.updates or {}).get("packages") or []}
        out = []
        for p in pkgs or []:
            row = {"name": p.get("name"), "version": p.get("version"), "source": p.get("source")}
            if p.get("name") in pending:
                row["update_available"] = pending[p["name"]]
            if fixed_version:
                row["status"] = "vulnerable" if compare_versions(str(p.get("version") or ""), fixed_version) < 0 else "fixed"
                if row["status"] == "vulnerable" and row.get("update_available"):
                    row["fixed_by_pending_update"] = compare_versions(str(row["update_available"]), fixed_version) >= 0
            out.append(row)
        return {"installed": out, "vulnerable": sum(1 for r in out if r.get("status") == "vulnerable")}

    rows = await fan_out(targets, call)
    rows.sort(key=lambda r: (-((r.get("result") or {}).get("vulnerable") or 0), not (r.get("result") or {}).get("installed"),
                             r["server"]))
    return rows


# ------------------------------------------------------------------ data copies
WIN_PATH = re.compile(r"^[A-Za-z]:\\|^\\\\[^\\]+\\")


def _check_path(n: Node, p: str, what: str) -> str:
    p = (p or "").strip()
    if not p or len(p) > 1000 or "\x00" in p:
        raise NodeError(f"{what}: give a path")
    if n.kind == "windows" or n.os == "windows":
        if not WIN_PATH.match(p):
            raise NodeError(f"{what}: use a full Windows path such as C:\\Data")
    elif not p.startswith("/"):
        raise NodeError(f"{what}: use an absolute path such as /var/www")
    if p.rstrip("/\\") in ("", "/") or re.fullmatch(r"[A-Za-z]:\\?", p):
        raise NodeError(f"{what}: copying a whole drive or / is not supported")
    return p


def spool_dir() -> Path:
    d = get_settings().data_dir / "transfers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spool_file(tid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", tid):
        raise ValueError("bad transfer id")
    return spool_dir() / f"{tid}.tar.gz"


def transfer_out(t: Transfer) -> dict[str, Any]:
    return {"id": t.id, "source_node_id": t.source_node_id, "target_node_id": t.target_node_id,
            "source_server": t.source_name, "target_server": t.target_name, "source_paths": t.source_paths,
            "target_path": t.target_path, "overwrite": t.overwrite, "status": t.status, "stage": t.stage,
            "reason": t.reason, "source": t.source, "requested_by": t.requested_by, "send_job_id": t.send_job_id,
            "receive_job_id": t.receive_job_id, "bytes": t.bytes, "wire_bytes": t.wire_bytes, "files": t.files,
            "error": t.error, "started_at": t.started_at, "finished_at": t.finished_at}


MAX_TRANSFER_DEFAULT_GB = 50


async def max_transfer_bytes(db: AsyncSession) -> int:
    from mmn.services.settings_store import get_setting

    gb = (await get_setting(db, "agents")).get("transfer_max_gb") or MAX_TRANSFER_DEFAULT_GB
    return int(gb) * 1024**3


async def start_transfer(source: str, paths: list[str], target: str, target_path: str, *, overwrite: bool = False,
                         reason: str | None = None, origin: str = "ui", requested_by: str | None = None) -> Transfer:
    if not paths or len(paths) > 20:
        raise NodeError("give 1 to 20 source paths")
    async with sessionmaker()() as db:
        src = await nodes.get_node(db, source)
        dst = await nodes.get_node(db, target)
        for n, label in ((src, "source"), (dst, "target")):
            if not agent_hub.is_online(n.id):
                raise NodeError(f"the {label} server {n.name} is offline")
            if "copy" not in (n.capabilities or []):
                raise NodeError(f"the agent on {n.name} is too old for data copies: update the agent first")
        clean = [_check_path(src, p, "source") for p in paths]
        dest = _check_path(dst, target_path, "target")
        free = shutil.disk_usage(spool_dir()).free
        if free < 2 * 1024**3:
            raise NodeError("the app server has less than 2 GiB free disk space for the copy")
        t = Transfer(source_node_id=src.id, target_node_id=dst.id, source_name=src.name, target_name=dst.name,
                     source_paths=clean, target_path=dest, overwrite=overwrite, reason=(reason or "")[:500] or None,
                     source=origin, requested_by=requested_by, status="running", stage="send")
        db.add(t)
        await db.commit()
        tid = t.id
    agent_hub.spawn(_run_transfer(tid))
    return t


async def _set(tid: str, **fields: Any) -> None:
    async with sessionmaker()() as db:
        t = await db.get(Transfer, tid)
        if t is None:
            return
        for k, v in fields.items():
            setattr(t, k, v)
        await db.commit()
        nid = t.source_node_id
    events.publish("transfer", {"id": tid, **{k: v for k, v in fields.items() if k in ("status", "stage")},
                                "node_id": nid})


async def _run_transfer(tid: str) -> None:
    spool = spool_file(tid)
    try:
        async with sessionmaker()() as db:
            t = await db.get(Transfer, tid)
            src, dst, paths, dest, overwrite = (t.source_node_id, t.target_node_id, list(t.source_paths), t.target_path,
                                                t.overwrite)
            who, origin = t.requested_by, t.source
        url = f"/agent/transfer/{tid}"
        job = await nodes.start_job(src, "copy.send", "copy.send", {"url": url, "paths": paths}, source=origin,
                                    requested_by=who, exclusive=False, label={"transfer_id": tid, "target": dest})
        await _set(tid, send_job_id=job.id)
        status, result, error = await agent_hub.wait_job(job.id)
        if status != "success":
            raise NodeError(error or "the source server could not send the data")
        await _set(tid, stage="receive", bytes=result.get("bytes"), wire_bytes=result.get("wire_bytes"),
                   files=result.get("files"))
        job = await nodes.start_job(dst, "copy.receive", "copy.receive", {"url": url, "dest": dest, "overwrite": overwrite},
                                    source=origin, requested_by=who, exclusive=False, label={"transfer_id": tid})
        await _set(tid, receive_job_id=job.id)
        status, result, error = await agent_hub.wait_job(job.id)
        if status != "success":
            raise NodeError(error or "the target server could not unpack the data")
        await _set(tid, status="success", stage="done", files=result.get("files"), bytes=result.get("bytes"),
                   finished_at=datetime.now(UTC))
    except Exception as e:  # noqa: BLE001
        log.info("transfer %s failed: %s", tid, e)
        await _set(tid, status="failed", error=str(e)[:2000], finished_at=datetime.now(UTC))
    finally:
        spool.unlink(missing_ok=True)


async def cleanup_spool() -> None:
    """Remove left-over spool files (app restarted during a copy) and close stale transfers."""
    cutoff = datetime.now(UTC) - timedelta(hours=12)
    async with sessionmaker()() as db:
        rows = (await db.execute(select(Transfer).where(Transfer.status == "running",
                                                        Transfer.started_at < cutoff))).scalars().all()
        for t in rows:
            t.status, t.error, t.finished_at = "failed", "interrupted (the app restarted or the copy timed out)", datetime.now(UTC)
        await db.commit()
        running = {t.id for t in (await db.execute(select(Transfer).where(Transfer.status == "running"))).scalars()}
    for f in spool_dir().glob("*.tar.gz"):
        if f.name.split(".")[0] not in running:
            f.unlink(missing_ok=True)
