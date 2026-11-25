"""Uptime and SLA monitoring (phase 16): checks, history, incidents and the SLA per customer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, Node, UptimeCheck, User
from mmn.services import agent_hub, reports, uptime
from mmn.services.audit import audit

router = APIRouter(prefix="/api/uptime", tags=["uptime"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


async def _check(db: AsyncSession, cid: str) -> UptimeCheck:
    c = await db.get(UptimeCheck, cid)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return c


async def _env(db: AsyncSession, cid: str) -> Environment:
    e = await db.get(Environment, cid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return e


@router.get("/checks")
async def list_checks(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                      _: User = Depends(current_user)):
    q = select(UptimeCheck).order_by(UptimeCheck.name)
    if customer_id:
        q = q.where(UptimeCheck.customer_id == customer_id)
    rows = list((await db.execute(q)).scalars())
    names = await uptime.names(db)
    st = await uptime.stats(db, rows)
    counts: dict[str, int] = {}
    out = []
    for c in rows:
        o = uptime.check_out(c, names, st.get(c.id))
        counts[o["status"]] = counts.get(o["status"], 0) + 1
        out.append(o)
    return {"checks": out, "counts": counts, "kinds": list(uptime.KINDS), "record_types": list(uptime.RECORD_TYPES)}


@router.get("/probes")
async def probes(customer_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """Machines of a customer that can run checks (probe) or that a service can belong to."""
    rows = (await db.execute(select(Node).where(Node.environment_id == customer_id).order_by(Node.name))).scalars()
    return [{"id": n.id, "name": n.name, "kind": n.kind, "online": agent_hub.is_online(n.id)} for n in rows]


class CheckBody(BaseModel):
    customer_id: str
    name: str = Field(min_length=1, max_length=120)
    kind: str
    target: str = Field(min_length=1, max_length=500)
    port: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    probe_node_id: str | None = None
    node_id: str | None = None
    enabled: bool = True
    interval_seconds: int = 60
    timeout_seconds: int = 10
    fail_after: int = 2
    slow_ms: int | None = None
    cert_warn_days: int = 14
    severity: str = "critical"
    in_sla: bool = True


async def _save(db: AsyncSession, c: UptimeCheck | None, body: CheckBody, admin: User) -> dict[str, Any]:
    try:
        c = await uptime.save_check(db, c, by=admin.username, **body.model_dump())
    except uptime.UptimeError as e:
        raise _bad(e) from e
    await audit(db, "uptime.save", user=admin, target=c.name, kind=c.kind, check=uptime.label(c))
    await db.commit()
    return uptime.check_out(c, await uptime.names(db))


@router.post("/checks", status_code=201)
async def create_check(body: CheckBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    out = await _save(db, None, body, admin)
    if body.enabled:
        agent_hub.spawn(uptime.run_check(out["id"]))
    return out


@router.get("/checks/{cid}")
async def get_check(cid: str, range: str = "24h", db: AsyncSession = Depends(get_db),
                    _: User = Depends(current_user)):
    c = await _check(db, cid)
    try:
        hist = await uptime.history(db, c, range)
    except uptime.UptimeError as e:
        raise _bad(e) from e
    st = await uptime.stats(db, [c])
    return {**uptime.check_out(c, await uptime.names(db), st.get(c.id)), "history": hist}


@router.put("/checks/{cid}")
async def update_check(cid: str, body: CheckBody, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    return await _save(db, await _check(db, cid), body, admin)


@router.delete("/checks/{cid}", status_code=204)
async def delete_check(cid: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    c = await _check(db, cid)
    await audit(db, "uptime.delete", user=admin, target=c.name)
    await db.delete(c)
    await db.commit()


@router.post("/checks/{cid}/run")
async def run_now(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await _check(db, cid)
    res = await uptime.run_check(cid)
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return res


class TestBody(BaseModel):
    customer_id: str
    kind: str
    target: str = Field(min_length=1, max_length=500)
    port: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    probe_node_id: str | None = None
    timeout_seconds: int = 10


@router.post("/test")
async def test_check(body: TestBody, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Run a check once from the editor, without saving it."""
    await _env(db, body.customer_id)
    try:
        c = uptime.transient(body.customer_id, body.kind, body.target, body.port, body.config, body.timeout_seconds)
    except uptime.UptimeError as e:
        raise _bad(e) from e
    pn = None
    if body.probe_node_id:
        pn = await db.get(Node, body.probe_node_id)
        if pn is None or pn.environment_id != body.customer_id:
            raise _bad(uptime.UptimeError("probe machine not found at this customer"))
    return (await uptime.probe(c, pn)).out()


@router.get("/sla")
async def sla(period: str | None = None, customer_id: str | None = None, db: AsyncSession = Depends(get_db),
              _: User = Depends(current_user)):
    """The uptime SLA of every visible customer (or one) for a month (default: this month)."""
    tz = await reports.timezone(db)
    now = datetime.now(UTC)
    period = period or reports.current_period(now, tz)
    try:
        start, end = reports.month_bounds(period, tz)
    except Exception as e:  # noqa: BLE001
        raise _bad(e) from e
    q = select(Environment).order_by(Environment.name)
    if customer_id:
        q = q.where(Environment.id == customer_id)
    out = []
    for e in (await db.execute(q)).scalars():
        s = await uptime.customer_sla(db, e, start, end, now=now, with_incidents=bool(customer_id))
        s["settings"] = uptime.sla_of(e)
        out.append(s)
    return {"period": period, "current": reports.current_period(now, tz), "customers": out}


class SlaBody(BaseModel):
    target_pct: float | None = None
    exclude_maintenance: bool = True
    alert_breach: bool = True
    recipients: list[str] = Field(default_factory=list, max_length=20)


@router.put("/customers/{cid}/sla")
async def put_sla(cid: str, body: SlaBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    e = await _env(db, cid)
    try:
        cfg = uptime.save_sla(e, target_pct=body.target_pct, clear_target=body.target_pct is None,
                              exclude_maintenance=body.exclude_maintenance, alert_breach=body.alert_breach,
                              recipients=body.recipients)
    except uptime.UptimeError as ex:
        raise _bad(ex) from ex
    await audit(db, "uptime.sla", user=admin, target=e.name, **{k: v for k, v in cfg.items() if k != "recipients"})
    await db.commit()
    return cfg
