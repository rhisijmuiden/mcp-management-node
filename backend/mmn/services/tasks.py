"""Scheduled tasks: updates inside containers (community-script ``update`` or apt/apk/dnf),
package updates on nodes, the template sync and custom commands.

Each run can take a snapshot of every container first, roll it back when the step fails, keeps
only the newest few of its own snapshots, and mails a report. A run has one result per target.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Node, NodeJob, ScheduledTask, TaskRun
from mmn.services import agent_hub, events, guests, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.tasks")

KINDS = ("ct_community_update", "ct_apt_upgrade", "node_updates", "template_sync", "custom_command")
GUEST_KINDS = ("ct_community_update", "ct_apt_upgrade")
SNAP_PREFIX = {"ct_community_update": "mmn-upd", "ct_apt_upgrade": "mmn-apt", "custom_command": "mmn-cmd"}
SKIP_CODE = 99  # a script exits with this to mean "not applicable here"


class TargetsIn(BaseModel):
    scope: Literal["all", "environments", "nodes", "guests", "tag"] = "all"
    environment_ids: list[str] = Field(default_factory=list, max_length=100)
    node_ids: list[str] = Field(default_factory=list, max_length=500)
    guests: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)  # {"node_id", "vmid"}
    tag: str | None = Field(default=None, max_length=64)
    exclude_vmids: list[int] = Field(default_factory=list, max_length=2000)


class OptionsIn(BaseModel):
    snapshot: bool = True
    rollback: bool = True
    keep_snapshots: int = Field(default=3, ge=1, le=50)
    command: str | None = Field(default=None, max_length=20000)
    run_on: Literal["ct", "node"] = "ct"
    timeout: int = Field(default=3600, ge=60, le=6 * 3600)
    parallel_nodes: int = Field(default=4, ge=1, le=50)


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["ct_community_update", "ct_apt_upgrade", "node_updates", "template_sync", "custom_command"]
    enabled: bool = True
    cron: str = Field(min_length=9, max_length=100)
    targets: TargetsIn = Field(default_factory=TargetsIn)
    options: OptionsIn = Field(default_factory=OptionsIn)
    mail_to: list[EmailStr] = Field(default_factory=list, max_length=20)
    mail_on: Literal["always", "failure", "never"] = "always"

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from mmn.services.scheduler import validate_cron

        try:
            validate_cron(v.strip())
        except ValueError as e:
            raise ValueError(f"invalid schedule: {e}") from e
        return v.strip()


def task_out(t: ScheduledTask) -> dict[str, Any]:
    from mmn.services.scheduler import next_task_run

    return {"id": t.id, "name": t.name, "kind": t.kind, "enabled": t.enabled, "cron": t.cron, "targets": t.targets,
            "options": t.options, "mail_to": t.mail_to, "mail_on": t.mail_on, "created_by": t.created_by,
            "created_at": t.created_at, "last_run_at": t.last_run_at, "last_status": t.last_status,
            "next_run_at": next_task_run(t.id)}


def run_out(r: TaskRun) -> dict[str, Any]:
    res = r.results or []
    return {"id": r.id, "task_id": r.task_id, "name": r.name, "kind": r.kind, "status": r.status, "trigger": r.trigger,
            "requested_by": r.requested_by, "started_at": r.started_at, "finished_at": r.finished_at, "results": res,
            "counts": {k: sum(1 for x in res if x.get("status") == k) for k in ("success", "failed", "skipped")}}


def _validate(body: TaskIn) -> None:
    if body.kind == "custom_command" and not (body.options.command or "").strip():
        raise NodeError("enter the command to run")
    if body.kind in GUEST_KINDS and body.targets.scope == "nodes" and not body.targets.node_ids:
        raise NodeError("choose at least one node")
    if body.targets.scope == "tag" and not body.targets.tag:
        raise NodeError("enter a tag")


async def save_task(db: AsyncSession, body: TaskIn, task: ScheduledTask | None = None,
                    created_by: str | None = None) -> ScheduledTask:
    _validate(body)
    data = body.model_dump()
    data["mail_to"] = [str(m) for m in body.mail_to]
    if task is None:
        task = ScheduledTask(created_by=created_by)
        db.add(task)
    for k in ("name", "kind", "enabled", "cron", "targets", "options", "mail_to", "mail_on"):
        setattr(task, k, data[k])
    await db.flush()
    return task


async def get_task(db: AsyncSession, task_id: str) -> ScheduledTask:
    t = await db.get(ScheduledTask, task_id)
    if t is None:
        t = (await db.execute(select(ScheduledTask).where(ScheduledTask.name == task_id))).scalars().first()
    if t is None:
        raise NodeError("scheduled task not found")
    return t


async def ensure_defaults() -> None:
    """Create the nightly template sync once (it can be changed or turned off afterwards)."""
    from mmn.services.settings_store import set_setting

    async with sessionmaker()() as db:
        seeded = await get_setting(db, "seeded")
        if seeded.get("template_sync"):
            return
        db.add(ScheduledTask(name="Nightly template sync", kind="template_sync", enabled=True, cron="30 2 * * *",
                             targets={"scope": "all"}, options=OptionsIn().model_dump(), mail_to=[],
                             mail_on="failure", created_by="system"))
        await set_setting(db, "seeded", {**seeded, "template_sync": True})
        await db.commit()


# ------------------------------------------------------------------ targets
async def resolve_targets(db: AsyncSession, kind: str, targets: dict[str, Any], options: dict[str, Any]) -> list[dict]:
    t = TargetsIn(**(targets or {}))
    all_nodes = await nodes.list_nodes(db)

    def node_ok(n: Node) -> bool:
        if t.scope == "environments":
            return n.environment_id in t.environment_ids
        if t.scope == "nodes":
            return n.id in t.node_ids or n.name in t.node_ids
        return True

    guest_level = kind in GUEST_KINDS or (kind == "custom_command" and options.get("run_on", "ct") == "ct")
    if not guest_level:
        return [{"kind": "node", "node_id": n.id, "node": n.name, "label": n.name}
                for n in all_nodes if node_ok(n) and (kind != "node_updates" or "apt" in (n.capabilities or [])
                                                     or not n.capabilities)]
    out = []
    wanted = {(str(g.get("node_id")), int(g.get("vmid") or 0)) for g in t.guests}
    for n in all_nodes:
        if t.scope in ("environments", "nodes") and not node_ok(n):
            continue
        for g in guests.guests_of(n):
            if g["type"] != "lxc" or g["template"] or g["vmid"] in t.exclude_vmids:
                continue
            if t.scope == "guests" and (n.id, g["vmid"]) not in wanted:
                continue
            if t.scope == "tag" and t.tag not in g["tags"]:
                continue
            if kind == "ct_community_update" and t.scope in ("all", "environments", "nodes") \
                    and not g["community_script"]:
                continue
            out.append({"kind": "guest", "node_id": n.id, "node": n.name, "vmid": g["vmid"], "name": g["name"],
                        "status": g["status"], "label": f"{g['vmid']} {g['name']} ({n.name})"})
    return out


# ------------------------------------------------------------------ scripts that run on the node
def apt_script(vmid: int) -> str:
    inner = (
        "set -e; export DEBIAN_FRONTEND=noninteractive; "
        "if command -v apt-get >/dev/null 2>&1; then apt-get update && "
        "apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade && "
        "apt-get -y autoremove --purge && apt-get clean; "
        "elif command -v apk >/dev/null 2>&1; then apk update && apk upgrade; "
        "elif command -v dnf >/dev/null 2>&1; then dnf -y upgrade --refresh; "
        "elif command -v pacman >/dev/null 2>&1; then pacman -Syu --noconfirm; "
        f"else echo 'no supported package manager in this container'; exit {SKIP_CODE}; fi"
    )
    return f"set -o pipefail\npct exec {int(vmid)} -- sh -c {shlex.quote(inner)}\n"


def community_update_script(vmid: int) -> str:
    inner = (
        f"if [ ! -x /usr/bin/update ]; then echo 'this container has no community-script update command'; exit {SKIP_CODE}; fi; "
        "export PHS_SILENT=1 mode=default MODE=default TERM=dumb; /usr/bin/update </dev/null"
    )
    return f"pct exec {int(vmid)} -- bash -c {shlex.quote(inner)}\n"


def guest_command_script(vmid: int, command: str) -> str:
    return f"pct exec {int(vmid)} -- sh -c {shlex.quote(command)}\n"


# ------------------------------------------------------------------ running
async def run_task(task_id: str, *, trigger: str = "manual", requested_by: str | None = None) -> str:
    async with sessionmaker()() as db:
        t = await get_task(db, task_id)
        if t.kind == "template_sync":
            tg = TargetsIn(**(t.targets or {}))
            name, tid = t.name, t.id
            t.last_run_at = datetime.now(UTC)
            await db.commit()
            from mmn.services import templates

            return await templates.sync(environment_ids=tg.environment_ids if tg.scope == "environments" else None,
                                        node_ids=tg.node_ids if tg.scope == "nodes" else None, trigger=trigger,
                                        requested_by=requested_by, task_id=tid, name=name)
        running = (await db.execute(select(TaskRun).where(TaskRun.task_id == t.id, TaskRun.status == "running"))
                   ).scalars().first()
        if running:
            raise NodeError("this task is still running")
        run = TaskRun(task_id=t.id, name=t.name, kind=t.kind, trigger=trigger, requested_by=requested_by,
                      status="running", results=[])
        db.add(run)
        t.last_run_at = datetime.now(UTC)
        await db.commit()
        run_id = run.id
    agent_hub.spawn(_run(run_id, task_id, requested_by))
    events.publish("task.run", {"run_id": run_id, "task_id": task_id, "started": True})
    return run_id


async def _job_tail(job_id: str | None, lines: int = 25) -> str:
    if not job_id:
        return ""
    async with sessionmaker()() as db:
        j = await db.get(NodeJob, job_id)
        return "\n".join((j.output or "").strip().splitlines()[-lines:]) if j else ""


async def _guest_target(task: ScheduledTask, tg: dict[str, Any], requested_by: str | None) -> dict[str, Any]:
    opts = OptionsIn(**(task.options or {}))
    row: dict[str, Any] = {"target": tg["label"], "node_id": tg["node_id"], "vmid": tg["vmid"], "status": "failed"}
    if tg["status"] != "running":
        return {**row, "status": "skipped", "error": "container is not running"}
    if not agent_hub.is_online(tg["node_id"]):
        return {**row, "status": "skipped", "error": "node offline"}
    async with sessionmaker()() as db:
        node, g = await guests.find_guest(db, tg["vmid"], tg["node_id"])
    snap = None
    who = {"source": "schedule", "requested_by": requested_by or f"task:{task.name}"}
    try:
        if opts.snapshot:
            snap = guests.snapshot_name(SNAP_PREFIX.get(task.kind, "mmn-task"))
            job = await guests.create_snapshot(node, g, snap, f"Before scheduled task '{task.name}'", **who)
            status, _, err = await guests.run_and_wait(job, 1800)
            if status != "success":
                return {**row, "error": f"snapshot failed: {err}", "job_id": job.id}
            row["snapshot"] = snap
        if task.kind == "ct_apt_upgrade":
            body = apt_script(g["vmid"])
        elif task.kind == "ct_community_update":
            body = community_update_script(g["vmid"])
        else:
            body = guest_command_script(g["vmid"], opts.command or "")
        job = await guests.script(node.id, f"task.{task.kind}", body, timeout=opts.timeout, vmid=g["vmid"],
                                  label={"task": task.name}, **who)
        status, result, err = await guests.run_and_wait(job, opts.timeout + 300)
        row["job_id"] = job.id
        row["output"] = await _job_tail(job.id)
        if status == "success":
            row["status"] = "success"
        elif (result or {}).get("exit_code") == SKIP_CODE:
            row["status"] = "skipped"
            row["error"] = row["output"].splitlines()[-1] if row["output"] else "not applicable"
        else:
            row["error"] = err or "failed"
            if snap and opts.rollback:
                rb = await guests.rollback_snapshot(node, g, snap, start=True, **who)
                rstatus, _, rerr = await guests.run_and_wait(rb, 1800)
                row["rolled_back"] = rstatus == "success"
                if rstatus != "success":
                    row["error"] += f"; rollback failed: {rerr}"
        if snap and row["status"] == "skipped" and not row.get("rolled_back"):
            # Nothing changed: the snapshot is not needed.
            dj = await guests.delete_snapshot(node, g, snap, **who)
            await guests.run_and_wait(dj, 900)
            row.pop("snapshot", None)
        elif snap:
            await _prune_snapshots(node, g, SNAP_PREFIX.get(task.kind, "mmn-task"), opts.keep_snapshots, who)
    except (NodeError, AgentError) as e:
        row["error"] = str(e)
    return row


async def _prune_snapshots(node: Node, g: dict[str, Any], prefix: str, keep: int, who: dict[str, Any]) -> None:
    try:
        snaps = [s for s in await guests.list_snapshots(node, g) if (s["name"] or "").startswith(prefix + "-")]
    except AgentError:
        return
    for s in snaps[:-keep] if len(snaps) > keep else []:
        try:
            dj = await guests.delete_snapshot(node, g, s["name"], **who)
            await guests.run_and_wait(dj, 900)
        except (NodeError, AgentError) as e:
            log.info("pruning snapshot %s on %s: %s", s["name"], g["vmid"], e)


async def _node_target(task: ScheduledTask, tg: dict[str, Any], requested_by: str | None) -> dict[str, Any]:
    opts = OptionsIn(**(task.options or {}))
    row: dict[str, Any] = {"target": tg["label"], "node_id": tg["node_id"], "status": "failed"}
    if not agent_hub.is_online(tg["node_id"]):
        return {**row, "status": "skipped", "error": "node offline"}
    who = {"source": "schedule", "requested_by": requested_by or f"task:{task.name}"}
    try:
        if task.kind == "node_updates":
            job = await nodes.apply_updates(tg["node_id"], **who)
            status, result, err = await guests.run_and_wait(job, 3 * 3600)
            row.update(job_id=job.id, output=await _job_tail(job.id), remaining=(result or {}).get("remaining"),
                       reboot_required=(result or {}).get("reboot_required"))
        else:
            job = await guests.script(tg["node_id"], "task.custom_command", opts.command or "", timeout=opts.timeout,
                                      label={"task": task.name}, **who)
            status, result, err = await guests.run_and_wait(job, opts.timeout + 300)
            row.update(job_id=job.id, output=await _job_tail(job.id))
        if status == "success":
            row["status"] = "success"
        else:
            row["error"] = err or "failed"
    except (NodeError, AgentError) as e:
        row["error"] = str(e)
    return row


async def _run(run_id: str, task_id: str, requested_by: str | None) -> None:
    results: list[dict[str, Any]] = []
    try:
        async with sessionmaker()() as db:
            task = await get_task(db, task_id)
            targets = await resolve_targets(db, task.kind, task.targets or {}, task.options or {})
        opts = OptionsIn(**(task.options or {}))
        by_node: dict[str, list[dict]] = {}
        for tg in targets:
            by_node.setdefault(tg["node_id"], []).append(tg)
        sem = asyncio.Semaphore(opts.parallel_nodes)

        async def per_node(items: list[dict]) -> None:
            async with sem:
                for tg in items:
                    fn = _guest_target if tg["kind"] == "guest" else _node_target
                    try:
                        row = await fn(task, tg, requested_by)
                    except Exception as e:  # noqa: BLE001
                        log.exception("task target %s", tg.get("label"))
                        row = {"target": tg.get("label"), "status": "failed", "error": f"{type(e).__name__}: {e}"}
                    results.append(row)
                    events.publish("task.run", {"run_id": run_id, "task_id": task_id, "progress": len(results),
                                                "total": len(targets)})
                    await _store_partial(run_id, results)

        await asyncio.gather(*(per_node(v) for v in by_node.values()))
        if not targets:
            results.append({"target": "-", "status": "skipped", "error": "no matching targets"})
    except Exception as e:  # noqa: BLE001
        log.exception("task run %s failed", run_id)
        results.append({"target": "task", "status": "failed", "error": f"{type(e).__name__}: {e}"})
    await finish_run(run_id, results)


async def _store_partial(run_id: str, results: list[dict]) -> None:
    async with sessionmaker()() as db:
        r = await db.get(TaskRun, run_id)
        if r is not None:
            r.results = list(results)
            await db.commit()


async def finish_run(run_id: str, results: list[dict[str, Any]]) -> None:
    ok = sum(1 for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "failed")
    status = "failed" if failed and not ok else "partial" if failed else "success"
    async with sessionmaker()() as db:
        run = await db.get(TaskRun, run_id)
        if run is None:
            return
        run.results, run.status, run.finished_at = list(results), status, datetime.now(UTC)
        task = await db.get(ScheduledTask, run.task_id) if run.task_id else None
        if task is not None:
            task.last_status = status
        await db.commit()
        mail_to = list(task.mail_to or []) if task else []
        mail_on = task.mail_on if task else "never"
    events.publish("task.run", {"run_id": run_id, "done": True, "status": status})
    if mail_to and (mail_on == "always" or (mail_on == "failure" and status != "success")):
        await _mail_report(run_id, mail_to)


async def _mail_report(run_id: str, to: list[str]) -> None:
    from mmn.i18n import t
    from mmn.services.mailer import MailContent, try_send_mail

    async with sessionmaker()() as db:
        run = await db.get(TaskRun, run_id)
        general = await get_setting(db, "general")
        lang = general["default_language"]
        out = run_out(run)
        c = out["counts"]
        lines = []
        for r in out["results"]:
            mark = {"success": "✔", "failed": "✖", "skipped": "–"}.get(r.get("status"), "?")
            extra = []
            if r.get("snapshot"):
                extra.append(t(lang, "task.snapshot", name=r["snapshot"]))
            if r.get("rolled_back"):
                extra.append(t(lang, "task.rolled_back"))
            if r.get("error"):
                extra.append(str(r["error"])[:300])
            lines.append(f"{mark} {r.get('target')}" + (f": {'; '.join(extra)}" if extra else ""))
        base = (general.get("base_url") or "").rstrip("/")
        await try_send_mail(db, to, MailContent(
            subject=t(lang, f"task.subject_{out['status']}", name=run.name, ok=c["success"], failed=c["failed"]),
            title=t(lang, "task.title", name=run.name),
            paragraphs=[t(lang, "task.body", ok=c["success"], failed=c["failed"], skipped=c["skipped"]), *lines],
            muted=[t(lang, "task.footer", started=run.started_at.strftime("%Y-%m-%d %H:%M UTC"))],
            button_url=f"{base}/tasks/runs/{run.id}" if base else None,
            button_label=t(lang, "task.button") if base else None), lang)
