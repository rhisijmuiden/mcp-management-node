"""Phase 22: change calendar per customer. Conflicts with the maintenance window, other changes and scheduled
tasks, the free-slot suggestion, notices before/after (and when moved or cancelled), status by time, the
customer portal, the iCal feeds and the MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as N
from zoneinfo import ZoneInfo

from test_costs import _drop
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init
from test_tickets import _body, _mail_on

from mmn.services import changes, gateway

AMS = ZoneInfo("Europe/Amsterdam")


def _next_saturday(at_hour: int = 22, weeks: int = 2) -> datetime:
    """A Saturday ``weeks`` from now at ``at_hour`` local time, as UTC."""
    d = datetime.now(AMS).replace(hour=at_hour, minute=0, second=0, microsecond=0) + timedelta(weeks=weeks)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d.astimezone(UTC)


def _drain(smtp_server) -> list:
    out = []
    while not smtp_server.queue.empty():
        out.append(smtp_server.queue.get_nowait())
    return out


# ------------------------------------------------------------------ unit tests
def test_window_cover_and_notices():
    w = gateway.parse_window("Sat 22:00-02:00")
    sat = datetime(2026, 12, 5, 22, 0)
    assert changes.covered(w, sat, sat + timedelta(hours=4))
    assert changes.covered(w, sat + timedelta(minutes=30), sat + timedelta(hours=2))
    assert not changes.covered(w, sat - timedelta(minutes=30), sat + timedelta(hours=1))
    assert not changes.covered(w, sat + timedelta(hours=3), sat + timedelta(hours=5))
    assert changes.covered(gateway.parse_window("daily 00:00-00:00"), sat, sat + timedelta(hours=30))

    start = datetime(2026, 12, 5, 21, 0, tzinfo=UTC)
    ch = N(status="planned", starts_at=start, notices=[])
    assert changes.due_notices(ch, [4320, 60], start - timedelta(days=4)) == (None, [])
    assert changes.due_notices(ch, [4320, 60], start - timedelta(days=2)) == ("before:4320", [])
    # Planned at short notice: one mail (the closest), the 3-day notice is skipped.
    assert changes.due_notices(ch, [4320, 60], start - timedelta(minutes=30)) == ("before:60", ["before:4320"])
    ch.notices = [{"key": "before:4320", "ok": True}]
    assert changes.due_notices(ch, [4320, 60], start - timedelta(days=1)) == (None, [])
    assert changes.due_notices(ch, [4320, 60], start + timedelta(minutes=1)) == (None, [])
    assert changes.clean_offsets([60, "4320", 60]) == [4320, 60]
    for bad in ([0], [99999999], list(range(1, 8)), "x"):
        try:
            changes.clean_offsets(bad)
            raise AssertionError(bad)
        except changes.ChangeError:
            pass
    assert changes.clean_emails("A@x.nl; b@y.nl a@x.nl") == ["a@x.nl", "b@y.nl"]

    s = datetime(2026, 12, 5, 21, 0, tzinfo=UTC)
    assert changes.fmt_when(s, s + timedelta(hours=1), AMS, "nl") == "zaterdag 5 december 2026, 22:00–23:00"
    assert changes.fmt_when(s, s + timedelta(hours=4), AMS, "en") == \
        "Saturday 5 December 2026 22:00 – Sunday 6 December 2026 02:00"

    ch = N(id="abc", number=1001, status="planned", title="Windows-updates; DC01, DC02", description="Regel 1\nRegel 2",
           kind="update", impact="outage", starts_at=s, ends_at=s + timedelta(hours=2), sequence=3, updated_at=s,
           outcome_note=None, customer=N(name="Klant"))
    ics = changes.to_ical([ch, N(**{**ch.__dict__, "status": "draft", "id": "d"})], name="Klant", lang="nl",
                          staff=True, host="sp.example")
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "UID:abc@sp.example" in ics and "UID:d@" not in ics
    assert "SUMMARY:Klant: CHG-1001 Windows-updates\\; DC01\\, DC02" in ics
    assert "DTSTART:20261205T210000Z" in ics and "SEQUENCE:3" in ics and "TRANSP:OPAQUE" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


# ------------------------------------------------------------------ the whole flow
async def test_change_flow(app, admin_client, smtp_server):
    c = admin_client
    await _mail_on(c, smtp_server)
    try:
        await _flow(app, c, smtp_server)
    finally:
        await c.put("/api/settings/mail", json={"enabled": False})


async def _flow(app, c, smtp_server):
    from mmn.db import sessionmaker
    from mmn.models import PortalUser
    from mmn.security import hash_password

    cust = (await c.post("/api/customers", json={
        "name": "Wijziging Klant", "maintenance_window": "Sat 22:00-02:00", "contact_email": "Baas@Klant.example",
        "contacts": [{"name": "Piet", "email": "piet@klant.example"}, {"name": "Zonder mail"}]})).json()
    other = (await c.post("/api/customers", json={"name": "Wijziging Ander"})).json()
    srv = await _machine(cust["id"], "wk-app01")
    srv2 = await _machine(cust["id"], "wk-sql01")
    foreign = await _machine(other["id"], "wa-app01")
    async with sessionmaker()() as db:
        db.add(PortalUser(customer_id=cust["id"], email="portal@klant.example", full_name="Portaal", language="en",
                          password_hash=hash_password("portal password 123")))
        db.add(PortalUser(customer_id=cust["id"], email="nog-niet@klant.example", language="nl"))  # not activated
        await db.commit()

    r = await c.put(f"/api/changes/customers/{cust['id']}/settings", json={"lang": "nl", "extra": ["Staf@Mij.example"]})
    assert r.status_code == 200, r.text
    cs = r.json()
    assert cs["effective"]["notify_before"] == [4320, 60] and cs["extra"] == ["staf@mij.example"]
    assert sorted(x["email"] for x in cs["recipient_list"]) == [
        "baas@klant.example", "piet@klant.example", "portal@klant.example", "staf@mij.example"]

    sat = _next_saturday()
    # Suggestion: the next Saturday evening that fits two hours.
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "minutes": 120})
    assert r.status_code == 200, r.text
    sug = r.json()["suggestion"]
    assert sug and datetime.fromisoformat(sug["starts_at"]).astimezone(AMS).strftime("%a %H:%M") == "Sat 22:00"

    # Outside the window and a machine of another customer.
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "starts_at": (sat - timedelta(hours=3)).isoformat(),
                                                 "minutes": 60})
    assert [x["kind"] for x in r.json()["conflicts"]] == ["outside_window"]
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "x", "starts_at": sat.isoformat(),
                                           "node_ids": [foreign]})
    assert r.status_code == 400 and "does not belong" in r.text
    assert (await c.post("/api/changes", json={"customer_id": cust["id"], "title": "x", "starts_at": sat.isoformat(),
                                               "ends_at": sat.isoformat()})).status_code == 400

    r = await c.post("/api/changes", json={
        "customer_id": cust["id"], "title": "Windows-updates servers", "kind": "update", "impact": "outage",
        "description": "De servers worden bijgewerkt en herstart.", "internal_notes": "Eerst snapshot!",
        "starts_at": sat.isoformat(), "minutes": 120, "node_ids": [srv, "wk-sql01"]})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["ref"].startswith("CHG-") and ch["status"] == "planned" and ch["minutes"] == 120
    assert {m["name"] for m in ch["machines"]} == {"wk-app01", "wk-sql01"}
    assert ch["conflicts"] == [] and len(ch["recipients"]) == 4

    # A second change at the same time on the same machine: overlap warning, both ways.
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Firmware switch", "status": "draft",
                                           "starts_at": (sat + timedelta(hours=1)).isoformat(), "minutes": 30,
                                           "node_ids": [srv2]})
    ch2 = r.json()
    assert [(x["kind"], x["severity"]) for x in ch2["conflicts"]] == [("overlap", "warning")]
    assert ch2["conflicts"][0]["ref"] == ch["ref"]
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "minutes": 120})
    assert datetime.fromisoformat(r.json()["suggestion"]["starts_at"]) != sat  # the taken evening is skipped
    badge = (await c.get("/api/changes/badge")).json()
    assert badge["warnings"] >= 2

    # 3 days before: the first notice, one mail per recipient, in their language.
    _drain(smtp_server)
    start = datetime.fromisoformat(ch["starts_at"])
    did = await changes.tick(start - timedelta(days=3) + timedelta(minutes=1))
    assert did[ch["ref"]] == ["before:4320"]
    mails = [await smtp_server.wait() for _ in range(4)]
    by_to = {m["To"]: m for m in mails}
    assert set(by_to) == {"baas@klant.example", "piet@klant.example", "portal@klant.example", "staf@mij.example"}
    assert by_to["baas@klant.example"]["Subject"].startswith(f"[{ch['ref']}] Updates gepland: Windows-updates servers")
    nl = _body(by_to["piet@klant.example"])
    assert "zaterdag" in nl and "De servers worden bijgewerkt" in nl and "Eerst snapshot" not in nl
    assert "niet beschikbaar" in nl and "wk-app01" in nl
    assert "Planned" not in by_to["portal@klant.example"]["Subject"] and "Updates planned" in by_to["portal@klant.example"]["Subject"]
    assert (await changes.tick(start - timedelta(days=3) + timedelta(minutes=2))).get(ch["ref"]) is None
    # The draft got nothing.
    assert ch2["ref"] not in did

    # Moved by a week: one "new time" mail per recipient, the reminders start again.
    new_start = start + timedelta(days=7)
    r = await c.put(f"/api/changes/{ch['id']}", json={"starts_at": new_start.isoformat()})
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["minutes"] == 120 and moved["sequence"] == ch["sequence"] + 1
    assert any(n["key"] == "rescheduled" and n.get("pending") for n in moved["notices"])
    did = await changes.tick(start - timedelta(days=3) + timedelta(minutes=5))
    assert did[ch["ref"]] == ["rescheduled"]
    mails = [await smtp_server.wait() for _ in range(4)]
    assert all("Nieuw tijdstip" in m["Subject"] or "New time" in m["Subject"] for m in mails)
    assert smtp_server.queue.empty()

    # 3 days and 1 hour before the new time, then the start and the end.
    assert (await changes.tick(new_start - timedelta(days=3)))[ch["ref"]] == ["before:4320"]
    assert len([await smtp_server.wait() for _ in range(4)]) == 4
    assert (await changes.tick(new_start - timedelta(minutes=59)))[ch["ref"]] == ["before:60"]
    _ = [await smtp_server.wait() for _ in range(4)]
    assert (await changes.tick(new_start + timedelta(minutes=1)))[ch["ref"]] == ["in_progress"]
    r = await c.put(f"/api/changes/{ch['id']}", json={"outcome_note": "Alle servers zijn bijgewerkt."})
    assert r.status_code == 200
    assert (await changes.tick(new_start + timedelta(hours=2, minutes=1)))[ch["ref"]] == ["completed", "after"]
    mails = [await smtp_server.wait() for _ in range(4)]
    after = next(m for m in mails if m["To"] == "piet@klant.example")
    assert after["Subject"] == f"[{ch['ref']}] Afgerond: Windows-updates servers"
    assert "Alle servers zijn bijgewerkt." in _body(after)
    assert (await c.get(f"/api/changes/{ch['id']}")).json()["status"] == "completed"
    r = await c.put(f"/api/changes/{ch['id']}", json={"starts_at": sat.isoformat()})
    assert r.status_code == 400  # a finished change can't move

    # Planned, announced by hand, then cancelled: a cancellation goes out right away.
    r = await c.post(f"/api/changes/{ch2['id']}/notify", json={"kind": "announce"})
    assert r.status_code == 409  # still a draft
    assert (await c.post(f"/api/changes/{ch2['id']}/status", json={"status": "planned"})).status_code == 200
    r = await c.post(f"/api/changes/{ch2['id']}/notify", json={"kind": "announce"})
    assert r.status_code == 200 and r.json()["notice"]["to"] == 4
    _ = [await smtp_server.wait() for _ in range(4)]
    assert (await c.post(f"/api/changes/{ch2['id']}/status", json={"status": "draft"})).status_code == 409
    assert (await c.delete(f"/api/changes/{ch2['id']}")).status_code == 409
    r = await c.post(f"/api/changes/{ch2['id']}/status", json={"status": "cancelled"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    mails = [await smtp_server.wait() for _ in range(4)]
    assert all("Geannuleerd" in m["Subject"] or "Cancelled" in m["Subject"] for m in mails)
    assert any(n["key"] == "cancelled" and n["ok"] for n in r.json()["notices"])

    # A draft for later, invisible for the customer.
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Geheim plan", "status": "draft",
                                           "starts_at": (sat + timedelta(days=21)).isoformat()})
    draft = r.json()
    assert draft["minutes"] == 120

    # Calendar: changes, the maintenance windows and scheduled task runs on the customer's machines.
    r = await c.post("/api/tasks", json={"name": "Wekelijkse updates WK", "kind": "node_updates", "cron": "30 23 * * sat",
                                         "targets": {"scope": "environments", "environment_ids": [cust["id"]]}})
    assert r.status_code in (200, 201), r.text
    task = r.json()
    cal = (await c.get("/api/changes/calendar", params={"customer_id": cust["id"], "start": sat.isoformat(),
                                                         "end": (sat + timedelta(days=28)).isoformat()})).json()
    assert {x["title"] for x in cal["changes"]} >= {"Windows-updates servers", "Geheim plan"}
    assert any(x["task_id"] == task["id"] for x in cal["tasks"])
    assert [w["window"] for w in cal["windows"]] == ["Sat 22:00-02:00"] and cal["windows"][0]["understood"]
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "starts_at": sat.isoformat(), "minutes": 120,
                                                 "task_id": task["id"]})
    kinds = [x["kind"] for x in r.json()["conflicts"]]
    assert "task" not in kinds and "task_outside" not in kinds  # the linked task runs during the change
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "starts_at": sat.isoformat(), "minutes": 60})
    assert "task" not in [x["kind"] for x in r.json()["conflicts"]]  # 23:30 is after 23:00
    r = await c.post("/api/changes/check", json={"customer_id": cust["id"], "starts_at": sat.isoformat(), "minutes": 120})
    assert [x["kind"] for x in r.json()["conflicts"]] == ["task"]

    # The portal: no drafts, no internal notes.
    import httpx

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50002))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as pc:
        r = await pc.post("/api/portal/login", json={"email": "portal@klant.example", "password": "portal password 123"})
        assert r.status_code == 200, r.text
        pdata = (await pc.get("/api/portal/changes")).json()
        titles = [x["title"] for x in pdata["upcoming"] + pdata["past"]]
        assert "Windows-updates servers" in titles and "Firmware switch" in titles and "Geheim plan" not in titles
        assert all("internal_notes" not in x and "notices" not in x for x in pdata["upcoming"] + pdata["past"])
        assert pdata["maintenance_window"] == "Sat 22:00-02:00" and pdata["ical_url"] is None

        # iCal: the customer's own feed, and the staff feed of every customer.
        r = await c.post(f"/api/changes/customers/{cust['id']}/ical", json={"enabled": True})
        url = r.json()["ical_url"]
        assert url and url.endswith(".ics")
        assert (await pc.get("/api/portal/changes")).json()["ical_url"] == url
    path = url[url.index("/api/changes/ical/"):]
    r = await c.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "Windows-updates servers" in r.text and "Geheim plan" not in r.text and "Eerst snapshot" not in r.text
    assert "STATUS:CANCELLED" in r.text  # the cancelled change, so calendars drop it
    assert (await c.get("/api/changes/ical/" + "x" * 32 + ".ics")).status_code == 404
    assert (await c.get("/api/changes/ical/short.ics")).status_code == 404
    old = path
    await c.post(f"/api/changes/customers/{cust['id']}/ical", json={"enabled": True})
    assert (await c.get(old)).status_code == 404  # a new link stops the old one
    staff = (await c.post("/api/changes/settings/ical", json={"enabled": True})).json()["ical_url"]
    r = await c.get(staff[staff.index("/api/changes/ical/"):])
    assert r.status_code == 200 and "Wijziging Klant: CHG-" in r.text

    # Viewers read, only admins change.
    lst = (await c.get("/api/changes", params={"customer_id": cust["id"]})).json()
    assert lst["counts"].get("completed") == 1 and lst["counts"].get("cancelled") == 1
    assert (await c.delete(f"/api/changes/{draft['id']}")).status_code == 204
    assert (await c.delete(f"/api/tasks/{task['id']}")).status_code in (200, 204)
    await c.post("/api/changes/settings/ical", json={"enabled": False})
    for x in (cust, other):
        await _drop(c, x["id"])


async def test_change_runbook_and_mcp(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "MCP Wijzigingen", "maintenance_window": "daily 00:00-00:00"})).json()
    other = (await c.post("/api/customers", json={"name": "MCP Wijzigingen B"})).json()
    await _machine(cust["id"], "mw-app01")
    token, _ = await _token(c, "claude-changes", ["read", "changes"])
    admin_token, _ = await _token(c, "claude-changes-admin", ["read", "changes", "changes_admin"])
    b_token, _ = await _token(c, "claude-changes-b", ["read", "changes", "changes_admin"], [other["id"]])
    for tk in (token, admin_token, b_token):
        await _init(client, tk)

    start = (datetime.now(UTC) + timedelta(days=10)).replace(minute=0, second=0, microsecond=0)
    data, err = await _call(client, token, "check_change_slot", {"customer": "MCP Wijzigingen", "start": start.isoformat(),
                                                                 "minutes": 90, "machines": ["mw-app01"]})
    assert err is None and data["ok"] and data["conflicts"] == [] and data["suggestion"]
    data, err = await _call(client, token, "save_change", {"customer": "MCP Wijzigingen", "title": "x"})
    assert err and "changes_admin" in err
    data, err = await _call(client, admin_token, "save_change", {
        "customer": "MCP Wijzigingen", "title": "Migratie fileserver", "start": start.isoformat(), "minutes": 90,
        "kind": "migration", "impact": "degraded", "machines": ["mw-app01"], "description": "Bestanden verhuizen."})
    assert err is None, err
    ref = data["ref"]
    assert data["status"] == "planned" and data["machines"][0]["name"] == "mw-app01" and data["conflicts"] == []
    data, err = await _call(client, admin_token, "save_change", {"customer": "MCP Wijzigingen", "change": ref,
                                                                 "minutes": 120, "impact": "outage"})
    assert err is None and data["minutes"] == 120 and data["impact"] == "outage"
    data, err = await _call(client, token, "list_changes", {"customer": "MCP Wijzigingen", "status": "upcoming"})
    assert err is None and [x["ref"] for x in data["changes"]] == [ref]
    data, err = await _call(client, token, "get_change", {"change": ref})
    assert err is None and data["title"] == "Migratie fileserver" and data["maintenance_window"] == "daily 00:00-00:00"
    data, err = await _call(client, token, "get_change_calendar", {"customer": "MCP Wijzigingen", "days": 20})
    assert err is None and any(x["ref"] == ref for x in data["changes"])
    # A token of another customer can't see or touch it.
    data, err = await _call(client, b_token, "get_change", {"change": ref})
    assert err and "not found" in err
    data, err = await _call(client, b_token, "save_change", {"customer": "MCP Wijzigingen", "title": "x",
                                                             "start": start.isoformat()})
    assert err
    data, err = await _call(client, b_token, "list_changes", {})
    assert err is None and data["changes"] == []
    data, err = await _call(client, admin_token, "send_change_notice", {"customer": "MCP Wijzigingen", "change": ref})
    assert err is None and data["notice"]["ok"] is False and data["notice"]["error"] == "mail_off"
    data, err = await _call(client, admin_token, "set_change_status", {"customer": "MCP Wijzigingen", "change": ref,
                                                                       "status": "cancelled"})
    assert err is None and data["status"] == "cancelled"

    # A change with a runbook: it starts at the beginning and its outcome decides the status.
    rb = (await c.post("/api/runbooks", json={"name": "Wijziging stappen", "category": "maintenance",
                                              "customer_id": cust["id"], "steps": [
                                                  {"title": "Handmatig controleren", "kind": "manual"}]})).json()
    assert "id" in rb, rb
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Met runbook",
                                           "starts_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                                           "minutes": 30, "runbook_id": rb["id"], "run_runbook": True, "notify": False})
    assert r.status_code == 201, r.text
    chg = r.json()
    did = await changes.tick(datetime.now(UTC) + timedelta(minutes=2))
    assert did[chg["ref"]][0] == "in_progress" and did[chg["ref"]][1].startswith("runbook:")
    d = (await c.get(f"/api/changes/{chg['id']}")).json()
    assert d["runbook_run_status"] in ("running", "waiting_input")
    run_id = d["runbook_run_id"]
    # Past the end, but the runbook still waits: the change stays in progress.
    assert chg["ref"] not in await changes.tick(datetime.now(UTC) + timedelta(hours=1))
    r = await c.post(f"/api/runbooks/runs/{run_id}/cancel")
    assert r.status_code == 200, r.text
    assert (await changes.tick(datetime.now(UTC) + timedelta(hours=1)))[chg["ref"]] == ["failed"]
    for x in (cust, other):
        await _drop(c, x["id"])
