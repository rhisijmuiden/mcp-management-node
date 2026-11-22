"""VMs and containers, template library, community scripts, scheduled tasks and the console, with a
fake Proxmox VE agent on a real WebSocket."""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import quote

import httpx
from test_agents import _csr, _dist, _secret, _wait, live_server  # noqa: F401
from test_mcp import _init, _rpc

GUESTS = [
    {"type": "lxc", "vmid": 101, "name": "dns", "node": "pve-a", "status": "running", "maxmem": 536870912,
     "tags": "community-script;adguard"},
    {"type": "lxc", "vmid": 102, "name": "web", "node": "pve-a", "status": "running", "tags": "prod"},
    {"type": "qemu", "vmid": 200, "name": "win11", "node": "pve-a", "status": "running"},
    {"type": "lxc", "vmid": 103, "name": "old", "node": "pve-a", "status": "stopped"},
]

HELLO = {"protocol": 1, "agent_version": "0.3.0", "os": "linux", "arch": "amd64", "hostname": "pve-a",
         "install": "deb",
         "capabilities": ["metrics", "apt", "pve", "pve-notifications", "exec", "guests", "console", "pve-api",
                          "terminal", "scripts"],
         "info": {"pve_version": "9.1.1", "cluster": {"name": "lab", "quorate": True,
                                                       "nodes": [{"name": "pve-a", "online": True, "local": True},
                                                                 {"name": "pve-b", "online": True}]}}}


class FakePve:
    def __init__(self, ws, port: int, headers: dict) -> None:
        self.ws, self.port, self.headers = ws, port, headers
        self.calls: list[tuple[str, dict]] = []
        self.snapshots: dict[int, list[dict]] = {101: [], 102: []}
        self.files: list[str] = []
        self.fail_scripts_for: set[int] = set()

    def exec(self, p: dict) -> dict:
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def script_lines(self, p: dict) -> list[str]:
        return ["Reading package lists...", "done"]

    async def send(self, msg: dict) -> None:
        await self.ws.send(json.dumps(msg))

    async def guests(self) -> dict:
        data = {"node": "pve-a", "at": "2026-11-22T00:00:00Z", "guests": GUESTS}
        await self.send({"type": "guests", "data": data})
        return data

    def api(self, method: str, path: str, args: dict):
        if path == "/cluster/nextid":
            return "120"
        if path.endswith("/snapshot") and method == "get":
            vmid = int(path.split("/")[4])
            return [*self.snapshots.get(vmid, []), {"name": "current"}]
        if path.endswith("/config") and method == "get":
            return {"hostname": "dns", "cores": 1, "memory": 512, "net0": "name=eth0,bridge=vmbr0,ip=dhcp"}
        if path.endswith("/config") and method == "set":
            return None
        if path.endswith("/interfaces"):
            return [{"name": "eth0", "inet": "10.0.0.11/24"}]
        if path == "/nodes/localhost/storage":
            if args.get("content") == "vztmpl":
                return [{"storage": "local", "content": "vztmpl,iso", "active": 1, "shared": 0}]
            return [{"storage": "local", "type": "dir", "content": "vztmpl,iso,backup", "active": 1, "avail": 10**10},
                    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir", "active": 1}]
        if path.startswith("/nodes/localhost/storage/local/content"):
            if args.get("content") == "vztmpl":
                return [{"volid": f"local:vztmpl/{f}", "size": 100} for f in self.files]
            return [{"volid": "local:iso/debian-13.iso", "size": 700}]
        if path == "/nodes/localhost/network":
            return [{"iface": "vmbr0", "type": "bridge"}, {"iface": "eno1", "type": "eth"}]
        if path == "/cluster/sdn/vnets":
            return []
        if path == "/nodes/localhost/aplinfo":
            return [{"template": "debian-13-standard_13.1-2_amd64.tar.zst", "package": "debian-13-standard",
                     "version": "13.1-2", "os": "debian-13", "section": "system", "headline": "Debian 13"}]
        raise ValueError(f"unexpected api {method} {path}")

    async def job(self, job_id: str, lines: list[str], ok: bool = True, result: dict | None = None,
                  error: str = "") -> None:
        await asyncio.sleep(0.02)
        for line in lines:
            await self.send({"type": "job", "job_id": job_id, "event": "line", "line": line})
        await self.send({"type": "job", "job_id": job_id, "event": "done", "status": "success" if ok else "failed",
                         "result": result or {}, "error": error})

    async def fetch(self, job_id: str, p: dict) -> None:
        url = f"http://127.0.0.1:{self.port}{p['url']}"
        async with httpx.AsyncClient() as c:
            r = await c.get(url, headers=self.headers)
        ok = r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == p["sha256"]
        if ok:
            self.files.append(p["volid"].split("/", 1)[1])
        await self.job(job_id, [f"downloaded {len(r.content)} bytes"], ok, {"volid": p["volid"]},
                       "" if ok else f"HTTP {r.status_code}")

    async def stream(self, sid: str) -> None:
        import websockets

        async with websockets.connect(f"ws://127.0.0.1:{self.port}/agent/stream/{sid}",
                                      additional_headers=self.headers) as s:
            async for msg in s:
                if isinstance(msg, bytes):
                    await s.send(b"echo:" + msg)
                    if msg == b"exit\r":
                        await s.send(json.dumps({"type": "exit", "code": 0}))
                        return

    async def run(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") != "req":
                continue
            method, p = m["method"], m.get("params") or {}
            self.calls.append((method, p))
            res, err = {"started": True}, None
            try:
                if method == "guests.list":
                    res = await self.guests()
                elif method == "pve.api":
                    res = self.api(p["method"], p["path"], p.get("args") or {})
                elif method == "pve.task":
                    path, args = p["path"], p.get("args") or {}
                    if path.endswith("/snapshot") and p["method"] == "create":
                        self.snapshots.setdefault(int(path.split("/")[4]), []).append(
                            {"name": args["snapname"], "snaptime": len(self.calls)})
                    if "/snapshot/" in path and p["method"] == "delete":
                        vmid, name = int(path.split("/")[4]), path.rsplit("/", 1)[1]
                        self.snapshots[vmid] = [s for s in self.snapshots[vmid] if s["name"] != name]
                    if path == "/nodes/localhost/aplinfo":
                        self.files.append(args["template"])
                    asyncio.ensure_future(self.job(p["job_id"], ["task UPID:pve-a:1:2:3:x:1:root@pam:", "TASK OK"]))
                elif method == "script.run":
                    fail = any(f"pct exec {v} " in p["script"] for v in self.fail_scripts_for)
                    asyncio.ensure_future(self.job(p["job_id"], self.script_lines(p), not fail,
                                                   {"exit_code": 1 if fail else 0}, "exit code 1" if fail else ""))
                elif method == "file.fetch":
                    asyncio.ensure_future(self.fetch(p["job_id"], p))
                elif method == "stream.open":
                    res = {"password": "vncpass1"} if p["kind"] == "vnc" else {}
                    asyncio.get_running_loop().call_later(0.05, lambda s=p["session"]: asyncio.ensure_future(self.stream(s)))
                elif method == "exec":
                    res = self.exec(p)
                else:
                    err = f"unknown method {method}"
            except ValueError as e:
                err = str(e)
            await self.send({"type": "res", "id": m["id"], "ok": err is None, "result": res, "error": err or ""})


async def _enroll(c, client) -> dict:
    _dist()
    home = next(e for e in (await c.get("/api/environments")).json() if e["name"] == "Home")
    code = (await c.post("/api/pairing", json={"environment_id": home["id"], "label": "pve-a"})).json()["code"]
    _, csr = _csr()
    r = await client.post("/agent/enroll", json={"code": code, "csr": csr, "hostname": "pve-a", "agent_version": "0.3.0"},
                          headers={"x-mmn-agent-secret": _secret()})
    assert r.status_code == 200, r.text
    return r.json()


async def _job_ok(c, job_id: str) -> dict:
    async def done():
        return (await c.get(f"/api/jobs/{job_id}")).json()["status"] != "running"
    assert await _wait(done, 10)
    return (await c.get(f"/api/jobs/{job_id}")).json()


async def test_guests_templates_tasks_console(admin_client, client, live_server):  # noqa: F811
    import websockets

    c = admin_client
    enrolled = await _enroll(c, client)
    node_id = enrolled["node_id"]
    headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS",
               "x-mmn-client-cert": quote(enrolled["certificate"])}
    async with websockets.connect(f"ws://127.0.0.1:{live_server}/agent/ws", additional_headers=headers) as ws:
        agent = FakePve(ws, live_server, headers)
        runner = asyncio.create_task(agent.run())
        await agent.send({"type": "hello", "data": HELLO})
        await agent.send({"type": "metrics", "data": {"cpu_percent": 10, "mem_total": 100, "mem_used": 20}})
        await agent.guests()

        async def listed():
            return len((await c.get("/api/guests", params={"node_id": node_id})).json()) == 4
        assert await _wait(listed)
        gl = (await c.get("/api/guests", params={"q": "community-script"})).json()
        assert [g["vmid"] for g in gl] == [101] and gl[0]["community_script"]
        assert (await c.get("/api/guests/suggest-node")).json()["node_id"] == node_id

        # Details and power.
        d = (await c.get(f"/api/guests/{node_id}/101")).json()
        assert d["config"]["hostname"] == "dns" and d["interfaces"][0]["inet"] == "10.0.0.11/24"
        r = await c.post(f"/api/guests/{node_id}/101/power", json={"action": "reboot"})
        assert r.status_code == 202, r.text
        assert (await _job_ok(c, r.json()["id"]))["status"] == "success"
        call = next(p for m, p in agent.calls if m == "pve.task")
        assert call["path"] == "/nodes/localhost/lxc/101/status/reboot"

        # Snapshots.
        r = await c.post(f"/api/guests/{node_id}/101/snapshots", json={"name": "before-upgrade"})
        assert (await _job_ok(c, r.json()["id"]))["status"] == "success"
        snaps = (await c.get(f"/api/guests/{node_id}/101/snapshots")).json()
        assert [s["name"] for s in snaps] == ["before-upgrade"]
        r = await c.post(f"/api/guests/{node_id}/101/snapshots/before-upgrade/rollback", json={"confirm": "x"})
        assert r.status_code == 400
        assert (await c.post(f"/api/guests/{node_id}/101/snapshots", json={"name": "1bad"})).status_code == 400

        # Migration only inside the cluster.
        assert (await c.post(f"/api/guests/{node_id}/101/migrate", json={"target": "elsewhere"})).status_code == 400
        r = await c.post(f"/api/guests/{node_id}/200/migrate", json={"target": "pve-b"})
        assert r.status_code == 202, r.text
        await _job_ok(c, r.json()["id"])
        mig = [p for m, p in agent.calls if m == "pve.task" and p["path"].endswith("/migrate")][-1]
        assert mig["args"] == {"target": "pve-b", "online": True, "with-local-disks": True}

        # Resources on the node for the create dialog.
        res = (await c.get(f"/api/nodes/{node_id}/resources")).json()
        assert res["nextid"] == 120 and "vmbr0" in res["bridges"] and res["isos"][0]["volid"] == "local:iso/debian-13.iso"

        # Custom template: upload, missing on the node, pushed automatically when creating a container.
        blob = b"\x28\xb5\x2f\xfd" + b"x" * 4096
        r = await c.put("/api/templates/upload", params={"filename": "rhis-base_1.0.tar.zst", "name": "RHIS base"},
                        content=blob)
        assert r.status_code == 201, r.text
        tpl = r.json()
        assert tpl["sha256"] == hashlib.sha256(blob).hexdigest()
        await c.post("/api/templates/scan")
        lib = (await c.get("/api/templates")).json()
        loc = next(t for t in lib["templates"] if t["id"] == tpl["id"])["locations"][0]
        assert loc["status"] == "missing"
        r = await c.post("/api/guests/create/ct", json={
            "node_id": node_id, "name": "app1", "template_id": tpl["id"], "password": "secret123",
            "net": {"bridge": "vmbr0", "ip": "10.0.0.50/24", "gateway": "10.0.0.1", "vlan": 20}})
        assert r.status_code == 202, r.text
        job = await _job_ok(c, r.json()["id"])
        assert job["status"] == "success", job
        assert "rhis-base_1.0.tar.zst" in agent.files
        create = next(p for m, p in agent.calls if m == "pve.task" and p["path"] == "/nodes/localhost/lxc")
        assert create["args"]["ostemplate"] == "local:vztmpl/rhis-base_1.0.tar.zst"
        assert create["args"]["net0"] == "name=eth0,bridge=vmbr0,ip=10.0.0.50/24,gw=10.0.0.1,tag=20"
        assert create["args"]["vmid"] == 120 and create["args"]["rootfs"] == "local-lvm:8"
        jobs = (await c.get(f"/api/nodes/{node_id}/jobs")).json()
        assert "secret123" not in json.dumps(jobs)
        lib = (await c.get("/api/templates")).json()
        assert next(t for t in lib["templates"] if t["id"] == tpl["id"])["locations"][0]["status"] == "current"
        # A new version makes the node outdated.
        r = await c.put("/api/templates/upload", params={"filename": "rhis-base_1.1.tar.zst", "name": "RHIS base",
                                                         "replace_id": tpl["id"]}, content=blob + b"v2")
        assert r.status_code == 201, r.text
        assert r.json()["locations"][0]["status"] == "outdated"

        # VM from a cloud image: one script on the node, secrets passed as environment.
        r = await c.post("/api/guests/create/vm", json={
            "node_id": node_id, "name": "deb13", "source": "cloud", "cloud_image": "debian-13",
            "ci_password": "pw-123456", "disk_gb": 20})
        assert r.status_code == 202, r.text
        assert (await _job_ok(c, r.json()["id"]))["status"] == "success"
        sc = [p for m, p in agent.calls if m == "script.run"][-1]
        assert "debian-13-genericcloud-amd64.qcow2" in sc["script"] and "pw-123456" not in sc["script"]
        assert sc["env"]["MMN_CI_PASSWORD"] == "pw-123456" and "qm resize 120 scsi0 20G" in sc["script"]

        # Scheduled task: apt upgrade in all containers with snapshot + rollback; 102 fails.
        agent.fail_scripts_for = {102}
        body = {"name": "Weekly CT updates", "kind": "ct_apt_upgrade", "cron": "0 4 * * 0",
                "targets": {"scope": "all"}, "options": {"snapshot": True, "rollback": True, "keep_snapshots": 1}}
        prev = (await c.post("/api/tasks/preview", json=body)).json()
        assert sorted(t["vmid"] for t in prev) == [101, 102, 103]
        r = await c.post("/api/tasks", json=body)
        assert r.status_code == 201, r.text
        task_id = r.json()["id"]
        assert (await c.post("/api/tasks", json={**body, "cron": "every day"})).status_code == 422
        run_id = (await c.post(f"/api/tasks/{task_id}/run")).json()["run_id"]

        async def run_done():
            return (await c.get(f"/api/task-runs/{run_id}")).json()["status"] != "running"
        assert await _wait(run_done, 15)
        run = (await c.get(f"/api/task-runs/{run_id}")).json()
        by = {r["vmid"]: r for r in run["results"]}
        assert run["status"] == "partial"
        assert by[101]["status"] == "success" and by[101]["snapshot"].startswith("mmn-apt-")
        assert by[102]["status"] == "failed" and by[102]["rolled_back"] is True
        assert by[103]["status"] == "skipped"
        assert any(p["path"].endswith("/rollback") and "/102/" in p["path"] for m, p in agent.calls if m == "pve.task")
        # keep_snapshots=1: the manual snapshot stays, only one mmn-apt snapshot.
        assert sum(1 for s in agent.snapshots[101] if s["name"].startswith("mmn-apt-")) == 1
        assert next(t for t in (await c.get("/api/tasks")).json()["tasks"] if t["id"] == task_id)["last_status"] == "partial"

        # Console: VNC session for the VM, piped through the app.
        r = await c.post(f"/api/guests/{node_id}/200/console", json={})
        assert r.status_code == 200, r.text
        sess = r.json()
        assert sess["kind"] == "vnc" and sess["password"] == "vncpass1"
        cookie = f"mmn_session={client.cookies.get('mmn_session')}"
        async with websockets.connect(f"ws://127.0.0.1:{live_server}/api/console/ws/{sess['session']}",
                                      additional_headers={"cookie": cookie}) as b:
            await b.send(b"RFB 003.008\n")
            assert await asyncio.wait_for(b.recv(), 5) == b"echo:RFB 003.008\n"
        # A session is used once.
        try:
            async with websockets.connect(f"ws://127.0.0.1:{live_server}/api/console/ws/{sess['session']}",
                                          additional_headers={"cookie": cookie}) as b:
                await asyncio.wait_for(b.recv(), 2)
            raise AssertionError("second use accepted")
        except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed):
            pass

        # MCP: read with 'read', power needs 'guests'.
        tok = (await c.post("/api/tokens", json={"name": "claude-guests", "scopes": ["read"]})).json()["token"]
        await _init(client, tok)
        res = (await _rpc(client, tok, "tools/call", {"name": "list_guests", "arguments": {"query": "dns"}}, 2)).json()
        assert "adguard" in json.dumps(res["result"])
        res = (await _rpc(client, tok, "tools/call", {"name": "guest_power", "arguments": {
            "guest": "dns", "action": "stop"}}, 3)).json()
        assert res["result"]["isError"] and "guests" in json.dumps(res["result"])
        tok2 = (await c.post("/api/tokens", json={"name": "claude-ops2", "scopes": ["read", "guests"]})).json()["token"]
        await _init(client, tok2)
        res = (await _rpc(client, tok2, "tools/call", {"name": "guest_power", "arguments": {
            "guest": "dns", "action": "shutdown"}}, 4)).json()
        assert not res["result"].get("isError"), res
        log = (await c.get("/api/settings/audit", params={"source": "mcp"})).json()
        assert any(e["action"] == "mcp.guest_power" and e["target"] == "dns" for e in log)

        assert (await c.delete(f"/api/nodes/{node_id}")).status_code == 204
        runner.cancel()


def test_parsers():
    from mmn.models import CtTemplate, Node
    from mmn.services import community, templates

    text = '''#!/usr/bin/env bash
# Source: https://adguard.com/
APP="AdGuard Home"
var_tags="${var_tags:-adblock;dns}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-2}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"
'''
    p = community.parse_script(text, "adguard")
    assert p["name"] == "AdGuard Home" and p["source_url"] == "https://adguard.com/"
    assert p["defaults"] == {"cpu": 1, "ram": 512, "disk": 2, "os": "debian", "version": "13", "unprivileged": True}
    assert p["tags"] == ["adblock", "dns"]
    env = community.install_env({"cpu": 2, "hostname": "adg", "ip": "10.0.0.5/24", "vlan": 10, "tags": ["a", "b"]}, "ct")
    assert env == {"var_cpu": "2", "var_hostname": "adg", "var_vlan": "10", "var_tags": "a;b", "var_net": "10.0.0.5/24"}

    assert templates.parse_official("debian-13-standard_13.1-2_amd64.tar.zst") == ("debian-13-standard", "13.1-2")
    t = CtTemplate(kind="official", family="debian-13-standard", filename="debian-13-standard_13.1-2_amd64.tar.zst",
                   previous_filenames=[])
    n = Node(storage_scan={"checked_at": "x", "storages": {"local": {"files": [
        {"volid": "local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst",
         "filename": "debian-13-standard_13.0-1_amd64.tar.zst"}]}}})
    assert templates.status_on_node(t, n)["status"] == "outdated"
    assert templates.status_on_node(t, Node(storage_scan={}))["status"] == "unknown"


def test_nginx_has_stream_and_upload():
    from mmn.nginx import ProxyIn, render_nginx

    out = render_nginx(ProxyIn(mode="proxy", trusted_proxies=["10.0.0.2"], base_path="/"), agent_tls="/var/lib/x",
                       agent_secret="s" * 40)
    assert "location ^~ /agent/stream/" in out and "location ^~ /agent/templates/" in out
    assert "location = /api/templates/upload" in out and "client_max_body_size 8g;" in out
