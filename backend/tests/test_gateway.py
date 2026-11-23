"""Phase 8: AI gateway: policies, approvals, maintenance windows and the kill switch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from test_customers import _call
from test_mcp import _init

from mmn.services import gateway

TEST_GLOBAL = {"risks": {"read": "allow", "operate": "allow", "change": "allow", "destructive": "allow"},
               "tools": {}, "outside_window": "ignore"}


def test_parse_maintenance_window():
    w = gateway.parse_window("Tue 22:00–02:00")
    assert w and w[0].days == {1} and (w[0].start, w[0].end) == (22 * 60, 2 * 60)
    # Tuesday 23:00 and Wednesday 01:30 are inside, Wednesday 03:00 and Tuesday 21:00 are not.
    tue = datetime(2026, 11, 24, 23, 0)
    assert tue.weekday() == 1
    assert gateway.in_window(w, tue)
    assert gateway.in_window(w, tue + timedelta(hours=2, minutes=30))
    assert not gateway.in_window(w, tue + timedelta(hours=4))
    assert not gateway.in_window(w, tue - timedelta(hours=2))
    assert gateway.parse_window("Mon-Fri 18:00-07:00")[0].days == {0, 1, 2, 3, 4}
    assert gateway.parse_window("za, zo 00:00-06:00")[0].days == {5, 6}
    assert gateway.parse_window("dagelijks 02:00 - 04:00")[0].days == set(range(7))
    assert gateway.parse_window("02:00-04:00")[0].days == set(range(7))
    assert len(gateway.parse_window("Tue 22:00-02:00; Sat 08:00-12:00")) == 2
    assert gateway.parse_window("weekend 9-17")[0].days == {5, 6}
    assert gateway.parse_window("when the customer says so") is None
    assert gateway.parse_window("") is None


def test_risk_levels():
    import mmn.mcp_server  # noqa: F401 - registers the tools

    r = {t["name"]: t["risk"] for t in gateway.catalog()}
    assert r["get_overview"] == "read"
    assert r["guest_power"] == "operate"
    assert r["update_customer"] == "change"
    assert r["run_command"] == "destructive" and r["unlock_door"] == "destructive"


async def _token(c, name, scopes, customer_ids=None):
    r = await c.post("/api/tokens", json={"name": name, "scopes": scopes, "customer_ids": customer_ids})
    assert r.status_code == 201, r.text
    return r.json()["token"], r.json()["id"]


async def test_approval_flow(admin_client, client, smtp_server):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Gateway Klant"})).json()
    token, tok_id = await _token(c, "claude-gw", ["read", "nodes"])
    await _init(client, token)
    await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                            "security": "none", "from_address": "app@example.com"})
    try:
        r = await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                                       "notify_app": True, "notify_mail": True,
                                                       "notify_mail_to": "boss@example.com"})
        assert r.status_code == 200, r.text
        r = await c.put("/api/gateway/policies/global", json={**TEST_GLOBAL, "risks": {
            **TEST_GLOBAL["risks"], "change": "approve"}})
        assert r.status_code == 200 and r.json()["risks"]["change"] == "approve"

        # A change waits for approval and has not run yet.
        args = {"customer": cust["name"], "description": "Set by the AI"}
        data, err = await _call(client, token, "update_customer", args)
        assert err is None, err
        assert data["status"] == "pending_approval" and data["approval_id"]
        aid = data["approval_id"]
        assert (await c.get(f"/api/customers/{cust['id']}")).json()["description"] != "Set by the AI"
        mail = await smtp_server.wait()
        assert mail["To"] == "boss@example.com" and "update_customer" in mail["Subject"]
        await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                                   "notify_app": True, "notify_mail": False, "notify_mail_to": ""})

        # The same call again gives the same request instead of a duplicate.
        again, _ = await _call(client, token, "update_customer", args, 11)
        assert again["approval_id"] == aid
        pending = (await c.get("/api/gateway/approvals")).json()
        assert [a["id"] for a in pending if a["tool"] == "update_customer"] == [aid]
        assert (await c.get("/api/gateway/approvals/count")).json()["pending"] >= 1
        detail = (await c.get(f"/api/gateway/approvals/{aid}")).json()
        assert detail["customer_names"] == ["Gateway Klant"] and detail["args"]["description"] == "Set by the AI"
        assert detail["reasons"][0]["code"] == "risk"

        st, _ = await _call(client, token, "get_approval", {"approval_id": aid}, 12)
        assert st["status"] == "pending"

        # Approve: the app runs the call with the token's rights.
        r = await c.post(f"/api/gateway/approvals/{aid}/approve", json={"note": "ok"})
        assert r.status_code == 200, r.text
        for _ in range(40):
            st, _ = await _call(client, token, "get_approval", {"approval_id": aid}, 13)
            if st["status"] in ("executed", "failed"):
                break
            await asyncio.sleep(0.1)
        assert st["status"] == "executed", st
        assert st["decided_by"] == "admin"
        assert (await c.get(f"/api/customers/{cust['id']}")).json()["description"] == "Set by the AI"
        assert (await c.post(f"/api/gateway/approvals/{aid}/approve", json={})).status_code == 409

        # Reject.
        data, _ = await _call(client, token, "update_customer", {"customer": cust["name"], "sla": "24/7"}, 14)
        r = await c.post(f"/api/gateway/approvals/{data['approval_id']}/reject", json={"note": "not now"})
        assert r.status_code == 200
        st, _ = await _call(client, token, "get_approval", {"approval_id": data["approval_id"]}, 15)
        assert st["status"] == "rejected" and st["decision_note"] == "not now"
        assert (await c.get(f"/api/customers/{cust['id']}")).json()["sla"] != "24/7"

        # Approved while the AI is still waiting: the call answers with the real result.
        await c.put("/api/gateway/settings", json={"approval_wait_seconds": 10, "approval_expiry_hours": 24,
                                                   "notify_app": True, "notify_mail": False, "notify_mail_to": ""})
        call = asyncio.create_task(_call(client, token, "update_customer",
                                         {"customer": cust["name"], "contact_name": "Waiter"}, 16))
        aid2 = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            rows = [a for a in (await c.get("/api/gateway/approvals")).json() if a["tool"] == "update_customer"]
            if rows:
                aid2 = rows[0]["id"]
                break
        assert aid2
        await c.post(f"/api/gateway/approvals/{aid2}/approve", json={})
        data, err = await call
        assert err is None and data.get("status") != "pending_approval", data
        assert (await c.get(f"/api/customers/{cust['id']}")).json()["contact_name"] == "Waiter"

        # Another token cannot read this token's approvals.
        other, _ = await _token(c, "other-gw", ["read"])
        await _init(client, other)
        _, err = await _call(client, other, "get_approval", {"approval_id": aid}, 17)
        assert err and "not found" in err

        ov = (await c.get("/api/gateway/overview")).json()
        assert ov["stats"]["calls_24h"] > 0 and ov["sessions"]
        assert any(x["state"] == "pending" for x in ov["activity"]) or ov["stats"]["approved_24h"] >= 2
    finally:
        await c.put("/api/settings/mail", json={"enabled": False})
        await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                                   "notify_app": True, "notify_mail": True, "notify_mail_to": ""})
        await c.put("/api/gateway/policies/global", json=TEST_GLOBAL)


async def test_policy_layers_block_and_window(admin_client, client):
    c = admin_client
    home = (await c.post("/api/customers", json={"name": "Layer Home", "kind": "home"})).json()
    # A window that is certainly not now: one minute, twelve hours from now.
    from mmn.db import sessionmaker

    async with sessionmaker()() as db:
        now = await gateway.local_now(db)
    later = now + timedelta(hours=12)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    window = f"{days[later.weekday()]} {later:%H:%M}-{later + timedelta(minutes=1):%H:%M}"
    cust = (await c.post("/api/customers", json={"name": "Layer Klant", "maintenance_window": window})).json()
    token, tok_id = await _token(c, "claude-layers", ["read", "nodes"])
    await _init(client, token)
    try:
        await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                                   "notify_app": True, "notify_mail": False, "notify_mail_to": ""})
        await c.put("/api/gateway/policies/global", json={**TEST_GLOBAL, "risks": {
            **TEST_GLOBAL["risks"], "change": "approve"}, "outside_window": "approve"})
        # The customer "Layer Home" allows changes itself.
        r = await c.put(f"/api/gateway/policies/customers/{home['id']}", json={"risks": {"change": "allow"}})
        assert r.status_code == 200, r.text
        data, err = await _call(client, token, "update_customer", {"customer": home["name"], "sla": "best effort"})
        assert err is None and data.get("status") != "pending_approval", data

        # A token rule can only make it stricter.
        await c.put(f"/api/gateway/policies/tokens/{tok_id}", json={"tools": {"update_customer": "block"}})
        _, err = await _call(client, token, "update_customer", {"customer": home["name"], "sla": "x"}, 11)
        assert err and "Blocked by policy" in err
        await c.delete(f"/api/gateway/policies/tokens/{tok_id}")

        # Outside the maintenance window, even an allowed change needs approval.
        await c.put(f"/api/gateway/policies/customers/{cust['id']}", json={"risks": {"change": "allow"}})
        data, err = await _call(client, token, "update_customer", {"customer": cust["name"], "sla": "y"}, 12)
        assert err is None and data["status"] == "pending_approval", data
        assert "maintenance window" in " ".join(data["reasons"])

        pol = (await c.get("/api/gateway/policies")).json()
        row = next(x for x in pol["customers"] if x["customer_id"] == cust["id"])
        assert row["window_understood"] is True and row["in_window"] is False
        assert any(t["name"] == "run_command" for t in pol["tools"])

        g, _ = await _call(client, token, "get_guardrails", {"customer": cust["name"]}, 13)
        assert g["risks"]["change"]["action"] == "approve" and g["in_maintenance_window"] is False
        assert g["risks"]["read"]["action"] == "allow"

        # Dry runs count as read-only.
        await c.put("/api/gateway/policies/global", json={**TEST_GLOBAL, "risks": {
            **TEST_GLOBAL["risks"], "destructive": "block"}})
        net, _ = await _token(c, "claude-net", ["read", "network"])
        await _init(client, net)
        _, err = await _call(client, net, "apply_network_changes", {"node": "nope", "dry_run": True}, 14)
        assert err and "Blocked" not in err and "not found" in err
        _, err = await _call(client, net, "apply_network_changes", {"node": "nope", "dry_run": False}, 15)
        assert err and "Blocked by policy" in err
    finally:
        await c.put("/api/gateway/policies/global", json=TEST_GLOBAL)
        await c.delete(f"/api/gateway/policies/customers/{cust['id']}")
        await c.delete(f"/api/gateway/policies/customers/{home['id']}")


async def test_kill_switch(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Pause Klant"})).json()
    token, _ = await _token(c, "claude-pause", ["read", "nodes"])
    await _init(client, token)
    r = await c.post("/api/gateway/pause", json={"note": "incident"})
    assert r.status_code == 200 and r.json()["paused"] is True
    try:
        assert (await c.get("/api/gateway/status")).json()["paused"] is True
        _, err = await _call(client, token, "update_customer", {"customer": cust["name"], "sla": "z"})
        assert err and "paused" in err and "incident" in err
        data, err = await _call(client, token, "list_customers", {}, 11)
        assert err is None  # reading still works
        g, err = await _call(client, token, "get_guardrails", {}, 12)
        assert err is None and g["paused"] is True and g["tools"]["run_command"] == "destructive"
    finally:
        r = await c.post("/api/gateway/resume")
        assert r.json()["paused"] is False
    data, err = await _call(client, token, "update_customer", {"customer": cust["name"], "sla": "z"}, 13)
    assert err is None and data.get("status") != "pending_approval"

    # Pause one customer only.
    await c.put(f"/api/gateway/policies/customers/{cust['id']}", json={"paused": True})
    _, err = await _call(client, token, "update_customer", {"customer": cust["name"], "sla": "q"}, 14)
    assert err and "paused for customer" in err
    await c.delete(f"/api/gateway/policies/customers/{cust['id']}")


async def test_viewer_cannot_approve(admin_client):
    import httpx
    from conftest import csrf

    c = admin_client
    await c.post("/api/users", json={"username": "viewer-gw", "password": "viewer-password-gw"})
    async with httpx.AsyncClient(transport=c._transport, base_url="http://testserver") as v:
        await v.post("/api/auth/login", json={"username": "viewer-gw", "password": "viewer-password-gw"})
        assert (await v.get("/api/gateway/approvals", headers=csrf(v))).status_code == 403
        assert (await v.post("/api/gateway/pause", json={}, headers=csrf(v))).status_code == 403
        assert (await v.get("/api/gateway/approvals/count", headers=csrf(v))).json() == {"pending": 0}
        assert (await v.get("/api/gateway/overview", headers=csrf(v))).status_code == 200
