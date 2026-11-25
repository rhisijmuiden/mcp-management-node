"""Costs and licences per customer (phase 21): price list, cost items, resource use, monthly statements
and their CSV/PDF export."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import CostItem, Environment, User
from mmn.services import costs
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/costs", tags=["costs"])

PERIOD = r"^\d{4}-(0[1-9]|1[0-2])$"


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _missing() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")


async def _cust(db: AsyncSession, cid: str) -> Environment:
    e = await db.get(Environment, cid)
    if e is None:
        raise _missing()
    return e


async def _period(db: AsyncSession, period: str | None) -> str:
    if period:
        try:
            costs.parse_period(period)
        except costs.CostError as e:
            raise _bad(e) from e
        return period
    from mmn.services import reports

    return reports.current_period(datetime.now(UTC), await costs.tz_of(db))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "customer"


# ------------------------------------------------------------------ overview and statements
@router.get("/overview")
async def overview(period: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    p = await _period(db, period)
    from mmn.services import reports

    tz = await costs.tz_of(db)
    now = datetime.now(UTC)
    return {**await costs.overview(db, p), "current_period": reports.current_period(now, tz),
            "previous_period": reports.previous_period(now, tz)}


@router.get("/badge")
async def badge(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """For the menu: items that ran out or must be cancelled or renewed soon."""
    items = await costs.expiring(db)
    return {"attention": sum(1 for x in items if x["status"] in ("expired", "notice", "expiring"))}


@router.get("/statement/{cid}")
async def get_statement(cid: str, period: str | None = None, db: AsyncSession = Depends(get_db),
                        _: User = Depends(current_user)):
    e = await _cust(db, cid)
    p = await _period(db, period)
    return await costs.statement(db, e, p)


class FinalizeIn(BaseModel):
    period: str = Field(pattern=PERIOD)
    note: str | None = Field(default=None, max_length=500)


@router.post("/statement/{cid}/finalize")
async def finalize(cid: str, body: FinalizeIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    try:
        s = await costs.finalize(db, e, body.period, by=admin.username, note=body.note)
    except costs.CostError as ex:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(ex)) from ex
    await audit(db, "costs.finalize", user=admin, target=e.name, ip=client_ip(request), period=body.period,
                net=(s.data.get("totals") or {}).get("net"))
    await db.commit()
    return await costs.statement(db, e, body.period)


@router.delete("/statement/{cid}/{period}")
async def reopen(cid: str, period: str, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    p = await _period(db, period)
    if not await costs.reopen(db, e, p):
        raise _missing()
    await audit(db, "costs.reopen", user=admin, target=e.name, ip=client_ip(request), period=p)
    await db.commit()
    return await costs.statement(db, e, p)


@router.get("/statement/{cid}/export")
async def export_statement(cid: str, period: str | None = None, format: Literal["csv", "pdf"] = "pdf",
                           lang: Literal["en", "nl"] = "en", db: AsyncSession = Depends(get_db),
                           _: User = Depends(current_user)):
    e = await _cust(db, cid)
    p = await _period(db, period)
    s = await costs.statement(db, e, p)
    name = f"costs-{_safe(e.name)}-{p}.{format}"
    if format == "csv":
        return Response(costs.to_csv([s], lang), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    general = await get_setting(db, "general")
    pdf = costs.render_pdf(s, lang, await costs.tz_of(db), general.get("app_name") or "Stackpilot")
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export")
async def export_all(period: str | None = None, lang: Literal["en", "nl"] = "en", billable_only: bool = True,
                     db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """Every customer's statement of a month in one CSV (one row per line), for the bookkeeping."""
    from sqlalchemy import select

    p = await _period(db, period)
    out = []
    for e in (await db.execute(select(Environment).order_by(Environment.name))).scalars():
        s = await costs.statement(db, e, p)
        if billable_only and not s.get("billable", True):
            continue
        out.append(s)
    return Response(costs.to_csv(out, lang), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="costs-{p}.csv"'})


# ------------------------------------------------------------------ usage
@router.get("/usage/{cid}")
async def usage(cid: str, period: str | None = None, db: AsyncSession = Depends(get_db),
                _: User = Depends(current_user)):
    e = await _cust(db, cid)
    p = await _period(db, period)
    from sqlalchemy import select

    from mmn.models import CostUsage

    y, m = costs.parse_period(p)
    first, last = costs.month_days(y, m)
    days = [{"day": r.day, "cpu": r.cpu, "ram_gb": float(r.ram_gb or 0), "storage_gb": float(r.storage_gb or 0),
             "guests": r.guests, "devices": r.devices or {}, "m365_users": r.m365_users}
            for r in (await db.execute(select(CostUsage).where(CostUsage.customer_id == e.id, CostUsage.day >= first,
                                                               CostUsage.day <= last).order_by(CostUsage.day))).scalars()]
    return {"period": p, "live": await costs.live_usage(db, e), "month": await costs.period_usage(db, e, p),
            "days": days}


# ------------------------------------------------------------------ cost items
@router.get("/items")
async def list_items(customer_id: str | None = None, kind: str | None = None, status_: str | None = Query(None, alias="status"),
                     within_days: int | None = Query(None, ge=0, le=3650), db: AsyncSession = Depends(get_db),
                     _: User = Depends(current_user)):
    return await costs.list_items(db, customer_id, kind=kind, status=status_, within_days=within_days)


class ItemIn(BaseModel):
    customer_id: str
    kind: Literal["licence", "contract", "hardware", "service", "other"] = "licence"
    name: str = Field(min_length=1, max_length=200)
    vendor: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=200)
    serial: str | None = Field(default=None, max_length=120)
    node_id: str | None = None
    quantity: float = Field(default=1, ge=0, le=1_000_000)
    unit_price: float | None = Field(default=None, ge=0, le=100_000_000)
    cost_price: float | None = Field(default=None, ge=0, le=100_000_000)
    billing: Literal["monthly", "quarterly", "yearly", "once", "none"] = "monthly"
    start_date: date | None = None
    end_date: date | None = None
    auto_renew: bool = False
    renew_months: int | None = Field(default=None, ge=1, le=120)
    notice_days: int | None = Field(default=None, ge=0, le=730)
    warn_days: int | None = Field(default=None, ge=0, le=730)
    active: bool = True
    notes: str | None = Field(default=None, max_length=4000)


async def _item_out(db: AsyncSession, it: CostItem) -> dict[str, Any]:
    cfg = await costs.settings(db)
    return costs.item_out(it, await costs.today(db), int(cfg["warn_days"]))


@router.post("/items", status_code=201)
async def create_item(body: ItemIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    e = await _cust(db, body.customer_id)
    fields = body.model_dump(exclude={"customer_id"})
    try:
        it = await costs.save_item(db, None, customer_id=e.id, by=admin.username, **fields)
    except costs.CostError as ex:
        raise _bad(ex) from ex
    await audit(db, "costs.item_save", user=admin, target=f"{e.name} · {it.name}", ip=client_ip(request), kind=it.kind)
    await db.commit()
    return await _item_out(db, it)


@router.put("/items/{iid}")
async def update_item(iid: str, body: ItemIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    it = await db.get(CostItem, iid)
    if it is None:
        raise _missing()
    e = await _cust(db, it.customer_id)
    try:
        it = await costs.save_item(db, it, customer_id=e.id, by=admin.username, **body.model_dump(exclude={"customer_id"}))
    except costs.CostError as ex:
        raise _bad(ex) from ex
    await audit(db, "costs.item_save", user=admin, target=f"{e.name} · {it.name}", ip=client_ip(request), kind=it.kind)
    await db.commit()
    return await _item_out(db, it)


@router.delete("/items/{iid}", status_code=204)
async def delete_item(iid: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    it = await db.get(CostItem, iid)
    if it is None:
        raise _missing()
    e = await _cust(db, it.customer_id)
    await audit(db, "costs.item_delete", user=admin, target=f"{e.name} · {it.name}", ip=client_ip(request))
    await db.delete(it)
    await db.commit()
    return Response(status_code=204)


class ImportIn(BaseModel):
    customer_id: str


@router.post("/items/import-hardware")
async def import_hardware(body: ImportIn, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    e = await _cust(db, body.customer_id)
    made = await costs.import_hardware(db, e, by=admin.username)
    await audit(db, "costs.import_hardware", user=admin, target=e.name, ip=client_ip(request), count=len(made))
    await db.commit()
    return {"created": len(made), "items": [await _item_out(db, x) for x in made]}


# ------------------------------------------------------------------ price list
@router.get("/rates")
async def list_rates(customer_id: str | None = None, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await costs.list_rates(db, customer_id)


class RateIn(BaseModel):
    customer_id: str | None = None
    key: str = Field(min_length=3, max_length=120)
    name: str | None = Field(default=None, max_length=200)
    unit: str | None = Field(default=None, max_length=32)
    price: float = Field(ge=0, le=100_000_000)
    cost: float | None = Field(default=None, ge=0, le=100_000_000)


@router.put("/rates")
async def save_rate(body: RateIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    target = "default"
    if body.customer_id:
        target = (await _cust(db, body.customer_id)).name
    try:
        r = await costs.save_rate(db, key=body.key, price=body.price, customer_id=body.customer_id, name=body.name,
                                  unit=body.unit, cost=body.cost, by=admin.username)
    except costs.CostError as ex:
        raise _bad(ex) from ex
    await audit(db, "costs.rate_save", user=admin, target=f"{target} · {r.key}", ip=client_ip(request), price=body.price)
    await db.commit()
    return costs.rate_out(r)


@router.delete("/rates/{rid}", status_code=204)
async def delete_rate(rid: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    from mmn.models import CostRate

    r = await db.get(CostRate, rid)
    if r is None:
        raise _missing()
    await audit(db, "costs.rate_delete", user=admin, target=r.key, ip=client_ip(request), customer_id=r.customer_id)
    await db.delete(r)
    await db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ settings
@router.get("/customers/{cid}/settings")
async def get_customer_settings(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return costs.customer_settings(await _cust(db, cid))


class CustomerSettingsIn(BaseModel):
    billable: bool = True
    discount_pct: float = Field(default=0, ge=0, le=100)
    debtor: str = Field(default="", max_length=100)
    bill_resources: bool = True
    bill_devices: bool = True
    bill_m365: bool = True
    note: str = Field(default="", max_length=500)


@router.put("/customers/{cid}/settings")
async def put_customer_settings(cid: str, body: CustomerSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                                admin: User = Depends(require_admin)):
    e = await _cust(db, cid)
    e.costs = {**(e.costs or {}), **body.model_dump()}
    await audit(db, "costs.customer_settings", user=admin, target=e.name, ip=client_ip(request),
                billable=body.billable, discount_pct=body.discount_pct)
    await db.commit()
    return costs.customer_settings(e)


class SettingsIn(BaseModel):
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    vat_pct: float = Field(default=21, ge=0, le=100)
    warn_days: int = Field(default=60, ge=1, le=365)
    alert_expiring: bool = True
    auto_renew: bool = True
    usage_basis: Literal["average", "peak"] = "average"
    include_stopped: bool = True
    m365_quantity: Literal["enabled", "consumed"] = "enabled"


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)) -> dict[str, Any]:
    return await get_setting(db, "costs")


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)) -> dict[str, Any]:
    cur = await get_setting(db, "costs")
    out = await set_setting(db, "costs", {**cur, **body.model_dump()})
    await audit(db, "costs.settings", user=admin, ip=client_ip(request))
    await db.commit()
    return out


@router.post("/sample", status_code=202)
async def sample_now(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """Write today's usage of every customer now (normally every hour)."""
    await audit(db, "costs.sample", user=admin, ip=client_ip(request))
    await db.commit()
    n = await costs.tick()
    return {"customers": n}
