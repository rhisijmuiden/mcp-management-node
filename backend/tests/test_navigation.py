"""v0.26.0: what is linked per customer, the integrations overview and "view as customer" in the portal."""

from __future__ import annotations

from test_customers import _machine


async def test_customer_links_and_todo(admin_client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Links BV"})).json()
    r = await c.get(f"/api/customers/{cust['id']}/links")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["tiles"]["servers"] == {"total": 0, "online": 0}
    keys = {x["key"] for x in d["todo"]}
    assert {"machines", "maintenance_window", "sla", "contacts", "portal", "uptime"} <= keys

    srv = await _machine(cust["id"], "links-srv01")
    await c.put(f"/api/customers/{cust['id']}", json={"name": "Links BV", "maintenance_window": "Sun 02:00-05:00",
                                                       "sla": "Business", "contact_name": "Ans"})
    d = (await c.get(f"/api/customers/{cust['id']}/links")).json()
    assert d["tiles"]["servers"]["total"] == 1
    todo = {x["key"]: x for x in d["todo"]}
    done = {x["key"] for x in d["done"]}
    assert {"machines", "maintenance_window", "sla", "contacts"} <= done
    assert todo["offline"]["values"]["count"] == 1  # the test machine has no agent connected
    assert "backups_unknown" in todo

    # Other tests count every machine: remove ours again.
    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.delete(await db.get(Node, srv))
        await db.commit()


async def test_integrations_overview(admin_client):
    r = await admin_client.get("/api/integrations")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["github"]["configured"] is False
    assert set(d) >= {"m365", "unifi", "mcp", "ai", "agents", "mail", "app_backups"}


async def test_portal_preview_is_admin_only_and_read_only(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Preview BV"})).json()
    h = {"X-Portal-Preview": cust["id"]}
    st = (await c.get("/api/portal/status", headers=h)).json()
    assert st["authenticated"] is True and st["preview"] is True
    me = (await c.get("/api/portal/me", headers=h)).json()
    assert me["customer_id"] == cust["id"] and me["role"] == "manager" and me["customer_name"] == "Preview BV"
    assert (await c.get("/api/portal/tickets", headers=h)).status_code == 200
    # Nothing can be sent in the name of the customer.
    r = await c.post("/api/portal/tickets", headers={**h, "X-CSRF-Token": c.cookies.get("mmn_csrf", "")},
                     json={"subject": "x", "body": "y"})
    assert r.status_code == 403 and r.json()["detail"] == "portal_preview_read_only"
    # Without a staff session the header does nothing; an unknown customer neither.
    async with client.__class__(transport=client._transport, base_url=client.base_url) as anon:
        assert (await anon.get("/api/portal/me", headers=h)).status_code == 401
    assert (await c.get("/api/portal/me", headers={"X-Portal-Preview": "nope"})).status_code == 401
