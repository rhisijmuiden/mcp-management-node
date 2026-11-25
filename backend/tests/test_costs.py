"""Phase 21: costs and licences per customer. Price list, cost items with end dates and notice periods,
daily resource use, the monthly statement (live and final), CSV/PDF export, alerts, nightly findings, the
section in the monthly report and the MCP tools."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.models import CostItem
from mmn.services import costs

GIB = 1024 ** 3


def _item(**kw) -> CostItem:
    base = {"customer_id": "x", "kind": "contract", "name": "Support", "quantity": Decimal(1), "unit_price": Decimal(100),
            "billing": "monthly", "active": True, "auto_renew": False, "history": []}
    return CostItem(**{**base, **kw})


# ------------------------------------------------------------------ unit tests
def test_status_and_billing_cycles():
    day = date(2026, 11, 25)
    assert costs.status_of(_item(), day, 60)["status"] == "none"
    assert costs.status_of(_item(end_date=date(2027, 6, 1)), day, 60)["status"] == "ok"
    st = costs.status_of(_item(end_date=date(2026, 12, 20)), day, 60)
    assert st["status"] == "expiring" and st["days_left"] == 25
    assert costs.status_of(_item(end_date=date(2026, 11, 1)), day, 60)["status"] == "expired"
    # Notice period of 90 days on a contract ending in 4 months: give notice within 60 days.
    st = costs.status_of(_item(end_date=date(2027, 3, 31), notice_days=90), day, 60)
    assert st["status"] == "notice" and st["notice_by"] == date(2026, 12, 31) and st["notice_days_left"] == 36
    # Notice date passed and it renews by itself.
    assert costs.status_of(_item(end_date=date(2026, 12, 31), notice_days=90, auto_renew=True, renew_months=12),
                           day, 60)["status"] == "renews"
    assert costs.status_of(_item(end_date=date(2026, 11, 1), active=False), day, 60)["status"] == "inactive"
    assert costs.status_of(_item(end_date=date(2027, 1, 10), warn_days=30), day, 60)["status"] == "ok"

    yearly = _item(billing="yearly", start_date=date(2025, 3, 15), unit_price=Decimal(1200))
    assert costs.charged_in(yearly, 2026, 3) and not costs.charged_in(yearly, 2026, 4)
    assert not costs.charged_in(yearly, 2025, 2)  # before the start
    assert costs.monthly_equivalent(yearly) == Decimal(100)
    q = _item(billing="quarterly", start_date=date(2026, 2, 1))
    assert [m for m in range(1, 13) if costs.charged_in(q, 2026, m)] == [2, 5, 8, 11]
    once = _item(billing="once", start_date=date(2026, 11, 3))
    assert costs.charged_in(once, 2026, 11) and not costs.charged_in(once, 2026, 12)
    assert costs.monthly_equivalent(once) is None
    ended = _item(end_date=date(2026, 10, 31))
    assert costs.charged_in(ended, 2026, 10) and not costs.charged_in(ended, 2026, 11)
    assert costs.charged_in(_item(end_date=date(2026, 10, 31), auto_renew=True, renew_months=12), 2026, 11)
    assert not costs.charged_in(_item(billing="none"), 2026, 11)
    assert costs.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert costs.add_months(date(2026, 11, 30), 14) == date(2028, 1, 30)


def test_totals_money_and_csv():
    lines = [costs._line("resources", "resource.cpu", std="resource.cpu", quantity=7.5, unit="vcpu", price=Decimal("12.50"),
                         cost=Decimal(4)),
             costs._line("m365", "m365.sku.X", name="Microsoft 365 Business Premium", quantity=10, unit="licence", price=None),
             costs._line("contract", "item:1", name="Internet", quantity=1, unit=None, price=Decimal("49.99"), billing="monthly")]
    assert lines[0]["amount"] == 93.75 and lines[0]["cost"] == 30.0 and lines[1]["missing_price"]
    t = costs.totals(lines, 10, 21)
    assert t["subtotal"] == 143.74 and t["discount"] == 14.37 and t["net"] == 129.37
    assert t["vat"] == 27.17 and t["total"] == 156.54 and t["cost"] == 30.0 and t["missing_prices"] == 1
    assert costs.money(1234.5, "EUR", "nl") == "€ 1.234,50" and costs.money(1234.5, "EUR", "en") == "€1,234.50"
    assert costs.qty_str(7.5, "nl") == "7,50" and costs.qty_str(3, "en") == "3"
    s = {"customer": {"name": "Klant", "debtor": "D-1"}, "period": "2026-11", "status": "draft", "currency": "EUR",
         "lines": lines, "totals": t}
    nl = costs.to_csv([s], "nl")
    assert nl.startswith("﻿Klant;Debiteur;Periode")
    assert "Klant;D-1;2026-11;draft;resources;vCPU;;7,50;vCPU;12,50;93,75;EUR;monthly" in nl
    assert "Korting (10%)" in nl and ";-14,37;" in nl
    en = costs.to_csv([s], "en")
    assert "Klant,D-1,2026-11,draft,resources,vCPU,,7.50,vCPU,12.50,93.75,EUR,monthly" in en
    for bad in ("resource.gpu", "m365.sku.", "device.printer"):
        assert not costs.KEY_RE.match(bad)


# ------------------------------------------------------------------ the whole flow
async def _seed(env_id: str, p: str = "cost") -> str:
    """A Proxmox VE node with three guests (one stopped, one template) and a Microsoft 365 tenant."""
    from mmn.db import sessionmaker
    from mmn.models import M365Tenant

    guests = [
        {"vmid": 100, "name": "dc01", "type": "qemu", "status": "running", "maxcpu": 4, "maxmem": 8 * GIB, "maxdisk": 100 * GIB},
        {"vmid": 101, "name": "app01", "type": "lxc", "status": "stopped", "maxcpu": 2, "maxmem": 2 * GIB, "maxdisk": 20 * GIB},
        {"vmid": 9000, "name": "tpl", "type": "qemu", "status": "stopped", "template": 1, "maxcpu": 2, "maxmem": GIB,
         "maxdisk": 10 * GIB},
    ]
    pve = await _machine(env_id, f"{p}-pve1", "proxmox", guests={"guests": guests}, cluster_name="costcl",
                         inventory={"hardware": {"manufacturer": "Dell Inc.", "model": "PowerEdge R650", "serial": "ABC1234"}})
    # The second node of the cluster reports the same guests: they count once.
    await _machine(env_id, f"{p}-pve2", "proxmox", guests={"guests": guests}, cluster_name="costcl")
    await _machine(env_id, f"{p}-srv", "windows")
    await _machine(env_id, f"{p}-pc", "windows", role="workstation",
                   inventory={"hardware": {"manufacturer": "LENOVO", "model": "ThinkPad T14", "serial": "To be filled by O.E.M."}})
    async with sessionmaker()() as db:
        db.add(M365Tenant(customer_id=env_id, name="kostenklant.nl", tenant_id="kostenklant.nl", client_id="c", snapshot={
            "licences": [
                {"sku_id": "a", "sku": "O365_BUSINESS_PREMIUM", "name": "Microsoft 365 Business Premium", "enabled": 10,
                 "consumed": 8, "free": False},
                {"sku_id": "b", "sku": "EXCHANGESTANDARD", "name": "Exchange Online (Plan 1)", "enabled": 2, "consumed": 2,
                 "free": False},
                {"sku_id": "c", "sku": "FLOW_FREE", "name": "Power Automate Free", "enabled": 10000, "consumed": 3, "free": True},
            ],
            "users": [{"enabled": True, "licences": ["O365_BUSINESS_PREMIUM"]} for _ in range(8)]
            + [{"enabled": False, "licences": ["EXCHANGESTANDARD"]}, {"enabled": True, "licences": []}],
        }))
        await db.commit()
    return pve


async def _drop(c, cid: str) -> None:
    """Other tests count the machines and guests of all customers: remove ours."""
    from sqlalchemy import delete

    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.environment_id == cid))
        await db.commit()
    assert (await c.delete(f"/api/customers/{cid}")).status_code == 204


async def test_costs_flow(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Kostenklant"})).json()
    cid = cust["id"]
    try:
        pve = await _seed(cid)
        period = (await c.get("/api/costs/overview")).json()["current_period"]

        # Live usage: the stopped guest counts, the template and the copy on the second node do not.
        u = (await c.get(f"/api/costs/usage/{cid}")).json()
        live = u["live"]
        assert live["cpu"] == 6 and live["ram_gb"] == 10 and live["storage_gb"] == 120 and live["guests"] == 2
        assert live["devices"] == {"server": 1, "workstation": 1, "proxmox": 2} and live["m365_users"] == 8
        assert set(live["m365"]) == {"O365_BUSINESS_PREMIUM", "EXCHANGESTANDARD"}
        assert u["month"]["estimated"] is True

        # Price list: defaults plus a customer price that wins.
        for key, price in (("resource.cpu", 10), ("resource.ram", 5), ("resource.storage", 0.1), ("device.server", 25),
                           ("device.workstation", 15), ("m365.sku.O365_BUSINESS_PREMIUM", 20.6)):
            r = await c.put("/api/costs/rates", json={"key": key, "price": price})
            assert r.status_code == 200, r.text
        assert (await c.put("/api/costs/rates", json={"key": "resource.gpu", "price": 1})).status_code == 400
        r = await c.put("/api/costs/rates", json={"key": "resource.cpu", "price": 8, "cost": 3, "customer_id": cid})
        assert r.status_code == 200 and r.json()["customer_id"] == cid
        rates = (await c.get(f"/api/costs/rates?customer_id={cid}")).json()
        assert len(rates["customer"]) == 1 and {x["key"] for x in rates["unpriced_skus"]} == {"m365.sku.EXCHANGESTANDARD"}

        # Items: a monthly contract with a notice period, a yearly licence, hardware, a one-off charge, an inactive one.
        today = date.fromisoformat(period + "-15")
        items = [
            {"kind": "contract", "name": "Managed service", "unit_price": 150, "cost_price": 0, "billing": "monthly",
             "end_date": (date.today() + timedelta(days=100)).isoformat(), "notice_days": 60, "reference": "C-2026-01"},
            {"kind": "licence", "name": "ESET Protect", "vendor": "ESET", "quantity": 12, "unit_price": 30, "cost_price": 22,
             "billing": "yearly", "start_date": today.isoformat(), "end_date": (date.today() + timedelta(days=20)).isoformat()},
            {"kind": "service", "name": "Installatie", "unit_price": 95.5, "billing": "once", "start_date": today.isoformat()},
            {"kind": "contract", "name": "Oude lijn", "unit_price": 40, "billing": "monthly", "active": False},
            {"kind": "contract", "name": "Hosting zonder prijs", "billing": "monthly"},
        ]
        ids = {}
        for it in items:
            r = await c.post("/api/costs/items", json={"customer_id": cid, **it})
            assert r.status_code == 201, r.text
            ids[it["name"]] = r.json()["id"]
        assert (await c.post("/api/costs/items", json={"customer_id": cid, "name": "x", "billing": "once"})).status_code == 400
        assert (await c.post("/api/costs/items", json={"customer_id": cid, "name": "x", "start_date": "2026-05-01",
                                                       "end_date": "2026-04-01"})).status_code == 400
        lst = (await c.get(f"/api/costs/items?customer_id={cid}")).json()
        by = {x["name"]: x for x in lst["items"]}
        assert by["Managed service"]["status"] == "notice" and by["ESET Protect"]["status"] == "expiring"
        assert by["ESET Protect"]["monthly"] == 30.0 and by["Oude lijn"]["status"] == "inactive"
        soon = (await c.get(f"/api/costs/items?customer_id={cid}&within_days=60")).json()["items"]
        assert [x["name"] for x in soon] == ["ESET Protect", "Managed service"]

        # Hardware from the inventory: the node with a real serial, not the workstation with a placeholder serial twice.
        r = await c.post("/api/costs/items/import-hardware", json={"customer_id": cid})
        assert r.status_code == 200 and r.json()["created"] == 2
        hw = {x["name"]: x for x in r.json()["items"]}
        assert hw["cost-pve1 (Dell Inc. PowerEdge R650)"]["serial"] == "ABC1234" and hw["cost-pve1 (Dell Inc. PowerEdge R650)"]["node_id"] == pve
        assert hw["cost-pc (LENOVO ThinkPad T14)"]["serial"] is None
        assert (await c.post("/api/costs/items/import-hardware", json={"customer_id": cid})).json()["created"] == 0

        # Customer settings: 10% discount and a debtor number.
        r = await c.put(f"/api/costs/customers/{cid}/settings", json={"discount_pct": 10, "debtor": "D-042", "billable": True,
                                                                      "bill_resources": True, "bill_devices": True, "bill_m365": True})
        assert r.status_code == 200 and r.json()["debtor"] == "D-042"

        # Today's reading, then the statement of the month.
        assert (await c.post("/api/costs/sample")).status_code == 202
        s = (await c.get(f"/api/costs/statement/{cid}?period={period}")).json()
        assert s["status"] == "draft" and s["usage"]["estimated"] is False and s["usage"]["days"] == 1
        lines = {x["key"]: x for x in s["lines"]}
        assert lines["resource.cpu"]["unit_price"] == 8 and lines["resource.cpu"]["amount"] == 48
        assert lines["resource.ram"]["amount"] == 50 and lines["resource.storage"]["amount"] == 12
        assert lines["device.server"]["amount"] == 25 and lines["device.workstation"]["amount"] == 15
        assert "device.proxmox" not in lines  # no price for it
        assert lines["m365.sku.O365_BUSINESS_PREMIUM"]["quantity"] == 10  # bought, not assigned (setting)
        assert lines["m365.sku.EXCHANGESTANDARD"]["missing_price"] is True
        assert "m365.sku.FLOW_FREE" not in lines
        names = [x["name"] for x in s["lines"] if x["key"].startswith("item:")]
        assert "ESET Protect" in names and "Installatie" in names and "Oude lijn" not in names
        assert lines[f"item:{ids['ESET Protect']}"]["amount"] == 360
        assert s["totals"]["missing_prices"] == 2 and s["totals"]["discount_pct"] == 10
        expected = 48 + 50 + 12 + 25 + 15 + 206 + 150 + 360 + 95.5
        assert abs(s["totals"]["subtotal"] - expected) < 0.01
        assert s["monthly_recurring"] == round(48 + 50 + 12 + 25 + 15 + 206 + 150 + 30, 2)

        # CSV and PDF exports.
        r = await c.get(f"/api/costs/statement/{cid}/export?period={period}&format=csv&lang=nl")
        assert r.status_code == 200 and "D-042" in r.text and "Managed service" in r.text and "Korting (10%)" in r.text
        r = await c.get(f"/api/costs/statement/{cid}/export?period={period}&format=pdf&lang=nl")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        r = await c.get(f"/api/costs/export?period={period}")
        assert r.status_code == 200 and "Kostenklant" in r.text

        # Final: frozen against later price changes, reopen works.
        r = await c.post(f"/api/costs/statement/{cid}/finalize", json={"period": period, "note": "Factuur 2026-118"})
        assert r.status_code == 200 and r.json()["status"] == "final"
        net = r.json()["totals"]["net"]
        assert (await c.post(f"/api/costs/statement/{cid}/finalize", json={"period": period})).status_code == 409
        await c.put("/api/costs/rates", json={"key": "device.server", "price": 99})
        assert (await c.get(f"/api/costs/statement/{cid}?period={period}")).json()["totals"]["net"] == net
        ov = (await c.get(f"/api/costs/overview?period={period}")).json()
        row = next(x for x in ov["customers"] if x["customer_id"] == cid)
        assert row["status"] == "final" and row["net"] == net and row["expiring"] == 2
        r = await c.delete(f"/api/costs/statement/{cid}/{period}")
        assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["totals"]["net"] > net

        # Alerts and nightly findings.
        from mmn.db import sessionmaker
        from mmn.models import Environment
        from mmn.services.customers import _finding

        async with sessionmaker()() as db:
            want = await costs.conditions(db, datetime.now(UTC))
            mine = {k: v for k, v in want.items() if v["detail"]["customer_id"] == cid}
            assert {v["detail"]["status"] for v in mine.values()} == {"notice", "expiring"}
            assert all(v["kind"] == "contract" for v in mine.values())
            e = await db.get(Environment, cid)
            found = await costs.findings(db, e, datetime.now(UTC), _finding)
            codes = {f["code"] for f in found}
            assert {"contract_notice", "licence_expiring", "cost_price_missing"} <= codes
            from mmn.services.nightly import category_of

            assert {category_of(f) for f in found} == {"contracts"}

        # Automatic renewal moves an ended term on.
        async with sessionmaker()() as db:
            it = await db.get(CostItem, ids["Managed service"])
            it.auto_renew, it.renew_months, it.end_date = True, 12, date.today() - timedelta(days=3)
            await db.commit()
            assert await costs.renew_due(db, date.today()) >= 1
            await db.commit()
            it = await db.get(CostItem, ids["Managed service"])
            assert it.end_date > date.today() and it.history[-1]["by"] == "auto-renew"

        # The section in the monthly report, only when asked for.
        from mmn.services import reports

        async with sessionmaker()() as db:
            e = await db.get(Environment, cid)
            assert (await reports.build(db, e, period, "nl"))["costs"] is None
        r = await c.put(f"/api/customers/{cid}/reports/settings", json={"enabled": False, "day": 1, "recipients": [],
                                                                         "lang": "nl", "include_costs": True})
        assert r.status_code == 200 and r.json()["include_costs"] is True
        async with sessionmaker()() as db:
            e = await db.get(Environment, cid)
            data = await reports.build(db, e, period, "nl")
            assert data["costs"]["labels"]["title"] == "Kosten deze maand" and data["costs"]["lines"]
            tz = await reports.timezone(db)
            html = reports.render_html(data, tz)
            assert "Kosten deze maand" in html and "ESET Protect" in html
            assert reports.render_pdf(reports._jsonable(data), tz).startswith(b"%PDF")

        # Delete an item; viewer-only endpoints refuse changes without admin are covered by require_admin.
        assert (await c.delete(f"/api/costs/items/{ids['Oude lijn']}")).status_code == 204
    finally:
        await _drop(c, cid)


async def test_costs_mcp_tools(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "MCP Kosten"})).json()
    mid = cust["id"]
    try:
        await _seed(mid, "mcpc")
        await c.put("/api/costs/rates", json={"key": "resource.cpu", "price": 10})
        token, _ = await _token(c, "claude-costs", ["read", "costs"])
        await _init(client, token)
        data, err = await _call(client, token, "get_resource_usage", {"customer": "MCP Kosten"})
        assert err is None, err
        assert data["now"]["cpu"] == 6
        # Without costs_admin no changes.
        data, err = await _call(client, token, "save_cost_item", {"customer": "MCP Kosten", "name": "x"})
        assert data is None and "costs_admin" in err
        admin_token, _ = await _token(c, "claude-costs-admin", ["read", "costs", "costs_admin"])
        await _init(client, admin_token)
        data, err = await _call(client, admin_token, "save_cost_item", {
            "customer": "MCP Kosten", "name": "Microsoft Defender", "kind": "licence", "quantity": 5, "unit_price": 3,
            "end_date": (date.today() + timedelta(days=10)).isoformat(), "machine": "mcpc-srv"})
        assert err is None, err
        assert data["status"] == "expiring" and data["node_name"] is None and data["node_id"]
        data, err = await _call(client, admin_token, "save_cost_item", {"customer": "MCP Kosten", "item": "Microsoft Defender",
                                                                        "quantity": 6})
        assert err is None and data["quantity"] == 6 and data["unit_price"] == 3
        data, err = await _call(client, token, "list_expiring_contracts", {"customer": "MCP Kosten"})
        assert err is None and [x["name"] for x in data["items"]] == ["Microsoft Defender"]
        data, err = await _call(client, admin_token, "set_cost_rate", {"key": "m365.sku.EXCHANGESTANDARD", "price": 4.2,
                                                                       "customer": "MCP Kosten"})
        assert err is None and data["price"] == 4.2
        data, err = await _call(client, token, "get_cost_statement", {"customer": "MCP Kosten"})
        assert err is None, err
        keys = {x["key"]: x for x in data["lines"]}
        assert keys["resource.cpu"]["amount"] == 60 and keys["m365.sku.EXCHANGESTANDARD"]["amount"] == 8.4
        assert "m365.sku.O365_BUSINESS_PREMIUM" in keys and keys["resource.cpu"]["name"] == "vCPU"
        data, err = await _call(client, token, "get_cost_overview", {})
        assert err is None and any(x["customer_name"] == "MCP Kosten" for x in data["customers"])
        data, err = await _call(client, token, "list_cost_rates", {"customer": "MCP Kosten"})
        assert err is None and data["customer"][0]["key"] == "m365.sku.EXCHANGESTANDARD"
        period = (await c.get("/api/costs/overview")).json()["current_period"]
        data, err = await _call(client, admin_token, "finalize_cost_statement", {"customer": "MCP Kosten", "period": period})
        assert err is None and data["status"] == "final"
        data, err = await _call(client, token, "list_cost_items", {"customer": "MCP Kosten", "kind": "licence"})
        assert err is None and len(data["items"]) == 1
        data, err = await _call(client, admin_token, "delete_cost_item", {"customer": "MCP Kosten", "item": "Microsoft Defender"})
        assert err is None and data["deleted"] == "Microsoft Defender"
    finally:
        await _drop(c, mid)
