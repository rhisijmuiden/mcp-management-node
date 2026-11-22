"""Environments, pairing, the agent WebSocket protocol and the node actions, with a fake agent.

The fake agent talks to a real uvicorn server (WebSocket), sending the headers Nginx adds after
it has verified the client certificate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from urllib.parse import quote

import pytest
from conftest import DATA_DIR
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_mcp import _init, _rpc


def _dist() -> None:
    d = DATA_DIR / "agent-dist"
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("mmn-agent_0.2.0_amd64.deb", "mmn-agent-linux-amd64"):
        (d / name).write_bytes(b"fake " + name.encode())
        files[name] = {"sha256": hashlib.sha256((d / name).read_bytes()).hexdigest(), "size": 10}
    (d / "manifest.json").write_text(json.dumps({"version": "0.2.0", "files": files}))


def _secret() -> str:
    return (DATA_DIR / "agent-tls" / "proxy-secret").read_text().strip()


def _csr() -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve-test")])).sign(key, hashes.SHA256()))
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture(scope="module")
async def live_server(app):
    import uvicorn

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning",
                                           ws="websockets-sansio"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    yield port
    server.should_exit = True
    await task


class FakeAgent:
    """Answers requests like the Go agent does."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.calls: list[tuple[str, dict]] = []
        self.governor = "schedutil"

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def run(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") != "req":
                continue
            method, params = m["method"], m.get("params") or {}
            self.calls.append((method, params))
            res, err = {}, None
            if method == "governor.set":
                self.governor = params["governor"]
                res = {"current": self.governor, "available": ["performance", "schedutil", "powersave"]}
            elif method == "updates.check":
                res = {"checked_at": "2026-11-22T00:00:00Z", "packages": [], "reboot_required": False}
            elif method == "updates.apply":
                res = {"started": True}
                asyncio.get_running_loop().call_later(0.05, lambda j=params["job_id"]: asyncio.ensure_future(self.job(j)))
            elif method == "notifications.apply":
                res = {"target": "mmn-smtp"}
            elif method == "exec":
                res = {"exit_code": 0, "stdout": f"ran: {params['command']}\n", "stderr": ""}
            elif method == "packages.query":
                res = [{"name": "nginx", "version": "1.26.3-3", "arch": "amd64", "status": "ii"}]
            elif method == "logs.journal":
                res = {"exit_code": 0, "stdout": "Nov 22 nginx[1]: started\n", "stderr": ""}
            elif method == "unenroll":
                res = {"removed": True}
            else:
                err = f"unknown method {method}"
            await self.send({"type": "res", "id": m["id"], "ok": err is None, "result": res, "error": err or ""})

    async def job(self, job_id: str) -> None:
        await self.send({"type": "job", "job_id": job_id, "event": "line", "line": "$ apt-get update -q"})
        await self.send({"type": "job", "job_id": job_id, "event": "line", "line": "Setting up pve-manager"})
        await self.send({"type": "job", "job_id": job_id, "event": "done", "status": "success",
                         "result": {"remaining": 0, "reboot_required": True}})


HELLO = {"protocol": 1, "agent_version": "0.1.9", "os": "linux", "arch": "amd64", "hostname": "pve-test",
         "install": "deb", "capabilities": ["metrics", "apt", "governor", "pve", "pve-notifications", "exec", "logs",
                                            "packages"],
         "info": {"pve_version": "9.0.3", "cluster": {"name": "home", "quorate": True, "nodes": []}},
         "governor": {"current": "schedutil", "available": ["performance", "schedutil", "powersave"]}}


async def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if await cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_environments_crud(admin_client):
    c = admin_client
    envs = (await c.get("/api/environments")).json()
    assert any(e["name"] == "Home" and e["kind"] == "home" for e in envs)
    r = await c.post("/api/environments", json={"name": "Klant A", "kind": "customer"})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert (await c.post("/api/environments", json={"name": "klant a"})).status_code == 400
    r = await c.put(f"/api/environments/{eid}/notifications", json={"enabled": True, "mailto": []})
    assert r.status_code == 400
    assert (await c.delete(f"/api/environments/{eid}")).status_code == 204


async def test_enroll_connect_and_actions(admin_client, client, live_server):
    import websockets

    _dist()
    c = admin_client
    home = next(e for e in (await c.get("/api/environments")).json() if e["name"] == "Home")
    r = await c.post("/api/pairing", json={"environment_id": home["id"], "label": "pve-test"})
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    assert "install.sh" in r.json()["command"] and code in r.json()["command"]
    fp = r.json()["connect"]["fingerprint"]

    script = (await client.get("/api/agent/install.sh")).text
    assert fp in script and "mmn-agent_0.2.0_amd64.deb" in script
    # The web address may use a self-signed certificate; the package is checked by its SHA-256 instead.
    assert 'curl -fsSL --insecure -o "$TMP/$1"' in script and "sha256sum -c" in script

    key, csr = _csr()
    body = {"code": code.lower().replace("-", ""), "csr": csr, "hostname": "pve-test.lan", "agent_version": "0.1.9"}
    # Without the Nginx secret the endpoint does not exist.
    assert (await client.post("/agent/enroll", json=body)).status_code == 404
    hdr = {"x-mmn-agent-secret": _secret(), "host": "pve-mmn.lan"}
    bad = await client.post("/agent/enroll", json={**body, "code": "AAAA-BBBB-CCCC"}, headers=hdr)
    assert bad.status_code == 403
    r = await client.post("/agent/enroll", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    enrolled = r.json()
    assert enrolled["node_name"] == "pve-test" and enrolled["environment"] == "Home"
    # No agent or public address configured: the agent keeps the host it reached us on.
    assert enrolled["server"] == "pve-mmn.lan:8443"
    ca = x509.load_pem_x509_certificate(enrolled["ca"].encode())
    assert hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest() == fp
    cert = x509.load_pem_x509_certificate(enrolled["certificate"].encode())
    cert.verify_directly_issued_by(ca)
    # A code works once.
    assert (await client.post("/agent/enroll", json=body, headers=hdr)).status_code == 403
    node_id = enrolled["node_id"]

    ws_headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS",
                  "x-mmn-client-cert": quote(enrolled["certificate"])}
    url = f"ws://127.0.0.1:{live_server}/agent/ws"
    # No verified certificate: refused.
    with pytest.raises(websockets.exceptions.WebSocketException):
        async with websockets.connect(url, additional_headers={**ws_headers, "x-mmn-client-verify": "NONE"}) as w:
            await w.recv()

    # Desired governor stored before the agent connects is applied on connect.
    assert (await c.put(f"/api/nodes/{node_id}/governor", json={"governor": "performance"})).json()["queued"]

    async with websockets.connect(url, additional_headers=ws_headers) as ws:
        agent = FakeAgent(ws)
        runner = asyncio.create_task(agent.run())
        await agent.send({"type": "hello", "data": HELLO})
        await agent.send({"type": "metrics", "data": {"cpu_percent": 12.5, "mem_total": 1000, "mem_used": 500,
                                                       "guests": {"vms_running": 2, "vms_total": 3,
                                                                  "cts_running": 4, "cts_total": 5}}})
        await agent.send({"type": "updates", "data": {"checked_at": "2026-11-22T00:00:00Z", "reboot_required": False,
                                                       "packages": [{"name": "nginx", "current": "1.26.3-2",
                                                                     "new": "1.26.3-3"}]}})

        async def online():
            n = (await c.get(f"/api/nodes/{node_id}")).json()
            return n["online"] and n["updates"]["count"] == 1 and n["metrics"].get("cpu_percent") == 12.5
        assert await _wait(online)
        # reconcile: governor applied, agent update started (0.1.9 -> 0.2.0, auto-update on).
        assert await _wait(lambda: asyncio.sleep(0, any(m == "governor.set" for m, _ in agent.calls)))
        assert await _wait(lambda: asyncio.sleep(0, any(m == "agent.update" for m, _ in agent.calls)))
        upd = next(p for m, p in agent.calls if m == "agent.update")
        assert upd["format"] == "deb" and upd["url"] == "/agent/download/mmn-agent_0.2.0_amd64.deb"

        ov = (await c.get("/api/proxmox/overview")).json()
        assert ov["totals"]["online"] >= 1 and ov["totals"]["vms_running"] >= 2
        n = (await c.get(f"/api/nodes/{node_id}")).json()
        assert n["cluster_name"] == "home" and n["agent_update_available"]

        # Package updates as a job with streamed output.
        r = await c.post(f"/api/nodes/{node_id}/updates/apply")
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]

        async def job_done():
            return (await c.get(f"/api/jobs/{job_id}")).json()["status"] == "success"
        assert await _wait(job_done)
        job = (await c.get(f"/api/jobs/{job_id}")).json()
        assert "Setting up pve-manager" in job["output"] and job["result"]["reboot_required"] is True

        # Server admin through the agent.
        r = await c.post(f"/api/nodes/{node_id}/ops", json={"action": "exec", "params": {"command": "uptime"}})
        assert r.json()["stdout"] == "ran: uptime\n"
        found = (await c.get("/api/packages/search", params={"pattern": "nginx"})).json()
        row = next(x for x in found if x["node_id"] == node_id)
        assert row["packages"][0]["update_available"] == "1.26.3-3"

        # Through MCP, with a token that may run commands.
        tok = (await c.post("/api/tokens", json={"name": "claude-ops", "scopes": ["read", "logs", "exec"]})).json()["token"]
        await _init(client, tok)
        res = (await _rpc(client, tok, "tools/call", {"name": "list_nodes", "arguments": {}}, 2)).json()["result"]
        assert "pve-test" in json.dumps(res)
        res = (await _rpc(client, tok, "tools/call", {"name": "run_command", "arguments": {
            "node": "pve-test", "command": "systemctl is-active nginx", "reason": "check web server"}}, 3)).json()
        assert "ran: systemctl is-active nginx" in json.dumps(res["result"])
        res = (await _rpc(client, tok, "tools/call", {"name": "install_node_updates", "arguments": {
            "node": "pve-test", "confirm": "pve-test"}}, 4)).json()
        assert res["result"]["isError"] and "node_updates" in json.dumps(res["result"])
        log = (await c.get("/api/settings/audit", params={"source": "mcp"})).json()
        cmd = next(e for e in log if e["action"] == "mcp.run_command")
        assert cmd["detail"]["args"]["command"] == "systemctl is-active nginx" and cmd["target"] == "pve-test"

        # Notifications are pushed to the node when saved.
        await c.put("/api/settings/mail", json={"enabled": True, "host": "smtp.example.com", "port": 587,
                                                "security": "starttls", "from_address": "pve@example.com"})
        r = await c.put(f"/api/environments/{home['id']}/notifications",
                        json={"enabled": True, "mailto": ["ops@example.com"], "types": ["vzdump"],
                              "severities": ["error"]})
        assert r.status_code == 200, r.text
        assert r.json()["push"][0]["ok"] is True
        payload = next(p for m, p in agent.calls if m == "notifications.apply")
        assert payload["server"] == "smtp.example.com" and payload["mailto"] == ["ops@example.com"]

        # Removing the node unenrolls the agent.
        assert (await c.delete(f"/api/nodes/{node_id}")).status_code == 204
        assert any(m == "unenroll" for m, _ in agent.calls)
        runner.cancel()

    assert (await c.get(f"/api/nodes/{node_id}")).status_code == 404


def test_nginx_agent_endpoint():
    from mmn.nginx import ProxyIn, render_nginx

    cfg = ProxyIn(mode="proxy", trusted_proxies=["10.0.0.2"])
    plain = render_nginx(cfg)
    assert "agent" not in plain.split("upstream")[1].split("server {")[0]
    out = render_nginx(cfg, agent_tls="/var/lib/mcp-management-node/agent-tls", agent_secret="s" * 40)
    assert "listen 8443 ssl;" in out and "ssl_verify_client optional;" in out
    assert 'X-MMN-Agent-Secret "' + "s" * 40 in out
    with pytest.raises(ValueError):
        render_nginx(ProxyIn(mode="proxy", trusted_proxies=["10.0.0.2"], agent_port=80))


def test_agent_endpoint_behind_proxy(monkeypatch):
    """Behind Zoraxy the public name points at the router/gateway, so agents get the LAN IP instead."""
    from mmn.services import agent_packages as ap
    from mmn.services import nodes

    general = {"base_url": "https://proxmox.home.example.nl"}
    proxy = {"mode": "proxy", "trusted_proxies": ["10.10.4.2"]}
    seen = {}

    def fake_lan_ip(towards):
        seen["towards"] = towards
        return "10.10.4.140"

    monkeypatch.setattr(ap, "lan_ip", fake_lan_ip)
    assert ap.agent_endpoint_info({}, general, "proxmox.home.example.nl", proxy) == ("10.10.4.140:8443", "lan_ip")
    assert seen["towards"] == ["10.10.4.2"]
    # An explicit agent address always wins.
    assert ap.agent_endpoint_info({"address": "agents.example.nl", "port": 9443}, general, None, proxy) == \
        ("agents.example.nl:9443", "address")
    # Direct mode keeps using the host of the public address.
    assert ap.agent_endpoint_info({}, general, None, {"mode": "direct"}) == ("proxmox.home.example.nl:8443", "base_url")
    # Without a LAN address it falls back to the public name, then to the request host.
    monkeypatch.setattr(ap, "lan_ip", lambda towards: None)
    assert ap.agent_endpoint_info({}, general, None, proxy)[1] == "base_url"
    assert ap.agent_endpoint_info({}, {}, "10.0.0.5:443", proxy) == ("10.0.0.5:8443", "request")
    assert ap.agent_endpoint_info({}, {}, "[fd00::5]:443", None) == ("[fd00::5]:8443", "request")

    # After enrolling, an agent keeps the address it enrolled through, unless one is configured.
    assert nodes._enrolled_server({}, general, "10.10.4.140:8443", proxy) == "10.10.4.140:8443"
    assert nodes._enrolled_server({"address": "a.example.nl"}, general, "10.10.4.140:8443", proxy) == "a.example.nl:8443"


def test_lan_ip_is_a_real_address():
    from mmn.services.agent_packages import lan_ip

    ip = lan_ip(["127.0.0.1/32", "not-an-ip"])  # loopback route is skipped, garbage ignored
    assert ip is None or not ip.startswith("127.")
