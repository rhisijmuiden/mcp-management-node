"""Change calendar per customer (phase 22): planned maintenance and changes, conflict checks, notices to the
customer, settings and the iCal feeds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Change, Environment, User
from mmn.services import changes, events
from mmn.services.audit import audit
from mmn.services.links import public_base_url
from mmn.services.settings_store import set_setting

router = APIRouter(prefix="/api/changes", tags=["changes"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _missing() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")


async def _cust(db: AsyncSession, cid: str) -> Environment:
    e = await db.get(Environment, cid)
    if e is None:
        raise _missing()
    return e


async def _change(db: AsyncSession, cid: str) -> Change:
    ch = await db.get(Change, cid)
    if ch is None:
        raise _missing()
    return ch


def _range(start: str | None, end: str | None, default_days: int = 42) -> tuple[datetime, datetime]:
    try:
        s = changes.parse_dt(start, "start") if start else datetime.now(UTC) - timedelta(days=7)
        e = changes.parse_dt(end, "end") if end else s + timedelta(days=default_days)
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    if e <= s or e - s > timedelta(days=400):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="range")
    return s, e


async def _detail(db: AsyncSession, ch: Change) -> dict[str, Any]:
    out = changes.change_out(ch, await changes.names(db))
    e = ch.customer
    eff = await changes.effective(db, e, ch)
    out["conflicts"] = await changes.conflicts(db, e, ch.starts_at, ch.ends_at, node_ids=ch.node_ids, exclude_id=ch.id,
                                               task_id=ch.task_id, notify_before=eff["notify_before"])
    out["recipients"] = await changes.recipients(db, e, eff)
    out["effective"] = {k: eff[k] for k in ("notify_before", "notify_after", "recipients")}
    out["maintenance_window"] = e.maintenance_window
    if ch.runbook_run_id:
        from mmn.models import RunbookRun

        run = await db.get(RunbookRun, ch.runbook_run_id)
        out["runbook_run_status"] = run.status if run else None
    return out


# ------------------------------------------------------------------ list, calendar, badge
@router.get("")
async def list_changes(customer_id: str | None = None, state: str | None = None, start: str | None = None,
                       end: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    if state and state not in (*changes.STATUSES, "active", "upcoming"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="state")
    s = changes.parse_dt(start, "start") if start else None
    e = changes.parse_dt(end, "end") if end else None
    rows = await changes.search(db, customer_id=customer_id, status=state, start=s, end=e)
    nm = await changes.names(db)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"changes": [changes.change_out(r, nm) for r in rows], "counts": counts}


@router.get("/calendar")
async def calendar(start: str | None = None, end: str | None = None, customer_id: str | None = None,
                   db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    s, e = _range(start, end)
    return await changes.calendar(db, s, e, customer_id)


@router.get("/badge")
async def badge(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await changes.attention(db)


# ------------------------------------------------------------------ settings and iCal feeds
class SettingsIn(BaseModel):
    notify_before: list[int] = Field(default_factory=list, max_length=changes.MAX_NOTICES)
    notify_after: bool = True
    notify_cancel: bool = True
    recipients: Literal["contacts", "portal", "both", "none"] = "both"
    auto_status: bool = True
    default_minutes: int = Field(default=120, ge=5, le=60 * 24 * 14)


def _settings_out(cfg: dict[str, Any], base: str) -> dict[str, Any]:
    out = {k: v for k, v in cfg.items() if k != "ical_token"}
    out["ical_url"] = f"{base}/api/changes/ical/{cfg['ical_token']}.ics" if cfg.get("ical_token") else None
    return out


@router.get("/settings")
async def get_settings_(request: Request, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return _settings_out(await changes.settings(db), await public_base_url(db, request))


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        vals = {**body.model_dump(), "notify_before": changes.clean_offsets(body.notify_before)}
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    cfg = await set_setting(db, "changes", vals)
    await audit(db, "changes.settings", user=admin, ip=client_ip(request))
    await db.commit()
    return _settings_out(cfg, await public_base_url(db, request))


class FeedIn(BaseModel):
    enabled: bool = True


@router.post("/settings/ical")
async def staff_feed(body: FeedIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    """Make a new secret link for the staff feed (every customer), or switch it off. The old link stops."""
    cfg = await set_setting(db, "changes", {"ical_token": changes.new_feed_token() if body.enabled else ""})
    await audit(db, "changes.ical", user=admin, ip=client_ip(request), enabled=body.enabled, feed="staff")
    await db.commit()
    return _settings_out(cfg, await public_base_url(db, request))


class CustomerSettingsIn(BaseModel):
    notify_before: list[int] | None = Field(default=None, max_length=changes.MAX_NOTICES)
    notify_after: bool | None = None
    recipients: Literal["contacts", "portal", "both", "none"] | None = None
    extra: list[str] = Field(default_factory=list, max_length=20)
    lang: Literal["en", "nl"] | None = None


async def _customer_out(db: AsyncSession, e: Environment, base: str) -> dict[str, Any]:
    c = changes.customer_settings(e)
    eff = await changes.effective(db, e)
    return {"customer_id": e.id, "customer_name": e.name, "maintenance_window": e.maintenance_window,
            **{k: v for k, v in c.items() if k != "ical_token"},
            "ical_url": f"{base}/api/changes/ical/{c['ical_token']}.ics" if c.get("ical_token") else None,
            "effective": {k: eff[k] for k in ("notify_before", "notify_after", "recipients")},
            "recipient_list": await changes.recipients(db, e, eff)}


@router.get("/customers/{cid}/settings")
async def get_customer_settings(cid: str, request: Request, db: AsyncSession = Depends(get_db),
                                _: User = Depends(current_user)):
    return await _customer_out(db, await _cust(db, cid), await public_base_url(db, request))


@router.put("/customers/{cid}/settings")
async def put_customer_settings(cid: str, body: CustomerSettingsIn, request: Request,
                                db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    try:
        vals = {"notify_before": None if body.notify_before is None else changes.clean_offsets(body.notify_before),
                "notify_after": body.notify_after, "recipients": body.recipients,
                "extra": changes.clean_emails(body.extra), "lang": body.lang}
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    e.changes = {**(e.changes or {}), **vals}
    await audit(db, "changes.customer_settings", user=admin, target=e.name, ip=client_ip(request))
    await db.commit()
    return await _customer_out(db, e, await public_base_url(db, request))


@router.post("/customers/{cid}/ical")
async def customer_feed(cid: str, body: FeedIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    """A new secret iCal link for one customer (to share with them, also shown in their portal), or off."""
    e = await _cust(db, cid)
    e.changes = {**(e.changes or {}), "ical_token": changes.new_feed_token() if body.enabled else ""}
    await audit(db, "changes.ical", user=admin, target=e.name, ip=client_ip(request), enabled=body.enabled,
                feed="customer")
    await db.commit()
    return await _customer_out(db, e, await public_base_url(db, request))


@router.get("/ical/{name}")
async def ical(name: str, db: AsyncSession = Depends(get_db)):
    """The iCal feed behind a secret link (no sign-in: calendar apps can't). 404 for an unknown link."""
    token = name[:-4] if name.endswith(".ics") else name
    found = await changes.feed(db, token)
    if found is None:
        raise _missing()
    fname, text = found
    return Response(text, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{fname}"', "Cache-Control": "private, max-age=300",
                             "X-Robots-Tag": "noindex"})


# ------------------------------------------------------------------ conflict check
class CheckIn(BaseModel):
    customer_id: str
    starts_at: str | None = Field(default=None, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 14)
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    task_id: str | None = None
    notify_before: list[int] | None = None
    exclude_id: str | None = None


@router.post("/check")
async def check(body: CheckIn, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """Conflicts of a proposed slot and the next free slot in the maintenance window."""
    e = await _cust(db, body.customer_id)
    tz = await changes.tz_of(db)
    cfg = await changes.settings(db)
    try:
        start = changes.parse_dt(body.starts_at, "starts_at", tz) if body.starts_at else None
        end = changes.parse_dt(body.ends_at, "ends_at", tz) if body.ends_at else None
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    minutes = body.minutes or (int((end - start).total_seconds() // 60) if start and end else cfg["default_minutes"])
    if start and not end:
        end = start + timedelta(minutes=minutes)
    found: list[dict[str, Any]] = []
    if start and end:
        if end <= start:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="the end must be after the start")
        eff = await changes.effective(db, e)
        before = changes.clean_offsets(body.notify_before) if body.notify_before is not None else eff["notify_before"]
        found = await changes.conflicts(db, e, start, end, node_ids=body.node_ids, exclude_id=body.exclude_id,
                                        task_id=body.task_id, notify_before=before)
    sug = await changes.suggest(db, e, minutes, node_ids=body.node_ids, exclude_id=body.exclude_id)
    return {"conflicts": found, "suggestion": sug, "maintenance_window": e.maintenance_window, "minutes": minutes}


# ------------------------------------------------------------------ one change
class ChangeIn(BaseModel):
    customer_id: str
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    internal_notes: str | None = Field(default=None, max_length=20000)
    kind: Literal["maintenance", "update", "change", "migration", "other"] = "maintenance"
    impact: Literal["none", "minor", "degraded", "outage"] = "minor"
    status: Literal["draft", "planned"] = "planned"
    starts_at: str = Field(max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    minutes: int | None = Field(default=None, ge=5, le=60 * 24 * 14)
    node_ids: list[str] = Field(default_factory=list, max_length=100)
    task_id: str | None = None
    runbook_id: str | None = None
    run_runbook: bool = False
    notify: bool = True
    notify_before: list[int] | None = Field(default=None, max_length=changes.MAX_NOTICES)


class ChangeUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20000)
    internal_notes: str | None = Field(default=None, max_length=20000)
    kind: Literal["maintenance", "update", "change", "migration", "other"] | None = None
    impact: Literal["none", "minor", "degraded", "outage"] | None = None
    starts_at: str | None = Field(default=None, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    node_ids: list[str] | None = Field(default=None, max_length=100)
    task_id: str | None = None
    runbook_id: str | None = None
    run_runbook: bool | None = None
    notify: bool | None = None
    notify_before: list[int] | None = Field(default=None, max_length=changes.MAX_NOTICES)
    outcome_note: str | None = Field(default=None, max_length=5000)


@router.post("", status_code=201)
async def create(body: ChangeIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    await _cust(db, body.customer_id)
    fields = body.model_dump(exclude={"customer_id"})
    if fields.get("ends_at") is None:
        fields.pop("ends_at")
    if fields.get("minutes") is None:
        fields.pop("minutes")
    try:
        ch = await changes.save_change(db, None, customer_id=body.customer_id, by=admin.username, **fields)
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    await audit(db, "changes.create", user=admin, target=f"{changes.label(ch)} {ch.title}", ip=client_ip(request),
                customer=ch.customer_id, starts_at=ch.starts_at.isoformat())
    await db.commit()
    events.publish("changes.updated", {"id": ch.id})
    return await _detail(db, ch)


@router.get("/{cid}")
async def get_change(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await _detail(db, await _change(db, cid))


@router.put("/{cid}")
async def update(cid: str, body: ChangeUpdate, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    ch = await _change(db, cid)
    fields = body.model_dump(exclude_unset=True)
    for k in ("title", "starts_at", "ends_at", "kind", "impact", "node_ids", "run_runbook", "notify"):
        if k in fields and fields[k] is None:
            fields.pop(k)
    try:
        await changes.save_change(db, ch, customer_id=ch.customer_id, by=admin.username, **fields)
    except changes.ChangeError as ex:
        raise _bad(ex) from ex
    await audit(db, "changes.update", user=admin, target=f"{changes.label(ch)} {ch.title}", ip=client_ip(request),
                fields=sorted(fields))
    await db.commit()
    events.publish("changes.updated", {"id": ch.id})
    return await _detail(db, ch)


class StatusIn(BaseModel):
    status: Literal["draft", "planned", "in_progress", "completed", "failed", "cancelled"]
    note: str | None = Field(default=None, max_length=5000)


@router.post("/{cid}/status")
async def change_status(cid: str, body: StatusIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    ch = await _change(db, cid)
    old = ch.status
    try:
        await changes.set_status(db, ch, body.status, by=admin.username, note=body.note)
    except changes.ChangeError as ex:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(ex)) from ex
    await audit(db, "changes.status", user=admin, target=f"{changes.label(ch)} {ch.title}", ip=client_ip(request),
                old=old, new=ch.status)
    await db.commit()
    # Cancel and finish notices go out right away instead of on the next minute.
    if ch.status in changes.FINISHED:
        await changes.tick_one(db, ch, datetime.now(UTC), base_url=await public_base_url(db, request))
        await db.commit()
    events.publish("changes.updated", {"id": ch.id})
    return await _detail(db, ch)


class NoticeIn(BaseModel):
    kind: Literal["announce", "rescheduled", "after"] = "announce"


@router.post("/{cid}/notify")
async def notify(cid: str, body: NoticeIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """Send a notice to the customer now (the scheduled ones go out by themselves)."""
    ch = await _change(db, cid)
    if ch.status == "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="a draft is not sent to the customer: plan it first")
    if body.kind == "after" and ch.status not in ("completed", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the change is not finished yet")
    entry = await changes.send_notice(db, ch, body.kind, base_url=await public_base_url(db, request))
    await audit(db, "changes.notify", user=admin, target=f"{changes.label(ch)} {ch.title}", ip=client_ip(request),
                kind=body.kind, sent=entry.get("to", 0), success=bool(entry.get("ok")))
    await db.commit()
    return {**await _detail(db, ch), "notice": entry}


@router.delete("/{cid}", status_code=204)
async def delete(cid: str, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    ch = await _change(db, cid)
    if any(n.get("ok") for n in ch.notices or []) and ch.status in changes.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the customer was told about this change: cancel it instead")
    await audit(db, "changes.delete", user=admin, target=f"{changes.label(ch)} {ch.title}", ip=client_ip(request))
    await db.delete(ch)
    await db.commit()
    events.publish("changes.updated", {"id": cid})
    return Response(status_code=204)

