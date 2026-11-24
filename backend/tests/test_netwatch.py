"""Phase 15: network adapters in automatic recovery (an uplink without link is restarted, also by the
agent itself when it cannot reach Stackpilot) and scheduled PowerShell scripts on Windows machines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.services import netwatch, remediation

WIN_LIST = ("NIC\tSLOT 3 Port 1\tIntel(R) Ethernet Controller X710 for 10GbE SFP+\t{x710}\t10 Gbps\t00-11\t0\r\n"
            "NIC\tEthernet 4\tspare port\tDisconnected\t0 bps\t00-12\t0\r\n"
            "NIC\tWi-Fi\tIntel Wi-Fi 6\tDisconnected\t\t00-13\t1\r\n"
            "NIC\tMgmt\tBroadcom NetXtreme\tUp\t1 Gbps\t00-14\t0\r\n")


# ------------------------------------------------------------------ unit tests
def test_parse_scripts_and_patterns():
    a = netwatch.parse_adapters(WIN_LIST.format(x710="Disconnected") + "junk\nNIC\t\tx\tUp\n")
    assert [x["name"] for x in a] == ["SLOT 3 Port 1", "Ethernet 4", "Wi-Fi", "Mgmt"]
    assert a[0]["status"] == "down" and a[2]["wireless"] and a[3]["status"] == "up"
    lin = netwatch.parse_adapters("NIC\teno1\tixgbe\tup\t10000\taa:bb\t0\nNIC\teno2\ti40e\tdisabled\t-1\tcc\t0\n")
    assert lin[0]["speed"] == "10 Gbps" and lin[1]["status"] == "disabled" and lin[1]["speed"] == ""
    assert "Get-NetAdapter -Physical" in netwatch.list_script("windows")
    assert "/sys/class/net" in netwatch.list_script("linux")

    ps = netwatch.restart_script("windows", "SLOT 3 Port 1")
    assert "Restart-NetAdapter -Name 'SLOT 3 Port 1' -Confirm:$false" in ps
    sh = netwatch.restart_script("linux", "eno1")
    assert "ip link set dev eno1 down" in sh and "ip link set dev eno1 up" in sh
    for bad in ("x'; Remove-Item C:\\", "eno1; reboot", "$(id)", ""):
        with pytest.raises(netwatch.NetwatchError):
            netwatch.restart_script("windows", bad)

    assert netwatch.glob_any([], a[0]) and netwatch.glob_any(["*x710*"], a[0])
    assert netwatch.glob_any(["slot 3 port ?"], a[0]) and not netwatch.glob_any(["eno*"], a[0])
    assert netwatch.clean_patterns(["*X710*", " ", "*X710*", "Ethernet 2"]) == ["*X710*", "Ethernet 2"]
    with pytest.raises(netwatch.NetwatchError):
        netwatch.clean_patterns(["eth0; rm"])


def test_network_rules_match_and_agent_config():
    from mmn.models import Alert, Node, RemediationRule

    with pytest.raises(remediation.RemediationError, match="adapter"):
        remediation.clean_match("alert", {"kinds": ["network"], "adapters": ["`bad`"]})
    m = remediation.clean_match("alert", {"kinds": ["network"], "adapters": ["*X710*"], "local": 1})
    assert m == {"kinds": ["network"], "adapters": ["*X710*"], "local": True}
    # The fields only belong to network rules.
    assert remediation.clean_match("alert", {"kinds": ["disk"], "adapters": ["x"], "local": True}) == {"kinds": ["disk"]}

    rule = RemediationRule(name="nic", trigger="alert", match=m, customer_id=None, delay_minutes=3,
                           cooldown_minutes=15, max_attempts=3)
    host = Node(name="hyperv01", kind="windows", role="server", environment_id="c1")
    hit = Alert(kind="network", key="network:x:SLOT 3 Port 1", severity="critical", message="down",
                detail={"adapter": "SLOT 3 Port 1", "description": "Intel(R) Ethernet Controller X710"})
    other = Alert(kind="network", key="network:x:Mgmt", severity="critical", message="down",
                  detail={"adapter": "Mgmt", "description": "Broadcom"})
    assert remediation.match_alert(rule, hit, host, "c1") and not remediation.match_alert(rule, other, host, "c1")

    assert netwatch.agent_config([]) == {"enabled": False}
    r2 = RemediationRule(id="r2", name="all", trigger="alert", match={"kinds": ["network"], "local": True},
                         delay_minutes=0, cooldown_minutes=60, max_attempts=1)
    rule.id = "r1"
    cfg = netwatch.agent_config([rule, r2])
    assert cfg == {"enabled": True, "adapters": [], "down_minutes": 2, "cooldown_minutes": 15, "max_per_day": 1,
                   "rules": ["r1", "r2"]}
    assert netwatch.agent_config([rule])["adapters"] == ["*X710*"]
    assert netwatch.config_hash(cfg) == netwatch.config_hash(dict(cfg)) != netwatch.config_hash({"enabled": False})
    assert "restart_network_adapter" in {p["key"] for p in remediation.PRESETS}


# ------------------------------------------------------------------ a fake Windows host
class FakeHost:
    def __init__(self):
        self.x710 = "Up"
        self.fixable = True
        self.calls: list[tuple[str, str, dict]] = []
        self.online: set[str] = set()
        self.netwatch: dict[str, dict] = {}  # node id -> {"hash", "actions"}

    async def request(self, node_id, method, params=None, timeout=60):
        params = params or {}
        self.calls.append((node_id, method, params))
        if method == "exec":
            cmd = params["command"]
            if "Restart-NetAdapter" in cmd:
                if self.fixable:
                    self.x710 = "Up"
                st = "Up" if self.x710 == "Up" else "Disconnected"
                return {"exit_code": 0, "stdout": f"NIC\tSLOT 3 Port 1\tIntel(R) Ethernet Controller X710\t{st}\t10 Gbps"
                                                  "\t00-11\t0\n", "stderr": ""}
            if "Get-NetAdapter -Physical" in cmd:
                return {"exit_code": 0, "stdout": WIN_LIST.format(x710=self.x710), "stderr": ""}
            if "systemctl show" in cmd or "Get-Service" in cmd:
                return {"exit_code": 0, "stdout": "", "stderr": ""}
        if method == "netwatch.status":
            st = self.netwatch.setdefault(node_id, {"hash": "", "actions": []})
            return {"hash": st["hash"], "enabled": bool(st["hash"]), "actions": list(st["actions"])}
        if method == "netwatch.apply":
            self.netwatch.setdefault(node_id, {"actions": []})["hash"] = params["hash"]
            self.netwatch[node_id]["config"] = params
            return params
        if method == "netwatch.ack":
            st = self.netwatch[node_id]
            st["actions"] = [a for a in st["actions"] if a["at"] > params["until"]]
            return {"removed": 1}
        raise AssertionError(f"unexpected agent call {method}")


@pytest.fixture
def fake_host(monkeypatch):
    from mmn.services import agent_hub

    fh = FakeHost()
    monkeypatch.setattr(agent_hub, "request", fh.request)
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: nid in fh.online)
    netwatch._nic_state.clear()
    remediation._service_state.clear()
    yield fh
    netwatch._nic_state.clear()


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    from sqlalchemy import delete

    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.name.like("nw-%")))
        await db.commit()


async def _cycle():
    from mmn.services import monitoring, runbooks

    await netwatch.probe(force=True)
    await monitoring.evaluate()
    await remediation.tick()
    await runbooks.wait_idle()
    await remediation.sync()


async def test_network_recovery_end_to_end(admin_client, client, fake_host):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Netwerk Klant"})).json()
    host = await _machine(cust["id"], "nw-hyperv01", kind="windows", capabilities=["exec", "netwatch"])
    old = await _machine(cust["id"], "nw-old01", kind="windows", capabilities=["exec"])
    fake_host.online |= {host, old}
    r = await c.post("/api/remediation/presets/restart_network_adapter",
                     json={"customer_id": cust["id"], "lang": "nl", "adapters": ["*X710*"], "local": True})
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["name"] == "Netwerkadapter zonder link herstarten"
    assert rule["match"] == {"kinds": ["network"], "adapters": ["*X710*"], "local": True}
    assert rule["params"]["adapter"] == "{{alert.detail.adapter}}"

    # First look: the X710 is up (seen up), the spare port was never up, so no alert for it.
    await _cycle()
    alerts = [a for a in (await c.get("/api/monitoring/alerts", params={"open_only": True})).json()
              if a["kind"] == "network"]
    assert alerts == []
    # The agent with the watchdog got its configuration; the old agent is listed as needing an update.
    cfg = fake_host.netwatch[host]["config"]
    assert cfg["enabled"] and cfg["adapters"] == ["*X710*"] and cfg["down_minutes"] == 3
    st = (await c.get("/api/remediation/status")).json()
    assert "network" in st["alert_kinds"] and "nw-old01" in st["network"]["needs_agent_update"]

    # The switch reboots and the X710 does not come back: alert, recovery, the adapter is restarted.
    fake_host.x710 = "Disconnected"
    await _cycle()
    alerts = [a for a in (await c.get("/api/monitoring/alerts", params={"open_only": True})).json()
              if a["kind"] == "network" and a["node_id"] == host]
    assert alerts and alerts[0]["detail"]["adapter"] == "SLOT 3 Port 1", alerts
    # The rule waits 3 minutes (delay): pretend the alert is older.
    from sqlalchemy import update

    from mmn.db import sessionmaker
    from mmn.models import Alert

    async with sessionmaker()() as db:
        await db.execute(update(Alert).where(Alert.kind == "network")
                         .values(started_at=datetime.now(UTC) - timedelta(minutes=5)))
        await db.commit()
    await _cycle()

    async def mine():
        rows = (await c.get("/api/remediation/attempts", params={"customer_id": cust["id"]})).json()
        return [a for a in rows if a["target"] == "nw-hyperv01"]

    # Both Windows hosts lost their X710 link (the fake host answers the same for both).
    att = await mine()
    assert len(att) == 1 and att[0]["status"] in ("running", "verifying", "succeeded"), att
    assert any(m == "exec" and "Restart-NetAdapter -Name 'SLOT 3 Port 1'" in p["command"]
               for _, m, p in fake_host.calls)
    run = (await c.get(f"/api/runbooks/runs/{att[0]['run_id']}")).json()
    assert run["status"] == "running" and run["current"]["id"] == "wait", run
    # Skip the minute of waiting.
    from mmn.models import RunbookRun
    from mmn.services import runbooks

    async with sessionmaker()() as db:
        rr = await db.get(RunbookRun, att[0]["run_id"])
        rr.state = [{**st, "wait_until": "2000-01-01T00:00:00+00:00"} if st.get("wait_until") else st
                    for st in rr.state]
        await db.commit()
    await runbooks.tick()
    await runbooks.wait_idle()
    run = (await c.get(f"/api/runbooks/runs/{att[0]['run_id']}")).json()
    assert run["status"] == "succeeded", run
    await _cycle()
    att = await mine()
    assert att[0]["status"] == "succeeded" and att[0]["resolved"] is True, att

    # The agent repaired it itself while it was offline: recorded once, as a local attempt.
    fake_host.netwatch[host]["actions"] = [{"at": "2026-11-24T03:12:00Z", "adapter": "SLOT 3 Port 1",
                                           "description": "Intel X710", "before": "down", "after": "up", "ok": True}]
    await netwatch.probe(force=True)
    await netwatch.probe(force=True)
    att = await mine()
    local = [a for a in att if a["source"] == "local"]
    assert len(local) == 1 and local[0]["status"] == "succeeded" and local[0]["reason"]["code"] == "local_ok", att
    assert local[0]["target"] == "nw-hyperv01" and fake_host.netwatch[host]["actions"] == []
    assert "itself" in remediation.reason_text(local[0]["reason"], "en")
    assert "zelf" in remediation.reason_text(local[0]["reason"], "nl")

    # Switching the local fallback off turns the watchdog off on the agent.
    body = {**{k: rule[k] for k in ("name", "customer_id", "trigger", "runbook_id", "params", "mode",
                                    "delay_minutes", "cooldown_minutes", "max_attempts")},
            "match": {"kinds": ["network"], "adapters": ["*X710*"]}, "enabled": True}
    assert (await c.put(f"/api/remediation/rules/{rule['id']}", json=body)).status_code == 200
    await netwatch.probe(force=True)
    assert fake_host.netwatch[host]["config"] == {"enabled": False, "hash": netwatch.config_hash({"enabled": False})}

    # MCP: read the adapters and restart one; an adapter that is up needs force and confirm.
    token, _ = await _token(c, "claude-nw", ["read", "services"])
    await _init(client, token)
    data, err = await _call(client, token, "list_network_adapters", {"node": "nw-hyperv01"})
    assert err is None and {a["name"] for a in data["adapters"]} >= {"SLOT 3 Port 1", "Mgmt"}
    data, err = await _call(client, token, "restart_network_adapter",
                            {"node": "nw-hyperv01", "adapter": "Mgmt", "reason": "test"})
    assert err is None and data["restarted"] is False and data["status"] == "up"
    _, err = await _call(client, token, "restart_network_adapter",
                         {"node": "nw-hyperv01", "adapter": "Mgmt", "reason": "test", "force": True})
    assert err and "confirm" in err
    _, err = await _call(client, token, "restart_network_adapter",
                         {"node": "nw-hyperv01", "adapter": "Nope", "reason": "test"})
    assert err and "no physical network adapter" in err
    fake_host.x710, fake_host.fixable = "Disconnected", False
    data, err = await _call(client, token, "restart_network_adapter",
                            {"node": "nw-hyperv01", "adapter": "SLOT 3 Port 1", "reason": "test"})
    assert err is None and data["restarted"] and not data["fixed"] and "cable" in data["message"]


# ------------------------------------------------------------------ scheduled scripts on Windows
async def test_scheduled_powershell_scripts(admin_client, monkeypatch):
    from mmn.db import sessionmaker
    from mmn.services import agent_hub, guests, tasks

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Script Klant"})).json()
    win = await _machine(cust["id"], "nw-win01", kind="windows", capabilities=["exec", "ps-scripts"])
    old = await _machine(cust["id"], "nw-win02", kind="windows", capabilities=["exec"])
    lin = await _machine(cust["id"], "nw-lin01", kind="linux", capabilities=["exec", "scripts"])
    targets = {"scope": "nodes", "node_ids": [win, old, lin]}

    # Validation: a script for at least one OS; containers still need the shell script.
    base = {"name": "Link check", "kind": "custom_command", "cron": "*/15 * * * *", "targets": targets}
    r = await c.post("/api/tasks", json={**base, "options": {"run_on": "node"}})
    assert r.status_code == 400 and "Windows" in r.text, r.text
    r = await c.post("/api/tasks", json={**base, "options": {"run_on": "ct", "command_windows": "Get-Date"}})
    assert r.status_code == 400

    ps = "Get-NetAdapter | Where-Object Status -ne 'Up' | Restart-NetAdapter -Confirm:$false"
    async with sessionmaker()() as db:
        only_ps = await tasks.resolve_targets(db, "custom_command", targets, {"run_on": "node", "command_windows": ps})
        both = await tasks.resolve_targets(db, "custom_command", targets,
                                           {"run_on": "node", "command_windows": ps, "command": "ip -br link"})
    assert {t["node"] for t in only_ps} == {"nw-win01", "nw-win02"}
    assert {t["node"] for t in both} == {"nw-win01", "nw-win02", "nw-lin01"}

    r = await c.post("/api/tasks", json={**base, "options": {"run_on": "node", "command_windows": ps,
                                                             "command": "ip -br link"}})
    assert r.status_code in (200, 201), r.text
    sent: list[tuple[str, str, dict]] = []

    class Job:
        def __init__(self, i):
            self.id = i

    async def fake_script(node_id, kind, body, **kw):
        sent.append((node_id, body, kw))
        return Job(f"job{len(sent)}")

    async def fake_wait(job, timeout=0):
        return "success", {"exit_code": 0}, None

    monkeypatch.setattr(guests, "script", fake_script)
    monkeypatch.setattr(guests, "run_and_wait", fake_wait)
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: True)
    async with sessionmaker()() as db:
        task = await tasks.get_task(db, r.json()["id"])
        rows = {}
        for tg in both:
            rows[tg["node"]] = await tasks._node_target(task, tg, "tester")
    by_node = {nid: body for nid, body, _ in sent}
    assert by_node[win] == ps and by_node[lin] == "ip -br link" and old not in by_node
    assert rows["nw-win01"]["status"] == "success" and rows["nw-lin01"]["status"] == "success"
    assert rows["nw-win02"]["status"] == "skipped" and "v0.15.0" in rows["nw-win02"]["error"]
    assert next(kw for nid, _, kw in sent if nid == win)["label"]["shell"] == "powershell"
