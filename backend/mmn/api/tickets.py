"""Tickets for staff (phase 17): the tickets of every customer, replies and internal notes, the first
automatic diagnosis, portal accounts of customers and the portal settings."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, PortalSession, PortalUser, Ticket, User
from mmn.services import agent_hub, events, tickets
from mmn.services.audit import audit
from mmn.services.links import public_base_url
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


async def _ticket(db: AsyncSession, tid: str) -> Ticket:
    try:
        return await tickets.find_ticket(db, tid)
    except tickets.TicketError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found") from e


def _name(u: User) -> str:
    return u.full_name or u.username


# ------------------------------------------------------------------ fixed paths first
@router.get("/counts")
async def counts(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await tickets.counts(db)


@router.get("/staff")
async def staff(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """Who a ticket can be assigned to."""
    rows = (await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username))).scalars()
    return [{"id": u.id, "name": _name(u), "role": u.role.value, "email": u.email} for u in rows]


@router.get("/machines")
async def machines(customer_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return [tickets.machine_out(n) for n in await tickets.customer_machines(db, customer_id)]


class PortalSettings(BaseModel):
    enabled: bool = True
    staff_to: str = Field(default="", max_length=1000)
    auto_diagnose: bool = True
    auto_close_days: int = Field(default=7, ge=0, le=365)
    welcome: str = Field(default="", max_length=2000)


@router.get("/settings")
async def get_settings_(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await get_setting(db, "portal")


@router.put("/settings")
async def put_settings(body: PortalSettings, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    out = await set_setting(db, "portal", body.model_dump())
    await audit(db, "portal.settings", user=admin, ip=client_ip(request))
    await db.commit()
    return out


# ------------------------------------------------------------------ portal accounts
class PortalUserIn(BaseModel):
    customer_id: str
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=50)
    role: Literal["member", "manager"] = "member"
    language: Literal["en", "nl"] = "nl"
    notify_all: bool = False
    send_invite: bool = True


class PortalUserUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, max_length=50)
    role: Literal["member", "manager"] | None = None
    language: Literal["en", "nl"] | None = None
    notify_all: bool | None = None
    is_active: bool | None = None


@router.get("/portal-users")
async def list_portal_users(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                            _: User = Depends(current_user)):
    q = select(PortalUser).order_by(func.lower(PortalUser.email))
    if customer_id:
        q = q.where(PortalUser.customer_id == customer_id)
    return [tickets.portal_user_out(u) for u in (await db.execute(q)).scalars()]


@router.post("/portal-users", status_code=201)
async def create_portal_user(body: PortalUserIn, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    if await db.get(Environment, body.customer_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    email = str(body.email).strip().lower()
    if (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="email_taken")
    u = PortalUser(customer_id=body.customer_id, email=email, full_name=body.full_name or None,
                   phone=body.phone or None, role=body.role, language=body.language, notify_all=body.notify_all)
    db.add(u)
    await db.flush()
    await db.refresh(u, ["customer"])
    sent = await tickets.send_invite(db, u, await public_base_url(db, request)) if body.send_invite else False
    await audit(db, "portal.user_create", user=admin, target=email, ip=client_ip(request), customer=body.customer_id)
    await db.commit()
    return {**tickets.portal_user_out(u), "invite_sent": sent}


async def _portal_user(db: AsyncSession, uid: str) -> PortalUser:
    u = await db.get(PortalUser, uid)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return u


@router.patch("/portal-users/{uid}")
async def update_portal_user(uid: str, body: PortalUserUpdate, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    u = await _portal_user(db, uid)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None or k in ("full_name", "phone"):
            setattr(u, k, v)
    if data.get("is_active") is False:
        for s in (await db.execute(select(PortalSession).where(PortalSession.portal_user_id == u.id))).scalars():
            await db.delete(s)
    await audit(db, "portal.user_update", user=admin, target=u.email, ip=client_ip(request), fields=sorted(data))
    await db.commit()
    return tickets.portal_user_out(u)


@router.post("/portal-users/{uid}/invite")
async def resend_invite(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    u = await _portal_user(db, uid)
    if not u.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="inactive")
    base = await public_base_url(db, request)
    sent = await (tickets.send_invite(db, u, base) if u.password_hash is None else tickets.send_reset(db, u, base))
    await audit(db, "portal.user_invite", user=admin, target=u.email, ip=client_ip(request), sent=sent)
    await db.commit()
    if not sent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="mail_not_sent")
    return {"sent": True, "kind": "invite" if u.password_hash is None else "reset"}


@router.delete("/portal-users/{uid}", status_code=204)
async def delete_portal_user(uid: str, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    u = await _portal_user(db, uid)
    await db.delete(u)
    await audit(db, "portal.user_delete", user=admin, target=u.email, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ tickets
@router.get("")
async def list_tickets(customer_id: str | None = None, status: str | None = None, assignee: str | None = None,
                       q: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    rows = await tickets.search(db, customer_id=customer_id, status=status,
                                assignee_id=user.id if assignee == "me" else assignee, q=q)
    nm = await tickets.names(db)
    cnt = await tickets.message_counts(db, [r.id for r in rows])
    return {"tickets": [tickets.ticket_out(r, nm, counts=cnt) for r in rows], "counts": await tickets.counts(db),
            "statuses": list(tickets.STATUSES), "priorities": list(tickets.PRIORITIES),
            "categories": list(tickets.CATEGORIES)}


class CreateIn(BaseModel):
    customer_id: str
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    category: Literal["problem", "question", "request"] = "problem"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    node_ids: list[str] = Field(default_factory=list)
    reporter_id: str | None = None  # a portal account of the customer: gets the mails and sees it in the portal
    reporter_name: str | None = Field(default=None, max_length=128)
    reporter_email: EmailStr | None = None
    assignee_id: str | None = None
    diagnose: bool = True


@router.post("", status_code=201)
async def create(body: CreateIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """A ticket on behalf of a customer (a phone call or a mail)."""
    reporter = None
    if body.reporter_id:
        reporter = await db.get(PortalUser, body.reporter_id)
        if reporter is None or reporter.customer_id != body.customer_id:
            raise _bad(tickets.TicketError("the reporter is not a portal account of this customer"))
    try:
        tk = await tickets.create_ticket(
            db, customer_id=body.customer_id, subject=body.subject, body=body.body, category=body.category,
            priority=body.priority, node_ids=body.node_ids, reporter_name=body.reporter_name,
            reporter_email=str(body.reporter_email) if body.reporter_email else None, by_user=admin)
        if reporter is not None:
            tk.reporter_id = reporter.id
            tk.reporter_name = reporter.full_name or reporter.email
            tk.reporter_email = reporter.email
        if body.assignee_id:
            await tickets.update_ticket(db, tk, by=_name(admin), assignee_id=body.assignee_id)
    except tickets.TicketError as e:
        raise _bad(e) from e
    await audit(db, "ticket.create", user=admin, target=f"#{tk.number}", ip=client_ip(request),
                customer=body.customer_id)
    await db.commit()
    if reporter is not None:
        tickets.notify_later(tk.id, "created", base_url=await public_base_url(db, request))
    if body.diagnose:
        tickets.start_diagnosis(tk.id)
    events.publish("ticket.created", {"id": tk.id, "number": tk.number, "customer_id": tk.customer_id})
    return tickets.ticket_out(tk, await tickets.names(db))


async def _detail(db: AsyncSession, tk: Ticket) -> dict:
    nm = await tickets.names(db)
    nodes = await tickets.customer_machines(db, tk.customer_id)
    env = tk.customer
    contact = {"name": env.contact_name, "email": env.contact_email, "phone": env.contact_phone,
               "maintenance_window": env.maintenance_window, "sla": env.sla} if env else {}
    reporter = await db.get(PortalUser, tk.reporter_id) if tk.reporter_id else None
    return {**tickets.ticket_out(tk, nm), "thread": await tickets.messages(db, tk), "diagnosis": tk.diagnosis or {},
            "customer": contact, "reporter": tickets.portal_user_out(reporter) if reporter else None,
            "customer_machines": [tickets.machine_out(n) for n in nodes]}


@router.get("/{tid}")
async def get_ticket(tid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await _detail(db, await _ticket(db, tid))


class UpdateIn(BaseModel):
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    assignee_id: str | None = None
    node_ids: list[str] | None = None
    subject: str | None = Field(default=None, max_length=200)


@router.patch("/{tid}")
async def update(tid: str, body: UpdateIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    tk = await _ticket(db, tid)
    data = body.model_dump(exclude_unset=True)
    try:
        changed = await tickets.update_ticket(
            db, tk, by=_name(admin), status=data.get("status"), priority=data.get("priority"),
            category=data.get("category"), assignee_id=data["assignee_id"] if "assignee_id" in data else False,
            node_ids=data.get("node_ids"), subject=data.get("subject"))
    except tickets.TicketError as e:
        raise _bad(e) from e
    if changed:
        await audit(db, "ticket.update", user=admin, target=f"#{tk.number}", ip=client_ip(request),
                    fields=sorted(changed))
    await db.commit()
    base = await public_base_url(db, request)
    if "status" in changed:
        tickets.notify_later(tk.id, "status", base_url=base)
    if changed.get("assigned"):
        tickets.notify_later(tk.id, "assigned", base_url=base, by=admin.username)
    if changed:
        events.publish("ticket.updated", {"id": tk.id})
    return await _detail(db, tk)


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    internal: bool = False
    status: str | None = None  # e.g. waiting_customer or resolved together with the reply


@router.post("/{tid}/messages", status_code=201)
async def add_message(tid: str, body: MessageIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    tk = await _ticket(db, tid)
    old = tk.status
    try:
        m = await tickets.add_message(db, tk, kind="staff", body=body.body, internal=body.internal,
                                      author_name=_name(admin), user=admin, status=body.status)
    except tickets.TicketError as e:
        raise _bad(e) from e
    await audit(db, "ticket.note" if body.internal else "ticket.reply", user=admin, target=f"#{tk.number}",
                ip=client_ip(request))
    await db.commit()
    if not body.internal:
        tickets.notify_later(tk.id, "staff_reply", base_url=await public_base_url(db, request), message_id=m.id)
    elif tk.status != old:
        tickets.notify_later(tk.id, "status", base_url=await public_base_url(db, request))
    events.publish("ticket.updated", {"id": tk.id})
    return await _detail(db, tk)


@router.post("/{tid}/diagnose", status_code=202)
async def diagnose(tid: str, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    """Run the first diagnosis again (after linking other machines, or later)."""
    tk = await _ticket(db, tid)
    if (tk.diagnosis or {}).get("status") == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already_running")
    tk.diagnosis = {**(tk.diagnosis or {}), "status": "queued"}
    await audit(db, "ticket.diagnose", user=admin, target=f"#{tk.number}", ip=client_ip(request))
    await db.commit()
    agent_hub.spawn(tickets.run_diagnosis(tk.id))
    return {"status": "queued"}


@router.delete("/{tid}", status_code=204)
async def delete(tid: str, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    tk = await _ticket(db, tid)
    await db.delete(tk)
    await audit(db, "ticket.delete", user=admin, target=f"#{tk.number}", ip=client_ip(request))
    await db.commit()
    events.publish("ticket.updated", {"id": tid})
    return Response(status_code=204)

