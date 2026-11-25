"""Change calendar per customer (phase 22).

Planned maintenance and changes of a customer (updates, a migration, a reboot...) with a start and an end,
the machines they touch and optionally a linked scheduled task and runbook. Stackpilot checks every change
against the customer's maintenance window, other changes of the same customer and the scheduled tasks that
run in the same period, and suggests the next free slot in the window.

Notices go to the customer's contacts and portal users, through the app's own mail server, at the moments
set per customer (default 3 days and 1 hour before the start), again when a change moves or is cancelled,
and afterwards with the outcome. The portal shows the customer its own calendar and every customer (and
staff, for all customers) can subscribe to an iCal feed with a secret link.

The scheduler calls :func:`tick` every minute: it moves changes to in progress and completed (or follows
the linked runbook run) and sends the notices that are due. Times are stored in UTC; the maintenance
window and the notices use the app's time zone (Settings → schedules).
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.i18n import t
from mmn.models import Change, Environment, Node, PortalUser, Runbook, RunbookRun, ScheduledTask
from mmn.services import tenancy
from mmn.services.mailer import MailContent, try_send_mail
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.changes")

KINDS = ("maintenance", "update", "change", "migration", "other")
IMPACTS = ("none", "minor", "degraded", "outage")
STATUSES = ("draft", "planned", "in_progress", "completed", "failed", "cancelled")
ACTIVE = ("planned", "in_progress")
FINISHED = ("completed", "failed", "cancelled")
RECIPIENTS = ("contacts", "portal", "both", "none")
# Allowed moves between statuses (by hand; the scheduler moves planned → in_progress → completed).
MOVES: dict[str, tuple[str, ...]] = {
    "draft": ("planned", "cancelled"),
    "planned": ("draft", "in_progress", "completed", "failed", "cancelled"),
    "in_progress": ("completed", "failed", "cancelled"),
    "completed": ("failed",),
    "failed": ("completed",),
    "cancelled": ("planned", "draft"),
}
MAX_SPAN = timedelta(days=14)
MAX_NOTICES = 5
MAX_NOTICE_MINUTES = 60 * 24 * 30
CUSTOMER_DEFAULTS: dict[str, Any] = {
    "notify_before": None,  # None: the global setting
    "notify_after": None,
    "recipients": None,
    "extra": [],  # extra addresses that get every notice of this customer
    "lang": None,  # None: the language of the monthly report, else the app's default language
    "ical_token": "",  # the customer's own iCal feed; empty: off
}
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")


class ChangeError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(v: datetime) -> datetime:
    return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC)


async def settings(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "changes")


async def tz_of(db: AsyncSession) -> ZoneInfo:
    name = (await get_setting(db, "schedules")).get("timezone") or "Europe/Amsterdam"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def customer_settings(e: Environment) -> dict[str, Any]:
    return {**CUSTOMER_DEFAULTS, **(e.changes or {})}


def clean_offsets(v: Any) -> list[int]:
    if v is None:
        return []
    if not isinstance(v, list | tuple):
        raise ChangeError("notify_before must be a list of minutes")
    out: set[int] = set()
    for x in v:
        try:
            m = int(x)
        except (TypeError, ValueError) as ex:
            raise ChangeError("notify_before must be a list of minutes") from ex
        if m < 1 or m > MAX_NOTICE_MINUTES:
            raise ChangeError("a notice is 1 minute to 30 days before the start")
        out.add(m)
    if len(out) > MAX_NOTICES:
        raise ChangeError(f"at most {MAX_NOTICES} notices before the start")
    return sorted(out, reverse=True)


def clean_emails(v: Any) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,;\s]+", v)
    out: list[str] = []
    for x in v or []:
        x = str(x).strip().lower()
        if not x:
            continue
        if not EMAIL_RE.match(x) or len(x) > 255:
            raise ChangeError(f"not an email address: {x}")
        if x not in out:
            out.append(x)
    if len(out) > 20:
        raise ChangeError("at most 20 extra addresses")
    return out


async def effective(db: AsyncSession, e: Environment, ch: Change | None = None) -> dict[str, Any]:
    """The notice settings that apply to a customer (and a change): change → customer → global."""
    g = await settings(db)
    c = customer_settings(e)
    before = c["notify_before"] if c["notify_before"] is not None else g["notify_before"]
    if ch is not None and ch.notify_before is not None:
        before = ch.notify_before
    return {
        "notify": ch.notify if ch is not None else True,
        "notify_before": clean_offsets(before),
        "notify_after": bool(g["notify_after"] if c["notify_after"] is None else c["notify_after"]),
        "notify_cancel": bool(g.get("notify_cancel", True)),
        "recipients": c["recipients"] or g["recipients"],
        "extra": c["extra"] or [],
        "lang": c["lang"],
        "auto_status": bool(g.get("auto_status", True)),
    }


# ------------------------------------------------------------------ output
async def names(db: AsyncSession) -> dict[str, dict[str, str]]:
    nodes = {n.id: n.name for n in (await db.execute(select(Node))).scalars()}
    tasks = {x.id: x.name for x in (await db.execute(select(ScheduledTask))).scalars()}
    runbooks = {x.id: x.name for x in (await db.execute(select(Runbook))).scalars()}
    return {"machines": nodes, "tasks": tasks, "runbooks": runbooks}


def label(ch: Change) -> str:
    return f"CHG-{ch.number}"


def change_out(ch: Change, nm: dict[str, dict[str, str]] | None = None, *, staff: bool = True) -> dict[str, Any]:
    nm = nm or {"machines": {}, "tasks": {}, "runbooks": {}}
    out: dict[str, Any] = {
        "id": ch.id, "number": ch.number, "ref": label(ch), "customer_id": ch.customer_id,
        "customer_name": ch.customer.name if ch.customer else None, "title": ch.title, "description": ch.description,
        "kind": ch.kind, "impact": ch.impact, "status": ch.status, "starts_at": ch.starts_at.isoformat(),
        "ends_at": ch.ends_at.isoformat(), "minutes": int((ch.ends_at - ch.starts_at).total_seconds() // 60),
        "machines": [{"id": i, "name": nm["machines"].get(i, "?")} for i in ch.node_ids or []],
        "outcome_note": ch.outcome_note,
    }
    if staff:
        out.update({
            "internal_notes": ch.internal_notes, "node_ids": list(ch.node_ids or []), "task_id": ch.task_id,
            "task_name": nm["tasks"].get(ch.task_id) if ch.task_id else None, "runbook_id": ch.runbook_id,
            "runbook_name": nm["runbooks"].get(ch.runbook_id) if ch.runbook_id else None,
            "run_runbook": ch.run_runbook, "runbook_run_id": ch.runbook_run_id, "notify": ch.notify,
            "notify_before": ch.notify_before, "notices": list(ch.notices or []), "sequence": ch.sequence,
            "created_at": ch.created_at.isoformat(), "created_by": ch.created_by,
            "updated_at": ch.updated_at.isoformat(), "updated_by": ch.updated_by,
            "started_at": ch.started_at.isoformat() if ch.started_at else None,
            "finished_at": ch.finished_at.isoformat() if ch.finished_at else None,
        })
    return out


# ------------------------------------------------------------------ find and search
async def find_change(db: AsyncSession, ref: str, customer_id: str | None = None) -> Change:
    ref = (ref or "").strip()
    ch: Change | None = None
    m = re.fullmatch(r"(?i)(?:chg-?)?(\d{1,9})", ref)
    if m:
        ch = (await db.execute(select(Change).where(Change.number == int(m.group(1))))).scalar_one_or_none()
    if ch is None and ref:
        ch = await db.get(Change, ref)
    if ch is None and ref:
        q = select(Change).where(Change.title.ilike(ref))
        if customer_id:
            q = q.where(Change.customer_id == customer_id)
        rows = list((await db.execute(q.order_by(Change.starts_at.desc()).limit(2))).scalars())
        if len(rows) > 1:
            raise ChangeError(f"more than one change is called {ref}: give its number (CHG-...)")
        ch = rows[0] if rows else None
    if ch is None or (customer_id and ch.customer_id != customer_id):
        raise ChangeError(f"change {ref} not found")
    return ch


async def search(db: AsyncSession, *, customer_id: str | None = None, status: str | None = None,
                 start: datetime | None = None, end: datetime | None = None, include_drafts: bool = True,
                 limit: int = 500) -> list[Change]:
    q = select(Change)
    if customer_id:
        q = q.where(Change.customer_id == customer_id)
    if status == "active":
        q = q.where(Change.status.in_(ACTIVE))
    elif status == "upcoming":
        q = q.where(Change.status.in_(("draft", "planned")) if include_drafts else Change.status == "planned",
                    Change.ends_at >= _now())
    elif status:
        q = q.where(Change.status == status)
    if not include_drafts:
        q = q.where(Change.status != "draft")
    if start:
        q = q.where(Change.ends_at > start)
    if end:
        q = q.where(Change.starts_at < end)
    return list((await db.execute(q.order_by(Change.starts_at).limit(limit))).scalars())


# ------------------------------------------------------------------ maintenance window and conflicts
def _window_span(w: Any) -> int:
    if w.start == w.end:
        return 24 * 60
    return w.end - w.start if w.start < w.end else 24 * 60 - w.start + w.end


def covered(windows: list[Any], start: datetime, end: datetime) -> bool:
    """Whether [start, end) (local time) lies inside the maintenance window, checked every 5 minutes."""
    from mmn.services import gateway

    if end - start > MAX_SPAN:
        return False
    cur = start
    while cur < end:
        if not gateway.in_window(windows, cur):
            return False
        cur += timedelta(minutes=5)
    last = end - timedelta(minutes=1)
    return last < start or gateway.in_window(windows, last)


def task_customers(task: ScheduledTask, node_env: dict[str, str], node_by_name: dict[str, str]) -> set[str] | None:
    """The customers a scheduled task works on; None: every customer (scope all or tag)."""
    tg = task.targets or {}
    scope = tg.get("scope") or "all"
    if scope == "environments":
        return set(tg.get("environment_ids") or [])
    if scope == "nodes":
        out = set()
        for ref in tg.get("node_ids") or []:
            nid = ref if ref in node_env else node_by_name.get(str(ref))
            if nid:
                out.add(node_env[nid])
        return out
    if scope == "guests":
        return {node_env[g.get("node_id")] for g in tg.get("guests") or [] if g.get("node_id") in node_env}
    return None


async def task_occurrences(db: AsyncSession, start: datetime, end: datetime, customer_id: str | None = None,
                           *, per_task: int = 60) -> list[dict[str, Any]]:
    """When the enabled scheduled tasks run between ``start`` and ``end`` (UTC), with the customers they
    touch. Only tasks of ``customer_id`` (or of every customer) when given."""
    from apscheduler.triggers.cron import CronTrigger

    tz = (await get_setting(db, "schedules")).get("timezone") or "UTC"
    with tenancy.unrestricted():
        nodes = list((await db.execute(select(Node.id, Node.name, Node.environment_id))).all())
    node_env = {n.id: n.environment_id for n in nodes}
    node_by_name = {n.name: n.id for n in nodes}
    tasks = (await db.execute(select(ScheduledTask).where(ScheduledTask.enabled.is_(True)))).scalars().all()
    out: list[dict[str, Any]] = []
    for task in tasks:
        custs = task_customers(task, node_env, node_by_name)
        if customer_id and custs is not None and customer_id not in custs:
            continue
        allowed = tenancy.allowed()
        if allowed is not None and custs is not None and not custs & allowed:
            continue
        try:
            trig = CronTrigger.from_crontab(task.cron, timezone=tz)
        except ValueError:
            continue
        fire = trig.get_next_fire_time(None, start)
        n = 0
        while fire is not None and fire < end and n < per_task:
            out.append({"task_id": task.id, "name": task.name, "kind": task.kind, "at": _utc(fire).isoformat(),
                        "customer_ids": sorted(custs) if custs is not None else None})
            n += 1
            fire = trig.get_next_fire_time(fire, fire)
    out.sort(key=lambda x: x["at"])
    return out


async def conflicts(db: AsyncSession, e: Environment, start: datetime, end: datetime, *,
                    node_ids: list[str] | None = None, exclude_id: str | None = None, task_id: str | None = None,
                    notify_before: list[int] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Warnings for a change of customer ``e`` from ``start`` to ``end`` (UTC):

    * ``outside_window`` (warning): not (completely) inside the customer's maintenance window;
    * ``no_window`` (info): the customer has no maintenance window that can be read;
    * ``overlap``: another change of the customer in the same period (warning when they share machines or
      one of them has none, else info);
    * ``task`` (info): a scheduled task runs on the customer's machines in that period;
    * ``task_outside`` (warning): the linked scheduled task does not run during the change;
    * ``short_notice`` (info): starts sooner than the first notice, so the customer hears of it later;
    * ``past`` (info): the change starts in the past."""
    from mmn.services import gateway

    now = now or _now()
    start, end = _utc(start), _utc(end)
    out: list[dict[str, Any]] = []
    tz = await tz_of(db)
    windows = gateway.parse_window(e.maintenance_window)
    if windows is None:
        out.append({"kind": "no_window", "severity": "info", "window": e.maintenance_window or None})
    elif not covered(windows, start.astimezone(tz).replace(tzinfo=None), end.astimezone(tz).replace(tzinfo=None)):
        out.append({"kind": "outside_window", "severity": "warning", "window": e.maintenance_window})
    mine = set(node_ids or [])
    q = select(Change).where(Change.customer_id == e.id, Change.status.in_(("draft", *ACTIVE)),
                             Change.starts_at < end, Change.ends_at > start)
    if exclude_id:
        q = q.where(Change.id != exclude_id)
    for other in (await db.execute(q.order_by(Change.starts_at))).scalars():
        theirs = set(other.node_ids or [])
        shared = mine & theirs
        hard = bool(shared) or not mine or not theirs
        out.append({"kind": "overlap", "severity": "warning" if hard else "info", "change_id": other.id,
                    "ref": label(other), "title": other.title, "starts_at": other.starts_at.isoformat(),
                    "ends_at": other.ends_at.isoformat(), "shared_machines": sorted(shared)})
    occ = await task_occurrences(db, start, end, e.id, per_task=5)
    seen: set[str] = set()
    for o in occ:
        if o["task_id"] == task_id or o["task_id"] in seen:
            continue
        seen.add(o["task_id"])
        out.append({"kind": "task", "severity": "info", "task_id": o["task_id"], "name": o["name"], "at": o["at"]})
    if task_id:
        task = await db.get(ScheduledTask, task_id)
        if task is not None and not any(o["task_id"] == task_id for o in occ):
            out.append({"kind": "task_outside", "severity": "warning", "task_id": task.id, "name": task.name,
                        "enabled": task.enabled})
    if start < now:
        out.append({"kind": "past", "severity": "info"})
    elif notify_before and start - now < timedelta(minutes=max(notify_before)):
        out.append({"kind": "short_notice", "severity": "info", "minutes": max(notify_before),
                    "hours_left": round((start - now).total_seconds() / 3600, 1)})
    return out


async def suggest(db: AsyncSession, e: Environment, minutes: int, *, after: datetime | None = None,
                  node_ids: list[str] | None = None, exclude_id: str | None = None,
                  days: int = 56) -> dict[str, Any] | None:
    """The first slot of ``minutes`` inside the customer's maintenance window, after ``after`` (default now),
    that does not overlap another change. None when the customer has no window or nothing fits."""
    from mmn.services import gateway

    windows = gateway.parse_window(e.maintenance_window)
    if not windows or minutes < 1:
        return None
    tz = await tz_of(db)
    after = _utc(after or _now())
    local_after = after.astimezone(tz).replace(tzinfo=None)
    base = local_after.replace(hour=0, minute=0, second=0, microsecond=0)
    dur = timedelta(minutes=minutes)
    candidates: list[datetime] = []
    if gateway.in_window(windows, local_after):  # the window is open now: from the next quarter hour
        q = local_after + timedelta(minutes=(15 - local_after.minute % 15) % 15 or 15)
        candidates.append(q.replace(second=0, microsecond=0))
    for offset in range(-1, days + 1):
        day = base + timedelta(days=offset)
        for w in windows:
            if day.weekday() in w.days and _window_span(w) >= minutes:
                candidates.append(day + timedelta(minutes=w.start if w.start != w.end else 0))
    for c in sorted(set(candidates)):
        if c < local_after or not covered(windows, c, c + dur):
            continue
        start = c.replace(tzinfo=tz).astimezone(UTC)
        found = await conflicts(db, e, start, start + dur, node_ids=node_ids, exclude_id=exclude_id)
        if any(x["kind"] == "overlap" for x in found):
            continue
        return {"starts_at": start.isoformat(), "ends_at": (start + dur).isoformat(), "window": e.maintenance_window}
    return None


# ------------------------------------------------------------------ create, change, status
def _text(v: Any, field: str, limit: int, required: bool = False) -> str | None:
    s = str(v).strip() if v is not None else ""
    if required and not s:
        raise ChangeError(f"{field} is required")
    if len(s) > limit:
        raise ChangeError(f"{field} is longer than {limit} characters")
    return s or None


def parse_dt(v: Any, field: str, tz: ZoneInfo | None = None) -> datetime:
    """ISO time; without an offset it is local time (the app's time zone)."""
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError as ex:
            raise ChangeError(f"{field}: not a date and time (use ISO, e.g. 2026-12-01T22:00)") from ex
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz or UTC)
    return d.astimezone(UTC)


async def _clean_machines(db: AsyncSession, customer_id: str, refs: list[str] | None) -> list[str]:
    out: list[str] = []
    for ref in refs or []:
        n = await db.get(Node, ref)
        if n is None:
            n = (await db.execute(select(Node).where(Node.name.ilike(str(ref)), Node.environment_id == customer_id))
                 ).scalars().first()
        if n is None or n.environment_id != customer_id:
            raise ChangeError(f"machine {ref} does not belong to this customer")
        if n.id not in out:
            out.append(n.id)
    if len(out) > 100:
        raise ChangeError("at most 100 machines")
    return out


TIME_FIELDS = ("starts_at", "ends_at")
SEQ_FIELDS = ("starts_at", "ends_at", "title", "description", "status", "impact")


async def save_change(db: AsyncSession, ch: Change | None, *, customer_id: str, by: str | None,
                      **fields: Any) -> Change:
    """Create a change (``ch`` None) or change the given fields of one. Raises ChangeError."""
    e = await db.get(Environment, customer_id)
    if e is None:
        raise ChangeError("customer not found")
    if ch is not None and ch.status in FINISHED and any(k in fields for k in TIME_FIELDS):
        raise ChangeError("a finished change can't be moved: plan a new one")
    tz = await tz_of(db)
    vals: dict[str, Any] = {}
    if ch is None or "title" in fields:
        vals["title"] = _text(fields.get("title"), "title", 200, required=True)
    for k, limit in (("description", 20000), ("internal_notes", 20000), ("outcome_note", 5000)):
        if k in fields:
            vals[k] = _text(fields[k], k, limit)
    if "kind" in fields:
        if fields["kind"] not in KINDS:
            raise ChangeError(f"kind must be one of {', '.join(KINDS)}")
        vals["kind"] = fields["kind"]
    if "impact" in fields:
        if fields["impact"] not in IMPACTS:
            raise ChangeError(f"impact must be one of {', '.join(IMPACTS)}")
        vals["impact"] = fields["impact"]
    if ch is None:
        st = fields.get("status") or "planned"
        if st not in ("draft", "planned"):
            raise ChangeError("a new change is draft or planned")
        vals["status"] = st
    start = parse_dt(fields["starts_at"], "starts_at", tz) if fields.get("starts_at") else (ch.starts_at if ch else None)
    if fields.get("ends_at"):
        end = parse_dt(fields["ends_at"], "ends_at", tz)
    elif fields.get("minutes"):
        if start is None:
            raise ChangeError("starts_at is required")
        end = start + timedelta(minutes=int(fields["minutes"]))
    elif ch is not None and "starts_at" in fields:
        end = start + (ch.ends_at - ch.starts_at)
    elif ch is not None:
        end = ch.ends_at
    elif start is not None:
        end = start + timedelta(minutes=int((await settings(db)).get("default_minutes") or 120))
    else:
        end = None
    if start is None or end is None:
        raise ChangeError("starts_at is required")
    if end <= start:
        raise ChangeError("the end must be after the start")
    if end - start > MAX_SPAN:
        raise ChangeError("a change lasts at most 14 days")
    vals["starts_at"], vals["ends_at"] = start, end
    if "node_ids" in fields:
        vals["node_ids"] = await _clean_machines(db, customer_id, fields["node_ids"])
    if "task_id" in fields:
        tid = fields["task_id"] or None
        if tid:
            task = await db.get(ScheduledTask, tid)
            if task is None:
                task = (await db.execute(select(ScheduledTask).where(ScheduledTask.name.ilike(str(tid))))).scalars().first()
            if task is None:
                raise ChangeError(f"scheduled task {tid} not found")
            tid = task.id
        vals["task_id"] = tid
    if "runbook_id" in fields:
        rid = fields["runbook_id"] or None
        if rid:
            rb = await db.get(Runbook, rid)
            if rb is None:
                rb = (await db.execute(select(Runbook).where(Runbook.name.ilike(str(rid))))).scalars().first()
            if rb is None:
                raise ChangeError(f"runbook {rid} not found")
            if rb.customer_id and rb.customer_id != customer_id:
                raise ChangeError("this runbook belongs to another customer")
            rid = rb.id
        vals["runbook_id"] = rid
    for k in ("run_runbook", "notify"):
        if k in fields and fields[k] is not None:
            vals[k] = bool(fields[k])
    if "notify_before" in fields:
        vals["notify_before"] = None if fields["notify_before"] is None else clean_offsets(fields["notify_before"])
    run_rb = vals.get("run_runbook", ch.run_runbook if ch else False)
    if run_rb and not vals.get("runbook_id", ch.runbook_id if ch else None):
        raise ChangeError("choose the runbook to start")

    now = _now()
    if ch is None:
        ch = Change(customer_id=customer_id, created_by=by, created_at=now, notices=[], node_ids=[], sequence=0,
                    notify=True, run_runbook=False)
        for k, v in vals.items():
            setattr(ch, k, v)
        ch.updated_at, ch.updated_by = now, by
        db.add(ch)
        await db.flush()
        await db.refresh(ch)
        return ch
    moved = (vals["starts_at"] != ch.starts_at or vals["ends_at"] != ch.ends_at)
    bump = any(k in vals and vals[k] != getattr(ch, k) for k in SEQ_FIELDS)
    for k, v in vals.items():
        setattr(ch, k, v)
    if moved and ch.status in ("draft", "planned"):
        announced = any(n.get("ok") and n["key"] != "after" for n in ch.notices or [])
        kept = [n for n in ch.notices or [] if not n["key"].startswith("before:")
                and not (n["key"] == "rescheduled" and n.get("pending"))]
        if announced and ch.status == "planned":
            kept.append({"key": "rescheduled", "pending": True, "at": now.isoformat()})
        ch.notices = kept
    if bump:
        ch.sequence = (ch.sequence or 0) + 1
    ch.updated_at, ch.updated_by = now, by
    await db.flush()
    return ch


async def set_status(db: AsyncSession, ch: Change, status: str, *, by: str | None, note: str | None = None) -> Change:
    if status not in STATUSES:
        raise ChangeError(f"status must be one of {', '.join(STATUSES)}")
    if status == ch.status:
        if note is not None:
            ch.outcome_note = _text(note, "outcome_note", 5000)
        return ch
    if status not in MOVES[ch.status]:
        raise ChangeError(f"a change that is {ch.status} can't become {status}")
    now = _now()
    if status == "draft" and any(n.get("ok") for n in ch.notices or []):
        raise ChangeError("the customer was already told about this change: cancel it instead")
    if status == "planned" and ch.status == "cancelled" and ch.ends_at <= now:
        raise ChangeError("this change is in the past: plan a new one")
    if status == "in_progress":
        ch.started_at = now
    if status in FINISHED:
        ch.finished_at = now
        if ch.started_at is None and status != "cancelled":
            ch.started_at = min(now, ch.starts_at)
    if status in ("planned", "draft") and ch.status == "cancelled":
        ch.finished_at = None
        ch.notices = [n for n in ch.notices or [] if n["key"] != "cancelled"]
    if note is not None:
        ch.outcome_note = _text(note, "outcome_note", 5000)
    ch.status = status
    ch.sequence = (ch.sequence or 0) + 1
    ch.updated_at, ch.updated_by = now, by
    await db.flush()
    return ch


# ------------------------------------------------------------------ notices
_DAYS = {"en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
         "nl": ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]}
_MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December"],
           "nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
                  "november", "december"]}


def fmt_day(d: datetime | date, lang: str) -> str:
    lang = lang if lang in _DAYS else "en"
    return f"{_DAYS[lang][d.weekday()]} {d.day} {_MONTHS[lang][d.month - 1]} {d.year}"


def fmt_when(start: datetime, end: datetime, tz: ZoneInfo, lang: str) -> str:
    s, e = start.astimezone(tz), end.astimezone(tz)
    if s.date() == e.date() or (e - s <= timedelta(hours=24) and e.time().hour == 0 and e.minute == 0
                                and e.date() == s.date() + timedelta(days=1)):
        return f"{fmt_day(s, lang)}, {s:%H:%M}–{e:%H:%M}"
    return f"{fmt_day(s, lang)} {s:%H:%M} – {fmt_day(e, lang)} {e:%H:%M}"


def fmt_offset(minutes: int, lang: str) -> str:
    if minutes % 1440 == 0:
        return t(lang, "change.offset_days", n=minutes // 1440)
    if minutes % 60 == 0:
        return t(lang, "change.offset_hours", n=minutes // 60)
    return t(lang, "change.offset_minutes", n=minutes)


async def recipients(db: AsyncSession, e: Environment, eff: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Who gets the notices of customer ``e``: [{"email", "lang", "source"}] (contacts, portal, extra)."""
    eff = eff or await effective(db, e)
    general = await get_setting(db, "general")
    lang = eff["lang"] or (e.report or {}).get("lang") or general["default_language"]
    out: dict[str, dict[str, str]] = {}

    def add(email: str | None, lng: str, source: str) -> None:
        email = (email or "").strip().lower()
        if email and EMAIL_RE.match(email) and email not in out:
            out[email] = {"email": email, "lang": lng if lng in ("en", "nl") else "en", "source": source}

    mode = eff["recipients"]
    if mode in ("contacts", "both"):
        add(e.contact_email, lang, "contact")
        for c in e.contacts or []:
            add(c.get("email"), lang, "contact")
    if mode in ("portal", "both"):
        with tenancy.unrestricted():
            rows = (await db.execute(select(PortalUser).where(
                PortalUser.customer_id == e.id, PortalUser.is_active.is_(True),
                PortalUser.password_hash.is_not(None)))).scalars().all()
        for u in rows:
            add(u.email, u.language, "portal")
    for x in eff["extra"] or []:
        add(x, lang, "extra")
    return list(out.values())


def _paragraphs(body: str | None, keep: int = 10) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    return parts[:keep] + (["…"] if len(parts) > keep else [])


async def _content(db: AsyncSession, ch: Change, key: str, lang: str, base_url: str, tz: ZoneInfo,
                   machines: str) -> MailContent:
    general = await get_setting(db, "general")
    portal = await get_setting(db, "portal")
    kind = t(lang, f"change.kind_{ch.kind}")
    when = fmt_when(ch.starts_at, ch.ends_at, tz, lang)
    kw = {"ref": label(ch), "title": ch.title, "when": when, "kind": kind,
          "customer": ch.customer.name if ch.customer else "", "app": general["app_name"]}
    variant = key.split(":", 1)[0]
    if variant == "after":
        variant = "after_failed" if ch.status == "failed" else "after_ok"
    paras = [t(lang, f"change.body_{variant}", **kw)]
    info = [t(lang, "change.when", when=when)]
    if variant in ("before", "announce", "rescheduled"):
        info.append(t(lang, "change.impact", impact=t(lang, f"change.impact_{ch.impact}")))
    if machines:
        info.append(t(lang, "change.machines", machines=machines))
    paras.append(" · ".join(info))
    if variant in ("before", "announce", "rescheduled"):
        paras += _paragraphs(ch.description)
    elif variant.startswith("after") and ch.outcome_note:
        paras += _paragraphs(ch.outcome_note)
    muted = [t(lang, "change.footer", ref=label(ch), tz=str(tz))]
    button_url = f"{base_url}/portal/changes" if base_url and portal.get("enabled", True) else None
    return MailContent(subject=t(lang, f"change.subject_{variant}", **kw), title=t(lang, f"change.title_{variant}", **kw),
                       paragraphs=paras, muted=muted, button_url=button_url,
                       button_label=t(lang, "change.button") if button_url else None)


async def send_notice(db: AsyncSession, ch: Change, key: str, *, base_url: str | None = None) -> dict[str, Any]:
    """Mail notice ``key`` (before:<minutes>, announce, rescheduled, cancelled, after) of a change to the
    customer's recipients, one mail each, and write it to ``notices``. Never raises."""
    from mmn.services.links import public_base_url

    e = ch.customer or await db.get(Environment, ch.customer_id)
    eff = await effective(db, e, ch)
    to = await recipients(db, e, eff)
    tz = await tz_of(db)
    base = base_url if base_url is not None else await public_base_url(db)
    with tenancy.unrestricted():
        nm = {n.id: n.name for n in (await db.execute(select(Node).where(Node.id.in_(ch.node_ids or [])))).scalars()}
    machines = ", ".join(nm.get(i, "?") for i in ch.node_ids or [])
    entry: dict[str, Any] = {"key": key, "at": _now().isoformat(), "to": 0, "ok": False}
    if not (await get_setting(db, "mail")).get("enabled"):
        entry["error"] = "mail_off"
    elif not to:
        entry["error"] = "no_recipients"
    else:
        sent = 0
        for r in to:
            try:
                content = await _content(db, ch, key, r["lang"], base, tz, machines)
            except Exception:  # noqa: BLE001
                log.exception("building change notice failed")
                continue
            if await try_send_mail(db, r["email"], content, r["lang"]):
                sent += 1
        entry.update(to=sent, ok=sent > 0, recipients=len(to))
        if not sent:
            entry["error"] = "send_failed"
    ch.notices = [n for n in ch.notices or [] if not (n["key"] == key and n.get("pending"))] + [entry]
    await db.flush()
    return entry


def _sent(ch: Change, key: str) -> bool:
    return any(n["key"] == key and not n.get("pending") for n in ch.notices or [])


def _announced(ch: Change) -> bool:
    return any(n.get("ok") and n["key"] not in ("after", "cancelled") for n in ch.notices or [])


def due_notices(ch: Change, offsets: list[int], now: datetime) -> tuple[str | None, list[str]]:
    """Which before-notice to send now and which to skip (passed while one was due anyway): the one closest
    to the start of those that are due, so a change planned at short notice gets one mail, not three."""
    if ch.status != "planned" or now >= ch.starts_at or ch.starts_at - now < timedelta(minutes=1):
        return None, []
    due = [m for m in offsets if now >= ch.starts_at - timedelta(minutes=m) and not _sent(ch, f"before:{m}")]
    if not due:
        return None, []
    send = min(due)
    return f"before:{send}", [f"before:{m}" for m in due if m != send]


async def _start_runbook(db: AsyncSession, ch: Change) -> str | None:
    from mmn.services import runbooks

    rb = await db.get(Runbook, ch.runbook_id) if ch.runbook_id else None
    if rb is None:
        return None
    try:
        run = await runbooks.start_run(db, rb, customer=ch.customer, params=None, trigger="schedule",
                                       reason=f"{label(ch)}: {ch.title}", user_id=None,
                                       username=ch.updated_by or ch.created_by, context={"change_id": ch.id})
    except runbooks.RunbookError as ex:
        ch.internal_notes = ((ch.internal_notes or "") + f"\n\nRunbook not started: {ex}").strip()
        return None
    ch.runbook_run_id = run.id
    return run.id


async def tick_one(db: AsyncSession, ch: Change, now: datetime, *, base_url: str) -> list[str]:
    """One change: status by time (or by its runbook run) and the notices that are due. Returns what
    happened, for the tests and the log."""
    did: list[str] = []
    e = ch.customer or await db.get(Environment, ch.customer_id)
    eff = await effective(db, e, ch)
    if eff["auto_status"]:
        if ch.status == "planned" and ch.starts_at <= now:
            if ch.ends_at > now or (ch.run_runbook and not ch.runbook_run_id):
                await set_status(db, ch, "in_progress", by="schedule")
                did.append("in_progress")
            else:
                await set_status(db, ch, "completed", by="schedule")
                did.append("completed")
        if ch.status == "in_progress" and ch.run_runbook and ch.runbook_id and not ch.runbook_run_id:
            rid = await _start_runbook(db, ch)
            if rid:
                did.append(f"runbook:{rid}")
        if ch.status == "in_progress":
            if ch.runbook_run_id:
                run = await db.get(RunbookRun, ch.runbook_run_id)
                if run is None or run.status in ("succeeded", "failed", "cancelled"):
                    ok = run is not None and run.status == "succeeded"
                    await set_status(db, ch, "completed" if ok else "failed", by="schedule")
                    did.append(ch.status)
            elif ch.ends_at <= now:
                await set_status(db, ch, "completed", by="schedule")
                did.append("completed")
    if not ch.notify or ch.status == "draft":
        return did
    if ch.status == "planned" and any(n["key"] == "rescheduled" and n.get("pending") for n in ch.notices or []):
        if now < ch.starts_at:
            await send_notice(db, ch, "rescheduled", base_url=base_url)
            did.append("rescheduled")
            # The new time is in that mail: the reminders that are due now are not needed any more.
            key, skip = due_notices(ch, eff["notify_before"], now)
            for k in [*skip, *([key] if key else [])]:
                ch.notices = [*ch.notices, {"key": k, "at": now.isoformat(), "skipped": True}]
        else:
            ch.notices = [n for n in ch.notices if not (n["key"] == "rescheduled" and n.get("pending"))]
    key, skip = due_notices(ch, eff["notify_before"], now)
    if key:
        for k in skip:
            ch.notices = [*ch.notices, {"key": k, "at": now.isoformat(), "skipped": True}]
        await send_notice(db, ch, key, base_url=base_url)
        did.append(key)
    if ch.status == "cancelled" and eff["notify_cancel"] and _announced(ch) and not _sent(ch, "cancelled"):
        await send_notice(db, ch, "cancelled", base_url=base_url)
        did.append("cancelled")
    if ch.status in ("completed", "failed") and eff["notify_after"] and not _sent(ch, "after") \
            and (_announced(ch) or ch.created_at < ch.starts_at - timedelta(minutes=30)):
        await send_notice(db, ch, "after", base_url=base_url)
        did.append("after")
    return did


async def tick(now: datetime | None = None) -> dict[str, list[str]]:
    """Scheduler (every minute): statuses and notices of every change that is active or just finished."""
    from mmn.services import runbooks
    from mmn.services.links import public_base_url

    now = now or _now()
    out: dict[str, list[str]] = {}
    kicks: list[str] = []
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            base = await public_base_url(db)
            ids = list((await db.execute(select(Change.id).where(or_(
                Change.status.in_(ACTIVE),
                (Change.status.in_(FINISHED)) & (Change.finished_at > now - timedelta(days=2)),
            )).order_by(Change.starts_at))).scalars())
            for cid in ids:
                ch = await db.get(Change, cid)
                if ch is None:
                    continue
                had_run = ch.runbook_run_id
                try:
                    did = await tick_one(db, ch, now, base_url=base)
                    await db.commit()
                except Exception:  # noqa: BLE001
                    log.exception("change %s", cid)
                    await db.rollback()
                    continue
                if ch.runbook_run_id and ch.runbook_run_id != had_run:
                    kicks.append(ch.runbook_run_id)
                if did:
                    out[label(ch)] = did
    for rid in kicks:
        runbooks.kick(rid)
    if out:
        from mmn.services import events

        events.publish("changes.updated", {"changes": list(out)})
    return out


# ------------------------------------------------------------------ calendar and iCal
async def calendar(db: AsyncSession, start: datetime, end: datetime, customer_id: str | None = None) -> dict[str, Any]:
    from mmn.services import gateway

    nm = await names(db)
    rows = await search(db, customer_id=customer_id, start=start, end=end)
    custs = [e for e in (await db.execute(select(Environment).order_by(Environment.name))).scalars()
             if not customer_id or e.id == customer_id]
    return {
        "start": start.isoformat(), "end": end.isoformat(), "timezone": str(await tz_of(db)),
        "changes": [change_out(c, nm) for c in rows],
        "tasks": await task_occurrences(db, start, end, customer_id),
        "windows": [{"customer_id": e.id, "customer_name": e.name, "window": e.maintenance_window,
                     "understood": bool(gateway.parse_window(e.maintenance_window))} for e in custs],
    }


def _ics_escape(v: str) -> str:
    return (v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n")
            .replace("\n", "\\n").replace("\r", ""))


def _fold(line: str) -> list[str]:
    """RFC 5545: lines of at most 75 octets, continued with a space."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode()
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode())
            cur = b""
        cur += b
    out.append(cur.decode())
    return [out[0], *[" " + x for x in out[1:]]]


def _ics_time(d: datetime) -> str:
    return _utc(d).strftime("%Y%m%dT%H%M%SZ")


def to_ical(rows: list[Change], *, name: str, lang: str, staff: bool, host: str = "stackpilot") -> str:
    """An iCalendar feed of changes (no drafts; cancelled ones as STATUS:CANCELLED so calendars drop them)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stackpilot//Change calendar//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_escape(name)}", "X-PUBLISHED-TTL:PT1H"]
    stamp = _ics_time(_now())
    for ch in rows:
        if ch.status == "draft":
            continue
        summary = f"{label(ch)} {ch.title}"
        if staff and ch.customer:
            summary = f"{ch.customer.name}: {summary}"
        desc = [t(lang, f"change.kind_{ch.kind}") + " · " + t(lang, "change.impact",
                                                               impact=t(lang, f"change.impact_{ch.impact}"))]
        if ch.description:
            desc.append(ch.description)
        if ch.status in ("completed", "failed") and ch.outcome_note:
            desc.append(ch.outcome_note)
        status = {"cancelled": "CANCELLED", "draft": "TENTATIVE"}.get(ch.status, "CONFIRMED")
        ev = ["BEGIN:VEVENT", f"UID:{ch.id}@{host}", f"DTSTAMP:{stamp}", f"DTSTART:{_ics_time(ch.starts_at)}",
              f"DTEND:{_ics_time(ch.ends_at)}", f"SUMMARY:{_ics_escape(summary)}",
              f"DESCRIPTION:{_ics_escape(chr(10).join(desc))}", f"STATUS:{status}", f"SEQUENCE:{ch.sequence or 0}",
              f"LAST-MODIFIED:{_ics_time(ch.updated_at)}",
              f"CATEGORIES:{_ics_escape(t(lang, f'change.kind_{ch.kind}'))}",
              "TRANSP:OPAQUE" if ch.impact in ("degraded", "outage") else "TRANSP:TRANSPARENT", "END:VEVENT"]
        lines += ev
    lines.append("END:VCALENDAR")
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"


def new_feed_token() -> str:
    return secrets.token_urlsafe(24)


async def feed(db: AsyncSession, token: str) -> tuple[str, str] | None:
    """(file name, iCal text) of the feed that ``token`` opens, or None. Staff feed: every customer."""
    from urllib.parse import urlparse

    from mmn.services.links import public_base_url

    if not TOKEN_RE.match(token or ""):
        return None
    general = await get_setting(db, "general")
    host = urlparse(await public_base_url(db)).hostname or "stackpilot"
    since = _now() - timedelta(days=180)
    g = await settings(db)
    with tenancy.unrestricted():
        if g.get("ical_token") and secrets.compare_digest(g["ical_token"], token):
            rows = await search(db, start=since, include_drafts=False, limit=5000)
            return "stackpilot-changes.ics", to_ical(rows, name=f"{general['app_name']} · changes",
                                                     lang=general["default_language"], staff=True, host=host)
        for e in (await db.execute(select(Environment))).scalars():
            tok = (e.changes or {}).get("ical_token") or ""
            if tok and secrets.compare_digest(tok, token):
                rows = await search(db, customer_id=e.id, start=since, include_drafts=False, limit=5000)
                lang = customer_settings(e)["lang"] or (e.report or {}).get("lang") or general["default_language"]
                return f"changes-{e.id[:8]}.ics", to_ical(rows, name=f"{e.name} · {t(lang, 'change.calendar')}",
                                                          lang=lang, staff=False, host=host)
    return None


async def attention(db: AsyncSession, days: int = 30) -> dict[str, int]:
    """For the menu: changes in progress, and planned changes in the next ``days`` with a warning."""
    now = _now()
    rows = await search(db, start=now, end=now + timedelta(days=days))
    in_progress = sum(1 for c in rows if c.status == "in_progress")
    warn = 0
    envs: dict[str, Environment] = {}
    for c in rows:
        if c.status not in ("planned", "draft"):
            continue
        e = envs.get(c.customer_id) or c.customer
        envs[c.customer_id] = e
        found = await conflicts(db, e, c.starts_at, c.ends_at, node_ids=c.node_ids, exclude_id=c.id,
                                task_id=c.task_id)
        if any(x["severity"] == "warning" for x in found):
            warn += 1
    today_end = now + timedelta(hours=24)
    return {"in_progress": in_progress, "warnings": warn,
            "next_24h": sum(1 for c in rows if c.status == "planned" and c.starts_at < today_end)}
