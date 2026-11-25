"""Costs and licences per customer (phase 21): the basis for invoicing.

What a customer costs is made of four parts:

* the **price list** (``CostRate``): a monthly price per vCPU, GB of memory and GB of disk given to the
  customer's VMs and containers, per machine with the agent (server, workstation, Proxmox VE node), per
  licensed Microsoft 365 user and per Microsoft 365 licence (SKU). A customer can have its own price for any
  of these; a key without a price is not billed (Microsoft 365 licences are listed with "no price" so they
  are not forgotten);
* **resource use** (``CostUsage``): every hour the scheduler writes what each customer uses today (the last
  reading of the day stays). A month bills the average of its days (or the peak, a setting);
* **cost items** (``CostItem``): software licences, contracts, hardware with its warranty, recurring services
  and one-off charges, each with a sale price per billing period (monthly, quarterly, yearly, once) and
  optionally what it costs you, so the margin is known;
* the Microsoft 365 licences that phase 19 reads from every tenant (bought and assigned per SKU).

A **statement** is one customer's month: the lines, the discount, VAT and totals. It is worked out live
until it is made final; a final statement is frozen, so the invoice made from it does not change. It exports
as CSV (one line per row, for the bookkeeping) and as a PDF in the house style, and can be part of the
monthly customer report.

Items with an end date (contract end, licence expiry, end of warranty) are warned about before they run out
and before the date to give notice; with automatic renewal the end date moves on by itself.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import CostItem, CostRate, CostStatement, CostUsage, Environment, M365Tenant, Node
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.costs")

KINDS = ("licence", "contract", "hardware", "service", "other")
BILLINGS = ("monthly", "quarterly", "yearly", "once", "none")
STATUSES = ("ok", "notice", "expiring", "expired", "renews", "none", "inactive")
GROUPS = ("resources", "devices", "m365", "licence", "contract", "service", "hardware", "other")
PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
KEY_RE = re.compile(r"^(resource\.(cpu|ram|storage)|device\.(server|workstation|proxmox)|m365\.user|"
                    r"m365\.sku\.[A-Za-z0-9_\-.]{1,80})$")
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}

# The standard keys of the price list, with their unit.
STANDARD_KEYS: dict[str, str] = {
    "resource.cpu": "vcpu", "resource.ram": "gb", "resource.storage": "gb",
    "device.server": "device", "device.workstation": "device", "device.proxmox": "device",
    "m365.user": "user",
}

CUSTOMER_DEFAULTS: dict[str, Any] = {
    "billable": True,  # the "home" customer defaults to not billable (see customer_settings)
    "discount_pct": 0,
    "debtor": "",  # debtor or customer number in the bookkeeping, goes into the CSV export
    "bill_resources": True,
    "bill_devices": True,
    "bill_m365": True,
    "note": "",  # printed on the statement
}

# Texts of the statement (PDF, CSV, report section). Item lines carry their own name.
TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Cost statement", "customer": "Customer", "period": "Period", "debtor": "Debtor",
        "generated": "Generated", "status_final": "Final", "status_draft": "Draft (not final yet)",
        "description": "Description", "quantity": "Quantity", "unit_price": "Price", "amount": "Amount",
        "subtotal": "Subtotal", "discount": "Discount ({pct}%)", "net": "Total excl. VAT", "vat": "VAT ({pct}%)",
        "total": "Total incl. VAT", "no_price": "no price", "none": "Nothing to charge this month.",
        "usage": "Resource use", "usage_line": "{basis} of {days} day(s): {cpu} vCPU, {ram} GB memory, {storage} GB disk "
                                            "in {guests} VM(s) and container(s).",
        "usage_estimated": "Current use (no daily readings for this month yet).",
        "basis_average": "Average", "basis_peak": "Peak",
        "g_resources": "Resources", "g_devices": "Managed machines", "g_m365": "Microsoft 365",
        "g_licence": "Licences", "g_contract": "Contracts", "g_service": "Services", "g_hardware": "Hardware",
        "g_other": "Other",
        "resource.cpu": "vCPU", "resource.ram": "Memory (GB)", "resource.storage": "Disk space (GB)",
        "device.server": "Managed server", "device.workstation": "Managed workstation",
        "device.proxmox": "Proxmox VE node", "m365.user": "Microsoft 365 user (licensed)",
        "u_vcpu": "vCPU", "u_gb": "GB", "u_device": "pcs", "u_user": "user(s)", "u_licence": "licence(s)",
        "b_monthly": "per month", "b_quarterly": "per quarter", "b_yearly": "per year", "b_once": "once",
        "b_none": "not billed",
        "until": "until {date}", "footer": "Generated by {app}. Prices excl. VAT unless stated otherwise.",
        "costs_section": "Costs this month", "costs_line": "Total for {period}: {net} excl. VAT ({total} incl. VAT).",
        "expiring": "Expiring contracts and licences", "item": "Item", "ends": "Ends", "notice_by": "Give notice by",
        "kind": "Type", "k_licence": "Licence", "k_contract": "Contract", "k_hardware": "Hardware",
        "k_service": "Service", "k_other": "Other",
    },
    "nl": {
        "title": "Kostenoverzicht", "customer": "Klant", "period": "Periode", "debtor": "Debiteur",
        "generated": "Gemaakt", "status_final": "Definitief", "status_draft": "Concept (nog niet definitief)",
        "description": "Omschrijving", "quantity": "Aantal", "unit_price": "Prijs", "amount": "Bedrag",
        "subtotal": "Subtotaal", "discount": "Korting ({pct}%)", "net": "Totaal excl. btw", "vat": "Btw ({pct}%)",
        "total": "Totaal incl. btw", "no_price": "geen prijs", "none": "Deze maand valt er niets te factureren.",
        "usage": "Resourcegebruik", "usage_line": "{basis} van {days} dag(en): {cpu} vCPU, {ram} GB geheugen, "
                                                "{storage} GB schijf in {guests} VM('s) en container(s).",
        "usage_estimated": "Huidig gebruik (nog geen dagmetingen voor deze maand).",
        "basis_average": "Gemiddelde", "basis_peak": "Piek",
        "g_resources": "Resources", "g_devices": "Beheerde machines", "g_m365": "Microsoft 365",
        "g_licence": "Licenties", "g_contract": "Contracten", "g_service": "Diensten", "g_hardware": "Hardware",
        "g_other": "Overig",
        "resource.cpu": "vCPU", "resource.ram": "Geheugen (GB)", "resource.storage": "Schijfruimte (GB)",
        "device.server": "Beheerde server", "device.workstation": "Beheerde werkplek",
        "device.proxmox": "Proxmox VE-node", "m365.user": "Microsoft 365-gebruiker (met licentie)",
        "u_vcpu": "vCPU", "u_gb": "GB", "u_device": "st.", "u_user": "gebruiker(s)", "u_licence": "licentie(s)",
        "b_monthly": "per maand", "b_quarterly": "per kwartaal", "b_yearly": "per jaar", "b_once": "eenmalig",
        "b_none": "niet gefactureerd",
        "until": "tot {date}", "footer": "Gemaakt door {app}. Prijzen excl. btw tenzij anders vermeld.",
        "costs_section": "Kosten deze maand", "costs_line": "Totaal over {period}: {net} excl. btw ({total} incl. btw).",
        "expiring": "Aflopende contracten en licenties", "item": "Onderdeel", "ends": "Loopt af", "notice_by": "Opzeggen vóór",
        "kind": "Soort", "k_licence": "Licentie", "k_contract": "Contract", "k_hardware": "Hardware",
        "k_service": "Dienst", "k_other": "Overig",
    },
}


class CostError(Exception):
    pass


# ------------------------------------------------------------------ small helpers
CENT = Decimal("0.01")


def dec(v: Any, field: str = "value") -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", ".").strip())
    except (InvalidOperation, ValueError) as e:
        raise CostError(f"{field} is not a number") from e
    if not d.is_finite():
        raise CostError(f"{field} is not a number")
    return d


def cents(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def num(d: Any) -> float | None:
    return None if d is None else float(d)


def texts(lang: str) -> dict[str, str]:
    return TEXT.get(lang, TEXT["en"])


def money(v: Any, currency: str = "EUR", lang: str = "en") -> str:
    if v is None:
        return "–"
    d = cents(Decimal(str(v)))
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    groups = f"{int(whole):,}"
    sym = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency, currency + " ")
    if lang == "nl":
        return f"{sign}{sym} {groups.replace(',', '.')},{frac}"
    return f"{sign}{sym}{groups}.{frac}"


def qty_str(v: Any, lang: str = "en") -> str:
    if v is None:
        return "–"
    d = Decimal(str(v))
    s = f"{d.normalize():f}" if d == d.to_integral() else f"{cents(d)}"
    return s.replace(".", ",") if lang == "nl" else s


def parse_period(period: str) -> tuple[int, int]:
    m = PERIOD_RE.match(period or "")
    if not m:
        raise CostError("period must look like 2026-11")
    return int(m.group(1)), int(m.group(2))


def month_days(y: int, m: int) -> tuple[date, date]:
    first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return first, nxt - timedelta(days=1)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = month_days(y, m)[1].day
    return date(y, m, min(d.day, last))


async def tz_of(db: AsyncSession) -> ZoneInfo:
    from mmn.services import reports

    return await reports.timezone(db)


async def today(db: AsyncSession, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(await tz_of(db)).date()


async def settings(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "costs")


def customer_settings(e: Environment) -> dict[str, Any]:
    base = {**CUSTOMER_DEFAULTS, "billable": e.kind != "home"}
    return {**base, **{k: v for k, v in (e.costs or {}).items() if k in CUSTOMER_DEFAULTS}}


# ------------------------------------------------------------------ price list
def rate_out(r: CostRate) -> dict[str, Any]:
    return {"id": r.id, "customer_id": r.customer_id, "key": r.key, "name": r.name,
            "unit": r.unit or STANDARD_KEYS.get(r.key) or ("licence" if r.key.startswith("m365.sku.") else None),
            "price": num(r.price), "cost": num(r.cost), "updated_at": r.updated_at, "updated_by": r.updated_by}


async def rates_for(db: AsyncSession, customer_id: str | None) -> dict[str, CostRate]:
    """The price per key for a customer: its own price, else the default one."""
    q = select(CostRate).where(or_(CostRate.customer_id.is_(None), CostRate.customer_id == customer_id)) \
        if customer_id else select(CostRate).where(CostRate.customer_id.is_(None))
    out: dict[str, CostRate] = {}
    for r in (await db.execute(q)).scalars():
        if r.key not in out or r.customer_id is not None:
            out[r.key] = r
    return out


async def list_rates(db: AsyncSession, customer_id: str | None = None) -> dict[str, Any]:
    """The default price list, and with ``customer_id`` that customer's own prices next to it, plus the
    Microsoft 365 licences seen at the customers that have no price yet."""
    defaults = (await db.execute(select(CostRate).where(CostRate.customer_id.is_(None)).order_by(CostRate.key))).scalars().all()
    own = []
    if customer_id:
        own = (await db.execute(select(CostRate).where(CostRate.customer_id == customer_id)
                                .order_by(CostRate.key))).scalars().all()
    priced = {r.key for r in defaults} | {r.key for r in own}
    skus: dict[str, str] = {}
    q = select(M365Tenant)
    if customer_id:
        q = q.where(M365Tenant.customer_id == customer_id)
    for t in (await db.execute(q)).scalars():
        for lic in (t.snapshot or {}).get("licences") or []:
            if lic.get("sku") and not lic.get("free"):
                skus.setdefault(f"m365.sku.{lic['sku']}", lic.get("name") or lic["sku"])
    return {"defaults": [rate_out(r) for r in defaults], "customer": [rate_out(r) for r in own],
            "standard_keys": list(STANDARD_KEYS), "sku_names": skus,
            "unpriced_skus": [{"key": k, "name": v} for k, v in sorted(skus.items()) if k not in priced]}


async def save_rate(db: AsyncSession, *, key: str, price: Any, customer_id: str | None = None, name: str | None = None,
                    unit: str | None = None, cost: Any = None, by: str | None = None) -> CostRate:
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise CostError("key must be resource.cpu, resource.ram, resource.storage, device.server, device.workstation, "
                        "device.proxmox, m365.user or m365.sku.<SKU>")
    p = dec(price, "price")
    if p is None:
        raise CostError("give a price")
    c = dec(cost, "cost")
    if p < 0 or (c is not None and c < 0):
        raise CostError("prices cannot be negative")
    q = select(CostRate).where(CostRate.key == key,
                               CostRate.customer_id == customer_id if customer_id else CostRate.customer_id.is_(None))
    r = (await db.execute(q)).scalar_one_or_none()
    if r is None:
        r = CostRate(key=key, customer_id=customer_id)
        db.add(r)
    r.price, r.cost = p, c
    r.name = (name or "").strip()[:200] or None
    r.unit = (unit or "").strip()[:32] or None
    r.updated_at, r.updated_by = datetime.now(UTC), by
    await db.flush()
    return r


async def find_rate(db: AsyncSession, key: str, customer_id: str | None) -> CostRate:
    q = select(CostRate).where(CostRate.key == key,
                               CostRate.customer_id == customer_id if customer_id else CostRate.customer_id.is_(None))
    r = (await db.execute(q)).scalar_one_or_none()
    if r is None:
        raise CostError("no price for this key")
    return r


# ------------------------------------------------------------------ cost items
def monthly_equivalent(it: CostItem) -> Decimal | None:
    """What a recurring item is worth per month (yearly / 12); one-off and untracked items: None."""
    if it.unit_price is None or it.billing not in CYCLE_MONTHS:
        return None
    return Decimal(it.quantity or 0) * Decimal(it.unit_price) / CYCLE_MONTHS[it.billing]


def status_of(it: CostItem, day: date, warn_default: int) -> dict[str, Any]:
    """Where an item with an end date stands: ok, notice (give notice now if you want to stop), renews
    (the notice date passed and it renews by itself), expiring, expired; none without an end date."""
    if not it.active:
        return {"status": "inactive", "days_left": None, "notice_by": None, "notice_days_left": None}
    if it.end_date is None:
        return {"status": "none", "days_left": None, "notice_by": None, "notice_days_left": None}
    warn = int(it.warn_days if it.warn_days is not None else warn_default)
    left = (it.end_date - day).days
    notice_by = it.end_date - timedelta(days=int(it.notice_days)) if it.notice_days else None
    notice_left = (notice_by - day).days if notice_by else None
    if left < 0:
        st = "expired"
    elif notice_left is not None and 0 <= notice_left <= warn:
        st = "notice"
    elif notice_left is not None and notice_left < 0 and it.auto_renew:
        st = "renews"
    elif left <= warn:
        st = "renews" if it.auto_renew and it.renew_months else "expiring"
    else:
        st = "ok"
    return {"status": st, "days_left": left, "notice_by": notice_by, "notice_days_left": notice_left}


def item_out(it: CostItem, day: date, warn_default: int, *, customer_name: str | None = None,
             node_name: str | None = None) -> dict[str, Any]:
    mrr = monthly_equivalent(it)
    return {
        "id": it.id, "customer_id": it.customer_id, "customer_name": customer_name, "kind": it.kind, "name": it.name,
        "vendor": it.vendor, "reference": it.reference, "serial": it.serial, "node_id": it.node_id, "node_name": node_name,
        "quantity": num(it.quantity), "unit_price": num(it.unit_price), "cost_price": num(it.cost_price),
        "billing": it.billing, "start_date": it.start_date, "end_date": it.end_date, "auto_renew": it.auto_renew,
        "renew_months": it.renew_months, "notice_days": it.notice_days, "warn_days": it.warn_days, "active": it.active,
        "notes": it.notes, "history": it.history or [], "monthly": num(cents(mrr)) if mrr is not None else None,
        "created_at": it.created_at, "created_by": it.created_by, "updated_at": it.updated_at, "updated_by": it.updated_by,
        **status_of(it, day, warn_default),
    }


ITEM_FIELDS = ("kind", "name", "vendor", "reference", "serial", "node_id", "quantity", "unit_price", "cost_price",
               "billing", "start_date", "end_date", "auto_renew", "renew_months", "notice_days", "warn_days", "active",
               "notes")


def _date(v: Any, field: str) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError as e:
        raise CostError(f"{field} must be a date like 2027-01-31") from e


async def save_item(db: AsyncSession, it: CostItem | None, *, customer_id: str, by: str | None = None,
                    **fields: Any) -> CostItem:
    """Create (``it`` None) or change a cost item. Only the fields given are changed."""
    unknown = set(fields) - set(ITEM_FIELDS)
    if unknown:
        raise CostError(f"unknown field(s): {', '.join(sorted(unknown))}")
    new = it is None
    if new:
        it = CostItem(customer_id=customer_id, created_by=by, history=[])
    v = dict(fields)
    if "kind" in v and v["kind"] not in KINDS:
        raise CostError(f"kind must be one of {', '.join(KINDS)}")
    if "billing" in v and v["billing"] not in BILLINGS:
        raise CostError(f"billing must be one of {', '.join(BILLINGS)}")
    if "name" in v:
        v["name"] = (v["name"] or "").strip()[:200]
        if not v["name"]:
            raise CostError("give a name")
    elif new:
        raise CostError("give a name")
    for k in ("vendor", "reference", "serial"):
        if k in v:
            v[k] = (v[k] or "").strip()[:200 if k != "serial" else 120] or None
    if "notes" in v:
        v["notes"] = (v["notes"] or "").strip()[:4000] or None
    for k in ("quantity", "unit_price", "cost_price"):
        if k in v:
            v[k] = dec(v[k], k)
            if v[k] is not None and v[k] < 0:
                raise CostError(f"{k} cannot be negative")
    if "quantity" in v and v["quantity"] is None:
        v["quantity"] = Decimal(1)
    for k in ("start_date", "end_date"):
        if k in v:
            v[k] = _date(v[k], k)
    for k, hi in (("renew_months", 120), ("notice_days", 730), ("warn_days", 730)):
        if k in v and v[k] is not None:
            try:
                v[k] = int(v[k])
            except (TypeError, ValueError) as e:
                raise CostError(f"{k} must be a whole number") from e
            if not 0 <= v[k] <= hi:
                raise CostError(f"{k} must be between 0 and {hi}")
    if v.get("node_id"):
        n = await db.get(Node, v["node_id"])
        if n is None or n.environment_id != customer_id:
            raise CostError("that machine is not one of this customer's")
    elif "node_id" in v:
        v["node_id"] = None
    for k, val in v.items():
        setattr(it, k, val)
    if it.start_date and it.end_date and it.end_date < it.start_date:
        raise CostError("the end date is before the start date")
    if it.auto_renew and not it.renew_months:
        it.renew_months = 12
    if it.billing == "once" and it.start_date is None:
        raise CostError("a one-off charge needs a start date (the month it is charged)")
    it.updated_at, it.updated_by = datetime.now(UTC), by
    if new:
        db.add(it)
    await db.flush()
    return it


async def find_item(db: AsyncSession, ref: str, customer_id: str | None = None) -> CostItem:
    it = await db.get(CostItem, ref)
    if it is None:
        q = select(CostItem).where(CostItem.name.ilike(ref.strip()))
        if customer_id:
            q = q.where(CostItem.customer_id == customer_id)
        hits = (await db.execute(q)).scalars().all()
        if len(hits) > 1:
            raise CostError("more than one item has this name: give its id")
        it = hits[0] if hits else None
    if it is None or (customer_id and it.customer_id != customer_id):
        raise CostError("cost item not found")
    return it


async def list_items(db: AsyncSession, customer_id: str | None = None, *, kind: str | None = None,
                     status: str | None = None, within_days: int | None = None, active_only: bool = False,
                     now: datetime | None = None) -> dict[str, Any]:
    cfg = await settings(db)
    day = await today(db, now)
    warn = int(cfg["warn_days"])
    q = select(CostItem).order_by(CostItem.kind, CostItem.name)
    if customer_id:
        q = q.where(CostItem.customer_id == customer_id)
    if kind:
        q = q.where(CostItem.kind == kind)
    if active_only:
        q = q.where(CostItem.active.is_(True))
    cust = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    names = {n.id: n.name for n in (await db.execute(select(Node))).scalars()}
    rows = [item_out(it, day, warn, customer_name=cust.get(it.customer_id), node_name=names.get(it.node_id or ""))
            for it in (await db.execute(q)).scalars()]
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    if status:
        rows = [r for r in rows if r["status"] == status]
    if within_days is not None:
        rows = [r for r in rows if r["active"] and r["end_date"] is not None and (
            r["days_left"] <= within_days or (r["notice_days_left"] is not None and 0 <= r["notice_days_left"] <= within_days))]
        rows.sort(key=lambda r: min([x for x in (r["days_left"], r["notice_days_left"]) if x is not None and x >= 0]
                                    or [r["days_left"]]))
    mrr = sum((Decimal(str(r["monthly"])) for r in rows if r["monthly"] is not None and r["active"]), Decimal(0))
    return {"items": rows, "counts": counts, "warn_days": warn, "monthly": num(cents(mrr)), "today": day}


async def expiring(db: AsyncSession, customer_id: str | None = None, within_days: int | None = None,
                   now: datetime | None = None) -> list[dict[str, Any]]:
    """Active items that run out, or whose notice date comes, within ``within_days`` (default the warning
    setting), and the ones already expired."""
    cfg = await settings(db)
    days = int(within_days if within_days is not None else cfg["warn_days"])
    res = await list_items(db, customer_id, within_days=days, active_only=True, now=now)
    return res["items"]


async def renew_due(db: AsyncSession, day: date) -> int:
    """Move the end date of items with automatic renewal on by their term once it has passed."""
    n = 0
    q = select(CostItem).where(CostItem.active.is_(True), CostItem.auto_renew.is_(True), CostItem.end_date.is_not(None),
                               CostItem.end_date < day)
    for it in (await db.execute(q)).scalars():
        months = int(it.renew_months or 12)
        old, end = it.end_date, it.end_date
        while end < day:
            end = add_months(end, months)
        it.end_date = end
        it.history = [*(it.history or []), {"at": datetime.now(UTC).isoformat(), "from": old.isoformat(),
                                            "to": end.isoformat(), "by": "auto-renew"}][-50:]
        n += 1
    return n


async def import_hardware(db: AsyncSession, e: Environment, by: str | None = None) -> list[CostItem]:
    """Create a hardware item for every machine of the customer whose inventory has a model or serial
    number and that has no hardware item yet (warranty dates are filled in by hand)."""
    have = {(x.node_id, (x.serial or "").lower()) for x in (await db.execute(
        select(CostItem).where(CostItem.customer_id == e.id, CostItem.kind == "hardware"))).scalars()}
    have_nodes = {n for n, _ in have if n}
    have_serials = {s for _, s in have if s}
    out = []
    for n in (await db.execute(select(Node).where(Node.environment_id == e.id).order_by(Node.name))).scalars():
        hw = (n.inventory or {}).get("hardware") or {}
        model = " ".join(str(x) for x in (hw.get("manufacturer"), hw.get("model")) if x).strip()
        serial = str(hw.get("serial") or "").strip()
        if serial.lower() in ("", "none", "default string", "to be filled by o.e.m.", "system serial number", "0"):
            serial = ""
        if not model and not serial:
            continue
        if n.id in have_nodes or (serial and serial.lower() in have_serials):
            continue
        it = await save_item(db, None, customer_id=e.id, by=by, kind="hardware", name=f"{n.name}" + (f" ({model})" if model else ""),
                             vendor=hw.get("manufacturer") or None, serial=serial or None, node_id=n.id, billing="none",
                             warn_days=None)
        out.append(it)
    return out


# ------------------------------------------------------------------ resource use
GIB = 1024 ** 3


def _r2(v: float) -> float:
    return float(Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP))


async def live_usage(db: AsyncSession, e: Environment, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the customer uses right now: resources given to VMs and containers (templates left out,
    stopped guests per setting), machines per type and Microsoft 365 licences."""
    from mmn.services.customers import machine_type

    cfg = cfg or await settings(db)
    seen: set[tuple[str, int]] = set()
    guests: list[dict[str, Any]] = []
    devices = {"server": 0, "workstation": 0, "proxmox": 0}
    for n in (await db.execute(select(Node).where(Node.environment_id == e.id))).scalars():
        devices[machine_type(n)] = devices.get(machine_type(n), 0) + 1
        if n.kind != "proxmox":
            continue
        for g in (n.guests or {}).get("guests") or []:
            try:
                vmid = int(g.get("vmid") or 0)
            except (TypeError, ValueError):
                continue
            k = (n.cluster_name or n.id, vmid)
            if not vmid or k in seen or g.get("template"):
                continue
            seen.add(k)
            if g.get("status") != "running" and not cfg.get("include_stopped", True):
                continue
            guests.append({"vmid": vmid, "name": g.get("name") or str(vmid), "type": g.get("type"),
                           "status": g.get("status"), "node": g.get("node") or n.pve_name or n.name,
                           "cpu": int(g.get("maxcpu") or 0), "ram_gb": _r2((g.get("maxmem") or 0) / GIB),
                           "storage_gb": _r2((g.get("maxdisk") or 0) / GIB)})
    guests.sort(key=lambda g: g["vmid"])
    m365: dict[str, dict[str, Any]] = {}
    users = 0
    for t in (await db.execute(select(M365Tenant).where(M365Tenant.customer_id == e.id,
                                                        M365Tenant.enabled.is_(True)))).scalars():
        snap = t.snapshot or {}
        users += sum(1 for u in snap.get("users") or [] if u.get("enabled") and u.get("licences"))
        for lic in snap.get("licences") or []:
            if not lic.get("sku") or lic.get("free"):
                continue
            cur = m365.setdefault(lic["sku"], {"name": lic.get("name") or lic["sku"], "enabled": 0, "consumed": 0})
            cur["enabled"] += int(lic.get("enabled") or 0)
            cur["consumed"] += int(lic.get("consumed") or 0)
    return {"cpu": sum(g["cpu"] for g in guests), "ram_gb": _r2(sum(g["ram_gb"] for g in guests)),
            "storage_gb": _r2(sum(g["storage_gb"] for g in guests)), "guests": len(guests), "devices": devices,
            "m365_users": users, "m365": m365, "detail": {"guests": guests}}


async def sample(db: AsyncSession, e: Environment, day: date, cfg: dict[str, Any] | None = None) -> CostUsage:
    u = await live_usage(db, e, cfg)
    row = (await db.execute(select(CostUsage).where(CostUsage.customer_id == e.id, CostUsage.day == day))).scalar_one_or_none()
    if row is None:
        row = CostUsage(customer_id=e.id, day=day)
        db.add(row)
    row.cpu, row.ram_gb, row.storage_gb, row.guests = u["cpu"], Decimal(str(u["ram_gb"])), Decimal(str(u["storage_gb"])), u["guests"]
    row.devices, row.m365_users, row.m365, row.detail = u["devices"], u["m365_users"], u["m365"], u["detail"]
    row.taken_at = datetime.now(UTC)
    await db.flush()
    return row


def _usage_row(r: CostUsage) -> dict[str, Any]:
    return {"day": r.day, "cpu": r.cpu, "ram_gb": float(r.ram_gb or 0), "storage_gb": float(r.storage_gb or 0),
            "guests": r.guests, "devices": r.devices or {}, "m365_users": r.m365_users, "m365": r.m365 or {},
            "detail": r.detail or {}}


def _combine(rows: list[dict[str, Any]], basis: str, m365_qty: str) -> dict[str, Any]:
    """Average or peak of daily readings. Resources keep two decimals; counts (machines, users,
    licences) are whole numbers (rounded half up)."""
    def agg(vals: list[float]) -> float:
        if not vals:
            return 0.0
        return max(vals) if basis == "peak" else sum(vals) / len(vals)

    def whole(v: float) -> int:
        return int(Decimal(str(v)).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    dev_keys = sorted({k for r in rows for k in r["devices"]})
    skus = sorted({k for r in rows for k in r["m365"]})
    names = {k: v.get("name") for r in rows for k, v in r["m365"].items()}
    return {
        "cpu": _r2(agg([r["cpu"] for r in rows])), "ram_gb": _r2(agg([r["ram_gb"] for r in rows])),
        "storage_gb": _r2(agg([r["storage_gb"] for r in rows])), "guests": whole(agg([r["guests"] for r in rows])),
        "devices": {k: whole(agg([r["devices"].get(k, 0) for r in rows])) for k in dev_keys},
        "m365_users": whole(agg([r["m365_users"] for r in rows])),
        "m365": {k: {"name": names.get(k) or k,
                     "quantity": whole(agg([(r["m365"].get(k) or {}).get(m365_qty, 0) for r in rows])),
                     "enabled": whole(agg([(r["m365"].get(k) or {}).get("enabled", 0) for r in rows])),
                     "consumed": whole(agg([(r["m365"].get(k) or {}).get("consumed", 0) for r in rows]))}
                 for k in skus},
    }


async def period_usage(db: AsyncSession, e: Environment, period: str, cfg: dict[str, Any] | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Usage of a month from the daily readings; a month without readings (the first days after the
    update, or a month in the future) takes the current use and says so (``estimated``)."""
    cfg = cfg or await settings(db)
    y, m = parse_period(period)
    first, last = month_days(y, m)
    rows = [_usage_row(r) for r in (await db.execute(
        select(CostUsage).where(CostUsage.customer_id == e.id, CostUsage.day >= first, CostUsage.day <= last)
        .order_by(CostUsage.day))).scalars()]
    basis = cfg.get("usage_basis") or "average"
    m365_qty = "consumed" if cfg.get("m365_quantity") == "consumed" else "enabled"
    estimated = not rows
    if estimated:
        rows = [await live_usage(db, e, cfg)]
    out = _combine(rows, basis, m365_qty)
    out.update({"basis": basis, "days": 0 if estimated else len(rows), "estimated": estimated,
                "first_day": None if estimated else rows[0]["day"], "last_day": None if estimated else rows[-1]["day"],
                "guest_detail": rows[-1].get("detail", {}).get("guests", []), "m365_quantity": m365_qty})
    return out


# ------------------------------------------------------------------ statement
def charged_in(it: CostItem, y: int, m: int) -> bool:
    """Is a (recurring or one-off) item charged in this month? Quarterly and yearly items are charged in
    the month they started and every 3 / 12 months after (January when there is no start date)."""
    if not it.active or it.billing == "none":
        return False
    first, last = month_days(y, m)
    if it.start_date and it.start_date > last:
        return False
    if it.end_date and it.end_date < first and not it.auto_renew:
        return False
    if it.billing == "once":
        return bool(it.start_date) and (it.start_date.year, it.start_date.month) == (y, m)
    if it.billing == "monthly":
        return True
    anchor = it.start_date.month if it.start_date else 1
    return (m - anchor) % CYCLE_MONTHS[it.billing] == 0


def _line(group: str, key: str, *, std: str | None = None, name: str | None = None, detail: str | None = None,
          quantity: Any, unit: str | None, price: Decimal | None, cost: Decimal | None = None,
          billing: str | None = None, item_id: str | None = None) -> dict[str, Any]:
    q = Decimal(str(quantity))
    amount = cents(q * price) if price is not None else None
    return {"group": group, "key": key, "std": std, "name": name, "detail": detail, "quantity": num(q), "unit": unit,
            "unit_price": num(price), "amount": num(amount), "cost": num(cents(q * cost)) if cost is not None else None,
            "billing": billing, "item_id": item_id, "missing_price": price is None}


async def build(db: AsyncSession, e: Environment, period: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Work out a customer's statement for a month (live)."""
    cfg = await settings(db)
    cs = customer_settings(e)
    y, m = parse_period(period)
    rates = await rates_for(db, e.id)
    usage = await period_usage(db, e, period, cfg, now)
    lines: list[dict[str, Any]] = []

    def rate_line(group: str, key: str, qty: Any, unit: str, *, name: str | None = None, always: bool = False) -> None:
        r = rates.get(key)
        if r is None and not always:
            return
        if not qty:
            return
        lines.append(_line(group, key, std=key if key in STANDARD_KEYS else None, name=(r.name if r else None) or name,
                           quantity=qty, unit=(r.unit if r and r.unit else unit), price=Decimal(r.price) if r else None,
                           cost=Decimal(r.cost) if r and r.cost is not None else None))

    if cs["bill_resources"]:
        rate_line("resources", "resource.cpu", usage["cpu"], "vcpu")
        rate_line("resources", "resource.ram", usage["ram_gb"], "gb")
        rate_line("resources", "resource.storage", usage["storage_gb"], "gb")
    if cs["bill_devices"]:
        for k in ("server", "workstation", "proxmox"):
            rate_line("devices", f"device.{k}", usage["devices"].get(k, 0), "device")
    if cs["bill_m365"]:
        rate_line("m365", "m365.user", usage["m365_users"], "user")
        for sku, v in sorted(usage["m365"].items(), key=lambda kv: kv[1]["name"].lower()):
            rate_line("m365", f"m365.sku.{sku}", v["quantity"], "licence", name=v["name"], always=True)
    items = (await db.execute(select(CostItem).where(CostItem.customer_id == e.id).order_by(CostItem.name))).scalars().all()
    order = {k: i for i, k in enumerate(GROUPS)}
    item_lines = []
    for it in items:
        if not charged_in(it, y, m):
            continue
        bits = [x for x in (it.vendor, it.reference, it.serial) if x]
        item_lines.append(_line(it.kind if it.kind in GROUPS else "other", f"item:{it.id}", name=it.name,
                                detail=" · ".join(bits) or None, quantity=it.quantity or 1, unit=None,
                                price=Decimal(it.unit_price) if it.unit_price is not None else None,
                                cost=Decimal(it.cost_price) if it.cost_price is not None else None,
                                billing=it.billing, item_id=it.id))
    item_lines.sort(key=lambda x: (order.get(x["group"], 99), (x["name"] or "").lower()))
    lines += item_lines
    mrr = sum((monthly_equivalent(it) or Decimal(0) for it in items if it.active and _running(it, y, m)), Decimal(0))
    mrr += sum((Decimal(str(x["amount"])) for x in lines
                if x["group"] in ("resources", "devices", "m365") and x["amount"] is not None), Decimal(0))
    return {"version": 1, "customer": {"id": e.id, "name": e.name, "debtor": cs["debtor"] or None},
            "period": period, "status": "draft", "currency": cfg["currency"], "lines": lines,
            "totals": totals(lines, cs["discount_pct"], cfg["vat_pct"]), "monthly_recurring": num(cents(mrr)),
            "usage": {k: v for k, v in usage.items() if k != "guest_detail"}, "guests": usage["guest_detail"],
            "billable": cs["billable"], "note": cs["note"] or None,
            "generated_at": (now or datetime.now(UTC)).isoformat()}


def _running(it: CostItem, y: int, m: int) -> bool:
    first, last = month_days(y, m)
    if it.start_date and it.start_date > last:
        return False
    return not (it.end_date and it.end_date < first and not it.auto_renew)


def totals(lines: list[dict[str, Any]], discount_pct: Any, vat_pct: Any) -> dict[str, Any]:
    sub = sum((Decimal(str(x["amount"])) for x in lines if x["amount"] is not None), Decimal(0))
    dp = Decimal(str(discount_pct or 0))
    disc = cents(sub * dp / 100)
    net = sub - disc
    vp = Decimal(str(vat_pct or 0))
    vat = cents(net * vp / 100)
    known_cost = [Decimal(str(x["cost"])) for x in lines if x["cost"] is not None]
    cost = sum(known_cost, Decimal(0)) if known_cost else None
    return {"subtotal": num(cents(sub)), "discount_pct": float(dp), "discount": num(disc), "net": num(cents(net)),
            "vat_pct": float(vp), "vat": num(vat), "total": num(cents(net + vat)), "cost": num(cents(cost)) if cost is not None else None,
            "margin": num(cents(net - cost)) if cost is not None else None,
            "missing_prices": sum(1 for x in lines if x["missing_price"])}


async def final_of(db: AsyncSession, customer_id: str, period: str) -> CostStatement | None:
    return (await db.execute(select(CostStatement).where(CostStatement.customer_id == customer_id,
                                                         CostStatement.period == period))).scalar_one_or_none()


async def statement(db: AsyncSession, e: Environment, period: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The final statement of the month if there is one, otherwise the live one."""
    parse_period(period)
    f = await final_of(db, e.id, period)
    if f is not None:
        return {**(f.data or {}), "status": "final", "finalized_at": f.created_at, "finalized_by": f.created_by,
                "final_note": f.note}
    return await build(db, e, period, now=now)


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


async def finalize(db: AsyncSession, e: Environment, period: str, *, by: str | None = None, note: str | None = None,
                   now: datetime | None = None) -> CostStatement:
    if await final_of(db, e.id, period) is not None:
        raise CostError("this month is final already: reopen it first")
    data = _jsonable(await build(db, e, period, now=now))
    s = CostStatement(customer_id=e.id, period=period, data=data, note=(note or "").strip()[:500] or None, created_by=by)
    db.add(s)
    await db.flush()
    return s


async def reopen(db: AsyncSession, e: Environment, period: str) -> bool:
    f = await final_of(db, e.id, period)
    if f is None:
        return False
    await db.delete(f)
    await db.flush()
    return True


async def overview(db: AsyncSession, period: str, customer_ids: list[str] | None = None,
                   now: datetime | None = None) -> dict[str, Any]:
    """All customers for a month: totals, whether final, missing prices, what runs out soon."""
    cfg = await settings(db)
    parse_period(period)
    q = select(Environment).order_by(Environment.name)
    if customer_ids is not None:
        q = q.where(Environment.id.in_(customer_ids))
    rows = []
    sums = {"net": Decimal(0), "total": Decimal(0), "cost": Decimal(0), "monthly_recurring": Decimal(0)}
    soon = await list_items(db, within_days=int(cfg["warn_days"]), active_only=True, now=now)
    soon_by: dict[str, int] = {}
    for it in soon["items"]:
        soon_by[it["customer_id"]] = soon_by.get(it["customer_id"], 0) + 1
    for e in (await db.execute(q)).scalars():
        s = await statement(db, e, period, now=now)
        t = s["totals"]
        rows.append({"customer_id": e.id, "customer_name": e.name, "kind": e.kind, "billable": s.get("billable", True),
                     "debtor": (s.get("customer") or {}).get("debtor"), "status": s["status"], "lines": len(s["lines"]),
                     "net": t["net"], "vat": t["vat"], "total": t["total"], "cost": t["cost"], "margin": t["margin"],
                     "missing_prices": t["missing_prices"], "monthly_recurring": s.get("monthly_recurring"),
                     "estimated": (s.get("usage") or {}).get("estimated", False), "expiring": soon_by.get(e.id, 0)})
        if s.get("billable", True):
            sums["net"] += Decimal(str(t["net"]))
            sums["total"] += Decimal(str(t["total"]))
            sums["cost"] += Decimal(str(t["cost"] or 0))
            sums["monthly_recurring"] += Decimal(str(s.get("monthly_recurring") or 0))
    return {"period": period, "currency": cfg["currency"], "customers": rows,
            "totals": {k: num(cents(v)) for k, v in sums.items()}, "expiring": len(soon["items"]),
            "final": sum(1 for r in rows if r["status"] == "final")}


# ------------------------------------------------------------------ rendering: names, CSV, PDF
def line_name(x: dict[str, Any], lang: str) -> str:
    T = texts(lang)
    if x.get("name"):
        return str(x["name"])
    if x.get("std"):
        return T.get(x["std"], x["std"])
    return str(x.get("key") or "")


def unit_name(x: dict[str, Any], lang: str) -> str:
    u = x.get("unit")
    if not u:
        return ""
    return texts(lang).get(f"u_{u}", u)


def to_csv(statements: list[dict[str, Any]], lang: str = "en") -> str:
    """One row per line, for the bookkeeping. Dutch: semicolons and decimal commas (Excel NL)."""
    T = texts(lang)
    sep = ";" if lang == "nl" else ","

    def n(v: Any) -> str:
        if v is None:
            return ""
        s = f"{Decimal(str(v)):.2f}" if isinstance(v, float) or isinstance(v, Decimal) else str(v)
        return s.replace(".", ",") if lang == "nl" else s

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\r\n")
    w.writerow([T["customer"], T["debtor"], T["period"], "status", "group", T["description"], "detail", T["quantity"],
                "unit", T["unit_price"], T["amount"], "currency", "billing"])
    for s in statements:
        c = s.get("customer") or {}
        for x in s["lines"]:
            w.writerow([c.get("name"), c.get("debtor") or "", s["period"], s["status"], x["group"], line_name(x, lang),
                        x.get("detail") or "", n(x["quantity"]), unit_name(x, lang), n(x["unit_price"]), n(x["amount"]),
                        s.get("currency") or "EUR", x.get("billing") or "monthly"])
        t = s["totals"]
        if t.get("discount"):
            w.writerow([c.get("name"), c.get("debtor") or "", s["period"], s["status"], "discount",
                        T["discount"].format(pct=qty_str(t["discount_pct"], lang)), "", "", "", "", n(-t["discount"]),
                        s.get("currency") or "EUR", ""])
    return "﻿" + buf.getvalue()


def render_pdf(s: dict[str, Any], lang: str, tz: ZoneInfo, app: str = "Stackpilot") -> bytes:
    """The statement as an A4 PDF in the house style (navy header with the coral dot, like the report)."""
    from fpdf import FPDF

    from mmn.services.reports import KORAAL, LINE, MUTED, NACHT, SOFT, period_label
    from mmn.services.reports import _latin as L

    T = texts(lang)
    cur = s.get("currency") or "EUR"
    M = lambda v: money(v, cur, lang).replace("€", "EUR")  # noqa: E731 (the PDF font is latin-1)
    plabel = period_label(s["period"], lang)

    class PDF(FPDF):
        def header(self) -> None:
            self.set_fill_color(*NACHT)
            self.rect(0, 0, 210, 18, "F")
            self.set_xy(12, 5)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 13)
            self.cell(0, 8, L(app), new_x="LMARGIN", new_y="NEXT")
            self.set_fill_color(*KORAAL)
            self.ellipse(12 + self.get_string_width(L(app)) + 2, 8.2, 2.2, 2.2, "F")
            self.set_xy(110, 5)
            self.set_font("Helvetica", "", 9)
            self.cell(88, 8, L(f"{s['customer']['name']} - {plabel}"), align="R")
            self.set_y(24)

        def footer(self) -> None:
            self.set_y(-14)
            self.set_draw_color(*LINE)
            self.line(12, self.get_y(), 198, self.get_y())
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 8, L(T["footer"].format(app=app)), align="L")
            self.cell(0, 8, f"{self.page_no()}/{{nb}}", align="R")

    pdf = PDF(format="A4")
    pdf.set_margins(12, 22, 12)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_text_color(*NACHT)
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, L(T["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*MUTED)
    meta = [f"{T['customer']}: {s['customer']['name']}", f"{T['period']}: {plabel}"]
    if s["customer"].get("debtor"):
        meta.append(f"{T['debtor']}: {s['customer']['debtor']}")
    gen = s.get("finalized_at") or s.get("generated_at")
    if gen:
        try:
            g = datetime.fromisoformat(str(gen).replace("Z", "+00:00")).astimezone(tz).strftime("%d-%m-%Y")
            meta.append(f"{T['generated']}: {g}")
        except ValueError:
            pass
    pdf.cell(0, 5, L("    ".join(meta)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*((28, 138, 87) if s["status"] == "final" else (168, 107, 0)))
    pdf.cell(0, 6, L(T["status_final"] if s["status"] == "final" else T["status_draft"]), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    widths = [92, 24, 34, 36]

    def row(cells: list[str], bold: bool = False, fill: bool = False, muted: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.set_text_color(*(MUTED if muted else NACHT))
        if fill:
            pdf.set_fill_color(*SOFT)
        lines = max(len(pdf.multi_cell(w, 5, L(v), dry_run=True, output="LINES")) for v, w in zip(cells, widths, strict=True))
        h = 5 * lines + 1
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
        y0, x = pdf.get_y(), 12.0
        pdf.set_auto_page_break(False)
        for i, (v, w) in enumerate(zip(cells, widths, strict=True)):
            pdf.set_xy(x, y0)
            pdf.multi_cell(w, 5, L(v), align="L" if i == 0 else "R", fill=fill, new_x="RIGHT", new_y="TOP")
            x += w
        pdf.set_auto_page_break(True, margin=18)
        pdf.set_xy(12, y0 + h)
        pdf.set_draw_color(*LINE)
        pdf.line(12, pdf.get_y(), 198, pdf.get_y())
        pdf.ln(0.5)

    row([T["description"], T["quantity"], T["unit_price"], T["amount"]], bold=True, fill=True, muted=True)
    if not s["lines"]:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 7, L(T["none"]), new_x="LMARGIN", new_y="NEXT")
    group = None
    for x in s["lines"]:
        if x["group"] != group:
            group = x["group"]
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*NACHT)
            pdf.set_fill_color(*KORAAL)
            pdf.rect(12, pdf.get_y() + 1.3, 1.2, 4, "F")
            pdf.set_x(15)
            pdf.cell(0, 6.5, L(T.get(f"g_{group}", group)), new_x="LMARGIN", new_y="NEXT")
        name = line_name(x, lang)
        if x.get("billing") and x["billing"] != "monthly":
            name += f" ({T.get('b_' + x['billing'], x['billing'])})"
        if x.get("detail"):
            name += f"\n{x['detail']}"
        unit = unit_name(x, lang)
        row([name, f"{qty_str(x['quantity'], lang)} {unit}".strip(),
             M(x["unit_price"]) if x["unit_price"] is not None else T["no_price"],
             M(x["amount"]) if x["amount"] is not None else "–"])
    t = s["totals"]
    pdf.ln(3)
    widths_t = [126, 60]

    def total(label: str, value: str, bold: bool = False) -> None:
        pdf.set_font("Helvetica", "B" if bold else "", 10 if bold else 9)
        pdf.set_text_color(*NACHT)
        pdf.cell(widths_t[0], 6, L(label), align="R")
        pdf.cell(widths_t[1], 6, L(value), align="R", new_x="LMARGIN", new_y="NEXT")

    total(T["subtotal"], M(t["subtotal"]))
    if t.get("discount"):
        total(T["discount"].format(pct=qty_str(t["discount_pct"], lang)), M(-t["discount"]))
    total(T["net"], M(t["net"]), bold=True)
    total(T["vat"].format(pct=qty_str(t["vat_pct"], lang)), M(t["vat"]))
    total(T["total"], M(t["total"]), bold=True)
    u = s.get("usage") or {}
    if u and (u.get("guests") or u.get("cpu")):
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, L(T["usage"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        txt = T["usage_estimated"] if u.get("estimated") else T["usage_line"].format(
            basis=T.get(f"basis_{u.get('basis')}", ""), days=u.get("days"), cpu=qty_str(u.get("cpu"), lang),
            ram=qty_str(u.get("ram_gb"), lang), storage=qty_str(u.get("storage_gb"), lang), guests=u.get("guests"))
        pdf.multi_cell(0, 5, L(txt), new_x="LMARGIN", new_y="NEXT")
    note = s.get("final_note") or s.get("note")
    if note:
        pdf.ln(3)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 5, L(note), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


async def report_section(db: AsyncSession, e: Environment, period: str, lang: str) -> dict[str, Any] | None:
    """The costs part of the monthly customer report (only when the customer's report settings ask for it)."""
    if not (e.report or {}).get("include_costs"):
        return None
    from mmn.services.reports import period_label

    s = await statement(db, e, period)
    soon = await expiring(db, e.id)
    T = texts(lang)
    cur = s.get("currency") or "EUR"
    return {
        "labels": {"title": T["costs_section"], "description": T["description"], "quantity": T["quantity"],
                   "unit_price": T["unit_price"], "amount": T["amount"], "expiring": T["expiring"], "item": T["item"],
                   "kind": T["kind"], "ends": T["ends"], "notice_by": T["notice_by"], "net": T["net"], "total": T["total"],
                   "status": T["status_final"] if s["status"] == "final" else T["status_draft"],
                   "line": T["costs_line"].format(period=period_label(period, lang), net=money(s["totals"]["net"], cur, lang),
                                                  total=money(s["totals"]["total"], cur, lang))},
        "status": s["status"], "currency": s.get("currency") or "EUR", "totals": s["totals"],
        "lines": [{"group": T.get(f"g_{x['group']}", x["group"]), "name": line_name(x, lang), "detail": x.get("detail"),
                   "quantity": qty_str(x["quantity"], lang) + (f" {unit_name(x, lang)}" if x.get("unit") else ""),
                   "unit_price": money(x["unit_price"], s.get("currency") or "EUR", lang) if x["unit_price"] is not None else T["no_price"],
                   "amount": money(x["amount"], s.get("currency") or "EUR", lang) if x["amount"] is not None else "–"}
                  for x in s["lines"]],
        "net": money(s["totals"]["net"], s.get("currency") or "EUR", lang),
        "total": money(s["totals"]["total"], s.get("currency") or "EUR", lang),
        "expiring": [{"name": x["name"], "kind": T.get(f"k_{x['kind']}", x["kind"]),
                      "end_date": x["end_date"].strftime("%d-%m-%Y") if x["end_date"] else "–",
                      "notice_by": x["notice_by"].strftime("%d-%m-%Y") if x["notice_by"] else "–",
                      "status": x["status"]} for x in soon[:20]],
    }


# ------------------------------------------------------------------ alerts and nightly findings
async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Monitoring alerts: a contract or licence that runs out (or whose notice date comes) within the warning
    period, or that ran out; hardware whose warranty runs out (an expired warranty is a finding, not an alert)."""
    cfg = await settings(db)
    if not cfg.get("alert_expiring", True):
        return {}
    day = await today(db, now)
    warn = int(cfg["warn_days"])
    cust = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    want: dict[str, dict[str, Any]] = {}
    q = select(CostItem).where(CostItem.active.is_(True), CostItem.end_date.is_not(None))
    for it in (await db.execute(q)).scalars():
        st = status_of(it, day, warn)
        s = st["status"]
        if s not in ("expiring", "notice", "expired") or (s == "expired" and it.kind == "hardware"):
            continue
        what = {"hardware": "The warranty of", "licence": "Licence", "contract": "Contract"}.get(it.kind, "")
        label = f"{what} {it.name}".strip()
        if s == "expired":
            msg = f"{label} ended on {it.end_date.isoformat()}"
        elif s == "notice":
            msg = f"{label}: give notice by {st['notice_by'].isoformat()} ({st['notice_days_left']} day(s)) or it continues"
        else:
            msg = f"{label} ends in {st['days_left']} day(s), on {it.end_date.isoformat()}"
        want[f"cost-item:{it.id}"] = {
            "node_id": it.node_id, "kind": "contract",
            "severity": "critical" if s == "expired" or (st["days_left"] is not None and st["days_left"] <= 7) else "warning",
            "value": st["days_left"],
            "detail": {"source": f"{cust.get(it.customer_id, '?')} · costs", "customer_id": it.customer_id,
                       "item_id": it.id, "status": s, "end_date": it.end_date.isoformat()},
            "message": msg[:500]}
    return want


async def findings(db: AsyncSession, e: Environment, now: datetime, finding) -> list[dict[str, Any]]:
    """Nightly findings (category contracts): what runs out or must be cancelled soon, and the lines of this
    month's statement that have no price."""
    cfg = await settings(db)
    day = await today(db, now)
    warn = int(cfg["warn_days"])
    out: list[dict[str, Any]] = []
    for it in (await db.execute(select(CostItem).where(CostItem.customer_id == e.id, CostItem.active.is_(True),
                                                       CostItem.end_date.is_not(None)))).scalars():
        st = status_of(it, day, warn)
        s = st["status"]
        d = it.end_date.isoformat()
        kind = "warranty" if it.kind == "hardware" else ("licence" if it.kind == "licence" else "contract")
        if s == "expired":
            if kind == "warranty":
                out.append(finding("low", "contracts", it.name, f"The warranty of {it.name} ended on {d}.",
                                   "Replace the hardware, extend the warranty, or accept the risk and mark it inactive.",
                                   code="warranty_expired", name=it.name, date=d))
            else:
                out.append(finding("high", "contracts", it.name, f"{kind.capitalize()} {it.name} ended on {d}.",
                                   "Renew it and enter the new end date, or mark the item inactive if it was stopped on purpose.",
                                   code=f"{kind}_expired", name=it.name, date=d))
        elif s == "notice":
            nb = st["notice_by"].isoformat()
            out.append(finding("medium", "contracts", it.name,
                               f"{it.name}: the notice period ends on {nb} ({st['notice_days_left']} day(s)); after that it "
                               f"{'renews' if it.auto_renew else 'cannot be cancelled before'} {d}.",
                               "Decide with the customer whether to continue, and cancel before that date if not.",
                               code="contract_notice", name=it.name, date=nb, end=d, days=st["notice_days_left"]))
        elif s == "expiring":
            out.append(finding("medium" if st["days_left"] <= 30 else "low", "contracts", it.name,
                               f"{'The warranty of ' + it.name if kind == 'warranty' else kind.capitalize() + ' ' + it.name} "
                               f"ends in {st['days_left']} day(s), on {d}.",
                               "Extend or renew it in time, or plan the replacement." if kind == "warranty" else
                               "Renew it in time and enter the new end date.",
                               code=f"{kind}_expiring", name=it.name, days=st["days_left"], date=d))
    if customer_settings(e)["billable"]:
        from mmn.services import reports

        period = reports.current_period(now, await tz_of(db))
        s = await build(db, e, period, now=now)
        miss = [line_name(x, "en") for x in s["lines"] if x["missing_price"]]
        if miss:
            out.append(finding("info", "contracts", e.name,
                               f"{len(miss)} line(s) of this month's statement have no price: {', '.join(miss[:6])}.",
                               "Add the price in Costs & licences (price list or the item) before invoicing.",
                               code="cost_price_missing", count=len(miss), names=", ".join(miss[:6])))
    return out


# ------------------------------------------------------------------ scheduler
async def tick(now: datetime | None = None) -> int:
    """Every hour: write today's usage of every customer and renew items whose term ended."""
    from mmn.db import sessionmaker

    n = 0
    async with sessionmaker()() as db:
        cfg = await settings(db)
        day = await today(db, now)
        for e in (await db.execute(select(Environment))).scalars().all():
            try:
                await sample(db, e, day, cfg)
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("usage of %s", e.name)
        if cfg.get("auto_renew", True):
            await renew_due(db, day)
        await db.commit()
    return n
