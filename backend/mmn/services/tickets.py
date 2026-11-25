"""Customer portal with tickets (phase 17).

Customers sign in to their own portal (``/portal``) with a portal account (``PortalUser``), separate
from the staff accounts of the app. There they report problems and questions, link them to their
servers and workstations, follow the status and reply. Staff see every ticket under Operations >
Tickets: status, priority, assignment, public replies and internal notes, with mail notifications
through the app's own SMTP settings.

When a ticket comes in, Stackpilot runs a first diagnosis on the linked machines (the same checks
as the MCP tools ``diagnose_server``, ``diagnose_workstation`` and ``find_errors``) and on the
open alerts and failing uptime checks of the customer. When the customer linked no machine, it
looks for machine names in the text, and otherwise checks every server of the customer for errors.
The result is shown to staff only (and to the AI through ``get_ticket``), never in the portal.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.i18n import t
from mmn.models import (
    Alert,
    Environment,
    Node,
    PortalToken,
    PortalUser,
    Role,
    Ticket,
    TicketMessage,
    UptimeCheck,
    User,
)
from mmn.security import hash_token, new_token
from mmn.services import agent_hub, events, tenancy
from mmn.services.mailer import MailContent, try_send_mail
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.tickets")

STATUSES = ("new", "open", "waiting_customer", "on_hold", "resolved", "closed")
OPEN_STATUSES = ("new", "open", "waiting_customer", "on_hold")
PRIORITIES = ("low", "normal", "high", "urgent")
CATEGORIES = ("problem", "question", "request")
PORTAL_ROLES = ("member", "manager")
MAX_MACHINES = 10
DIAG_TIMEOUT = 90
INVITE_DAYS = 7


class TicketError(Exception):
    """A refusal with a message for the user (HTTP 400, MCP error)."""


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ serialisation
async def names(db: AsyncSession) -> dict[str, dict[str, str]]:
    """Names of customers, machines and staff, to show next to their ids."""
    customers = {i: n for i, n in (await db.execute(select(Environment.id, Environment.name))).all()}
    machines = {i: n for i, n in (await db.execute(select(Node.id, Node.name))).all()}
    staff = {i: (f or u) for i, u, f in (await db.execute(select(User.id, User.username, User.full_name))).all()}
    return {"customers": customers, "machines": machines, "staff": staff}


def ticket_out(tk: Ticket, nm: dict[str, dict[str, str]] | None = None, *, staff: bool = True,
               counts: dict[str, int] | None = None) -> dict[str, Any]:
    nm = nm or {"customers": {}, "machines": {}, "staff": {}}
    out: dict[str, Any] = {
        "id": tk.id, "number": tk.number, "ref": f"#{tk.number}", "subject": tk.subject, "category": tk.category,
        "priority": tk.priority, "status": tk.status, "customer_id": tk.customer_id,
        "customer_name": nm["customers"].get(tk.customer_id),
        "machines": [{"id": i, "name": nm["machines"].get(i)} for i in tk.node_ids or [] if i in nm["machines"]],
        "reporter_name": tk.reporter_name, "reporter_email": tk.reporter_email,
        "created_at": tk.created_at, "updated_at": tk.updated_at, "resolved_at": tk.resolved_at,
        "closed_at": tk.closed_at, "last_customer_at": tk.last_customer_at, "last_staff_at": tk.last_staff_at,
        "messages": (counts or {}).get(tk.id, 0),
    }
    if staff:
        d = tk.diagnosis or {}
        out.update({
            "reporter_id": tk.reporter_id, "created_by": tk.created_by, "assignee_id": tk.assignee_id,
            "assignee_name": nm["staff"].get(tk.assignee_id) if tk.assignee_id else None,
            "first_response_at": tk.first_response_at,
            "diagnosis_status": d.get("status"), "diagnosis_verdict": d.get("verdict"),
            # The customer waits on us: new, open, or the customer replied after our last reply.
            "needs_reply": tk.status in ("new", "open") and (
                tk.last_staff_at is None or (tk.last_customer_at is not None and tk.last_customer_at > tk.last_staff_at)),
        })
    else:
        out["can_reply"] = tk.status != "closed"
    return out


def message_out(m: TicketMessage, *, staff: bool = True) -> dict[str, Any]:
    out = {"id": m.id, "author_kind": m.author_kind, "author_name": m.author_name, "body": m.body,
           "created_at": m.created_at, "event": m.event or {}}
    if staff:
        out["internal"] = m.internal
    return out


def portal_user_out(u: PortalUser) -> dict[str, Any]:
    return {"id": u.id, "customer_id": u.customer_id, "customer_name": u.customer.name if u.customer else None,
            "email": u.email, "full_name": u.full_name, "phone": u.phone, "role": u.role, "language": u.language,
            "is_active": u.is_active, "notify_all": u.notify_all, "activated": u.password_hash is not None,
            "created_at": u.created_at, "invited_at": u.invited_at, "last_login_at": u.last_login_at}


async def message_counts(db: AsyncSession, ids: list[str], *, staff: bool = True) -> dict[str, int]:
    if not ids:
        return {}
    q = select(TicketMessage.ticket_id, func.count()).where(TicketMessage.ticket_id.in_(ids),
                                                             TicketMessage.author_kind != "system")
    if not staff:
        q = q.where(TicketMessage.internal.is_(False))
    return {i: c for i, c in (await db.execute(q.group_by(TicketMessage.ticket_id))).all()}


async def messages(db: AsyncSession, tk: Ticket, *, staff: bool = True) -> list[dict[str, Any]]:
    q = select(TicketMessage).where(TicketMessage.ticket_id == tk.id).order_by(TicketMessage.created_at,
                                                                                TicketMessage.id)
    if not staff:
        q = q.where(TicketMessage.internal.is_(False))
    return [message_out(m, staff=staff) for m in (await db.execute(q)).scalars()]


def machine_out(n: Node) -> dict[str, Any]:
    """What a customer sees of a machine in the portal: no addresses or other details."""
    return {"id": n.id, "name": n.name, "kind": n.kind, "role": n.role,
            "type": "workstation" if n.role == "workstation" else "server", "online": agent_hub.is_online(n.id)}


async def customer_machines(db: AsyncSession, customer_id: str) -> list[Node]:
    return list((await db.execute(select(Node).where(Node.environment_id == customer_id)
                                  .order_by(Node.role, func.lower(Node.name)))).scalars())


# ------------------------------------------------------------------ lookup
async def find_ticket(db: AsyncSession, ref: str) -> Ticket:
    """A ticket by id, number or "#number"."""
    ref = (ref or "").strip()
    tk = await db.get(Ticket, ref) if re.fullmatch(r"[a-f0-9]{32}", ref) else None
    if tk is None and re.fullmatch(r"#?\d{1,9}", ref):
        tk = (await db.execute(select(Ticket).where(Ticket.number == int(ref.lstrip("#"))))).scalar_one_or_none()
    if tk is None:
        raise TicketError("ticket not found")
    return tk


async def search(db: AsyncSession, *, customer_id: str | None = None, status: str | None = None,
                 assignee_id: str | None = None, q: str | None = None, reporter_id: str | None = None,
                 limit: int = 500) -> list[Ticket]:
    stmt = select(Ticket)
    if customer_id:
        stmt = stmt.where(Ticket.customer_id == customer_id)
    if reporter_id:
        stmt = stmt.where(Ticket.reporter_id == reporter_id)
    if status == "active":
        stmt = stmt.where(Ticket.status.in_(OPEN_STATUSES))
    elif status:
        stmt = stmt.where(Ticket.status == status)
    if assignee_id == "none":
        stmt = stmt.where(Ticket.assignee_id.is_(None))
    elif assignee_id:
        stmt = stmt.where(Ticket.assignee_id == assignee_id)
    if q:
        like = f"%{q.strip()[:100].lower()}%"
        conds = [func.lower(Ticket.subject).like(like), func.lower(Ticket.reporter_name).like(like),
                 Ticket.id.in_(select(TicketMessage.ticket_id).where(func.lower(TicketMessage.body).like(like)))]
        if re.fullmatch(r"#?\d{1,9}", q.strip()):
            conds.append(Ticket.number == int(q.strip().lstrip("#")))
        stmt = stmt.where(or_(*conds))
    order = [Ticket.updated_at.desc()]
    return list((await db.execute(stmt.order_by(*order).limit(limit))).scalars())


# ------------------------------------------------------------------ machines
def _mentions(text: str, nodes: list[Node]) -> list[str]:
    """Machines whose name (at least 3 characters) appears as a word in the text."""
    found = []
    low = (text or "").lower()
    for n in nodes:
        name = (n.name or "").lower()
        if len(name) >= 3 and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", low):
            found.append(n.id)
    return found


async def clean_machines(db: AsyncSession, customer_id: str, node_ids: list[str] | None) -> list[str]:
    """Only machines of the ticket's customer; refused otherwise (never silently dropped)."""
    ids: list[str] = []
    for nid in node_ids or []:
        n = await db.get(Node, nid)
        if n is None:
            n = (await db.execute(select(Node).where(func.lower(Node.name) == str(nid).lower(),
                                                     Node.environment_id == customer_id))).scalars().first()
        if n is None or n.environment_id != customer_id:
            raise TicketError(f"machine {nid} not found at this customer")
        if n.id not in ids:
            ids.append(n.id)
    if len(ids) > MAX_MACHINES:
        raise TicketError(f"link at most {MAX_MACHINES} machines")
    return ids


# ------------------------------------------------------------------ changes
def _clean_text(v: str | None, field: str, max_len: int, required: bool = True) -> str:
    v = (v or "").strip()
    if required and not v:
        raise TicketError(f"{field} is required")
    if len(v) > max_len:
        raise TicketError(f"{field} is too long (max {max_len} characters)")
    return v


def _one_line(v: str) -> str:
    """Subjects end up in mail headers: no line breaks or control characters."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", v).strip()


def _check_choice(value: str, choices: tuple[str, ...], field: str) -> str:
    if value not in choices:
        raise TicketError(f"{field} must be one of: {', '.join(choices)}")
    return value


async def create_ticket(db: AsyncSession, *, customer_id: str, subject: str, body: str, category: str = "problem",
                        priority: str = "normal", node_ids: list[str] | None = None,
                        reporter: PortalUser | None = None, reporter_name: str | None = None,
                        reporter_email: str | None = None, by_user: User | None = None,
                        by_name: str | None = None) -> Ticket:
    env = await db.get(Environment, customer_id)
    if env is None:
        raise TicketError("customer not found")
    subject = _one_line(_clean_text(subject, "subject", 200))
    body = _clean_text(body, "description", 20000)
    _check_choice(category, CATEGORIES, "category")
    _check_choice(priority, PRIORITIES, "priority")
    ids = await clean_machines(db, customer_id, node_ids)
    if not ids:
        ids = _mentions(f"{subject}\n{body}", await customer_machines(db, customer_id))[:MAX_MACHINES]
    now = _now()
    tk = Ticket(customer_id=customer_id, subject=subject, category=category, priority=priority, status="new",
                node_ids=ids, reporter_id=reporter.id if reporter else None,
                reporter_name=(reporter.full_name or reporter.email) if reporter else (reporter_name or None),
                reporter_email=reporter.email if reporter else (reporter_email or None),
                created_by=by_name or (by_user.username if by_user else None), created_at=now, updated_at=now,
                last_customer_at=now if reporter else None, diagnosis={})
    db.add(tk)
    await db.flush()
    await db.refresh(tk, ["number"])
    if reporter:
        author_kind, author = "customer", reporter.full_name or reporter.email
    elif by_user:
        author_kind, author = "staff", by_user.full_name or by_user.username
    else:
        author_kind, author = "ai", by_name or "AI"
    db.add(TicketMessage(ticket_id=tk.id, author_kind=author_kind, author_name=author,
                         portal_user_id=reporter.id if reporter else None, user_id=by_user.id if by_user else None,
                         body=body, internal=False, created_at=now))
    await db.flush()
    return tk


async def add_message(db: AsyncSession, tk: Ticket, *, kind: str, body: str, internal: bool = False,
                      author_name: str | None = None, portal_user: PortalUser | None = None,
                      user: User | None = None, status: str | None = None) -> TicketMessage:
    """A reply or note. A customer's reply opens a ticket that waits for them or was resolved; the
    first public reply of staff or the AI moves a new ticket to open (or to ``status``)."""
    body = _clean_text(body, "message", 20000)
    if kind == "customer":
        if tk.status == "closed":
            raise TicketError("ticket_closed")
        internal = False
    now = _now()
    m = TicketMessage(ticket_id=tk.id, author_kind=kind, author_name=author_name,
                      portal_user_id=portal_user.id if portal_user else None, user_id=user.id if user else None,
                      body=body, internal=internal, created_at=now)
    db.add(m)
    tk.updated_at = now
    if kind == "customer":
        tk.last_customer_at = now
        if tk.status in ("waiting_customer", "resolved"):
            await set_status(db, tk, "open", by=author_name, note="customer replied")
    elif not internal:
        tk.last_staff_at = now
        tk.first_response_at = tk.first_response_at or now
        if status is None and tk.status == "new":
            status = "open"
    if status and status != tk.status:
        await set_status(db, tk, status, by=author_name)
    await db.flush()
    return m


async def set_status(db: AsyncSession, tk: Ticket, status: str, *, by: str | None = None,
                     note: str | None = None) -> bool:
    _check_choice(status, STATUSES, "status")
    if status == tk.status:
        return False
    old, now = tk.status, _now()
    tk.status, tk.updated_at = status, now
    if status == "resolved":
        tk.resolved_at = now
    elif status == "closed":
        tk.closed_at = now
        tk.resolved_at = tk.resolved_at or now
    else:
        tk.resolved_at = None
        tk.closed_at = None
    db.add(TicketMessage(ticket_id=tk.id, author_kind="system", author_name=by, body=f"{old} → {status}",
                         internal=False, event={"kind": "status", "from": old, "to": status, "by": by,
                                                "note": note}, created_at=now))
    return True


async def update_ticket(db: AsyncSession, tk: Ticket, *, by: str | None, status: str | None = None,
                        priority: str | None = None, assignee_id: str | None | bool = False,
                        node_ids: list[str] | None = None, subject: str | None = None,
                        category: str | None = None) -> dict[str, Any]:
    """Change fields; returns what changed (``assigned`` = the new assignee id, for the mail)."""
    changed: dict[str, Any] = {}
    now = _now()
    if subject is not None and subject.strip() != tk.subject:
        tk.subject = _one_line(_clean_text(subject, "subject", 200))
        changed["subject"] = tk.subject
    if category is not None and category != tk.category:
        tk.category = _check_choice(category, CATEGORIES, "category")
        changed["category"] = category
    if priority is not None and priority != tk.priority:
        old = tk.priority
        tk.priority = _check_choice(priority, PRIORITIES, "priority")
        db.add(TicketMessage(ticket_id=tk.id, author_kind="system", author_name=by, body=f"priority {old} → {priority}",
                             internal=True, event={"kind": "priority", "from": old, "to": priority, "by": by},
                             created_at=now))
        changed["priority"] = priority
    if assignee_id is not False and assignee_id != tk.assignee_id:
        if assignee_id:
            u = await db.get(User, assignee_id)
            if u is None or not u.is_active:
                raise TicketError("assignee not found")
        tk.assignee_id = assignee_id or None
        db.add(TicketMessage(ticket_id=tk.id, author_kind="system", author_name=by,
                             body=f"assigned to {assignee_id or 'nobody'}", internal=True,
                             event={"kind": "assign", "to": assignee_id or None, "by": by}, created_at=now))
        changed["assigned"] = assignee_id or None
    if node_ids is not None:
        ids = await clean_machines(db, tk.customer_id, node_ids)
        if ids != list(tk.node_ids or []):
            tk.node_ids = ids
            changed["machines"] = ids
    if status is not None and await set_status(db, tk, status, by=by):
        changed["status"] = status
    if changed:
        tk.updated_at = now
    await db.flush()
    return changed


# ------------------------------------------------------------------ first diagnosis
def _lines(text: Any, keep: int) -> list[str]:
    if isinstance(text, list):
        out = [str(x) for x in text if str(x).strip()]
    else:
        out = [ln for ln in str(text or "").splitlines() if ln.strip() and not ln.startswith("-- ")]
    return [ln[:400] for ln in out[-keep:]]


async def _diagnose_machine(n: Node) -> dict[str, Any]:
    from mmn.services import servers, workstations

    row: dict[str, Any] = {"node_id": n.id, "name": n.name, "kind": n.kind, "role": n.role,
                           "online": agent_hub.is_online(n.id), "findings": [], "failed_services": [],
                           "recent_errors": [], "top_cpu": []}
    if not row["online"]:
        seen = n.last_seen_at.isoformat() if n.last_seen_at else None
        row.update(status="offline", last_seen_at=seen)
        row["findings"] = ["The machine is offline" + (" (a workstation may be asleep)" if n.role == "workstation"
                                                       else "")]
        return row
    try:
        if n.role == "workstation":
            d = await asyncio.wait_for(workstations.diagnose(n, refresh=False), DIAG_TIMEOUT)
            health = d.get("health") or {}
            row["findings"] = [str(f) for f in d.get("findings") or []]
            row["failed_services"] = [s.get("name") for s in health.get("failed_services") or [] if isinstance(s, dict)]
            row["recent_errors"] = _lines(health.get("recent_errors"), 8)
            row["top_cpu"] = [p.get("name") for p in (health.get("top_cpu") or [])[:3] if isinstance(p, dict)]
        elif "diagnose" in (n.capabilities or []):
            d = await asyncio.wait_for(servers.diagnose(n.id), DIAG_TIMEOUT)
            row["findings"] = [str(f) for f in d.get("findings") or []]
            row["failed_services"] = [s.get("name") for s in d.get("failed_services") or [] if isinstance(s, dict)]
            row["recent_errors"] = _lines(d.get("recent_errors"), 8)
            row["top_cpu"] = [p.get("name") for p in (d.get("top_cpu") or [])[:3] if isinstance(p, dict)]
        else:
            res = (await asyncio.wait_for(servers.find_errors([n], since="2 hours ago", per_server=8),
                                          DIAG_TIMEOUT))[0]
            if res.get("error"):
                raise RuntimeError(res["error"])
            r = res.get("result") or {}
            row["findings"] = [str(f) for f in r.get("findings") or []]
            row["failed_services"] = [s for s in r.get("failed_services") or [] if s]
            row["recent_errors"] = _lines(r.get("recent_errors"), 8)
    except TimeoutError:
        row.update(status="error", error=f"no answer within {DIAG_TIMEOUT} seconds")
        return row
    except Exception as e:  # noqa: BLE001 - one machine must not stop the diagnosis
        row.update(status="error", error=str(e)[:300] or type(e).__name__)
        return row
    row["status"] = "issues" if row["findings"] or row["failed_services"] else "ok"
    return row


async def _scan_customer(nodes: list[Node]) -> list[dict[str, Any]]:
    """No machine linked: look for errors on every server of the customer (find_errors)."""
    from mmn.services import servers

    rows = await asyncio.wait_for(servers.find_errors(nodes, since="2 hours ago", per_server=5), DIAG_TIMEOUT)
    out = []
    for r in rows:
        res = r.get("result") or {}
        failed = [s for s in res.get("failed_services") or [] if s]
        findings = [str(f) for f in res.get("findings") or []]
        if r.get("error") or failed or findings or res.get("error_count"):
            out.append({"node_id": r["node_id"], "name": r["server"], "online": r["online"],
                        "error": r.get("error"), "failed_services": failed, "findings": findings,
                        "error_count": res.get("error_count") or 0, "recent_errors": _lines(res.get("recent_errors"), 3)})
    return out


def _summary(machines: list[dict[str, Any]], scan: list[dict[str, Any]] | None, alerts: list[dict[str, Any]],
             uptime: list[dict[str, Any]]) -> tuple[str, list[str], list[str]]:
    """verdict (issues_found | no_issues | unreachable), headline lines and suggested next steps."""
    lines: list[str] = []
    steps: list[str] = []
    for m in machines:
        if m.get("status") == "offline":
            lines.append(f"{m['name']}: offline")
            if m.get("role") != "workstation":
                steps.append(f"Check why {m['name']} is offline (power, network, agent service); recover_agent can "
                             f"restart the agent")
        elif m.get("status") == "error":
            lines.append(f"{m['name']}: could not be checked ({m.get('error')})")
        else:
            for f in m.get("findings") or []:
                lines.append(f"{m['name']}: {f}")
            if m.get("failed_services"):
                svc = ", ".join(m["failed_services"][:5])
                lines.append(f"{m['name']}: failed services: {svc}")
                steps.append(f"Look at the log of {svc} on {m['name']} (get_service_status) and restart it when safe "
                             f"(manage_service)")
            if any("disk" in f.lower() for f in m.get("findings") or []):
                steps.append(f"Free up disk space on {m['name']} (cleanup_disk)")
    for r in scan or []:
        what = []
        if r.get("failed_services"):
            what.append("failed services: " + ", ".join(r["failed_services"][:5]))
        what += r.get("findings") or []
        if r.get("error_count"):
            what.append(f"{r['error_count']} error lines in 2 hours")
        if r.get("error"):
            what.append(f"not checked ({r['error']})")
        lines.append(f"{r['name']}: " + "; ".join(what))
    for a in alerts:
        lines.append(f"Open alert ({a['severity']}): {a['message']}")
    for u in uptime:
        lines.append(f"Uptime check down: {u['name']} ({u['target']})" + (f": {u['error']}" if u.get("error") else ""))
    checked = [m for m in machines if m.get("status") in ("ok", "issues")]
    if machines and not checked:
        verdict = "unreachable"
    elif lines:
        verdict = "issues_found"
    else:
        verdict = "no_issues"
    if not steps and verdict == "no_issues":
        steps.append("No technical problem found on the linked machines; ask the customer for details or a screenshot")
    return verdict, lines[:40], list(dict.fromkeys(steps))[:10]


async def run_diagnosis(ticket_id: str) -> dict[str, Any] | None:
    """Run the first diagnosis of a ticket and store it. Runs in the background; never raises."""
    try:
        with tenancy.unrestricted():
            return await _run_diagnosis(ticket_id)
    except Exception:  # noqa: BLE001
        log.exception("ticket diagnosis failed")
        try:
            with tenancy.unrestricted():
                async with sessionmaker()() as db:
                    tk = await db.get(Ticket, ticket_id)
                    if tk is not None:
                        tk.diagnosis = {**(tk.diagnosis or {}), "status": "failed", "finished_at": _now().isoformat()}
                        await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not store the failed diagnosis")
        events.publish("ticket.updated", {"id": ticket_id})
        return None


async def _run_diagnosis(ticket_id: str) -> dict[str, Any] | None:
    started = _now()
    async with sessionmaker()() as db:
        tk = await db.get(Ticket, ticket_id)
        if tk is None:
            return None
        tk.diagnosis = {"status": "running", "started_at": started.isoformat()}
        await db.commit()
        all_nodes = await customer_machines(db, tk.customer_id)
        linked = [n for n in all_nodes if n.id in (tk.node_ids or [])]
        customer_id = tk.customer_id
    events.publish("ticket.updated", {"id": ticket_id})

    machines = list(await asyncio.gather(*(_diagnose_machine(n) for n in linked)))
    scan = None
    if not linked:
        servers_ = [n for n in all_nodes if n.role != "workstation"]
        scan = await _scan_customer(servers_) if servers_ else []
    node_ids = [n.id for n in (linked or all_nodes)]
    async with sessionmaker()() as db:
        alerts = [{"severity": a.severity, "message": a.message, "kind": a.kind, "since": a.started_at.isoformat()}
                  for a in (await db.execute(select(Alert).where(Alert.node_id.in_(node_ids), Alert.resolved_at.is_(None))
                                             .order_by(Alert.started_at.desc()).limit(20))).scalars()] if node_ids else []
        uptime = [{"name": c.name, "target": c.target, "error": c.last_error}
                  for c in (await db.execute(select(UptimeCheck).where(UptimeCheck.customer_id == customer_id,
                                                                       UptimeCheck.status == "down"))).scalars()]
        verdict, lines, steps = _summary(machines, scan, alerts, uptime)
        diag = {"status": "done", "started_at": started.isoformat(), "finished_at": _now().isoformat(),
                "verdict": verdict, "summary": lines, "next_steps": steps, "machines": machines,
                "scanned_all": not linked, "scan": scan, "alerts": alerts, "uptime_down": uptime}
        tk = await db.get(Ticket, ticket_id)
        if tk is None:
            return None
        tk.diagnosis = diag
        text = {"issues_found": "First diagnosis: problems found.", "no_issues": "First diagnosis: no problems found.",
                "unreachable": "First diagnosis: the machines could not be reached."}[verdict]
        if lines:
            text += "\n" + "\n".join(f"- {ln}" for ln in lines[:15])
        if steps:
            text += "\nSuggested next steps:\n" + "\n".join(f"- {s}" for s in steps)
        db.add(TicketMessage(ticket_id=ticket_id, author_kind="ai", author_name="Stackpilot", body=text,
                             internal=True, event={"kind": "diagnosis", "verdict": verdict}, created_at=_now()))
        await db.commit()
    events.publish("ticket.updated", {"id": ticket_id})
    return diag


def start_diagnosis(ticket_id: str) -> None:
    agent_hub.spawn(run_diagnosis(ticket_id))


# ------------------------------------------------------------------ mail
async def _staff_recipients(db: AsyncSession, tk: Ticket, cfg: dict[str, Any]) -> list[str]:
    explicit = [x.strip() for x in re.split(r"[,;\s]+", cfg.get("staff_to") or "") if x.strip()]
    if explicit:
        return explicit
    if tk.assignee_id:
        u = await db.get(User, tk.assignee_id)
        if u is not None and u.is_active and u.email:
            return [u.email]
    rows = (await db.execute(select(User.email).where(User.role == Role.admin, User.is_active.is_(True),
                                                      User.email.is_not(None)))).scalars()
    return [e for e in rows if e]


async def _customer_recipients(db: AsyncSession, tk: Ticket, exclude: str | None = None) -> list[tuple[str, str]]:
    """(email, language) of the reporter and the managers who follow every ticket of the customer."""
    out: dict[str, str] = {}
    if tk.reporter_id:
        u = await db.get(PortalUser, tk.reporter_id)
        if u is not None and u.is_active:
            out[u.email] = u.language
    elif tk.reporter_email:
        out[tk.reporter_email.lower()] = (await get_setting(db, "general"))["default_language"]
    for u in (await db.execute(select(PortalUser).where(PortalUser.customer_id == tk.customer_id,
                                                        PortalUser.is_active.is_(True),
                                                        PortalUser.notify_all.is_(True)))).scalars():
        out.setdefault(u.email, u.language)
    if exclude:
        out.pop(exclude.lower(), None)
    return list(out.items())


def _paragraphs(body: str, keep: int = 12) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    if len(parts) > keep:
        parts = parts[:keep] + ["…"]
    return parts


async def notify(ticket_id: str, event: str, *, base_url: str, message_id: str | None = None,
                 by: str | None = None, exclude: str | None = None) -> None:
    """Mail about a ticket. event: created (staff + confirmation to the customer), customer_reply
    (staff), staff_reply (customer), status (customer, for resolved and closed), assigned (assignee).
    Runs in the background; never raises."""
    try:
        with tenancy.unrestricted():
            async with sessionmaker()() as db:
                await _notify(db, ticket_id, event, base_url=base_url, message_id=message_id, by=by, exclude=exclude)
    except Exception:  # noqa: BLE001
        log.exception("ticket notification failed")


async def _notify(db: AsyncSession, ticket_id: str, event: str, *, base_url: str, message_id: str | None,
                  by: str | None, exclude: str | None) -> None:
    tk = await db.get(Ticket, ticket_id)
    if tk is None:
        return
    cfg = await get_setting(db, "portal")
    general = await get_setting(db, "general")
    app, lang = general["app_name"], general["default_language"]
    customer = tk.customer.name if tk.customer else "?"
    staff_url, portal_url = f"{base_url}/tickets/{tk.id}", f"{base_url}/portal/tickets/{tk.id}"
    msg = await db.get(TicketMessage, message_id) if message_id else None
    kw = {"number": tk.number, "subject": tk.subject, "customer": customer, "app": app}

    if event == "created":
        nm = await names(db)
        machines = ", ".join(nm["machines"].get(i, "?") for i in tk.node_ids or [])
        first = (await db.execute(select(TicketMessage).where(TicketMessage.ticket_id == tk.id)
                                  .order_by(TicketMessage.created_at).limit(1))).scalar_one_or_none()
        paras = [t(lang, "ticket.new_staff_body", reporter=tk.reporter_name or tk.created_by or "?",
                   priority=t(lang, f"ticket.priority_{tk.priority}"), **kw)]
        paras += _paragraphs(first.body if first else "")
        muted = []
        if machines:
            muted.append(t(lang, "ticket.machines", machines=machines))
        if cfg.get("auto_diagnose", True):
            muted.append(t(lang, "ticket.diagnosis_running"))
        to = await _staff_recipients(db, tk, cfg)
        if to:
            await try_send_mail(db, to, MailContent(
                subject=t(lang, "ticket.new_staff_subject", **kw), title=t(lang, "ticket.new_staff_title", **kw),
                paragraphs=paras, muted=muted, button_url=staff_url, button_label=t(lang, "ticket.open_button")), lang)
        if tk.reporter_id:
            for email, ulang in await _customer_recipients(db, tk):
                name = tk.reporter_name or email
                await try_send_mail(db, email, MailContent(
                    subject=t(ulang, "ticket.new_customer_subject", **kw),
                    title=t(ulang, "ticket.new_customer_title", **kw),
                    paragraphs=[t(ulang, "ticket.new_customer_body", name=name, **kw)], muted=[],
                    button_url=portal_url, button_label=t(ulang, "ticket.open_button")), ulang)
    elif event == "customer_reply" and msg is not None:
        to = await _staff_recipients(db, tk, cfg)
        if to:
            await try_send_mail(db, to, MailContent(
                subject=t(lang, "ticket.reply_staff_subject", **kw), title=t(lang, "ticket.reply_staff_title", **kw),
                paragraphs=[t(lang, "ticket.reply_staff_body", author=msg.author_name or "?", **kw),
                            *_paragraphs(msg.body)], muted=[], button_url=staff_url,
                button_label=t(lang, "ticket.open_button")), lang)
    elif event == "staff_reply" and msg is not None:
        for email, ulang in await _customer_recipients(db, tk, exclude=exclude):
            await try_send_mail(db, email, MailContent(
                subject=t(ulang, "ticket.reply_customer_subject", **kw),
                title=t(ulang, "ticket.reply_customer_title", **kw),
                paragraphs=[t(ulang, "ticket.reply_customer_body", author=msg.author_name or app, **kw),
                            *_paragraphs(msg.body)],
                muted=[t(ulang, "ticket.status_customer_body", status=t(ulang, f"ticket.status_{tk.status}"), **kw)],
                button_url=portal_url, button_label=t(ulang, "ticket.open_button")), ulang)
    elif event == "status" and tk.status in ("resolved", "closed", "waiting_customer"):
        for email, ulang in await _customer_recipients(db, tk, exclude=exclude):
            st = t(ulang, f"ticket.status_{tk.status}")
            await try_send_mail(db, email, MailContent(
                subject=t(ulang, "ticket.status_customer_subject", status=st, **kw),
                title=t(ulang, "ticket.status_customer_title", status=st, **kw),
                paragraphs=[t(ulang, "ticket.status_customer_body", status=st, **kw)],
                muted=[t(ulang, "ticket.resolved_hint")] if tk.status == "resolved" else [],
                button_url=portal_url, button_label=t(ulang, "ticket.open_button")), ulang)
    elif event == "assigned" and tk.assignee_id:
        u = await db.get(User, tk.assignee_id)
        if u is not None and u.is_active and u.email and u.username != by:
            ulang = u.language or lang
            await try_send_mail(db, u.email, MailContent(
                subject=t(ulang, "ticket.assigned_subject", **kw), title=t(ulang, "ticket.assigned_title", **kw),
                paragraphs=[t(ulang, "ticket.assigned_body", by=by or app, **kw)], muted=[],
                button_url=staff_url, button_label=t(ulang, "ticket.open_button")), ulang)


def notify_later(ticket_id: str, event: str, *, base_url: str, **kw: Any) -> None:
    agent_hub.spawn(notify(ticket_id, event, base_url=base_url, **kw))


# ------------------------------------------------------------------ portal accounts
async def issue_token(db: AsyncSession, u: PortalUser, purpose: str) -> str:
    token = new_token()
    life = timedelta(days=INVITE_DAYS) if purpose == "invite" else timedelta(minutes=60)
    db.add(PortalToken(id=hash_token(token), portal_user_id=u.id, purpose=purpose, expires_at=_now() + life))
    await db.flush()
    return token


async def send_invite(db: AsyncSession, u: PortalUser, base_url: str) -> bool:
    general = await get_setting(db, "general")
    token = await issue_token(db, u, "invite")
    u.invited_at = _now()
    await db.flush()
    lang, app = u.language, general["app_name"]
    customer = u.customer.name if u.customer else ""
    return await try_send_mail(db, u.email, MailContent(
        subject=t(lang, "ticket.invite_subject", app=app), title=t(lang, "ticket.invite_title"),
        paragraphs=[t(lang, "ticket.invite_body", name=u.full_name or u.email, customer=customer)], muted=[],
        button_url=f"{base_url}/portal/set-password?token={token}", button_label=t(lang, "ticket.invite_button")),
        lang)


async def send_reset(db: AsyncSession, u: PortalUser, base_url: str) -> bool:
    general = await get_setting(db, "general")
    token = await issue_token(db, u, "reset")
    lang, app = u.language, general["app_name"]
    return await try_send_mail(db, u.email, MailContent(
        subject=t(lang, "reset.subject", app=app), title=t(lang, "reset.title"),
        paragraphs=[t(lang, "ticket.reset_body", email=u.email)], muted=[t(lang, "reset.ignore")],
        button_url=f"{base_url}/portal/set-password?token={token}", button_label=t(lang, "reset.button")), lang)


# ------------------------------------------------------------------ housekeeping
async def close_stale() -> int:
    """Close tickets that were resolved ``auto_close_days`` ago without a reply of the customer."""
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            days = int((await get_setting(db, "portal")).get("auto_close_days") or 0)
            if days <= 0:
                return 0
            cutoff = _now() - timedelta(days=days)
            rows = list((await db.execute(select(Ticket).where(Ticket.status == "resolved",
                                                               Ticket.resolved_at < cutoff))).scalars())
            for tk in rows:
                await set_status(db, tk, "closed", by="Stackpilot", note=f"no reply in {days} days")
            await db.commit()
            return len(rows)


async def counts(db: AsyncSession) -> dict[str, int]:
    """For the menu badge: active tickets, new ones and those waiting for a reply of staff."""
    rows = (await db.execute(select(Ticket.status, func.count()).where(Ticket.status.in_(OPEN_STATUSES))
                             .group_by(Ticket.status))).all()
    by = {s: c for s, c in rows}
    waiting = (await db.execute(select(func.count()).select_from(Ticket).where(
        Ticket.status.in_(("new", "open")),
        or_(Ticket.last_staff_at.is_(None), Ticket.last_customer_at > Ticket.last_staff_at)))).scalar_one()
    return {"active": sum(by.values()), "new": by.get("new", 0), "needs_reply": waiting, **by}
