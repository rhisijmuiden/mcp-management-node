"""Phase 16: uptime checks (HTTP(S), TCP, ping, DNS, VPN), incidents, alerts and the uptime SLA per
customer in the API, the MCP tools and the monthly report."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.models import Environment, UptimeCheck
from mmn.services import gateway, uptime


# ------------------------------------------------------------------ unit tests
def test_clean_checks():
    t, p, cfg = uptime.clean_check("http", "www.example.com/health", None, {"keyword": "OK"})
    assert t == "https://www.example.com/health" and p is None
    assert cfg == {"method": "GET", "expect_status": "200-399", "keyword": "OK", "keyword_absent": False,
                   "verify_tls": True, "follow_redirects": True}
    for bad in ("https://exa mple.com", "https://x.com/'; rm", "ftp://x.com", "https://"):
        with pytest.raises(uptime.UptimeError):
            uptime.clean_check("http", bad, None, {})
    with pytest.raises(uptime.UptimeError, match="GET"):
        uptime.clean_check("http", "https://x.nl", None, {"method": "HEAD", "keyword": "x"})
    with pytest.raises(uptime.UptimeError, match="expected status"):
        uptime.clean_check("http", "https://x.nl", None, {"expect_status": "2xx"})
    assert uptime.clean_check("tcp", "mail.example.com:587", None, {}) == ("mail.example.com", 587, {})
    assert uptime.clean_check("tcp", "[2001:db8::1]:22", None, {})[:2] == ("2001:db8::1", 22)
    with pytest.raises(uptime.UptimeError, match="port"):
        uptime.clean_check("tcp", "mail.example.com", None, {})
    assert uptime.clean_check("ping", "10.40.5.3", None, {}) == ("10.40.5.3", None, {})
    for bad in ("host;reboot", "$(id)", "-oProxy", "a b"):
        with pytest.raises(uptime.UptimeError):
            uptime.clean_check("ping", bad, None, {})
    assert uptime.clean_check("vpn", "vpn.example.com", None, {}) == ("vpn.example.com", 443, {"mode": "ssl"})
    assert uptime.clean_check("vpn", "10.20.0.1", None, {"mode": "tunnel"}) == ("10.20.0.1", None, {"mode": "tunnel"})
    assert uptime.clean_check("dns", "Example.com.", None, {"record_type": "mx", "server": "1.1.1.1"})[2] == {
        "record_type": "MX", "expect": "", "server": "1.1.1.1"}
    with pytest.raises(uptime.UptimeError, match="IP"):
        uptime.clean_check("dns", "example.com", None, {"server": "dns.google"})
    assert uptime.status_ok(301, "200-399") and not uptime.status_ok(503, "200-299,301")


def test_verdicts_and_parsers():
    cfg = {"expect_status": "200", "keyword": "Welcome"}
    assert uptime.http_verdict(200, "<h1>welcome</h1>", cfg) is None
    assert "keyword" in uptime.http_verdict(200, "maintenance", cfg)
    assert "503" in uptime.http_verdict(503, "Welcome", cfg)
    assert "contains" in uptime.http_verdict(200, "Fatal error", {"keyword": "fatal error", "keyword_absent": True})
    assert uptime.dns_verdict(["10.0.0.1", "10.0.0.2"], {"expect": "10.0.0.2"}) is None
    assert "not in the answer" in uptime.dns_verdict(["10.0.0.1"], {"expect": "10.0.0.9"})
    assert uptime.dns_verdict([], {}) == "no records"

    ok = uptime.parse_ping("3 packets transmitted, 3 received, 0% packet loss\nrtt min/avg/max/mdev = 0.1/0.25/0.4/0.1 ms", 0)
    assert ok.ok and ok.ms == 0.25
    bad = uptime.parse_ping("ping: badhost: Name or service not known\n", 2)
    assert bad.ok is False and bad.error
    assert uptime.parse_ping("3 packets transmitted, 0 received, 100% packet loss", 1).error == "no reply"

    c = UptimeCheck(id="x", kind="tcp", target="10.0.0.5", port=3389, config={}, timeout_seconds=5)
    assert uptime.parse_agent(c, "SPTCP 0 12 \n").ok
    r = uptime.parse_agent(c, "SPTCP 1 3 bash: connect: Connection refused\n")
    assert r.ok is False and "refused" in r.error
    assert uptime.parse_agent(c, "SPTCP 124 5000 \n").error == "timed out"
    win = uptime.parse_agent(c, 'noise\nSPJSON {"ok":false,"ms":5001.2,"error":"timed out"}\n')
    assert win.ok is False and win.error == "timed out" and win.ms == 5001.2

    h = UptimeCheck(id="h", kind="http", target="https://intranet.local", config={"expect_status": "200-399",
                                                                                   "keyword": "Login"}, timeout_seconds=5)
    r = uptime.parse_agent(h, "SPHTTP 0 200 0.123\nSPERR \nSPBODY\n<form>Login</form>")
    assert r.ok and r.code == 200 and abs(r.ms - 123) < 0.01
    r = uptime.parse_agent(h, "SPHTTP 6 000 0.002\nSPERR curl: (6) Could not resolve host: intranet.local \nSPBODY\n")
    assert r.ok is False and "resolve" in r.error
    r = uptime.parse_agent(h, 'SPJSON {"ok":true,"ms":80,"code":500,"body":"Login"}')
    assert r.ok is False and r.code == 500

    d = UptimeCheck(id="d", kind="dns", target="ad.ijmond.local", config={"record_type": "A", "expect": "10.40.1.10"},
                    timeout_seconds=5)
    assert uptime.parse_agent(d, "SPDNS 0 4 dig\n10.40.1.10\n10.40.1.11\n").ok
    r = uptime.parse_agent(d, "SPDNS 0 4 host\nad.ijmond.local has address 10.40.1.99\n")
    assert r.ok is False and r.detail["values"] == ["10.40.1.99"]
    assert "NXDOMAIN" in uptime.parse_agent(d, "SPDNS 1 4 host\nHost x not found: 3(NXDOMAIN)\n").error
    assert uptime.parse_agent(d, 'SPJSON {"ok":true,"ms":3,"values":"10.40.1.10"}').ok
    assert uptime.parse_agent(d, "garbage").error == "no result from the agent"


def test_agent_scripts_quote_values():
    h = UptimeCheck(id="h", kind="http", target="https://x.example/a?b=1&c=2",
                    config={"method": "GET", "verify_tls": False, "follow_redirects": True}, timeout_seconds=7)
    sh = uptime.agent_script(h, windows=False)
    assert "'https://x.example/a?b=1&c=2'" in sh and " -k" in sh and " -L" in sh and "--max-time 7" in sh
    ps = uptime.agent_script(h, windows=True)
    assert "Invoke-WebRequest -Uri 'https://x.example/a?b=1&c=2'" in ps and "ServerCertificateValidationCallback" in ps
    p = UptimeCheck(id="p", kind="vpn", target="10.20.0.1", config={"mode": "tunnel"}, timeout_seconds=3)
    assert "ping -c 3" in uptime.agent_script(p, windows=False)
    assert "NetworkInformation.Ping" in uptime.agent_script(p, windows=True)
    d = UptimeCheck(id="d", kind="dns", target="example.com", config={"record_type": "MX", "server": "1.1.1.1"},
                    timeout_seconds=3)
    assert "dig +short +time=3 +tries=1 @1.1.1.1 example.com MX" in uptime.agent_script(d, windows=False)
    assert "Resolve-DnsName -Name 'example.com' -Type MX" in uptime.agent_script(d, windows=True)


def test_sla_math_and_maintenance():
    tz = ZoneInfo("Europe/Amsterdam")
    start = datetime(2026, 11, 2, tzinfo=tz).astimezone(UTC)  # a Monday
    stop = start + timedelta(days=7)
    win = gateway.parse_window("Tue 22:00-02:00")
    iv = uptime.maintenance_intervals(win, start, stop, tz)
    assert len(iv) == 1 and iv[0][0] == datetime(2026, 11, 3, 22, tzinfo=tz).astimezone(UTC)
    assert iv[0][1] - iv[0][0] == timedelta(hours=4)
    # An outage of 2 hours of which 1 hour inside the window.
    out = (datetime(2026, 11, 3, 21, tzinfo=tz).astimezone(UTC), datetime(2026, 11, 3, 23, tzinfo=tz).astimezone(UTC))
    assert uptime.downtime([out], start, stop, iv) == 3600
    assert uptime.downtime([out], start, stop, []) == 7200
    # Overlapping incidents count once.
    assert uptime.downtime([out, (out[0], out[0] + timedelta(minutes=30))], start, stop, []) == 7200
    assert uptime.merge([(start, start + timedelta(hours=1)), (start + timedelta(minutes=30), start + timedelta(hours=2))]) \
        == [(start, start + timedelta(hours=2))]

    e = Environment(name="x", sla="99,5% uptime, response within 4 hours", uptime={})
    assert uptime.sla_of(e)["target_pct"] == 99.5 and uptime.sla_of(e)["target_source"] == "sla_text"
    uptime.save_sla(e, target_pct=99.9, recipients=["Ops@Example.com"])
    assert uptime.sla_of(e) == {"target_pct": 99.9, "target_source": "setting", "exclude_maintenance": True,
                                "alert_breach": True, "recipients": ["Ops@example.com"]}
    with pytest.raises(uptime.UptimeError):
        uptime.save_sla(e, target_pct=101)
    with pytest.raises(uptime.UptimeError):
        uptime.save_sla(e, recipients=["not-an-address"])
    uptime.save_sla(e, clear_target=True)
    assert uptime.sla_of(e)["target_pct"] == 99.5


# ------------------------------------------------------------------ a small web server
class Web:
    def __init__(self):
        self.status = 200
        self.body = "<h1>Welcome to the shop</h1>"
        self.delay = 0.0
        self.server = None
        self.port = 0
        self.hits = 0

    async def handle(self, reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        self.hits += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        body = self.body.encode()
        writer.write(f"HTTP/1.1 {self.status} X\r\nContent-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def stop(self):
        self.server.close()


@pytest.fixture
async def web():
    w = Web()
    await w.start()
    yield w
    w.stop()


async def _alerts(c, check_id=None, customer_id=None):
    from mmn.services import monitoring

    await monitoring.evaluate()
    rows = (await c.get("/api/monitoring/alerts", params={"open": True, "limit": 500})).json()
    return [a for a in rows if (check_id and a["detail"].get("check_id") == check_id)
            or (customer_id and a["kind"] == "sla" and a["detail"].get("customer_id") == customer_id)]


async def _run(c, cid):
    r = await c.post(f"/api/uptime/checks/{cid}/run")
    assert r.status_code == 200, r.text
    return r.json()


async def test_http_check_incident_alert_and_sla(admin_client, web):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Uptime Webshop", "maintenance_window": "daily 03:00-03:30"})).json()
    body = {"customer_id": cust["id"], "name": "Webshop", "kind": "http", "target": f"http://127.0.0.1:{web.port}/",
            "config": {"keyword": "welcome"}, "interval_seconds": 60, "timeout_seconds": 3, "fail_after": 2,
            "slow_ms": 1500}
    r = await c.post("/api/uptime/checks", json=body)
    assert r.status_code == 201, r.text
    chk = r.json()
    assert chk["label"] == f"http://127.0.0.1:{web.port}/" and chk["config"]["keyword"] == "welcome"
    r = await c.post("/api/uptime/checks", json={**body, "kind": "tcp", "target": "x.example"})
    assert r.status_code == 400 and "port" in r.text

    res = await _run(c, chk["id"])
    assert res["ok"] is True and res["code"] == 200 and res["status"] == "up"

    # Broken page: one failure is not yet down, the second one is.
    web.status = 503
    res = await _run(c, chk["id"])
    assert res["ok"] is False and res["status"] == "up" and "503" in res["error"]
    res = await _run(c, chk["id"])
    assert res["status"] == "down" and res["change"] == "down"
    al = await _alerts(c, chk["id"])
    assert len(al) == 1 and al[0]["kind"] == "uptime" and al[0]["severity"] == "critical"
    assert "Webshop" in al[0]["message"] and al[0]["detail"]["source"] == "Uptime Webshop · Webshop"

    # Back up: the incident closes and the alert resolves.
    web.status = 200
    res = await _run(c, chk["id"])
    assert res["status"] == "up" and res["change"] == "up"
    left = await _alerts(c, chk["id"])
    assert left == [], [(a["key"], a["message"]) for a in left]

    # Slow is "degraded" with a warning.
    web.delay = 0.2
    await c.put(f"/api/uptime/checks/{chk['id']}", json={**body, "slow_ms": 50})
    res = await _run(c, chk["id"])
    assert res["status"] == "degraded"
    al = await _alerts(c, chk["id"])
    assert al and al[0]["severity"] == "warning" and "slowly" in al[0]["message"]
    web.delay = 0

    detail = (await c.get(f"/api/uptime/checks/{chk['id']}", params={"range": "24h"})).json()
    h = detail["history"]
    assert len(h["incidents"]) == 1 and h["incidents"][0]["open"] is False and "503" in h["incidents"][0]["error"]
    assert len(h["recent"]) >= 5 and h["points"] and detail["uptime_24h"] is not None

    lst = (await c.get("/api/uptime/checks", params={"customer_id": cust["id"]})).json()
    assert [x["name"] for x in lst["checks"]] == ["Webshop"] and lst["counts"] == {"degraded": 1}

    # SLA: an incident in a fresh check gives an availability below 100%; a target of 99.999% is missed.
    r = await c.put(f"/api/uptime/customers/{cust['id']}/sla", json={"target_pct": 99.999, "recipients": []})
    assert r.status_code == 200 and r.json()["target_pct"] == 99.999
    from sqlalchemy import select

    from mmn.db import sessionmaker
    from mmn.models import UptimeIncident

    async with sessionmaker()() as db:
        # Make the check a day old with a 30-minute outage, so the numbers are predictable.
        ch = await db.get(UptimeCheck, chk["id"])
        ch.created_at = datetime.now(UTC) - timedelta(days=1)
        inc = (await db.execute(select(UptimeIncident).where(
            UptimeIncident.check_id == chk["id"]))).scalar_one()
        inc.started_at = datetime.now(UTC) - timedelta(hours=5)
        inc.ended_at = inc.started_at + timedelta(minutes=30)
        await db.commit()
    s = (await c.get("/api/uptime/sla", params={"customer_id": cust["id"]})).json()
    one = s["customers"][0]
    assert one["met"] is False and one["checks"][0]["incidents"] == 1
    assert 97 < one["availability"] < 99.99 and one["downtime_minutes"] >= 29
    assert one["incidents"] and one["settings"]["target_pct"] == 99.999
    al = await _alerts(c, customer_id=cust["id"])
    assert al and "SLA" in al[0]["message"]

    # The monthly report shows the SLA (HTML and PDF).
    rep = (await c.post(f"/api/customers/{cust['id']}/reports", json={"period": s["period"]})).json()
    full = (await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}")).json()
    assert full["data"]["uptime"]["target_pct"] == 99.999 and full["data"]["summary"]["uptime_met"] is False
    html = (await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}/html")).text
    assert "Service availability (SLA)" in html and "Webshop" in html and "was not met" in html
    pdf = await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    rep_nl = (await c.post(f"/api/customers/{cust['id']}/reports", json={"period": s["period"], "lang": "nl"})).json()
    html = (await c.get(f"/api/customers/{cust['id']}/reports/{rep_nl['id']}/html")).text
    assert "Beschikbaarheid diensten (SLA)" in html and "niet gehaald" in html

    # Disabling ends an open incident and pauses the check; deleting removes it.
    r = await c.put(f"/api/uptime/checks/{chk['id']}", json={**body, "enabled": False})
    assert r.json()["status"] == "paused"
    assert (await c.delete(f"/api/uptime/checks/{chk['id']}")).status_code == 204
    assert await _alerts(c, customer_id=cust["id"]) == []


async def test_tcp_test_endpoint_and_tick(admin_client, web):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Uptime TCP"})).json()
    r = await c.post("/api/uptime/test", json={"customer_id": cust["id"], "kind": "tcp", "target": "127.0.0.1",
                                               "port": web.port, "timeout_seconds": 2})
    assert r.status_code == 200 and r.json()["ok"] is True
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    r = await c.post("/api/uptime/test", json={"customer_id": cust["id"], "kind": "tcp", "target": "127.0.0.1",
                                               "port": closed, "timeout_seconds": 2})
    assert r.json()["ok"] is False and r.json()["error"]

    chk = (await c.post("/api/uptime/checks", json={"customer_id": cust["id"], "name": "Port", "kind": "tcp",
                                                    "target": "127.0.0.1", "port": web.port, "interval_seconds": 30,
                                                    "timeout_seconds": 2})).json()
    await asyncio.sleep(0.5)  # the first run starts right after saving
    n = await uptime.tick(datetime.now(UTC) + timedelta(seconds=40))
    assert n >= 1
    got = (await c.get(f"/api/uptime/checks/{chk['id']}")).json()
    assert got["status"] == "up" and got["last_ms"] is not None


async def test_ping_from_stackpilot():
    r = await uptime.probe_ping("127.0.0.1", 2)
    assert r.ok is True or "not permitted" in (r.error or "") or "not allowed" in (r.error or ""), r
    if r.ok:
        assert r.ms is not None and r.ms < 1000
    bad = await uptime.probe_ping("no-such-host.invalid", 1)
    assert bad.ok is False and bad.error


async def test_agent_probe_and_mcp(admin_client, client, monkeypatch, web):
    from mmn.services import agent_hub

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Uptime Agent"})).json()
    other = (await c.post("/api/customers", json={"name": "Uptime Ander"})).json()
    dc = await _machine(cust["id"], "upt-dc01", kind="windows")
    lin = await _machine(cust["id"], "upt-gw01")
    calls = []

    async def request(node_id, method, params=None, timeout=60):
        calls.append((node_id, params["command"]))
        if node_id == dc:
            return {"exit_code": 0, "stdout": 'SPJSON {"ok":true,"ms":2.5,"values":["10.40.1.10"]}\r\n', "stderr": ""}
        return {"exit_code": 0, "stdout": "3 packets transmitted, 3 received\nrtt min/avg/max/mdev = 1/2.0/3/0 ms\nSPRC 0\n",
                "stderr": ""}

    monkeypatch.setattr(agent_hub, "request", request)
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: nid in (dc, lin))

    token, _ = await _token(c, "claude-uptime", ["read", "monitoring"])
    await _init(client, token)
    out, err = await _call(client, token, "save_uptime_check", {
        "customer": "Uptime Agent", "name": "AD DNS", "kind": "dns", "target": "ad.ijmond.local",
        "config": {"record_type": "A", "expect": "10.40.1.10"}, "probe": "upt-dc01", "machine": "upt-dc01"})
    assert err is None, err
    assert out["probe_name"] == "upt-dc01" and out["node_name"] == "upt-dc01"
    out2, err = await _call(client, token, "save_uptime_check", {
        "customer": "Uptime Agent", "name": "Tunnel Velsen", "kind": "vpn", "target": "10.20.0.1",
        "config": {"mode": "tunnel"}, "probe": "upt-gw01", "fail_after": 1}, 11)
    assert err is None, err
    _, err = await _call(client, token, "save_uptime_check", {
        "customer": "Uptime Ander", "name": "x", "kind": "ping", "target": "1.1.1.1", "probe": "upt-gw01"}, 12)
    assert "does not belong" in err

    res, err = await _call(client, token, "run_uptime_check", {"check": "AD DNS"}, 13)
    assert err is None and res["ok"] is True and res["values"] == ["10.40.1.10"], (res, err)
    assert any(nid == dc and "Resolve-DnsName" in cmd for nid, cmd in calls)
    res, _ = await _call(client, token, "run_uptime_check", {"check": out2["id"]}, 14)
    assert res["ok"] is True and res["ms"] == 2.0

    # The probe goes offline: the result is unknown, not down.
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: False)
    res, _ = await _call(client, token, "run_uptime_check", {"check": out2["id"]}, 15)
    assert res["ok"] is None and res["status"] == "unknown" and "offline" in res["error"]

    lst, _ = await _call(client, token, "list_uptime_checks", {"customer": "Uptime Agent"}, 16)
    assert {x["name"] for x in lst["checks"]} == {"AD DNS", "Tunnel Velsen"}
    one, _ = await _call(client, token, "get_uptime_check", {"check": "AD DNS", "range": "7d"}, 17)
    assert one["history"]["recent"][0]["ok"] is True
    cfg, err = await _call(client, token, "set_uptime_sla", {"customer": "Uptime Agent", "target_pct": 99.5}, 18)
    assert err is None and cfg["target_pct"] == 99.5
    rep, _ = await _call(client, token, "get_sla_report", {"customer": "Uptime Agent"}, 19)
    assert rep["customers"][0]["target_pct"] == 99.5 and len(rep["customers"][0]["checks"]) == 2

    # A token for the other customer sees nothing of this one.
    t2, _ = await _token(c, "claude-uptime-other", ["read", "monitoring"], [other["id"]])
    await _init(client, t2)
    lst, _ = await _call(client, t2, "list_uptime_checks", {}, 20)
    assert lst["checks"] == []
    _, err = await _call(client, t2, "run_uptime_check", {"check": "AD DNS"}, 21)
    assert "not found" in err
    # Without the monitoring scope a token cannot change checks.
    t3, _ = await _token(c, "claude-uptime-read", ["read"])
    await _init(client, t3)
    _, err = await _call(client, t3, "delete_uptime_check", {"check": "AD DNS", "confirm": "AD DNS"}, 22)
    assert "monitoring" in err

    _, err = await _call(client, token, "delete_uptime_check", {"check": "AD DNS", "confirm": "wrong"}, 23)
    assert "confirm" in err
    res, err = await _call(client, token, "delete_uptime_check", {"check": "AD DNS", "confirm": "AD DNS"}, 24)
    assert err is None and res["ok"]
