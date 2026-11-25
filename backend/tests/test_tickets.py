"""Phase 17: the customer portal with tickets: portal accounts and isolation, tickets, replies, internal
notes, status changes, mails, the first diagnosis and the MCP tools."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.services import tickets

PW = "portal password 123"


def _pcsrf(client) -> dict[str, str]:
    return {"x-portal-csrf": client.cookies.get("sp_portal_csrf", "")}


async def _mail_on(c, smtp_server):
    r = await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                                "security": "none", "from_address": "app@example.com"})
    assert r.status_code == 200, r.text
    while not smtp_server.queue.empty():
        smtp_server.queue.get_nowait()


async def _mails(smtp_server, n: int, timeout: float = 10) -> list:
    out = [await smtp_server.wait(timeout) for _ in range(n)]
    return out


def _body(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


async def _wait_diagnosis(c, tid: str, timeout: float = 10) -> dict:
    for _ in range(int(timeout * 10)):
        d = (await c.get(f"/api/tickets/{tid}")).json()
        if d["diagnosis"].get("status") in ("done", "failed"):
            return d
        await asyncio.sleep(0.1)
    raise AssertionError(f"diagnosis did not finish: {d['diagnosis']}")


def test_mentions_and_summary():
    from types import SimpleNamespace as N

    nodes = [N(id="a", name="RNN-APP01"), N(id="b", name="pc"), N(id="c", name="dc01")]
    assert tickets._mentions("De app op rnn-app01 start niet, ook dc01-backup faalt", nodes) == ["a"]
    verdict, lines, steps = tickets._summary(
        [{"name": "srv1", "status": "issues", "findings": ["disk / 95% full"], "failed_services": ["nginx"]}],
        None, [], [])
    assert verdict == "issues_found" and "srv1: disk / 95% full" in lines and any("cleanup_disk" in s for s in steps)
    assert tickets._summary([{"name": "x", "status": "offline", "role": "server"}], None, [], [])[0] == "unreachable"
    assert tickets._summary([{"name": "x", "status": "ok"}], None, [], [])[0] == "no_issues"
    assert tickets._one_line("Kassa\r\nBcc: x@y.z\tstuk") == "Kassa Bcc: x@y.z stuk"


async def test_portal_flow(app, admin_client, smtp_server, monkeypatch):
    c = admin_client
    await _mail_on(c, smtp_server)
    try:
        await _portal_flow(app, c, smtp_server, monkeypatch)
    finally:
        # Later tests expect the mail server to be off.
        await c.put("/api/settings/mail", json={"enabled": False})


async def _portal_flow(app, c, smtp_server, monkeypatch):
    from mmn.services import agent_hub

    a = (await c.post("/api/customers", json={"name": "Portaal Klant A"})).json()
    b = (await c.post("/api/customers", json={"name": "Portaal Klant B"})).json()
    srv = await _machine(a["id"], "pa-app01", capabilities=["diagnose"])
    pc_a = await _machine(a["id"], "PA-PC01", "windows", role="workstation")
    srv_b = await _machine(b["id"], "pb-sql01")

    async def request(node_id, method, params=None, timeout=60):
        assert method == "diagnose" and node_id == srv
        return {"findings": ["disk / 96% full"], "failed_services": [{"name": "apache2.service"}],
                "recent_errors": "line 1\nAH00558 could not bind", "top_cpu": [{"name": "php-fpm"}]}

    monkeypatch.setattr(agent_hub, "request", request)
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: nid == srv)

    # Invite: a mail with a link to choose a password.
    r = await c.post("/api/tickets/portal-users", json={
        "customer_id": a["id"], "email": "Jan@Klant-A.example", "full_name": "Jan", "language": "nl"})
    assert r.status_code == 201, r.text
    jan = r.json()
    assert jan["email"] == "jan@klant-a.example" and jan["invite_sent"] and not jan["activated"]
    assert (await c.post("/api/tickets/portal-users", json={
        "customer_id": b["id"], "email": "jan@klant-a.example"})).status_code == 409
    msg = (await _mails(smtp_server, 1))[0]
    assert "klantportaal" in msg["Subject"].lower()
    token = re.search(r"set-password\?token=([\w-]+)", _body(msg)).group(1)

    import httpx

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50001))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as pc:
        # Not signed in, and a staff session is no portal session.
        assert (await pc.get("/api/portal/me")).status_code == 401
        assert (await c.get("/api/portal/me")).status_code == 401
        assert (await pc.post("/api/portal/login", json={"email": jan["email"], "password": PW})).status_code == 401
        r = await pc.post("/api/portal/set-password", json={"token": token, "password": PW})
        assert r.status_code == 200, r.text
        assert r.json()["customer_name"] == "Portaal Klant A"
        assert (await pc.post("/api/portal/set-password", json={"token": token, "password": PW})).status_code == 400
        # A portal session can't reach the staff API.
        assert (await pc.get("/api/tickets")).status_code == 401
        assert (await pc.get("/api/customers")).status_code == 401
        # CSRF
        assert (await pc.post("/api/portal/tickets", json={"subject": "x", "body": "y"})).status_code == 403
        pc.headers.update(_pcsrf(pc))

        machines = (await pc.get("/api/portal/machines")).json()
        assert {m["name"] for m in machines} == {"pa-app01", "PA-PC01"}
        assert set(machines[0]) == {"id", "name", "kind", "role", "type", "online"}

        # Machines of another customer are refused.
        r = await pc.post("/api/portal/tickets", json={"subject": "SQL traag", "body": "...", "node_ids": [srv_b]})
        assert r.status_code == 400

        r = await pc.post("/api/portal/tickets", json={
            "subject": "Website geeft een foutmelding", "body": "Sinds vanochtend krijgen we een 503.\n\nGraag snel.",
            "priority": "high", "node_ids": [srv]})
        assert r.status_code == 201, r.text
        tk = r.json()
        assert tk["status"] == "new" and tk["machines"] == [{"id": srv, "name": "pa-app01"}]
        assert "diagnosis_status" not in tk and "assignee_id" not in tk

        # Staff mail + confirmation to the customer.
        mails = await _mails(smtp_server, 2)
        subjects = sorted(m["Subject"] for m in mails)
        assert any(s.startswith(f"New ticket #{tk['number']} from Portaal Klant A") for s in subjects)
        assert any(s.startswith(f"We hebben je ticket #{tk['number']}") for s in subjects)

        # The first diagnosis ran on the linked server; staff see it, the customer doesn't.
        d = await _wait_diagnosis(c, tk["id"])
        diag = d["diagnosis"]
        assert diag["verdict"] == "issues_found" and not diag["scanned_all"]
        assert diag["machines"][0]["failed_services"] == ["apache2.service"]
        assert "pa-app01: disk / 96% full" in diag["summary"]
        ai = [m for m in d["thread"] if m["author_kind"] == "ai"]
        assert ai and ai[0]["internal"] and "apache2.service" in ai[0]["body"]
        pt = (await pc.get(f"/api/portal/tickets/{tk['id']}")).json()
        assert "diagnosis" not in pt and all(m["author_kind"] != "ai" for m in pt["thread"])

        # Staff: internal note (not in the portal, no mail), then a public reply asking a question.
        r = await c.post(f"/api/tickets/{tk['id']}/messages", json={"body": "Apache staat stil, kijk ik naar", "internal": True})
        assert r.status_code == 201 and r.json()["status"] == "new"
        r = await c.post(f"/api/tickets/{tk['id']}/messages", json={
            "body": "We kijken ernaar. Welke pagina precies?", "status": "waiting_customer"})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "waiting_customer" and r.json()["first_response_at"]
        m = (await _mails(smtp_server, 1))[0]
        assert m["To"] == "jan@klant-a.example" and "Welke pagina precies?" in _body(m)
        pt = (await pc.get(f"/api/portal/tickets/{tk['id']}")).json()
        bodies = [x["body"] for x in pt["thread"]]
        assert "We kijken ernaar. Welke pagina precies?" in bodies
        assert not any("kijk ik naar" in x for x in bodies) and all("internal" not in x for x in pt["thread"])

        # The customer replies: the ticket opens again and staff get a mail.
        r = await pc.post(f"/api/portal/tickets/{tk['id']}/messages", json={"body": "De homepage."})
        assert r.status_code == 201
        assert (await pc.get(f"/api/portal/tickets/{tk['id']}")).json()["status"] == "open"
        m = (await _mails(smtp_server, 1))[0]
        assert m["Subject"].startswith(f"Customer reply on ticket #{tk['number']}")
        lst = (await c.get("/api/tickets", params={"customer_id": a["id"]})).json()
        row = next(x for x in lst["tickets"] if x["id"] == tk["id"])
        assert row["needs_reply"] and row["messages"] >= 3

        # Assign, resolve: the customer gets a mail.
        me = (await c.get("/api/auth/me")).json()
        r = await c.patch(f"/api/tickets/{tk['id']}", json={"assignee_id": me["id"], "status": "resolved"})
        assert r.status_code == 200, r.text
        assert r.json()["assignee_id"] == me["id"] and r.json()["resolved_at"]
        m = (await _mails(smtp_server, 1))[0]
        assert m["Subject"].startswith(f"Ticket #{tk['number']} is opgelost")

        # Another customer's portal account sees nothing of customer A.
        r = await c.post("/api/tickets/portal-users", json={"customer_id": b["id"], "email": "piet@b.example",
                                                            "send_invite": False})
        piet = r.json()
        from mmn.db import sessionmaker
        from mmn.models import PortalUser

        async with sessionmaker()() as db:
            u = await db.get(PortalUser, piet["id"])
            ptok = await tickets.issue_token(db, u, "invite")
            await db.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as pb:
            assert (await pb.post("/api/portal/set-password", json={"token": ptok, "password": PW})).status_code == 200
            pb.headers.update(_pcsrf(pb))
            assert (await pb.get(f"/api/portal/tickets/{tk['id']}")).status_code == 404
            assert (await pb.post(f"/api/portal/tickets/{tk['id']}/messages", json={"body": "x"})).status_code == 404
            assert (await pb.get("/api/portal/tickets")).json()["tickets"] == []
            assert {m["name"] for m in (await pb.get("/api/portal/machines")).json()} == {"pb-sql01"}

        # Members only see their own tickets; managers all of the customer.
        r = await c.post("/api/tickets/portal-users", json={"customer_id": a["id"], "email": "kees@klant-a.example",
                                                            "send_invite": False})
        kees = r.json()
        async with sessionmaker()() as db:
            ktok = await tickets.issue_token(db, await db.get(PortalUser, kees["id"]), "invite")
            await db.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as pk:
            assert (await pk.post("/api/portal/set-password", json={"token": ktok, "password": PW})).status_code == 200
            pk.headers.update(_pcsrf(pk))
            assert (await pk.get(f"/api/portal/tickets/{tk['id']}")).status_code == 404
            assert (await c.patch(f"/api/tickets/portal-users/{kees['id']}", json={"role": "manager"})).status_code == 200
            assert (await pk.get(f"/api/portal/tickets/{tk['id']}")).status_code == 200
            # Deactivated: signed out.
            assert (await c.patch(f"/api/tickets/portal-users/{kees['id']}", json={"is_active": False})).status_code == 200
            assert (await pk.get("/api/portal/me")).status_code == 401

        # The customer closes it; replies are refused afterwards.
        r = await pc.post(f"/api/portal/tickets/{tk['id']}/close")
        assert r.status_code == 200 and r.json()["status"] == "closed" and not r.json()["can_reply"]
        assert (await pc.post(f"/api/portal/tickets/{tk['id']}/messages", json={"body": "nog iets"})).status_code == 400

        # Mentioned machine names are linked; with no machine at all every server is checked.
        r = await pc.post("/api/portal/tickets", json={"subject": "PA-PC01 is traag", "body": "Mijn pc."})
        t2 = r.json()
        assert t2["machines"] == [{"id": pc_a, "name": "PA-PC01"}]
        d2 = await _wait_diagnosis(c, t2["id"])
        assert d2["diagnosis"]["verdict"] == "unreachable"
        await _mails(smtp_server, 2)
        r = await pc.post("/api/portal/tickets", json={"subject": "Vraag over licenties", "body": "Hoeveel?",
                                                       "category": "question"})
        t3 = r.json()
        d3 = await _wait_diagnosis(c, t3["id"])
        assert d3["diagnosis"]["scanned_all"] and d3["diagnosis"]["machines"] == []
        await _mails(smtp_server, 2)

        # Logout ends the portal session.
        assert (await pc.post("/api/portal/logout")).status_code == 204
        assert (await pc.get("/api/portal/me")).status_code == 401


async def test_auto_close_and_staff_ticket(admin_client):
    from mmn.db import sessionmaker
    from mmn.models import Ticket

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Ticket Telefoon"})).json()
    r = await c.post("/api/tickets", json={"customer_id": cust["id"], "subject": "Belde over printer",
                                           "body": "Printer op 2e verdieping", "reporter_name": "Els",
                                           "diagnose": False})
    assert r.status_code == 201, r.text
    tk = r.json()
    assert tk["created_by"] == "admin" and tk["reporter_name"] == "Els"
    assert (await c.patch(f"/api/tickets/{tk['number']}", json={"status": "resolved"})).status_code == 200
    async with sessionmaker()() as db:
        row = await db.get(Ticket, tk["id"])
        row.resolved_at = datetime.now(UTC) - timedelta(days=8)
        await db.commit()
    assert await tickets.close_stale() >= 1
    d = (await c.get(f"/api/tickets/{tk['id']}")).json()
    assert d["status"] == "closed" and d["thread"][-1]["event"]["note"] == "no reply in 7 days"
    assert (await c.patch(f"/api/tickets/{tk['id']}", json={"status": "bogus"})).status_code == 400
    s = (await c.put("/api/tickets/settings", json={"enabled": False, "auto_close_days": 3})).json()
    assert s["enabled"] is False and s["auto_close_days"] == 3
    assert (await c.post("/api/portal/login", json={"email": "x@y.z", "password": "x"})).status_code == 403
    await c.put("/api/tickets/settings", json={"enabled": True, "auto_close_days": 7})
    assert (await c.delete(f"/api/tickets/{tk['id']}")).status_code == 204


async def test_ticket_mcp_tools(admin_client, client, monkeypatch):
    from mmn.services import agent_hub

    monkeypatch.setattr(agent_hub, "is_online", lambda nid: False)
    c = admin_client
    a = (await c.post("/api/customers", json={"name": "Ticket MCP A"})).json()
    b = (await c.post("/api/customers", json={"name": "Ticket MCP B"})).json()
    await _machine(a["id"], "tm-dc01", "windows")
    token, _ = await _token(c, "claude-tickets", ["read", "tickets", "logs"])
    await _init(client, token)

    out, err = await _call(client, token, "create_ticket", {
        "customer": "Ticket MCP A", "subject": "Backup faalt", "description": "De backup van tm-dc01 faalt.",
        "machines": ["tm-dc01"], "diagnose": False})
    assert err is None, err
    num = out["ref"]
    out, err = await _call(client, token, "list_tickets", {"customer": "Ticket MCP A"})
    assert err is None and [t["ref"] for t in out["tickets"]] == [num]
    out, err = await _call(client, token, "reply_ticket", {"ticket": num, "message": "Disk is vol", "internal": True})
    assert err is None and out["thread"][-1]["internal"] and out["thread"][-1]["author_kind"] == "ai"
    out, err = await _call(client, token, "update_ticket", {"ticket": num, "status": "on_hold", "priority": "urgent",
                                                            "assignee": "admin"})
    assert err is None, err
    assert out["status"] == "on_hold" and out["priority"] == "urgent" and out["assignee_name"]
    assert set(out["changed"]) == {"status", "priority", "assigned"}
    out, err = await _call(client, token, "diagnose_ticket", {"ticket": num})
    assert err is None and out["verdict"] == "unreachable"
    out, err = await _call(client, token, "get_ticket", {"ticket": num})
    assert err is None and out["diagnosis"]["status"] == "done" and out["customer_context"] is not None
    _, err = await _call(client, token, "update_ticket", {"ticket": num, "machines": ["nope"]})
    assert err and "not found" in err

    # A token tied to customer B doesn't see customer A's ticket.
    tok_b, _ = await _token(c, "claude-tickets-b", ["read", "tickets"], customer_ids=[b["id"]])
    await _init(client, tok_b)
    _, err = await _call(client, tok_b, "get_ticket", {"ticket": num})
    assert err and "not found" in err
    out, err = await _call(client, tok_b, "list_tickets", {"status": ""})
    assert err is None and out["tickets"] == []
    # Read-only tokens can't reply.
    tok_r, _ = await _token(c, "claude-tickets-ro", ["read"])
    await _init(client, tok_r)
    _, err = await _call(client, tok_r, "reply_ticket", {"ticket": num, "message": "x"})
    assert err and "tickets" in err
