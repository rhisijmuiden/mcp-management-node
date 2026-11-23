"""Phase 14: automatic recovery: rules (alert and finding), watched services as alerts, starting the
runbook within the policy, verification (the problem has to be gone), loop protection (cooldown,
maximum attempts, rate limit), the circuit breaker and the emergency stop, mode approve and notify,
the Proxmox guardrails, presets, the morning summary, the monthly report and the MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.services import remediation


# ------------------------------------------------------------------ unit tests
def test_clean_match_and_matching():
    from mmn.models import Alert, Node, RemediationRule

    with pytest.raises(remediation.RemediationError, match="alert kind"):
        remediation.clean_match("alert", {"kinds": []})
    with pytest.raises(remediation.RemediationError, match="name the service"):
        remediation.clean_match("alert", {"kinds": ["service"]})
    with pytest.raises(remediation.RemediationError, match="service 'bad;rm'"):
        remediation.clean_match("alert", {"kinds": ["service"], "services": ["bad;rm"]})
    with pytest.raises(remediation.RemediationError, match="finding"):
        remediation.clean_match("finding", {})
    m = remediation.clean_match("alert", {"kinds": ["service", "disk", "nope"], "services": ["nginx", "nginx"],
                                          "machine": "web*", "machine_types": ["server", "x"]})
    assert m == {"kinds": ["service", "disk"], "services": ["nginx"], "machine": "web*", "machine_types": ["server"]}

    rule = RemediationRule(name="r", trigger="alert", match=m, customer_id="c1")
    web = Node(name="web01", kind="linux", role="server", environment_id="c1")
    db_ = Node(name="db01", kind="linux", role="server", environment_id="c1")
    al = Alert(kind="service", key="service:x:nginx", severity="critical", message="down", detail={"service": "nginx"})
    assert remediation.match_alert(rule, al, web, "c1")
    assert not remediation.match_alert(rule, al, db_, "c1")  # machine pattern
    assert not remediation.match_alert(rule, al, web, "c2")  # other customer
    al.detail = {"service": "redis"}
    assert not remediation.match_alert(rule, al, web, "c1")  # not a watched service
    disk = Alert(kind="disk", key="disk:x", severity="warning", message="full", detail={})
    assert remediation.match_alert(rule, disk, web, "c1")
    rule.match = {**m, "min_severity": "critical"}
    assert not remediation.match_alert(rule, disk, web, "c1")

    fr = RemediationRule(name="f", trigger="finding", match={"codes": ["agent_old"]}, customer_id=None)
    assert remediation.match_finding(fr, {"code": "agent_old"}, web, "c9")
    assert not remediation.match_finding(fr, {"code": "disk_full"}, web, "c9")
    # Rules of the customer come first.
    g = RemediationRule(name="a global", customer_id=None)
    c = RemediationRule(name="z customer", customer_id="c1")
    assert [r.name for r in remediation._ordered([g, c])] == ["z customer", "a global"]


def test_service_script_and_parser():
    sh = remediation.service_script("linux", ["nginx", "bad;rm -rf /", "php8.4-fpm"])
    assert "nginx" in sh and "php8.4-fpm" in sh and "rm -rf" not in sh
    ps = remediation.service_script("windows", ["Spooler"])
    assert "Get-Service" in ps and "'Spooler'" in ps
    out = "SVC\tnginx\tloaded\tfailed\nSVC\tredis\tnot-found\t\nSVC\tcron\tloaded\tactive\r\njunk\n"
    assert remediation.parse_services(out) == {"nginx": "failed", "redis": None, "cron": "active"}
    assert remediation.is_down("failed") and remediation.is_down("inactive") and remediation.is_down("stopped")
    assert not remediation.is_down("active") and not remediation.is_down("activating")


def test_templates_and_presets():
    import mmn.mcp_server  # noqa: F401 - registers the tools
    from mmn.services import runbook_templates

    keys = {t["key"] for t in runbook_templates.TEMPLATES}
    for p in remediation.PRESETS:
        assert p["template"] in keys
    for lang in ("en", "nl"):
        names = [p["name"] for p in remediation.preset_list(lang)]
        assert len(names) == len(remediation.PRESETS) and all(isinstance(n, str) for n in names)
    assert remediation.preset_list("nl")[0]["name"] == "Gestopte service herstarten"


# ------------------------------------------------------------------ a fake agent
class FakeAgent:
    """Services per machine; restarting a service that is not "broken" starts it."""

    def __init__(self):
        self.state: dict[tuple[str, str], str] = {}
        self.broken: set[str] = set()
        self.calls: list[tuple[str, str, dict]] = []
        self.online: set[str] = set()

    async def request(self, node_id, method, params=None, timeout=60):
        params = params or {}
        self.calls.append((node_id, method, params))
        if method == "services.action":
            unit = params["unit"]
            if params["action"] in ("restart", "start") and unit not in self.broken:
                self.state[(node_id, unit)] = "active"
            return {"ok": True, "unit": unit, "action": params["action"]}
        if method == "services.status":
            return {"unit": params["unit"], "active": self.state.get((node_id, params["unit"]), "unknown")}
        if method == "exec":
            cmd = params["command"]
            if "systemctl show" in cmd:
                lines = []
                for (nid, unit), st in self.state.items():
                    if nid == node_id and unit in cmd:
                        lines.append(f"SVC\t{unit}\tloaded\t{st}")
                return {"exit_code": 0, "stdout": "\n".join(lines) + "\n", "stderr": ""}
            return {"exit_code": 0, "stdout": "cleaned\n", "stderr": ""}
        raise AssertionError(f"unexpected agent call {method}")


@pytest.fixture
def fake_agent(monkeypatch):
    from mmn.services import agent_hub

    fa = FakeAgent()
    monkeypatch.setattr(agent_hub, "request", fa.request)
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: nid in fa.online)
    remediation._service_state.clear()
    yield fa
    remediation._service_state.clear()


@pytest.fixture(autouse=True)
async def _cleanup():
    """Machines of these tests do not count in the other tests' totals."""
    yield
    from sqlalchemy import delete

    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.name.like("rem-%")))
        await db.commit()


async def _cycle(now=None):
    """One minute of the scheduler: check the services, evaluate the alerts, recovery tick."""
    from mmn.services import monitoring, runbooks

    await remediation.probe_services(force=True)
    await monitoring.evaluate()
    res = await remediation.tick() if now is None else {"finished": await remediation.sync(now),
                                                        "started": len(await remediation.scan_alerts(now))}
    await runbooks.wait_idle()
    if now is None:
        await remediation.sync()
    return res


async def _attempts(c, cust_id):
    return (await c.get("/api/remediation/attempts", params={"customer_id": cust_id})).json()


RESTART = [
    {"id": "restart", "kind": "tool", "tool": "manage_service", "title": "Restart",
     "args": {"node": "{{params.server}}", "unit": "{{params.service}}", "action": "restart",
              "reason": "Runbook {{run.id}}"}},
    {"id": "after", "kind": "tool", "tool": "get_service_status", "title": "Status after",
     "args": {"node": "{{params.server}}", "unit": "{{params.service}}"}},
]


async def _setup(c, name):
    cust = (await c.post("/api/customers", json={"name": name})).json()
    rb = (await c.post("/api/runbooks", json={
        "name": f"Restart service {name}", "category": "remediation", "steps": RESTART,
        "params": [{"name": "server", "label": "Server"}, {"name": "service", "label": "Service"}]})).json()
    return cust, rb


async def _rule(c, cust, rb, **kw):
    body = {"name": kw.pop("name", "Restart web services"), "customer_id": cust["id"], "trigger": "alert",
            "match": {"kinds": ["service"], "services": kw.pop("services", ["nginx"])}, "runbook_id": rb["id"],
            "params": {"server": "{{machine.name}}", "service": "{{alert.detail.service}}"},
            "delay_minutes": 0, "cooldown_minutes": kw.pop("cooldown", 1), "max_attempts": kw.pop("max_attempts", 3),
            **kw}
    r = await c.post("/api/remediation/rules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ flows
async def test_service_recovery_end_to_end(admin_client, fake_agent):
    c = admin_client
    cust, rb = await _setup(c, "Herstel Klant")
    web = await _machine(cust["id"], "rem-web01")
    fake_agent.online.add(web)
    fake_agent.state[(web, "nginx")] = "failed"
    rule = await _rule(c, cust, rb)
    assert rule["runbook_name"] == rb["name"] and rule["customer_name"] == "Herstel Klant"

    # The watched service is down: an alert opens, the recovery starts and restarts it.
    await _cycle()
    alerts = (await c.get("/api/monitoring/alerts", params={"open_only": True})).json()
    mine = [a for a in alerts if a["kind"] == "service" and a["node_id"] == web]
    assert mine and mine[0]["detail"]["service"] == "nginx", alerts
    att = await _attempts(c, cust["id"])
    assert len(att) == 1 and att[0]["status"] in ("running", "verifying"), att
    assert any(m == "services.action" and p["unit"] == "nginx" for _, m, p in fake_agent.calls)
    run = (await c.get(f"/api/runbooks/runs/{att[0]['run_id']}")).json()
    assert run["trigger"] == "remediation" and run["status"] == "succeeded", run
    assert run["started_by"] == "Automatic recovery" and run["context"]["alert"]["kind"] == "service"

    # Next minute: the service runs, the alert resolves and the attempt counts as fixed.
    await _cycle()
    att = await _attempts(c, cust["id"])
    assert att[0]["status"] == "succeeded" and att[0]["resolved"] is True, att
    audit = (await c.get("/api/settings/audit", params={"limit": 200})).json()
    rows = audit["items"] if isinstance(audit, dict) else audit
    actions = {r["action"] for r in rows}
    assert {"remediation.start", "remediation.succeeded", "remediation.rule.save"} <= actions

    status = (await c.get("/api/remediation/status")).json()
    assert status["last_24h"]["succeeded"] >= 1 and status["rules_enabled"] >= 1
    # The rule shows its statistics.
    rules = (await c.get("/api/remediation/rules", params={"customer_id": cust["id"]})).json()
    assert [r for r in rules if r["id"] == rule["id"]][0]["stats"]["succeeded"] == 1


async def test_loop_protection_and_circuit_breaker(admin_client, fake_agent):
    c = admin_client
    cust, rb = await _setup(c, "Lus Klant")
    web = await _machine(cust["id"], "rem-loop01")
    fake_agent.online.add(web)
    fake_agent.state[(web, "redis")] = "failed"
    fake_agent.broken.add("redis")  # a restart does not help
    await c.put("/api/remediation/settings", json={"max_failures_per_hour": 2, "verify_minutes": 5})
    await _rule(c, cust, rb, name="Restart redis", services=["redis"], max_attempts=2, cooldown=10)

    now = datetime.now(UTC) + timedelta(seconds=5)
    await _cycle(now)
    att = await _attempts(c, cust["id"])
    assert len(att) == 1 and att[0]["status"] in ("running", "verifying")
    # Within the cooldown nothing new starts.
    await _cycle(now + timedelta(minutes=2))
    att = await _attempts(c, cust["id"])
    assert len(att) == 1 and att[0]["status"] == "verifying"
    # After the verify time the problem is still there: failed, not fixed.
    await _cycle(now + timedelta(minutes=6))
    att = await _attempts(c, cust["id"])
    assert att[-1]["status"] == "failed" and att[-1]["reason"]["code"] == "not_fixed" and att[-1]["resolved"] is False
    # After the cooldown a second try, which fails too ...
    await _cycle(now + timedelta(minutes=12))
    await _cycle(now + timedelta(minutes=18))
    att = await _attempts(c, cust["id"])
    assert [a["status"] for a in att][:2] == ["failed", "failed"], att
    # ... and two failures in an hour trip the circuit breaker: everything is paused.
    st = (await c.get("/api/remediation/status")).json()["settings"]
    assert st["paused"] is True and "circuit breaker" in st["pause_note"]
    assert await remediation.scan_alerts(now + timedelta(minutes=30)) == []
    # Resumed: the loop protection still refuses a third attempt, recorded once.
    r = await c.post("/api/remediation/pause", json={"paused": False})
    assert r.status_code == 200 and r.json()["paused"] is False
    from mmn.db import sessionmaker
    from mmn.services.settings_store import set_setting

    async with sessionmaker()() as db:  # the test's clock runs ahead of the real one
        cfg = await remediation.settings(db)
        await set_setting(db, "remediation", {**cfg, "resumed_at": (now + timedelta(minutes=20)).isoformat()})
        await db.commit()
    await _cycle(now + timedelta(minutes=30))
    await _cycle(now + timedelta(minutes=45))
    att = await _attempts(c, cust["id"])
    skipped = [a for a in att if a["status"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"]["code"] == "max_attempts", att
    await c.put("/api/remediation/settings", json={"max_failures_per_hour": 5, "verify_minutes": 15})
    fake_agent.state.pop((web, "redis"))


async def test_emergency_stop_and_approve_mode(admin_client, fake_agent):
    c = admin_client
    cust, rb = await _setup(c, "Noodknop Klant")
    web = await _machine(cust["id"], "rem-stop01")
    fake_agent.online.add(web)
    fake_agent.state[(web, "nginx")] = "failed"
    await _rule(c, cust, rb, mode="approve")
    await _cycle()
    att = await _attempts(c, cust["id"])
    assert att[0]["status"] == "waiting_approval", att
    run = (await c.get(f"/api/runbooks/runs/{att[0]['run_id']}")).json()
    aid = run["steps"][0]["state"]["approval_id"]
    a = (await c.get(f"/api/gateway/approvals/{aid}")).json()
    assert a["tool"] == "manage_service" and a["status"] == "pending"
    assert any(r.get("params", {}).get("recovery") == "Restart web services" for r in a["reasons"])
    assert not any(m == "services.action" for _, m, _p in fake_agent.calls)

    # The emergency stop cancels the busy recovery and withdraws its approval request.
    r = await c.post("/api/remediation/pause", json={"paused": True, "note": "test"})
    assert r.status_code == 200 and r.json()["cancelled"] == 1
    att = await _attempts(c, cust["id"])
    assert att[0]["status"] == "cancelled" and att[0]["reason"]["code"] == "paused"
    assert (await c.get(f"/api/gateway/approvals/{aid}")).json()["status"] == "rejected"
    assert (await c.get(f"/api/runbooks/runs/{att[0]['run_id']}")).json()["status"] == "cancelled"
    await _cycle()
    assert len(await _attempts(c, cust["id"])) == 1  # nothing new while stopped
    await c.post("/api/remediation/pause", json={"paused": False})
    fake_agent.state.pop((web, "nginx"))


async def test_guardrails_and_notify_mode(admin_client, fake_agent):
    from mmn.db import sessionmaker
    from mmn.models import NightlyFinding, NightlyRun
    from mmn.services import nodes

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Grens Klant"})).json()
    await _machine(cust["id"], "rem-pve01", "proxmox")
    await _machine(cust["id"], "rem-srv01")
    async with sessionmaker()() as db:
        assert "Proxmox" in await remediation.step_blocked(db, "reboot_node", {"node": "rem-pve01"})
        assert await remediation.step_blocked(db, "reboot_node", {"node": "rem-srv01"}) is None
        assert "never calls" in await remediation.step_blocked(db, "delete_guest", {})
        assert "never calls" in await remediation.step_blocked(db, "restore_guest", {})
        assert await remediation.step_blocked(db, "manage_service", {"node": "rem-pve01"}) is None
    with pytest.raises(nodes.NodeError, match="Proxmox VE node"):
        await remediation.recover_agent("rem-pve01")
    with pytest.raises(nodes.NodeError, match="not a VM or container"):
        await remediation.recover_agent("rem-srv01")

    # A runbook that deletes or reboots something cannot be used by a rule.
    bad = (await c.post("/api/runbooks", json={"name": "Grens delete", "steps": [
        {"kind": "tool", "tool": "delete_snapshot", "args": {"guest": "1", "name": "x", "confirm": "x"}}]})).json()
    r = await c.post("/api/remediation/rules", json={"name": "bad", "trigger": "finding", "match": {"codes": ["disk_full"]},
                                                     "runbook_id": bad["id"]})
    assert r.status_code == 400 and "never calls" in r.text

    # A finding rule in mode notify reports what would run, and the nightly proposal is not made.
    r = await c.post("/api/remediation/presets/update_agent", json={"customer_id": cust["id"], "lang": "nl"})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["name"] == "Verouderde agents bijwerken" and rule["runbook_name"] == "Agent bijwerken"
    rule = (await c.put(f"/api/remediation/rules/{rule['id']}", json={**{k: rule[k] for k in (
        "name", "customer_id", "trigger", "match", "runbook_id", "params", "delay_minutes", "cooldown_minutes",
        "max_attempts")}, "mode": "notify"})).json()
    assert rule["mode"] == "notify"
    async with sessionmaker()() as db:
        run = NightlyRun(trigger="manual", status="done", summary={})
        db.add(run)
        await db.flush()
        db.add(NightlyFinding(run_id=run.id, environment_id=cust["id"], key="k-agent-old", category="updates",
                              severity="low", area="agents", target="rem-srv01", code="agent_old", params={},
                              finding="The agent is old", suggestion="", is_new=True,
                              first_seen_at=datetime.now(UTC)))
        await db.commit()
        run_id = run.id
    ids = await remediation.on_findings(run_id, cust["id"])
    assert len(ids) == 1
    att = await _attempts(c, cust["id"])
    assert att[0]["status"] == "notified" and att[0]["reason"]["params"]["params"] == {"server": "rem-srv01"}
    assert att[0]["source"] == "finding"
    # Not again within the cooldown.
    assert await remediation.on_findings(run_id, cust["id"]) == []


async def test_summary_report_and_mcp(admin_client, client, fake_agent):
    from mmn.db import sessionmaker
    from mmn.models import Environment
    from mmn.services import reports

    c = admin_client
    cust, rb = await _setup(c, "Rapport Klant")
    web = await _machine(cust["id"], "rem-rep01")
    fake_agent.online.add(web)
    fake_agent.state[(web, "nginx")] = "failed"
    await _rule(c, cust, rb)
    await _cycle()
    await _cycle()
    async with sessionmaker()() as db:
        e = await db.get(Environment, cust["id"])
        now = datetime.now(UTC)
        period = f"{now.year}-{now.month:02d}"
        data = await reports.build(db, e, period, "nl", live=False)
        assert data["recovery"]["fixed"] == 1 and data["recovery"]["items"][0]["target"] == "rem-rep01"
        html = reports.render_html(data, reports.ZoneInfo("UTC"))
        assert "Automatisch herstel" in html and "Opgelost" in html
        assert reports.render_pdf(data, reports.ZoneInfo("UTC"))[:4] == b"%PDF"

    token, _ = await _token(c, "claude-rem", ["read", "runbooks", "services"])
    await _init(client, token)
    data, err = await _call(client, token, "get_remediation", {"customer": "Rapport Klant"})
    assert err is None and data["attempts"][0]["status"] == "succeeded" and data["rules"]
    data, err = await _call(client, token, "pause_remediation", {"paused": True, "reason": "AI test"})
    assert err is None and data["paused"] is True
    assert (await c.get("/api/remediation/status")).json()["settings"]["paused_by"] == "claude-rem"
    data, err = await _call(client, token, "pause_remediation", {"paused": False, "reason": "done"})
    assert err is None and data["paused"] is False
    data, err = await _call(client, token, "cleanup_disk", {"node": "rem-rep01", "reason": "test"})
    assert err is None and data["machine"] == "rem-rep01" and "cleaned" in data["output"]
    fake_agent.state.pop((web, "nginx"))
