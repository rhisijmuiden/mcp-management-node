"""The customer portal (phase 17): sign-in for people at a customer and their tickets.

Portal accounts are not staff accounts: they have their own session cookie, can only reach the
endpoints below and every query is limited to their own customer (explicit filters plus the
customer isolation of ``services.tenancy``). A member sees the tickets they reported, a manager
every ticket of the customer. Internal notes and the automatic diagnosis are never sent here.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.auth import _secure_cookie
from mmn.api.deps import SAFE_METHODS, client_ip, get_session
from mmn.api.ratelimit import RateLimiter, forgot_limiter, login_limiter
from mmn.config import get_settings
from mmn.db import get_db
from mmn.models import Environment, PortalSession, PortalToken, PortalUser, Role, Ticket
from mmn.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)
from mmn.services import events, tenancy, tickets
from mmn.services.audit import audit
from mmn.services.links import public_base_url
from mmn.services.settings_store import get_setting

router = APIRouter(prefix="/api/portal", tags=["portal"])

PORTAL_COOKIE = "sp_portal"
PORTAL_CSRF = "sp_portal_csrf"
PORTAL_CSRF_HEADER = "x-portal-csrf"
_DUMMY_HASH = hash_password("not-a-real-password-just-timing")
ticket_limiter = RateLimiter(limit=20, window_seconds=60 * 60)
reply_limiter = RateLimiter(limit=60, window_seconds=60 * 60)


# ------------------------------------------------------------------ session
def _set_cookies(response: Response, request: Request, token: str, max_age: int | None) -> None:
    s = get_settings()
    secure = _secure_cookie(request)
    response.set_cookie(PORTAL_COOKIE, token, max_age=max_age, httponly=True, secure=secure, samesite="lax",
                        path=s.cookie_path)
    response.set_cookie(PORTAL_CSRF, new_token(24), max_age=max_age, httponly=False, secure=secure,
                        samesite="strict", path=s.cookie_path)


def _clear_cookies(response: Response) -> None:
    path = get_settings().cookie_path
    response.delete_cookie(PORTAL_COOKIE, path=path)
    response.delete_cookie(PORTAL_CSRF, path=path)


async def _start(db: AsyncSession, request: Request, response: Response, u: PortalUser, remember: bool) -> None:
    s = get_settings()
    token = new_token()
    life = timedelta(days=s.remember_days) if remember else timedelta(hours=s.session_hours)
    db.add(PortalSession(id=hash_token(token), portal_user_id=u.id, expires_at=datetime.now(UTC) + life,
                         remember=remember, ip=client_ip(request),
                         user_agent=(request.headers.get("user-agent") or "")[:255]))
    u.last_login_at = datetime.now(UTC)
    _set_cookies(response, request, token, int(life.total_seconds()) if remember else None)


async def portal_session(request: Request, db: AsyncSession = Depends(get_db)) -> PortalSession | None:
    token = request.cookies.get(PORTAL_COOKIE)
    if not token:
        return None
    sess = await db.get(PortalSession, hash_token(token))
    now = datetime.now(UTC)
    if sess is None or sess.expires_at < now or not sess.user.is_active or sess.user.password_hash is None:
        return None
    if not sess.remember:
        new_exp = now + timedelta(hours=get_settings().session_hours)
        if new_exp - sess.expires_at > timedelta(minutes=5):
            sess.expires_at = new_exp
            await db.commit()
    return sess


async def _enabled(db: AsyncSession) -> None:
    if not (await get_setting(db, "portal")).get("enabled", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="portal_disabled")


PREVIEW_HEADER = "x-portal-preview"


async def _preview_user(request: Request, db: AsyncSession) -> PortalUser | None:
    """"View as customer" (v0.26.0): a signed-in admin sees the portal of one customer, read only.

    The admin's own session cookie is checked; the portal user is a manager of that customer that only exists for
    this request (never stored), so the admin sees exactly what the customer's managers see.
    """
    cid = request.headers.get(PREVIEW_HEADER)
    if not cid:
        return None
    sess = await get_session(request, db)
    if sess is None or sess.user.role != Role.admin:
        return None
    e = await db.get(Environment, cid)
    if e is None:
        return None
    u = PortalUser(id="preview", customer_id=e.id, email=sess.user.email or sess.user.username,
                   full_name=sess.user.full_name or sess.user.username, role="manager",
                   language=sess.user.language if sess.user.language in ("en", "nl") else "en",
                   is_active=True, password_hash="!")
    u.customer = e
    return u


async def portal_user(request: Request, db: AsyncSession = Depends(get_db),
                      sess: PortalSession | None = Depends(portal_session)) -> PortalUser:
    if sess is None:
        preview = await _preview_user(request, db)
        if preview is not None:
            if request.method not in SAFE_METHODS:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="portal_preview_read_only")
            return preview
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not_authenticated")
    if request.method not in SAFE_METHODS:
        cookie = request.cookies.get(PORTAL_CSRF, "")
        header = request.headers.get(PORTAL_CSRF_HEADER, "")
        if not cookie or not hmac.compare_digest(cookie, header):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf_failed")
    await _enabled(db)
    return sess.user


@contextmanager
def own(u: PortalUser) -> Iterator[None]:
    """Everything inside only sees the portal user's own customer."""
    with tenancy.limited_to([u.customer_id]):
        yield


def _me(u: PortalUser, welcome: str = "") -> dict:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "phone": u.phone, "role": u.role,
            "language": u.language, "customer_id": u.customer_id,
            "customer_name": u.customer.name if u.customer else None, "welcome": welcome}


# ------------------------------------------------------------------ sign-in
class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=256)
    remember: bool = False


class ForgotIn(BaseModel):
    email: str = Field(max_length=255)


class SetPasswordIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class MeIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=50)
    language: Literal["en", "nl"] | None = None


@router.get("/status")
async def portal_status(request: Request, db: AsyncSession = Depends(get_db),
                        sess: PortalSession | None = Depends(portal_session)):
    general = await get_setting(db, "general")
    cfg = await get_setting(db, "portal")
    preview = sess is None and await _preview_user(request, db) is not None
    return {"enabled": bool(cfg.get("enabled", True)) or preview, "authenticated": sess is not None or preview,
            "preview": preview,
            "app_name": general["app_name"], "default_language": general["default_language"],
            "mail_enabled": (await get_setting(db, "mail"))["enabled"]}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    await _enabled(db)
    ip = client_ip(request) or "?"
    email = body.email.strip().lower()
    key = f"portal:{ip}:{email}"
    if not login_limiter.hit(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    u = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if u is None or u.password_hash is None:
        verify_password(_DUMMY_HASH, body.password)
    if u is None or not u.is_active or u.password_hash is None or not verify_password(u.password_hash, body.password):
        await audit(db, "portal.login", source="portal", target=email[:64], ip=ip, success=False)
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    login_limiter.reset(key)
    if needs_rehash(u.password_hash):
        u.password_hash = hash_password(body.password)
    await _start(db, request, response, u, body.remember)
    await audit(db, "portal.login", source="portal", target=u.email, ip=ip, customer=u.customer_id)
    await db.commit()
    return _me(u)


@router.post("/logout", status_code=204)
async def logout(response: Response, db: AsyncSession = Depends(get_db),
                 sess: PortalSession | None = Depends(portal_session)):
    if sess is not None:
        await db.delete(sess)
        await db.commit()
    _clear_cookies(response)
    response.status_code = 204
    return response


@router.post("/forgot", status_code=202)
async def forgot(body: ForgotIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Always 202, so it can't be used to find out which accounts exist."""
    await _enabled(db)
    ip = client_ip(request) or "?"
    if not forgot_limiter.hit(f"portal:{ip}"):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    u = (await db.execute(select(PortalUser).where(PortalUser.email == body.email.strip().lower()))).scalar_one_or_none()
    if u is not None and u.is_active:
        await tickets.send_reset(db, u, await public_base_url(db, request))
        await audit(db, "portal.forgot", source="portal", target=u.email, ip=ip)
        await db.commit()
    return {"status": "accepted"}


@router.post("/set-password")
async def set_password(body: SetPasswordIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    """Choose a password with the link of an invitation or a reset; signs in right away."""
    await _enabled(db)
    pt = await db.get(PortalToken, hash_token(body.token))
    if pt is None or pt.used or pt.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    u = await db.get(PortalUser, pt.portal_user_id)
    if u is None or not u.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_token")
    u.password_hash = hash_password(body.password)
    await db.execute(update(PortalToken).where(PortalToken.portal_user_id == u.id).values(used=True))
    for s in (await db.execute(select(PortalSession).where(PortalSession.portal_user_id == u.id))).scalars():
        await db.delete(s)
    await _start(db, request, response, u, remember=False)
    await audit(db, f"portal.{pt.purpose}_password", source="portal", target=u.email, ip=client_ip(request))
    await db.commit()
    return _me(u)


@router.get("/me")
async def me(db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    return _me(u, (await get_setting(db, "portal")).get("welcome") or "")


@router.patch("/me")
async def update_me(body: MeIn, db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(u, k, (v or None) if k != "language" else (v or u.language))
    await db.commit()
    return _me(u)


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request, db: AsyncSession = Depends(get_db),
                          u: PortalUser = Depends(portal_user)):
    if not u.password_hash or not verify_password(u.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="wrong_password")
    u.password_hash = hash_password(body.password)
    current = hash_token(request.cookies.get(PORTAL_COOKIE, ""))
    for s in (await db.execute(select(PortalSession).where(PortalSession.portal_user_id == u.id,
                                                           PortalSession.id != current))).scalars():
        await db.delete(s)
    await audit(db, "portal.change_password", source="portal", target=u.email, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ machines and tickets
class TicketIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    category: Literal["problem", "question", "request"] = "problem"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    node_ids: list[str] = Field(default_factory=list, max_length=tickets.MAX_MACHINES)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


async def _visible(db: AsyncSession, u: PortalUser, tid: str) -> Ticket:
    tk = await db.get(Ticket, tid)
    if tk is None or tk.customer_id != u.customer_id or (u.role != "manager" and tk.reporter_id != u.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return tk


async def _names(db: AsyncSession, u: PortalUser) -> dict:
    """Only the names a customer may see: their own customer and machines, no staff."""
    nodes = await tickets.customer_machines(db, u.customer_id)
    return {"customers": {u.customer_id: u.customer.name if u.customer else ""},
            "machines": {n.id: n.name for n in nodes}, "staff": {}}


@router.get("/machines")
async def machines(db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    with own(u):
        return [tickets.machine_out(n) for n in await tickets.customer_machines(db, u.customer_id)]


@router.get("/tickets")
async def list_tickets(state: Literal["active", "closed", "all"] = "all", db: AsyncSession = Depends(get_db),
                       u: PortalUser = Depends(portal_user)):
    with own(u):
        rows = await tickets.search(db, customer_id=u.customer_id,
                                    reporter_id=None if u.role == "manager" else u.id,
                                    status="active" if state == "active" else None)
        if state == "closed":
            rows = [r for r in rows if r.status in ("resolved", "closed")]
        nm = await _names(db, u)
        cnt = await tickets.message_counts(db, [r.id for r in rows], staff=False)
        out = [tickets.ticket_out(r, nm, staff=False, counts=cnt) for r in rows]
        all_rows = rows if state == "all" else await tickets.search(
            db, customer_id=u.customer_id, reporter_id=None if u.role == "manager" else u.id)
    summary = {"active": sum(1 for r in all_rows if r.status in tickets.OPEN_STATUSES),
               "waiting_on_you": sum(1 for r in all_rows if r.status == "waiting_customer"),
               "resolved": sum(1 for r in all_rows if r.status in ("resolved", "closed"))}
    return {"tickets": out, "summary": summary}


@router.post("/tickets", status_code=201)
async def create_ticket(body: TicketIn, request: Request, db: AsyncSession = Depends(get_db),
                        u: PortalUser = Depends(portal_user)):
    if not ticket_limiter.hit(u.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    with own(u):
        try:
            tk = await tickets.create_ticket(db, customer_id=u.customer_id, subject=body.subject, body=body.body,
                                             category=body.category, priority=body.priority,
                                             node_ids=body.node_ids, reporter=u)
        except tickets.TicketError as e:
            raise _bad(e) from e
        await audit(db, "portal.ticket_create", source="portal", target=f"#{tk.number}", ip=client_ip(request),
                    customer=u.customer_id, by=u.email)
        await db.commit()
        out = tickets.ticket_out(tk, await _names(db, u), staff=False)
    cfg = await get_setting(db, "portal")
    base = await public_base_url(db, request)
    tickets.notify_later(tk.id, "created", base_url=base)
    if cfg.get("auto_diagnose", True):
        tickets.start_diagnosis(tk.id)
    events.publish("ticket.created", {"id": tk.id, "number": tk.number, "customer_id": u.customer_id})
    return out


@router.get("/tickets/{tid}")
async def get_ticket(tid: str, db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    with own(u):
        tk = await _visible(db, u, tid)
        return {**tickets.ticket_out(tk, await _names(db, u), staff=False),
                "thread": await tickets.messages(db, tk, staff=False)}


@router.post("/tickets/{tid}/messages", status_code=201)
async def reply(tid: str, body: ReplyIn, request: Request, db: AsyncSession = Depends(get_db),
                u: PortalUser = Depends(portal_user)):
    if not reply_limiter.hit(u.id):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="too_many_attempts")
    with own(u):
        tk = await _visible(db, u, tid)
        try:
            m = await tickets.add_message(db, tk, kind="customer", body=body.body,
                                          author_name=u.full_name or u.email, portal_user=u)
        except tickets.TicketError as e:
            raise _bad(e) from e
        await db.commit()
        out = tickets.message_out(m, staff=False)
    tickets.notify_later(tk.id, "customer_reply", base_url=await public_base_url(db, request), message_id=m.id)
    events.publish("ticket.updated", {"id": tk.id})
    return out


@router.post("/tickets/{tid}/close")
async def close(tid: str, db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    """The customer no longer needs help (solved by themselves, or reported by mistake)."""
    with own(u):
        tk = await _visible(db, u, tid)
        if tk.status != "closed":
            await tickets.set_status(db, tk, "closed", by=u.full_name or u.email, note="closed by the customer")
            await db.commit()
        out = tickets.ticket_out(tk, await _names(db, u), staff=False)
    events.publish("ticket.updated", {"id": tk.id})
    return out


# ------------------------------------------------------------------ change calendar (phase 22)
@router.get("/changes")
async def portal_changes(request: Request, db: AsyncSession = Depends(get_db), u: PortalUser = Depends(portal_user)):
    """The customer's planned maintenance and changes (no drafts, no internal notes), the last 90 days and
    everything ahead, plus the secret iCal link of the customer when staff made one."""
    from mmn.services import changes

    now = datetime.now(UTC)
    with own(u):
        rows = await changes.search(db, customer_id=u.customer_id, start=now - timedelta(days=90),
                                    include_drafts=False)
        nm = await _names(db, u)
        nm = {"machines": nm["machines"], "tasks": {}, "runbooks": {}}
        out = [changes.change_out(r, nm, staff=False) for r in rows]
    c = changes.customer_settings(u.customer)
    base = await public_base_url(db, request)
    upcoming = [x for x in out if x["status"] in changes.ACTIVE]
    past = sorted((x for x in out if x["status"] not in changes.ACTIVE), key=lambda x: x["starts_at"], reverse=True)
    return {"upcoming": upcoming, "past": past, "maintenance_window": u.customer.maintenance_window,
            "timezone": str(await changes.tz_of(db)),
            "ical_url": f"{base}/api/changes/ical/{c['ical_token']}.ics" if c.get("ical_token") else None}


@router.get("/changes/{change_id}/report")
async def portal_change_report(change_id: str, db: AsyncSession = Depends(get_db),
                               u: PortalUser = Depends(portal_user)):
    """The report of work Stackpilot did for a change (a maintenance run: the timeline in plain words), in the
    portal user's language."""
    from mmn.models import Change, TaskRun
    from mmn.services import maintenance

    with own(u):
        ch = await db.get(Change, change_id)
        if ch is None or ch.customer_id != u.customer_id or ch.status == "draft" or not ch.task_run_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
        r = await db.get(TaskRun, ch.task_run_id)
        e = await db.get(Environment, u.customer_id)
        if r is None or e is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
        lang = u.language if u.language in ("en", "nl") else maintenance._lang(e)
        tz = await maintenance.tz_of(db)
        if r.kind == maintenance.KIND:
            rep = maintenance.build_report(r, e, lang, tz)
        else:
            rep = await maintenance.task_report(db, r, e, lang, tz)
    return {k: rep[k] for k in ("title", "intro", "summary", "timeline_label", "lines", "timezone", "state", "date")}
