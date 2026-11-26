"""Work performed for a customer in a period, written for the customer (monthly report).

One list of everything that was done: maintenance and other tasks on their machines, procedures
(runbooks), automatic recovery, handled requests (tickets) and planned changes from the change
calendar. Each item has a plain-language title, the machine or subject, who did it (an engineer,
the AI assistant or Stackpilot automatically), how long it took and the outcome. Internal or
purely technical jobs (downloads, file reads, the receiving side of a copy) are left out, jobs that
belong to a procedure are shown as that procedure, and repeats of the same task on the same machine
on the same day are merged into one line.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Change, Environment, Node, NodeJob, RemediationAttempt, RunbookRun, TaskRun, Ticket

# Node job kind -> (category, English title, Dutch title). {x} is the guest or application name.
JOBS: dict[str, tuple[str, str, str]] = {
    "updates.apply": ("maintenance", "Installed updates", "Updates geïnstalleerd"),
    "agent.update": ("maintenance", "Updated the management software", "Beheersoftware bijgewerkt"),
    "backup.run": ("backup", "Made a backup", "Back-up gemaakt"),
    "backup.now": ("backup", "Made an extra backup of {x}", "Extra back-up gemaakt van {x}"),
    "backup.restore": ("backup", "Restored a backup", "Back-up teruggezet"),
    "guest.create": ("servers", "Set up a new server", "Nieuwe server ingericht"),
    "pve.create": ("servers", "Set up a new server", "Nieuwe server ingericht"),
    "guest.destroy": ("servers", "Removed a server that was no longer needed", "Overbodige server opgeruimd"),
    "guest.migrate": ("servers", "Moved {x} to another host", "{x} verplaatst naar een andere host"),
    "guest.snapshot": ("servers", "Made a restore point of {x}", "Herstelpunt gemaakt van {x}"),
    "guest.snapshot.delete": ("servers", "Cleaned up an old restore point of {x}", "Oud herstelpunt van {x} opgeruimd"),
    "guest.rollback": ("servers", "Returned {x} to a restore point", "{x} teruggezet naar een herstelpunt"),
    "guest.start": ("servers", "Started {x}", "{x} gestart"),
    "guest.stop": ("servers", "Stopped {x}", "{x} gestopt"),
    "guest.shutdown": ("servers", "Shut down {x}", "{x} afgesloten"),
    "guest.reboot": ("servers", "Restarted {x}", "{x} herstart"),
    "guest.suspend": ("servers", "Paused {x}", "{x} gepauzeerd"),
    "guest.resume": ("servers", "Resumed {x}", "{x} hervat"),
    "community.install": ("servers", "Installed a new application", "Nieuwe applicatie geïnstalleerd"),
    "deploy.run": ("servers", "Rolled out a new version of an application", "Nieuwe versie van een applicatie uitgerold"),
    "sdn.apply": ("network", "Changed the network settings", "Netwerkinstellingen aangepast"),
    "sdn.reload": ("network", "Reloaded the network settings", "Netwerkinstellingen opnieuw geladen"),
    "sdn.rollback": ("network", "Undid a network change", "Netwerkwijziging teruggedraaid"),
    "copy.send": ("servers", "Copied data between servers", "Gegevens gekopieerd tussen servers"),
    "script.run": ("maintenance", "Carried out a maintenance task", "Onderhoudstaak uitgevoerd"),
    "vault.run": ("maintenance", "Carried out a maintenance task", "Onderhoudstaak uitgevoerd"),
}

TEXT = {
    "en": {
        "title": "Work performed", "intro": "Everything that was done for you this month, in order.",
        "line": "{count} {tasks} carried out, {time} of work in total; {fixed} {problems} solved.",
        "task_1": "task", "task_n": "tasks", "problem_1": "problem", "problem_n": "problems",
        "when": "When", "what": "What was done", "where": "System or subject", "by": "By", "took": "Duration",
        "result": "Result", "none": "No work was needed this month.",
        "by_engineer": "Engineer", "by_ai": "AI assistant", "by_auto": "Automatic", "by_customer": "On your request",
        "r_done": "Done", "r_fixed": "Solved", "r_failed": "Not successful", "r_partial": "Partly done",
        "times": "{n}×", "lt_min": "< 1 min", "min": "{m} min", "hour": "{h} h", "hour_min": "{h} h {m} min",
        "day": "{d} day(s)",
        "c_maintenance": "Maintenance", "c_backup": "Backups", "c_servers": "Servers and applications",
        "c_network": "Network", "c_recovery": "Automatic recovery", "c_procedure": "Procedure",
        "c_change": "Planned change", "c_request": "Request",
        "fixed_auto": "Solved automatically: {problem}", "tried_auto": "Automatic repair attempted: {problem}",
        "followup": "An engineer followed up.", "procedure": "Carried out the procedure \"{name}\"",
        "request": "Handled your request #{number}: {subject}",
        "change_failed": "The change did not go as planned and was followed up.",
    },
    "nl": {
        "title": "Uitgevoerde werkzaamheden", "intro": "Alles wat er deze maand voor u is gedaan, op volgorde.",
        "line": "{count} {tasks} uitgevoerd, in totaal {time}; {fixed} {problems} opgelost.",
        "task_1": "werkzaamheid", "task_n": "werkzaamheden", "problem_1": "probleem", "problem_n": "problemen",
        "when": "Wanneer", "what": "Wat er is gedaan", "where": "Systeem of onderwerp", "by": "Door",
        "took": "Duur", "result": "Resultaat", "none": "Deze maand waren er geen werkzaamheden nodig.",
        "by_engineer": "Beheerder", "by_ai": "AI-assistent", "by_auto": "Automatisch", "by_customer": "Op uw verzoek",
        "r_done": "Gereed", "r_fixed": "Opgelost", "r_failed": "Niet gelukt", "r_partial": "Deels gereed",
        "times": "{n}×", "lt_min": "< 1 min", "min": "{m} min", "hour": "{h} uur", "hour_min": "{h} uur {m} min",
        "day": "{d} dag(en)",
        "c_maintenance": "Onderhoud", "c_backup": "Back-ups", "c_servers": "Servers en applicaties",
        "c_network": "Netwerk", "c_recovery": "Automatisch herstel", "c_procedure": "Procedure",
        "c_change": "Geplande wijziging", "c_request": "Verzoek",
        "fixed_auto": "Automatisch opgelost: {problem}", "tried_auto": "Automatisch herstel geprobeerd: {problem}",
        "followup": "Een beheerder heeft het opgepakt.", "procedure": "Procedure \"{name}\" uitgevoerd",
        "request": "Uw verzoek #{number} afgehandeld: {subject}",
        "change_failed": "De wijziging verliep niet zoals gepland en is opgevolgd.",
    },
}
MAX_ITEMS = 80


def texts(lang: str) -> dict[str, str]:
    return TEXT.get(lang) or TEXT["en"]


def duration_text(minutes: float | None, lang: str) -> str:
    """'< 1 min', '12 min', '1 h 5 min', '2 day(s)' (Dutch: 'uur', 'dag(en)')."""
    T = texts(lang)
    if minutes is None:
        return "–"
    m = int(round(minutes))
    if minutes < 1:
        return T["lt_min"]
    if m < 60:
        return T["min"].format(m=m)
    if m >= 48 * 60:
        return T["day"].format(d=round(m / 1440))
    h, rest = divmod(m, 60)
    return T["hour"].format(h=h) if not rest else T["hour_min"].format(h=h, m=rest)


def _aware(dt: datetime | None) -> datetime | None:
    from datetime import UTC

    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _minutes(a: datetime | None, b: datetime | None) -> float | None:
    a, b = _aware(a), _aware(b)
    if not a or not b or b < a:
        return None
    return (b - a).total_seconds() / 60


def _by(source: str | None, requested_by: str | None = None) -> str:
    s = (source or "").lower()
    if s == "mcp":
        return "ai"
    if s == "portal":
        return "customer"
    if s in ("ui", "cli", "manual") or (requested_by and s not in ("system", "schedule", "scheduler", "nightly",
                                                                     "alert", "finding", "uptime")):
        return "engineer"
    return "auto"


def _change_by(c: Change) -> str:
    if c.origin == "ai":
        return "ai"
    if c.automatic or c.origin in ("schedule", "recovery"):
        return "auto"
    return "engineer"


def _job_subject(j: NodeJob, node: Node | None) -> str | None:
    p = j.params or {}
    name = p.get("name") or (f"{p['vmid']}" if p.get("vmid") else None)
    return str(name) if name else None


async def collect(db: AsyncSession, e: Environment, nodes: list[Node], start: datetime, end: datetime,
                  lang: str, tz: ZoneInfo | None = None) -> dict[str, Any]:
    """The work-performed section of the report: items (oldest first) and totals."""
    T = texts(lang)
    tz = tz or ZoneInfo("UTC")
    by_id = {n.id: n for n in nodes}
    items: list[dict[str, Any]] = []

    # Procedures and the jobs, changes and repairs that point at them.
    runs = list((await db.execute(select(RunbookRun).where(
        RunbookRun.customer_id == e.id, RunbookRun.created_at < end,
        RunbookRun.finished_at.is_not(None), RunbookRun.finished_at >= start))).scalars())
    attempts = list((await db.execute(select(RemediationAttempt).where(
        RemediationAttempt.customer_id == e.id, RemediationAttempt.created_at >= start,
        RemediationAttempt.created_at < end))).scalars())
    changes = list((await db.execute(select(Change).where(
        Change.customer_id == e.id, Change.status.in_(("completed", "failed")),
        Change.ends_at >= start - timedelta(days=31), Change.starts_at < end))).scalars())
    run_jobs: set[str] = set()
    for r in runs:
        for st in r.state or []:
            if isinstance(st, dict) and st.get("job_id"):
                run_jobs.add(str(st["job_id"]))
    linked_runs = {a.run_id for a in attempts if a.run_id} | {c.runbook_run_id for c in changes if c.runbook_run_id}
    # Changes that Stackpilot recorded for a task run (a maintenance run, scheduled updates) stand for the jobs
    # of that run: those are not listed one by one as well.
    task_windows: list[tuple[str, datetime, datetime]] = []
    for c in changes:
        if not c.task_run_id:
            continue
        tr = await db.get(TaskRun, c.task_run_id)
        if tr is None:
            continue
        for row in tr.results or []:
            if isinstance(row, dict) and row.get("job_id"):
                run_jobs.add(str(row["job_id"]))
        for x in tr.timeline or []:
            for jid in x.get("job_ids") or []:
                run_jobs.add(str(jid))
        task_windows.append((f"task:{tr.name}", _aware(tr.started_at),
                             _aware(tr.finished_at) or _aware(tr.started_at) + timedelta(hours=12)))

    # Tasks on the customer's machines.
    if by_id:
        jobs = list((await db.execute(select(NodeJob).where(
            NodeJob.node_id.in_(list(by_id)), NodeJob.kind.in_(list(JOBS)),
            NodeJob.status.in_(("success", "failed")),
            NodeJob.finished_at >= start, NodeJob.finished_at < end).order_by(NodeJob.started_at))).scalars())
    else:
        jobs = []
    groups: dict[tuple, dict[str, Any]] = {}
    for j in jobs:
        if j.id in run_jobs:
            continue
        if any(j.requested_by == who and a <= (_aware(j.started_at) or a) <= b for who, a, b in task_windows):
            continue
        cat, en, nl = JOBS[j.kind]
        node = by_id.get(j.node_id)
        subject = _job_subject(j, node)
        title = (nl if lang == "nl" else en)
        if "{x}" in title:
            title = title.format(x=subject or (node.name if node else "–"))
        where = node.name if node else None
        if subject and "{x}" not in (nl if lang == "nl" else en) and j.kind in ("backup.restore", "guest.create",
                                                                              "pve.create", "guest.destroy"):
            where = f"{where} · {subject}" if where else subject
        at = _aware(j.started_at)
        key = (j.kind, j.node_id, subject, at.astimezone(tz).date() if at else None)
        g = groups.get(key)
        took = _minutes(j.started_at, j.finished_at)
        if g is None:
            g = groups[key] = {"at": at, "category": cat, "title": title, "where": where,
                               "by": _by(j.source, j.requested_by), "minutes": took, "count": 0,
                               "ok": 0, "failed": 0, "detail": None}
        elif took is not None:
            g["minutes"] = (g["minutes"] or 0) + took
        g["count"] += 1
        g["ok" if j.status == "success" else "failed"] += 1
        if j.kind == "updates.apply" and j.status == "success":
            r = j.result or {}
            n_pk = int(r.get("upgraded") or r.get("installed") or len(r.get("packages") or []) or 0)
            if n_pk:
                g["packages"] = g.get("packages", 0) + n_pk
    for g in groups.values():
        g["result"] = "done" if not g["failed"] else ("failed" if not g["ok"] else "partial")
        if g["count"] > 1:
            g["title"] = f"{g['title']} ({T['times'].format(n=g['count'])})"
        if g.get("packages"):
            g["detail"] = f"{g['packages']} update(s)"
        items.append(g)

    # Procedures (runbooks) that stand on their own.
    for r in runs:
        if r.id in linked_runs or r.status not in ("succeeded", "failed"):
            continue
        items.append({"at": _aware(r.created_at), "category": "procedure",
                      "title": T["procedure"].format(name=r.runbook_name), "where": r.reason or None,
                      "by": "ai" if r.token_id else ("auto" if r.trigger in ("schedule", "remediation", "change")
                                                     else "engineer"),
                      "minutes": _minutes(r.created_at, r.finished_at), "count": 1,
                      "result": "done" if r.status == "succeeded" else "failed", "detail": None})

    # Automatic recovery.
    for a in attempts:
        if a.status not in ("succeeded", "failed"):
            continue
        ok = a.status == "succeeded"
        items.append({"at": _aware(a.created_at), "category": "recovery",
                      "title": (T["fixed_auto"] if ok else T["tried_auto"]).format(problem=a.problem),
                      "where": a.target, "by": "auto", "minutes": _minutes(a.created_at, a.finished_at), "count": 1,
                      "result": "fixed" if ok else "failed", "detail": None if ok else T["followup"]})

    # Planned changes from the change calendar.
    for c in changes:
        done_at = _aware(c.finished_at) or _aware(c.ends_at)
        if not done_at or not (start <= done_at < end):
            continue
        if c.origin == "recovery" and c.automatic:
            continue  # the recovery attempt itself is listed above
        took = _minutes(c.started_at, c.finished_at) if c.started_at and c.finished_at else _minutes(c.starts_at, c.ends_at)
        where = ", ".join(by_id[i].name for i in (c.node_ids or []) if i in by_id) or None
        ok = c.status == "completed"
        items.append({"at": _aware(c.started_at) or _aware(c.starts_at), "category": "change", "title": c.title,
                      "where": where, "by": _change_by(c), "minutes": took, "count": 1,
                      "result": "done" if ok else "failed",
                      "detail": c.outcome_note or (None if ok else T["change_failed"])})

    # Requests (tickets) that were resolved in the period.
    tickets = list((await db.execute(select(Ticket).where(
        Ticket.customer_id == e.id, Ticket.resolved_at.is_not(None), Ticket.resolved_at >= start,
        Ticket.resolved_at < end))).scalars())
    for t in tickets:
        names = ", ".join(by_id[i].name for i in (t.node_ids or []) if i in by_id) or None
        items.append({"at": _aware(t.created_at), "category": "request",
                      "title": T["request"].format(number=t.number, subject=t.subject), "where": names,
                      "by": "engineer", "minutes": _minutes(t.created_at, t.resolved_at), "count": 1,
                      "result": "fixed", "detail": None})

    items.sort(key=lambda x: x["at"] or start)
    total_minutes = sum(x["minutes"] or 0 for x in items if x["category"] not in ("request",))
    fixed = sum(1 for x in items if x["result"] == "fixed")
    by_cat: dict[str, int] = {}
    for x in items:
        by_cat[x["category"]] = by_cat.get(x["category"], 0) + x["count"]
    count = sum(x["count"] for x in items)
    out_items = []
    for x in items[-MAX_ITEMS:]:
        out_items.append({**x, "took": duration_text(x["minutes"], lang), "category_label": T["c_" + x["category"]],
                          "by_label": T["by_" + x["by"]], "result_label": T["r_" + x["result"]]})
    return {"count": count, "minutes": round(total_minutes, 1), "time": duration_text(total_minutes, lang)
            if items else duration_text(0, lang), "fixed": fixed, "by_category": by_cat,
            "items": out_items, "truncated": max(0, len(items) - MAX_ITEMS),
            "labels": {k: T[k] for k in ("title", "intro", "when", "what", "where", "by", "took", "result", "none")},
            "line": T["line"].format(count=count, time=duration_text(total_minutes, lang) if items else "0 min",
                                     fixed=fixed, tasks=T["task_1" if count == 1 else "task_n"],
                                     problems=T["problem_1" if fixed == 1 else "problem_n"])}
