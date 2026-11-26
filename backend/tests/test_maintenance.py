"""v0.24.0: orchestrated Proxmox maintenance (checks, updates, moving or shutting down machines, restarts, start-up
order, checks after), the timeline report per customer in its language, and where changes come from in the change
calendar (origin, requested at, automatic records) in the app and the portal."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from test_customers import _machine
from test_tickets import _body, _mail_on

from mmn.services import maintenance


# ------------------------------------------------------------------ a fake cluster
class FakeCluster:
    """Proxmox nodes with guests, in memory. A node that reboots is offline for one wait and comes back with a
    new connection; Proxmox starts its onboot guests itself."""

    def __init__(self, nodes: dict[str, list[dict]], *, configs: dict[int, dict] | None = None,
                 updates: dict[str, int] | None = None):
        self.g = {nid: [dict(x, status=x.get("status", "running"), type=x.get("type", "qemu"), template=False,
                             lock=x.get("lock")) for x in gs] for nid, gs in nodes.items()}
        self.on = set(nodes)
        self.conn = {nid: datetime.now(UTC) - timedelta(days=1) for nid in nodes}
        self.configs = configs or {}
        self.pend = dict(updates or {})
        self.actions: list[tuple] = []
        self.fail_migrate: set[int] = set()
        self.no_return: set[str] = set()
        self.booting: dict[str, int] = {}
        self.slept = 0.0
        self.quorate = True

    def online(self, nid):
        return nid in self.on

    def connected_at(self, nid):
        return self.conn.get(nid)

    async def cluster(self, nid):
        return {"quorate": self.quorate, "members": [{"name": n, "online": n in self.on} for n in self.g]}

    async def guests(self, nid):
        return copy.deepcopy(self.g.get(nid, []))

    async def config(self, nid, g):
        return self.configs.get(int(g["vmid"]), {})

    async def pending(self, nid):
        n = self.pend.get(nid, 0)
        return {"packages": [{"name": f"pkg{i}"} for i in range(n)], "reboot_required": bool(n)}

    async def updates(self, nid):
        self.actions.append(("updates", nid))
        self.pend[nid] = 0
        return {"ok": True, "job_id": None, "remaining": 0, "reboot_required": True}

    async def migrate(self, src, g, tgt):
        vmid = int(g["vmid"])
        if vmid in self.fail_migrate:
            return {"ok": False, "error": "local disk"}
        self.actions.append(("migrate", vmid, src, tgt))
        x = next(x for x in self.g[src] if x["vmid"] == vmid)
        self.g[src].remove(x)
        self.g[tgt].append(x)
        return {"ok": True, "job_id": None}

    async def power(self, nid, g, action):
        vmid = int(g["vmid"])
        self.actions.append((action, vmid))
        x = next(x for x in self.g[nid] if x["vmid"] == vmid)
        x["status"] = "stopped" if action == "shutdown" else "running"
        return {"ok": True, "job_id": None}

    async def reboot(self, nid):
        self.actions.append(("reboot", nid))
        self.on.discard(nid)
        self.booting[nid] = 1
        for x in self.g[nid]:
            x["status"] = "stopped"
        return {"ok": True}

    async def sleep(self, seconds):
        self.slept += seconds
        for nid in list(self.booting):
            if nid in self.no_return:
                continue
            self.booting[nid] -= 1
            if self.booting[nid] <= 0:
                del self.booting[nid]
                self.on.add(nid)
                self.conn[nid] = datetime.now(UTC)
                for x in self.g[nid]:
                    if self.configs.get(int(x["vmid"]), {}).get("onboot"):
                        x["status"] = "running"
        await asyncio.sleep(0)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(maintenance, "POLL", 0.01)
    monkeypatch.setattr(maintenance, "SETTLE", 0)
    monkeypatch.setattr(maintenance, "QUORUM_WAIT", 0)


# ------------------------------------------------------------------ unit tests
def test_order_and_texts():
    assert maintenance.startup_of({"startup": "order=2,up=45"}) == (2, 45)
    assert maintenance.startup_of({"startup": "up=900,order=1"}) == (1, maintenance.MAX_UP_DELAY)
    assert maintenance.startup_of({}) == (None, 0)
    gs = [{"vmid": 300, "order": None}, {"vmid": 200, "order": 2}, {"vmid": 100, "order": 1}, {"vmid": 50, "order": None}]
    assert [g["vmid"] for g in maintenance.start_order(gs)] == [100, 200, 50, 300]
    assert [g["vmid"] for g in maintenance.stop_order(gs)] == [300, 50, 200, 100]
    e = {"key": "shutdown", "node": "pve1", "params": {"count": 1, "names": ["dc01"]}}
    assert maintenance.describe(e, "nl") == "pve1: 1 machine netjes afgesloten (dc01)"
    e = {"key": "shutdown", "node": "pve1", "params": {"count": 3, "names": ["a", "b", "c"]}}
    assert maintenance.describe(e, "en") == "pve1: 3 machines shut down cleanly (a, b, c)"
    e = {"key": "precheck_ok", "params": {"nodes": 2, "guests": 5}}
    assert maintenance.describe(e, "nl") == "Controles uitgevoerd: 2 servers online, 5 machines actief, alles in orde"
    e = {"key": "postcheck_issues", "params": {"issues": [{"key": "issue_not_running", "params": {"name": "app01"}},
                                                          {"key": "issue_offline", "params": {"node": "pve2"}}]}}
    assert maintenance.describe(e, "en") == "Final check: app01 is not running; pve2 is not reachable"
    e = {"key": "precheck_failed", "params": {"problem": {"key": "problem_offline", "params": {"node": "pve2"}}}}
    assert "pve2 is niet bereikbaar" in maintenance.describe(e, "nl")
    many = [f"vm{i}" for i in range(11)]
    assert maintenance.names_text(many, "nl").endswith("en nog 3")
    assert maintenance.describe({"key": "updates_done", "node": "pve1", "params": {"count": 1}}, "en") == \
        "pve1: 1 update installed"


# ------------------------------------------------------------------ helpers
async def _setup(c, name: str, *, window: str | None = None, lang: str = "nl", recipients=None):
    cust = (await c.post("/api/customers", json={"name": name, "contact_email": f"baas@{name.lower()}.example",
                                                 **({"maintenance_window": window} if window else {})})).json()
    r = await c.put(f"/api/customers/{cust['id']}/reports/settings", json={
        "lang": lang, "maintenance_recipients": recipients or [], "recipients": []})
    assert r.status_code == 200, r.text
    a = await _machine(cust["id"], f"{name.lower()}-pve1", "proxmox", cluster_name=f"{name}-c", capabilities=["guests"])
    b = await _machine(cust["id"], f"{name.lower()}-pve2", "proxmox", cluster_name=f"{name}-c", capabilities=["guests"])
    return cust, a, b


async def _task(c, cust_id: str, **options) -> str:
    r = await c.post("/api/tasks", json={"name": "Nachtelijk onderhoud", "kind": "pve_maintenance", "cron": "45 1 * * 6",
                                         "targets": {"scope": "environments", "environment_ids": [cust_id]},
                                         "options": options, "mail_to": [], "mail_on": "never"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _new_run(task_id: str, trigger: str = "manual") -> str:
    from mmn.db import sessionmaker
    from mmn.models import TaskRun

    async with sessionmaker()() as db:
        r = TaskRun(task_id=task_id, name="Nachtelijk onderhoud", kind="pve_maintenance", trigger=trigger,
                    status="running", results=[])
        db.add(r)
        await db.commit()
        return r.id


# ------------------------------------------------------------------ the flow
async def test_maintenance_run_and_report(app, admin_client, smtp_server, fast):
    c = admin_client
    await _mail_on(c, smtp_server)
    try:
        cust, a, b = await _setup(c, "Onderhoud", recipients=["it@onderhoud.example", "Baas@Onderhoud.example"])
        fake = FakeCluster(
            {a: [{"vmid": 101, "name": "dc01"}, {"vmid": 102, "name": "app01"}, {"vmid": 103, "name": "sql01"},
                 {"vmid": 104, "name": "test01", "status": "stopped"}],
             b: [{"vmid": 201, "name": "web01"}]},
            configs={101: {"startup": "order=1,up=30", "onboot": 1}, 103: {"startup": "order=2"}, 102: {}},
            updates={a: 12, b: 3})
        fake.fail_migrate = {101, 103}  # e.g. local disks: shut down instead
        tid = await _task(c, cust["id"])
        rid = await _new_run(tid)
        await maintenance.run_maintenance(rid, tid, trigger="manual", requested_by="admin", ops=fake)

        r = (await c.get(f"/api/task-runs/{rid}?lang=nl")).json()
        assert r["status"] == "success", r
        keys = [x["key"] for x in (await _timeline(rid))]
        assert keys[:3] == ["start", "precheck_ok", "updating"]
        assert "migrated" in keys and "shutdown" in keys and keys.count("rebooting") == 2 and "postcheck_ok" in keys
        assert keys[-1] == "done"
        # Shut down in reverse start-up order (sql01 order 2 before dc01 order 1), never the stopped test01.
        downs = [x[1] for x in fake.actions if x[0] == "shutdown"]
        assert downs == [103, 101]
        # dc01 is onboot: Proxmox started it itself; sql01 is started by Stackpilot. app01 moved and came back.
        assert ("start", 103) in fake.actions and ("start", 101) not in fake.actions
        assert ("migrate", 102, a, b) in fake.actions and ("migrate", 102, b, a) in fake.actions
        # web01 on pve2 moves to pve1 while pve2 restarts, and back.
        assert ("migrate", 201, b, a) in fake.actions and ("migrate", 201, a, b) in fake.actions
        assert {x["vmid"] for x in fake.g[a]} == {101, 102, 103, 104} and [x["vmid"] for x in fake.g[b]] == [201]
        assert fake.slept >= 0  # the "up" delay goes through ops.sleep
        view = r["maintenance"]["customers"][0]
        assert view["state"] == "ok" and view["lang"] == "nl"
        texts = [x["text"] for x in view["lines"]]
        assert any(t.startswith("Controles uitgevoerd: 2 servers online, 4 machines actief") for t in texts), texts
        assert any("netjes afgesloten (sql01, dc01)" in t for t in texts), texts
        assert any("door de server zelf gestart (dc01)" in t for t in texts), texts
        assert sorted(view["sent_to"]) == ["baas@onderhoud.example", "it@onderhoud.example"]

        # The report mail: one mail to both recipients, in Dutch, with the timeline.
        msg = await smtp_server.wait(10)
        assert "Onderhoud: onderhoud uitgevoerd" in msg["Subject"]
        body = _body(msg)
        assert "Vannacht hebben we het geplande onderhoud" in body and "2 servers onderhouden, 15 updates" in body
        assert "herstarten" in body and "Nacontrole: alle 2 servers online" in body

        # The change calendar: an automatic change with origin schedule, completed, linked to the run.
        ch = (await c.get("/api/changes", params={"customer_id": cust["id"]})).json()["changes"]
        assert len(ch) == 1, ch
        x = ch[0]
        assert x["origin"] == "schedule" and x["origin_ref"] == "Nachtelijk onderhoud" and x["automatic"]
        assert x["status"] == "completed" and x["task_run_id"] == rid and x["has_report"]
        assert x["title"] == "Onderhoud van de Proxmox-servers" and x["requested_at"]
        assert "Alles is goed gegaan" in (x["outcome_note"] or "")
        assert set(x["node_ids"]) == {a, b}

        # The portal shows it, with the report in the portal user's language.
        await _portal_user(cust["id"], "portaal@onderhoud.example", "en")
        import httpx

        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50003))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as pc:
            r = await pc.post("/api/portal/login", json={"email": "portaal@onderhoud.example",
                                                       "password": "portal password 123"})
            assert r.status_code == 200, r.text
            pdata = (await pc.get("/api/portal/changes")).json()
            px = next(y for y in pdata["past"] if y["id"] == x["id"])
            assert px["origin"] == "schedule" and px["has_report"] and "task_run_id" not in px
            rep = (await pc.get(f"/api/portal/changes/{x['id']}/report")).json()
            assert rep["title"] == "Maintenance report" and rep["state"] == "ok"
            assert any("moved to" in y["text"] for y in rep["lines"]), rep["lines"]

        # Mail the report again to someone else.
        r = await c.post(f"/api/task-runs/{rid}/report/{cust['id']}", json={"to": ["Extra@onderhoud.example"]})
        assert r.status_code == 200, r.text and r.json()["sent_to"] == ["extra@onderhoud.example"]
        msg = await smtp_server.wait(10)
        assert msg["To"] == "extra@onderhoud.example"

        # The monthly report's work list shows the maintenance once, done automatically, not every job.
        from mmn.db import sessionmaker
        from mmn.models import Environment, Node
        from mmn.services import worklog

        async with sessionmaker()() as db:
            e = await db.get(Environment, cust["id"])
            ns = [await db.get(Node, a), await db.get(Node, b)]
            now = datetime.now(UTC)
            wl = await worklog.collect(db, e, ns, now - timedelta(days=1), now + timedelta(hours=1), "nl")
        assert [(i["title"], i["by"]) for i in wl["items"]] == [("Onderhoud van de Proxmox-servers", "auto")]
    finally:
        await c.put("/api/settings/mail", json={"enabled": False})


async def _timeline(rid: str) -> list[dict]:
    from mmn.db import sessionmaker
    from mmn.models import TaskRun

    async with sessionmaker()() as db:
        return list((await db.get(TaskRun, rid)).timeline)


async def _portal_user(cid: str, email: str, lang: str) -> None:
    from mmn.db import sessionmaker
    from mmn.models import PortalUser
    from mmn.security import hash_password

    async with sessionmaker()() as db:
        db.add(PortalUser(customer_id=cid, email=email, full_name="Portaal", language=lang,
                          password_hash=hash_password("portal password 123")))
        await db.commit()


async def test_node_not_back_stops_the_cluster(admin_client, fast):
    c = admin_client
    cust, a, b = await _setup(c, "Stuk", lang="en")
    fake = FakeCluster({a: [{"vmid": 301, "name": "files"}], b: [{"vmid": 302, "name": "mail"}]}, updates={a: 1, b: 1})
    fake.no_return = {a}
    tid = await _task(c, cust["id"], boot_timeout=5, guest_handling="shutdown")
    rid = await _new_run(tid)
    orig = maintenance._now
    # Time runs fast: every look at the clock is a minute later, so the 5-minute boot wait ends.
    tick = {"t": datetime.now(UTC)}

    def clock():
        tick["t"] += timedelta(minutes=1)
        return tick["t"]

    maintenance._now = clock
    try:
        await maintenance.run_maintenance(rid, tid, trigger="manual", ops=fake)
    finally:
        maintenance._now = orig
    keys = [x["key"] for x in await _timeline(rid)]
    assert "not_back" in keys and "skipped_after_problem" in keys and keys[-1] == "done_failed", keys
    assert ("reboot", b) not in fake.actions and ("updates", b) not in fake.actions
    r = (await c.get(f"/api/task-runs/{rid}")).json()
    assert r["status"] == "failed" and r["counts"]["skipped"] == 1
    ch = (await c.get("/api/changes", params={"customer_id": cust["id"]})).json()["changes"][0]
    assert ch["status"] == "failed" and "did not go as planned" in ch["outcome_note"]


async def test_window_approval_and_precheck(admin_client, fast):
    from mmn.db import sessionmaker
    from mmn.models import AiApproval

    c = admin_client
    # Outside the window (a window that is never open now): a scheduled run does nothing, no change.
    now = datetime.now(UTC).astimezone(maintenance.ZoneInfo("Europe/Amsterdam"))
    day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][(now.weekday() + 3) % 7]
    cust, a, b = await _setup(c, "Venster", window=f"{day} 01:00-02:00", lang="en")
    fake = FakeCluster({a: [], b: []}, updates={a: 1})
    tid = await _task(c, cust["id"])
    rid = await _new_run(tid, "schedule")
    await maintenance.run_maintenance(rid, tid, trigger="schedule", ops=fake)
    assert [x["key"] for x in await _timeline(rid)] == ["outside_window"]
    assert not fake.actions
    assert not (await c.get("/api/changes", params={"customer_id": cust["id"]})).json()["changes"]

    # Approval asked (the task asks always): the run waits until an admin approves in the inbox.
    cust2, a2, b2 = await _setup(c, "Akkoord", lang="en")
    fake = FakeCluster({a2: [{"vmid": 401, "name": "erp"}], b2: []}, updates={})
    tid = await _task(c, cust2["id"], approval="always", reboot="never")
    rid = await _new_run(tid, "schedule")
    job = asyncio.create_task(maintenance.run_maintenance(rid, tid, trigger="schedule", ops=fake))
    aid = None
    for _ in range(200):
        await asyncio.sleep(0.02)
        r = (await c.get(f"/api/task-runs/{rid}")).json()
        if r["status"] == "waiting" and r["approval_id"]:
            aid = r["approval_id"]
            break
    assert aid, r
    async with sessionmaker()() as db:
        ap = await db.get(AiApproval, aid)
        assert ap.tool == "continue_maintenance" and ap.customer_ids == [cust2["id"]]
    r = await c.post(f"/api/gateway/approvals/{aid}/approve", json={})
    assert r.status_code == 200, r.text
    await asyncio.wait_for(job, 10)
    keys = [x["key"] for x in await _timeline(rid)]
    assert keys[:2] == ["approval_wait", "approved"] and "no_reboot" in keys and keys[-1] == "done", keys
    assert (await c.get(f"/api/task-runs/{rid}")).json()["status"] == "success"

    # A node offline before the start: nothing is changed.
    fake = FakeCluster({a2: [{"vmid": 401, "name": "erp"}], b2: []}, updates={a2: 2})
    fake.on.discard(b2)
    tid = await _task(c, cust2["id"])
    rid = await _new_run(tid)
    await maintenance.run_maintenance(rid, tid, trigger="manual", ops=fake)
    tl = await _timeline(rid)
    assert [x["key"] for x in tl][:2] == ["start", "precheck_failed"] and not fake.actions
    assert "is not reachable" in maintenance.describe(tl[1], "en")

    # A rejected approval: skipped.
    fake = FakeCluster({a2: [], b2: []}, updates={a2: 2})
    tid = await _task(c, cust2["id"], approval="always")
    rid = await _new_run(tid, "schedule")
    job = asyncio.create_task(maintenance.run_maintenance(rid, tid, trigger="schedule", ops=fake))
    for _ in range(200):
        await asyncio.sleep(0.02)
        r = (await c.get(f"/api/task-runs/{rid}")).json()
        if r["approval_id"]:
            break
    assert (await c.post(f"/api/gateway/approvals/{r['approval_id']}/reject", json={})).status_code == 200
    await asyncio.wait_for(job, 10)
    assert [x["key"] for x in await _timeline(rid)] == ["approval_wait", "rejected"] and not fake.actions


async def test_change_origin(admin_client):
    from mmn.db import sessionmaker
    from mmn.models import Ticket

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Herkomst"})).json()
    async with sessionmaker()() as db:
        tk = Ticket(customer_id=cust["id"], subject="Printer werkt niet", node_ids=[], diagnosis={})
        db.add(tk)
        await db.commit()
        await db.refresh(tk)
        number = tk.number
    start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Printerdriver vervangen",
                                           "starts_at": start.isoformat(), "ticket_id": str(number),
                                           "requested_at": (start - timedelta(days=4)).isoformat()})
    assert r.status_code == 201, r.text
    x = r.json()
    assert x["origin"] == "ticket" and x["origin_ref"] == f"#{number} Printer werkt niet" and x["ticket_id"]
    assert x["requested_at"].startswith((start - timedelta(days=4)).date().isoformat())
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Schijf vervangen", "origin": "issue",
                                           "origin_ref": "SMART-fout op schijf 2", "starts_at": start.isoformat()})
    assert r.json()["origin"] == "issue" and r.json()["requested_by"] == "admin"
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "x", "origin": "nonsense",
                                           "starts_at": start.isoformat()})
    assert r.status_code == 422
    r = await c.post("/api/changes", json={"customer_id": cust["id"], "title": "x", "ticket_id": "99999999",
                                           "starts_at": start.isoformat()})
    assert r.status_code == 400 and "not found" in r.text
    plain = (await c.post("/api/changes", json={"customer_id": cust["id"], "title": "Handwerk",
                                                "starts_at": start.isoformat()})).json()
    assert plain["origin"] == "manual" and not plain["automatic"]
    r = await c.put(f"/api/changes/{plain['id']}", json={"origin": "issue", "origin_ref": "Melding schijf vol"})
    assert r.status_code == 200 and r.json()["origin"] == "issue"
