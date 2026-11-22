"""Phase 5: standalone Linux and Windows servers, troubleshooting across servers and data copies."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import httpx
import pytest
from test_agents import _csr, _dist, _secret, live_server  # noqa: F401
from test_mcp import _init, _rpc

from mmn.services.servers import compare_versions


@pytest.mark.parametrize(("a", "b", "want"), [
    ("1.26.3-2", "1.26.3-3", -1), ("1.26.3-3", "1.26.3-3", 0), ("1:1.0-1", "2.0-1", 1), ("1.2~rc1", "1.2", -1),
    ("3.0.15-1~deb12u1", "3.0.17-1~deb12u2", -1), ("1.10", "1.9", 1), ("10.0.26100.2033", "10.0.26100.1742", 1),
    ("2.4.62-1~deb12u2", "2.4.62-1~deb12u1", 1), ("1.0a", "1.0", 1),
])
def test_compare_versions(a, b, want):
    assert compare_versions(a, b) == want


class ServerAgent:
    """A fake Linux or Windows agent that answers the server-admin methods and data copies."""

    def __init__(self, ws, port: int, headers: dict, kind: str) -> None:
        self.ws, self.port, self.headers, self.kind = ws, port, headers, kind
        self.calls: list[tuple[str, dict]] = []
        self.received: bytes | None = None

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def done(self, job_id: str, result: dict, ok: bool = True, error: str = "") -> None:
        await self.send({"type": "job", "job_id": job_id, "event": "done", "status": "success" if ok else "failed",
                         "result": result, "error": error})

    async def copy(self, method: str, p: dict) -> None:
        url = f"http://127.0.0.1:{self.port}{p['url']}"
        async with httpx.AsyncClient() as c:
            if method == "copy.send":
                r = await c.put(url, content=b"TARBALL" * 1000, headers=self.headers)
                await self.send({"type": "job", "job_id": p["job_id"], "event": "line", "line": "uploaded"})
                await self.done(p["job_id"], {"files": 3, "bytes": 7000, "wire_bytes": len(b"TARBALL" * 1000)},
                                ok=r.status_code == 200, error=r.text)
            else:
                r = await c.get(url, headers=self.headers)
                self.received = r.content
                await self.done(p["job_id"], {"files": 3, "bytes": 7000}, ok=r.status_code == 200, error=r.text)

    async def run(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") != "req":
                continue
            method, p = m["method"], m.get("params") or {}
            self.calls.append((method, p))
            res, err = {}, None
            win = self.kind == "windows"
            if method == "services.list":
                failed = [{"name": "Spooler" if win else "postfix.service", "load": "auto", "active": "failed",
                           "sub": "stopped", "description": "Print Spooler"}]
                res = failed if p.get("state") == "failed" else failed + [{"name": "W32Time", "active": "active"}]
            elif method == "logs.journal":
                res = {"exit_code": 0, "stdout": ("2026-11-22T10:00:00+01:00 System Error Service Control Manager[7034]: "
                                                  "The Print Spooler service terminated unexpectedly.\n" if win else
                                                  "Nov 22 10:00 www1 postfix/smtp[12]: connect to mx: Connection timed out\n"
                                                  "Nov 22 10:01 www1 postfix/smtp[13]: status=deferred\n"), "stderr": ""}
            elif method == "logs.events":
                if not win:
                    err = "the Windows Event Log is only available on Windows servers; use the journal"
                else:
                    res = [{"time": "2026-11-22T09:00:00Z", "log": "System", "level": "Error",
                            "provider": "Service Control Manager", "id": 7034, "message": "Spooler stopped"}]
            elif method == "logs.search":
                res = {"matches": [{"file": "/var/log/mail.log", "line": 12, "text": "reject: RCPT"}], "files_scanned": 4}
            elif method == "packages.query":
                res = ([{"name": "Notepad++ (64-bit x64)", "version": "8.6.2", "source": "windows"}] if win else
                       [{"name": "nginx", "version": "1.26.3-2", "source": "dpkg"}])
            elif method == "diagnose":
                res = {"findings": ["disk C:\\ is 91% full"], "failed_services": [], "top_cpu": []}
            elif method in ("copy.send", "copy.receive"):
                res = {"started": True}
                asyncio.get_running_loop().call_later(0.05, lambda mm=method, pp=p: asyncio.ensure_future(self.copy(mm, pp)))
            elif method == "exec":
                res = {"exit_code": 0, "stdout": f"PS> {p['command']}\nOK\n", "stderr": ""}
            else:
                err = f"unknown method {method}"
            await self.send({"type": "res", "id": m["id"], "ok": err is None, "result": res, "error": err or ""})


async def _enroll(c, client, env_id: str, hostname: str, kind: str, os_name: str) -> tuple[str, dict]:
    r = await c.post("/api/pairing", json={"environment_id": env_id, "label": hostname})
    assert r.status_code == 201, r.text
    key, csr = _csr()
    body = {"code": r.json()["code"], "csr": csr, "hostname": hostname, "agent_version": "0.5.0", "kind": kind,
            "os": os_name}
    r = await client.post("/agent/enroll", json=body, headers={"x-mmn-agent-secret": _secret()})
    assert r.status_code == 200, r.text
    e = r.json()
    headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS",
               "x-mmn-client-cert": quote(e["certificate"])}
    return e["node_id"], headers


async def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if await cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_servers_section(admin_client, client, live_server):  # noqa: F811
    import websockets

    _dist()
    c = admin_client
    home = next(e for e in (await c.get("/api/environments")).json() if e["name"] == "Home")
    r = await c.post("/api/pairing", json={"environment_id": home["id"]})
    pr = r.json()
    assert "install.ps1" in pr["windows_command"] and pr["code"] in pr["windows_command"]
    assert pr["msiexec"].startswith("msiexec /i ") and f"FINGERPRINT={pr['connect']['fingerprint']}" in pr["msiexec"]

    # Install scripts: Linux (deb or plain binary) and Windows (MSI with checksum).
    sh = (await client.get("/api/agent/install.sh")).text
    assert "mmn-agent-linux-amd64" in sh and "/usr/local/bin/mmn-agent" in sh and "sha256sum -c" in sh
    ps1 = (await client.get("/api/agent/install.ps1")).text
    assert pr["connect"]["fingerprint"] in ps1 and "Get-FileHash" in ps1 and "msiexec.exe" in ps1

    lin_id, lin_h = await _enroll(c, client, home["id"], "www1.example.lan", "linux", "linux")
    win_id, win_h = await _enroll(c, client, home["id"], "dc01", "windows", "windows")
    url = f"ws://127.0.0.1:{live_server}/agent/ws"
    async with websockets.connect(url, additional_headers=lin_h) as ws1, \
            websockets.connect(url, additional_headers=win_h) as ws2:
        lin, win = ServerAgent(ws1, live_server, lin_h, "linux"), ServerAgent(ws2, live_server, win_h, "windows")
        tasks = [asyncio.create_task(lin.run()), asyncio.create_task(win.run())]
        caps = ["metrics", "logs", "services", "packages", "exec", "copy", "diagnose", "logsearch", "updates"]
        await lin.send({"type": "hello", "data": {"protocol": 1, "agent_version": "0.5.0", "os": "linux", "arch": "amd64",
                                                  "hostname": "www1", "install": "binary", "capabilities": caps,
                                                  "info": {"os_pretty_name": "Rocky Linux 10.0", "update_manager": "dnf"}}})
        await win.send({"type": "hello", "data": {"protocol": 1, "agent_version": "0.5.0", "os": "windows", "arch": "amd64",
                                                  "hostname": "DC01", "install": "windows",
                                                  "capabilities": caps + ["eventlog", "powershell"],
                                                  "info": {"os_pretty_name": "Windows Server 2025 Standard 24H2"}}})
        await win.send({"type": "metrics", "data": {"cpu_percent": 9, "mem_total": 100, "mem_used": 50, "failed_services": 1,
                                                    "disks": [{"path": "C:\\", "total": 100, "used": 95}]}})

        async def both_online():
            o = (await c.get("/api/servers/overview")).json()
            return sum(1 for s in o["servers"] if s["online"]) == 2 and any(s["failed_services"] for s in o["servers"])

        assert await _wait(both_online)
        o = (await c.get("/api/servers/overview")).json()
        by = {s["name"]: s for s in o["servers"]}
        assert by["www1"]["kind"] == "linux" and by["dc01"]["kind"] == "windows"
        assert set(by["dc01"]["attention"]) == {"failed_services", "disk_full"}
        assert o["totals"]["windows"] == 1 and o["totals"]["linux"] == 1 and o["totals"]["attention"] == 1
        # The Proxmox pages do not show the standalone servers.
        px = (await c.get("/api/proxmox/overview")).json()
        assert not {"www1", "dc01"} & {n["name"] for n in px["nodes"]}
        assert {n["name"] for n in (await c.get("/api/nodes?kind=servers")).json()} >= {"www1", "dc01"}
        assert (await c.get("/api/servers/overview?os=windows")).json()["totals"]["servers"] == 1

        # Windows Event Log through the generic ops endpoint.
        r = await c.post(f"/api/nodes/{win_id}/ops", json={"action": "logs.events", "params": {"log": "System"}})
        assert r.status_code == 200 and r.json()[0]["id"] == 7034

        # Search logs everywhere: journal on Linux, Event Log on Windows.
        r = await c.post("/api/servers/search-logs", json={"pattern": "spooler|timed out", "servers": ["www1", "dc01"]})
        rows = {x["server"]: x for x in r.json()}
        assert rows["www1"]["result"]["source"] == "journal" and rows["www1"]["result"]["count"] == 2
        assert rows["dc01"]["result"]["source"] == "eventlog" and rows["dc01"]["result"]["events"][0]["provider"]
        r = await c.post("/api/servers/search-logs", json={"pattern": "reject", "source": "files", "servers": ["www1"],
                                                           "paths": ["/var/log/mail.log"], "since": "2 days ago"})
        assert r.json()[0]["result"]["matches"][0]["line"] == 12
        assert ("logs.search", {"pattern": "reject", "paths": ["/var/log/mail.log"], "max_matches": 100,
                                "modified_within_hours": 48, "include_gz": False}) in lin.calls

        # Find errors: most broken first.
        r = await c.post("/api/servers/errors", json={"servers": ["www1", "dc01"]})
        errs = r.json()
        assert errs[0]["result"]["failed_services"] and "disk_full" in [x for e in errs for x in e["result"]["findings"]]

        # CVE check across servers.
        r = await c.post("/api/servers/cve-check", json={"package": "nginx", "fixed_version": "1.26.3-3",
                                                         "servers": ["www1"]})
        assert r.json()[0]["result"]["installed"][0]["status"] == "vulnerable"

        r = await c.get(f"/api/servers/{win_id}/diagnose")
        assert r.json()["findings"]

        # Data copy from Linux to Windows, relayed through the app.
        bad = await c.post("/api/transfers", json={"source": "www1", "paths": ["relative/path"], "target": "dc01",
                                                    "target_path": "D:\\Restore"})
        assert bad.status_code == 400
        bad = await c.post("/api/transfers", json={"source": "www1", "paths": ["/var/www"], "target": "dc01",
                                                    "target_path": "/srv"})
        assert bad.status_code == 400 and "Windows path" in bad.json()["detail"]
        r = await c.post("/api/transfers", json={"source": "www1", "paths": ["/var/www"], "target": "dc01",
                                                  "target_path": "D:\\Restore", "reason": "test"})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]

        async def finished():
            return (await c.get(f"/api/transfers/{tid}")).json()["status"] != "running"

        assert await _wait(finished, 10)
        t = (await c.get(f"/api/transfers/{tid}")).json()
        assert t["status"] == "success", t
        assert t["stage"] == "done" and t["files"] == 3 and t["send_job_id"] and t["receive_job_id"]
        assert win.received == b"TARBALL" * 1000
        # The spool file is gone and the endpoints refuse the finished transfer.
        async with httpx.AsyncClient() as h:
            assert (await h.get(f"http://127.0.0.1:{live_server}/agent/transfer/{tid}", headers=win_h)).status_code == 403
        assert any(x["id"] == tid for x in (await c.get(f"/api/transfers?node_id={win_id}")).json())

        # MCP: the troubleshooting tools, with the client identity in the audit log.
        tok = (await c.post("/api/tokens", json={"name": "claude-servers",
                                                 "scopes": ["read", "logs", "exec", "copy", "services"]})).json()["token"]
        await _init(client, tok)
        names = {t["name"] for t in (await _rpc(client, tok, "tools/list", id_=2)).json()["result"]["tools"]}
        assert {"list_servers", "diagnose_server", "search_logs", "read_event_log", "find_errors", "check_cve",
                "copy_data", "get_transfer"} <= names

        async def call(name, args, id_=3):
            r = await _rpc(client, tok, "tools/call", {"name": name, "arguments": args}, id_=id_)
            res = r.json()["result"]
            if res.get("isError"):
                return res, res["content"][0]["text"]
            data = res.get("structuredContent") or (json.loads(res["content"][0]["text"]) if res["content"] else None)
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            return res, data

        res, data = await call("list_servers", {"os": "windows"})
        assert data["servers"][0]["name"] == "dc01"
        res, data = await call("read_event_log", {"server": "dc01", "level": "error"})
        assert data[0]["provider"] == "Service Control Manager"
        assert ("logs.events", {"log": "System,Application", "priority": "error", "since": "24 hours ago", "unit": "",
                                "grep": "", "lines": 100}) in win.calls
        res, data = await call("check_cve", {"package": "nginx", "fixed_version": "1.26.3-3"})
        assert data[0]["server"] == "www1" and data[0]["result"]["vulnerable"] == 1
        res, data = await call("run_command", {"node": "dc01", "command": "Get-Service Spooler", "reason": "check spooler"})
        assert data["exit_code"] == 0
        res, _ = await call("copy_data", {"source": "www1", "paths": ["/etc/nginx"], "target": "dc01",
                                          "target_path": "C:\\Backup", "reason": ""})
        assert res["isError"] and "reason" in _

        audit = (await c.get("/api/settings/audit?limit=200")).json()
        items = audit["items"] if isinstance(audit, dict) else audit
        cmd = next(a for a in items if a["action"] == "mcp.run_command")
        assert cmd["detail"]["args"]["command"] == "Get-Service Spooler" and "OK" in cmd["detail"]["result"]["output"]
        assert cmd["detail"]["token"] == "claude-servers" and cmd["target"] == "dc01"
        for t_ in tasks:
            t_.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
