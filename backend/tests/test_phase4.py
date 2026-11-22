"""Phase 4: network map and safe apply, firewall, SDN designer, HA/replication/backup jobs/storage,
monitoring with alerts, and GitHub deployments, against a fake Proxmox VE agent."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from test_agents import _secret, _wait, live_server  # noqa: F401
from test_guests import HELLO, FakePve, _enroll, _job_ok
from test_mcp import _init, _rpc

INTERFACES = """auto lo
iface lo inet loopback

auto vmbr0
iface vmbr0 inet static
\taddress 10.0.0.2/24
\tgateway 10.0.0.1
\tbridge-ports eno1
\tbridge-vlan-aware yes
"""

NIC_DUMP = "\n".join([
    "/etc/pve/local/lxc/101.conf\thostname: dns",
    "/etc/pve/local/lxc/101.conf\tnet0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:01,ip=dhcp,tag=20,type=veth,firewall=1",
    "/etc/pve/local/lxc/102.conf\thostname: web",
    "/etc/pve/local/lxc/102.conf\tnet0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:00:00:02,ip=dhcp,type=veth",
    "/etc/pve/local/qemu-server/200.conf\tname: win11",
    "/etc/pve/local/qemu-server/200.conf\tnet0: virtio=BC:24:11:00:00:03,bridge=vmbr1",
    "/etc/pve/local/qemu-server/200.conf\tnet1: virtio=BC:24:11:00:00:04,bridge=lab,firewall=0",
])


class FakePve4(FakePve):
    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.ifaces = [
            {"iface": "vmbr0", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "eno1",
             "bridge_vlan_aware": 1, "cidr": "10.0.0.2/24", "gateway": "10.0.0.1"},
            {"iface": "vmbr1", "type": "bridge", "active": 1, "autostart": 1, "bridge_ports": "eno2"},
            {"iface": "eno1", "type": "eth", "active": 1}, {"iface": "eno2", "type": "eth", "active": 1},
            {"iface": "eno3", "type": "eth", "active": 0},
        ]
        self.pending = False
        self.zones: dict[str, dict] = {}
        self.vnets: dict[str, dict] = {"lab": {"vnet": "lab", "zone": "old", "tag": 99}}
        self.subnets: dict[str, list[dict]] = {}
        self.sdn_files = {"/etc/pve/sdn/vnets.cfg": "vnet: lab\n\tzone old\n\ttag 99\n"}
        self.ha_resources: list[dict] = []
        self.replication: list[dict] = []
        self.backup_jobs: list[dict] = [{"id": "backup-1", "schedule": "sun 01:00", "storage": "local", "all": 1,
                                         "mode": "snapshot", "enabled": 1}]
        self.fw_rules: list[dict] = []
        self.deploy_fail = False

    def exec(self, p: dict) -> dict:
        cmd = p["command"]
        if "qemu-server/*.conf" in cmd:
            return {"exit_code": 0, "stdout": NIC_DUMP + "\n", "stderr": ""}
        if "diff -u" in cmd:
            diff = "--- current\n+++ pending\n@@ -1 +1,5 @@\n+auto vmbr2\n+iface vmbr2 inet manual\n" \
                if self.pending else ""
            return {"exit_code": 0, "stdout": diff, "stderr": ""}
        if "base64 -w0" in cmd:
            path = cmd.split("base64 -w0 ")[1].split(";")[0].strip()
            content = INTERFACES if path == "/etc/network/interfaces" else self.sdn_files.get(path)
            return {"exit_code": 0, "stdout": base64.b64encode(content.encode()).decode() if content else "MISSING"}
        if "mmn-net-applied" in cmd:
            return {"exit_code": 0, "stdout": "confirmed\n", "stderr": ""}
        if "base64 -d >" in cmd:
            if "/etc/pve/sdn/" in cmd:
                self.vnets = {"lab": {"vnet": "lab", "zone": "old", "tag": 99}}
                self.zones, self.subnets = {}, {}
            if "/etc/network/interfaces.new" in cmd:
                self.pending = True
            return {"exit_code": 0, "stdout": "staged\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def script_lines(self, p: dict) -> list[str]:
        if "mmn-git-token" in p["script"]:
            return ["Cloning", "MMN_PREV=", "MMN_SHA=" + "a" * 40, "Deployed"]
        return ["done"]

    def api(self, method: str, path: str, args: dict):
        if path == "/nodes/localhost/network" and method == "get":
            return self.ifaces
        if path.startswith("/nodes/localhost/network"):
            if method == "delete" and path == "/nodes/localhost/network":
                self.pending = False
            else:
                self.pending = True
            return None
        if path == "/cluster/sdn/zones":
            if method == "create":
                self.zones[args["zone"]] = {**args}
                return None
            return list(self.zones.values())
        if path == "/cluster/sdn/vnets":
            if method == "create":
                self.vnets[args["vnet"]] = {**args}
                return None
            return list(self.vnets.values())
        if path.startswith("/cluster/sdn/vnets/") and path.endswith("/subnets"):
            vnet = path.split("/")[4]
            if method == "create":
                cidr = args["subnet"]
                self.subnets.setdefault(vnet, []).append({**args, "cidr": cidr, "vnet": vnet,
                                                          "subnet": f"z-{cidr.replace('/', '-')}"})
                return None
            return self.subnets.get(vnet, [])
        if path == "/cluster/sdn/controllers":
            return []
        if path == "/cluster/resources":
            return [{"type": "sdn", "sdn": "home", "node": "pve-a", "status": "ok"}]
        if path == "/cluster/ha/status/current":
            return [{"id": "quorum", "type": "quorum", "status": "OK", "quorate": 1}]
        if path == "/cluster/ha/resources":
            if method == "create":
                self.ha_resources.append({"sid": args["sid"], "state": args["state"]})
                return None
            return self.ha_resources
        if path == "/cluster/ha/rules":
            return []
        if path == "/cluster/replication":
            if method == "create":
                self.replication.append({**args, "source": "pve-a", "guest": int(args["id"].split("-")[0])})
                return None
            return self.replication
        if path == "/nodes/pve-a/replication":
            return [{"id": j["id"], "last_sync": 1700000000, "fail_count": 0} for j in self.replication]
        if path == "/cluster/backup":
            return self.backup_jobs
        if path == "/cluster/backup-info/not-backed-up":
            return []
        if path == "/storage":
            return [{"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "path": "/var/lib/vz"},
                    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"}]
        if path == "/nodes/localhost/storage" and not args:
            return [{"storage": "local", "type": "dir", "active": 1, "total": 100 * 2**30, "used": 96 * 2**30,
                     "avail": 4 * 2**30, "content": "iso"},
                    {"storage": "local-lvm", "type": "lvmthin", "active": 1, "total": 500 * 2**30,
                     "used": 100 * 2**30, "avail": 400 * 2**30}]
        if path == "/nodes/localhost/disks/list":
            return [{"devpath": "/dev/sda", "model": "Samsung SSD", "size": 500 * 10**9, "type": "ssd",
                     "health": "PASSED", "wearout": 97},
                    {"devpath": "/dev/sdb", "model": "WD Red", "size": 4 * 10**12, "type": "hdd",
                     "health": "FAILED", "wearout": "N/A"}]
        if path == "/nodes/localhost/disks/smart":
            return {"health": "FAILED", "type": "ata", "attributes": [{"name": "Reallocated_Sector_Ct", "raw": "8"}]}
        if "/firewall/" in path:
            if path.endswith("/rules"):
                if method == "create":
                    self.fw_rules.append({**args, "pos": len(self.fw_rules)})
                    return None
                return self.fw_rules
            if "/rules/" in path and method == "delete":
                pos = int(path.rsplit("/", 1)[1])
                self.fw_rules = [r for r in self.fw_rules if r["pos"] != pos]
                return None
            if path.endswith("/options"):
                return {"enable": 0, "policy_in": "DROP"} if method == "get" else None
            return []
        return super().api(method, path, args)


async def test_phase4(admin_client, client, live_server, monkeypatch):  # noqa: F811
    import websockets

    from mmn.services import deploy, monitoring

    c = admin_client
    enrolled = await _enroll(c, client)
    node_id = enrolled["node_id"]
    headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS",
               "x-mmn-client-cert": quote(enrolled["certificate"])}
    async with websockets.connect(f"ws://127.0.0.1:{live_server}/agent/ws", additional_headers=headers) as ws:
        agent = FakePve4(ws, live_server, headers)
        runner = asyncio.create_task(agent.run())
        await agent.send({"type": "hello", "data": HELLO})
        await agent.send({"type": "metrics", "data": {"cpu_percent": 97, "mem_total": 100, "mem_used": 20,
                                                      "disk_total": 100, "disk_used": 10, "load": [1.5, 1, 1]}})
        await agent.guests()

        async def listed():
            return len((await c.get("/api/guests", params={"node_id": node_id})).json()) == 4
        assert await _wait(listed)

        # ---------------------------------------------------------- bridge map
        m = (await c.get("/api/network/map")).json()
        by = {b["name"]: b for b in m["bridges"]}
        assert by["vmbr0"]["vlan_aware"] and by["vmbr0"]["nodes"][0]["cidr"] == "10.0.0.2/24"
        lanes = {ln["tag"]: [g["vmid"] for g in ln["guests"]] for ln in by["vmbr0"]["lanes"]}
        assert lanes == {0: [102], 20: [101]}
        assert by["vmbr0"]["lanes"][1]["guests"][0]["firewall"] is True
        assert by["vmbr1"]["lanes"][0]["guests"][0]["vmid"] == 200
        assert by["lab"]["kind"] == "vnet" and by["lab"]["lanes"][0]["tag"] == 99

        # ---------------------------------------------------------- bridge: stage, dry run, apply
        r = await c.post(f"/api/network/{node_id}/bridges", json={"iface": "vmbr2", "ports": ["eno3"]})
        assert r.status_code == 201, r.text
        assert r.json()["has_changes"] and "+auto vmbr2" in r.json()["diff"]
        assert (await c.post(f"/api/network/{node_id}/bridges", json={"iface": "vmbr0"})).status_code == 400
        assert (await c.post(f"/api/network/{node_id}/bridges", json={"iface": "vmbr3", "ports": ["nope"]})
                ).status_code == 400
        # A bridge with guests is not deleted without force.
        assert (await c.delete(f"/api/network/{node_id}/bridges/vmbr1")).status_code == 400
        dry = (await c.post(f"/api/network/{node_id}/apply", json={"dry_run": True})).json()
        assert dry["dry_run"] and not dry["applied"] and dry["has_changes"]
        assert (await c.post(f"/api/network/{node_id}/apply", json={"dry_run": False, "confirm": "x"})
                ).status_code == 400
        r = await c.post(f"/api/network/{node_id}/apply", json={"dry_run": False, "confirm": "pve-a"})
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["applied"] and out["backup_id"]
        sc = [p for meth, p in agent.calls if meth == "script.run"][-1]
        assert "pvesh set /nodes/localhost/network" in sc["script"] and "ifreload -a" in sc["script"]
        assert (await _job_ok(c, out["job"]["id"]))["status"] == "success"
        bl = (await c.get("/api/network/backups", params={"kind": "network"})).json()
        assert bl[0]["id"] == out["backup_id"]
        full = (await c.get(f"/api/network/backups/{out['backup_id']}")).json()
        assert full["content"]["/etc/network/interfaces"] == INTERFACES

        # ---------------------------------------------------------- firewall
        fw = (await c.get(f"/api/firewall/{node_id}")).json()
        assert fw["cluster_options"]["policy_in"] == "DROP"
        assert next(g for g in fw["guests"] if g["vmid"] == 101)["nics"][0]["firewall"] is True
        r = await c.post(f"/api/firewall/{node_id}/rules", json={
            "level": "guest", "type": "lxc", "vmid": 101,
            "rule": {"type": "in", "action": "accept", "proto": "tcp", "dport": "443", "comment": "https"}})
        assert r.status_code == 201, r.text
        assert r.json()[0]["action"] == "ACCEPT"
        call = [p for meth, p in agent.calls if meth == "pve.api" and p["method"] == "create"][-1]
        assert call["path"] == "/nodes/localhost/lxc/101/firewall/rules" and call["args"]["dport"] == "443"
        assert (await c.put(f"/api/firewall/{node_id}/options", json={"level": "cluster", "enable": True})
                ).status_code == 400
        r = await c.put(f"/api/firewall/{node_id}/options", json={"level": "cluster", "enable": True, "confirm": "lab"})
        assert r.status_code == 200, r.text

        # ---------------------------------------------------------- SDN designer
        spec = {"zone": {"id": "home", "type": "vlan", "bridge": "vmbr0", "nodes": ["pve-a"]},
                "vnets": [{"id": "iot", "alias": "IoT", "tag": 20}, {"id": "guest", "tag": 60}],
                "subnets": [{"vnet": "iot", "cidr": "10.0.20.0/24", "gateway": "10.0.20.1", "snat": True},
                            {"vnet": "guest", "cidr": "10.0.20.128/25", "gateway": "10.0.20.129"}]}
        p = (await c.post("/api/sdn/plan", json={"node_id": node_id, "spec": spec})).json()
        assert any("overlaps" in e for e in p["errors"]) and not p["can_apply"]
        spec["subnets"][1]["cidr"], spec["subnets"][1]["gateway"] = "10.0.60.0/24", "10.0.60.1"
        spec["vnets"].append({"id": "lab", "tag": 30})
        p = (await c.post("/api/sdn/plan", json={"node_id": node_id, "spec": spec})).json()
        assert any("already exists in zone old" in e for e in p["errors"])
        spec["vnets"].pop()
        p = (await c.post("/api/sdn/plan", json={"node_id": node_id, "spec": spec})).json()
        assert p["errors"] == [] and p["can_apply"], p
        kinds = [(o["op"], o["kind"], o["id"]) for o in p["ops"]]
        assert kinds[:3] == [("create", "zone", "home"), ("create", "vnet", "iot"), ("create", "vnet", "guest")]
        assert kinds[-1] == ("reload", "sdn", "cluster")
        r = await c.post("/api/sdn/designs", json={"name": "Home VLANs", "node_id": node_id, "spec": spec})
        assert r.status_code == 201, r.text
        did = r.json()["id"]
        dry = (await c.post(f"/api/sdn/designs/{did}/apply", json={})).json()
        assert dry["dry_run"] and not dry["applied"] and not agent.zones
        assert (await c.post(f"/api/sdn/designs/{did}/apply", json={"dry_run": False, "confirm": "nope"})
                ).status_code == 400
        r = await c.post(f"/api/sdn/designs/{did}/apply", json={"dry_run": False, "confirm": "home"})
        assert r.status_code == 200, r.text
        job = await _job_ok(c, r.json()["job"]["id"])
        assert job["status"] == "success", job
        assert agent.zones["home"]["bridge"] == "vmbr0" and agent.vnets["iot"]["tag"] == 20
        assert agent.subnets["iot"][0]["snat"] == 1
        assert any(p["path"] == "/cluster/sdn" for meth, p in agent.calls if meth == "pve.task")
        design = (await c.get(f"/api/sdn/designs/{did}")).json()
        assert design["status"] == "applied" and design["last_backup_id"]
        # Same design again: nothing to change.
        p = (await c.post("/api/sdn/plan", json={"node_id": node_id, "spec": spec})).json()
        assert not p["can_apply"] and p["ops"][-1]["op"] == "none", p["ops"]
        # Import the live zone into a new design.
        r = await c.post("/api/sdn/designs/import", json={"node_id": node_id, "zone": "home"})
        assert r.status_code == 201 and {v["id"] for v in r.json()["spec"]["vnets"]} == {"iot", "guest"}
        # One-click rollback restores the files from before.
        r = await c.post(f"/api/network/backups/{design['last_backup_id']}/restore", json={"confirm": "pve-a"})
        assert r.status_code == 200, r.text
        assert (await _job_ok(c, r.json()["job"]["id"]))["status"] == "success"
        assert "home" not in agent.zones and "iot" not in agent.vnets
        assert (await c.get(f"/api/sdn/designs/{did}")).json()["status"] == "rolled_back"

        # ---------------------------------------------------------- HA, replication, backup jobs, storage
        r = await c.post(f"/api/cluster/{node_id}/ha/resources", json={"sid": "ct:101", "state": "started"})
        assert r.status_code == 201 and r.json()["mode"] == "rules" and r.json()["resources"][0]["sid"] == "ct:101"
        assert (await c.post(f"/api/cluster/{node_id}/ha/resources", json={"sid": "lxc:1"})).status_code == 422
        r = await c.post(f"/api/cluster/{node_id}/replication", json={"vmid": 101, "target": "pve-b"})
        assert r.status_code == 201 and r.json()[0]["id"] == "101-0" and r.json()[0]["fail_count"] == 0
        bj = (await c.get(f"/api/cluster/{node_id}/backup-jobs")).json()
        assert bj["jobs"][0]["id"] == "backup-1"
        r = await c.post(f"/api/cluster/{node_id}/backup-jobs/backup-1/run")
        assert r.status_code == 202, r.text
        vz = [p for meth, p in agent.calls if meth == "pve.task" and p["path"] == "/nodes/localhost/vzdump"][-1]
        assert vz["args"]["all"] is True and vz["args"]["storage"] == "local"
        st = (await c.get(f"/api/cluster/{node_id}/storage")).json()["storages"]
        assert st[0]["storage"] == "local" and st[0]["used_pct"] == 96.0
        assert (await c.post(f"/api/cluster/{node_id}/storage", json={"storage": "nas", "type": "nfs"})
                ).status_code == 422

        # ---------------------------------------------------------- monitoring
        await monitoring.collect_health(node_id)
        from mmn.db import sessionmaker
        from mmn.models import MetricSample

        async with sessionmaker()() as db:
            now = datetime.now(UTC)
            for i in range(12):
                db.add(MetricSample(node_id=node_id, at=now - timedelta(minutes=i), cpu=95, mem_used=20,
                                    mem_total=100, disk_used=10, disk_total=100))
            await db.commit()
        ov = (await c.get("/api/monitoring", params={"range": "1h"})).json()
        row = next(n for n in ov["nodes"] if n["node_id"] == node_id)
        assert row["disk_health"] == "failed" and len(row["trend"]) >= 10 and row["trend"][0]["mem_pct"] == 20.0
        assert ov["totals"]["online"] >= 1
        assert (await c.get("/api/monitoring", params={"range": "2y"})).status_code == 422
        await c.put("/api/monitoring/settings", json={"enabled": True, "mail_to": [], "cpu_pct": 90, "cpu_minutes": 10})
        res = (await c.post("/api/monitoring/evaluate")).json()
        assert res["opened"] >= 3
        alerts = (await c.get("/api/monitoring/alerts", params={"open": True})).json()
        kinds = {a["kind"] for a in alerts if a["node_id"] == node_id}
        assert {"cpu", "storage", "smart"} <= kinds, kinds
        smart = (await c.get(f"/api/monitoring/nodes/{node_id}/smart", params={"disk": "/dev/sdb"})).json()
        assert smart["health"] == "FAILED"
        assert (await c.get(f"/api/monitoring/nodes/{node_id}/smart", params={"disk": "sdb"})).status_code == 400

        # ---------------------------------------------------------- GitHub deployments
        async def fake_github(path, token, params=None):
            if path == "/user":
                return {"login": "rhis"}
            if path == "/user/repos":
                return [{"full_name": "rhis/camview", "private": True, "default_branch": "main"}]
            if path == "/repos/rhis/camview/branches":
                return [{"name": "main"}, {"name": "test"}]
            if path == "/repos/rhis/camview/git/trees/main":
                return {"tree": [{"path": "deploy"}, {"path": "README.md"}]}
            if path == "/repos/rhis/camview/contents/deploy":
                return [{"name": "install.sh"}]
            if path.startswith("/repos/rhis/camview/branches/"):
                return {"commit": {"sha": "b" * 40}}
            raise deploy.GitHubError("not found")

        monkeypatch.setattr(deploy, "github_get", fake_github)
        assert (await c.put("/api/github", json={"token": "ghp_secret_token"})).json()["login"] == "rhis"
        assert (await c.get("/api/github/repos")).json()[0]["full_name"] == "rhis/camview"
        det = (await c.get("/api/github/detect", params={"repo": "rhis/camview", "branch": "main"})).json()
        assert det["preset"] == "script" and det["deploy_cmd"] == "bash ./deploy/install.sh"
        body = {"name": "camview", "repo": "rhis/camview", "node_id": node_id, "vmid": 102, "preset": "script",
                "deploy_cmd": det["deploy_cmd"], "service": "camview", "env": "API_KEY=abc123",
                "auto_deploy": "webhook"}
        assert (await c.post("/api/deployments", json={**body, "vmid": 200})).status_code == 400  # a VM
        assert (await c.post("/api/deployments", json={**body, "path": "/etc"})).status_code == 422
        r = await c.post("/api/deployments", json=body)
        assert r.status_code == 201, r.text
        dep = r.json()
        assert dep["branch"] == "main" and dep["path"] == "/opt/camview" and dep["has_env"]
        r = await c.post(f"/api/deployments/{dep['id']}/run", json={})
        assert r.status_code == 202, r.text
        run = r.json()

        async def deployed():
            return (await c.get(f"/api/deployments/{dep['id']}")).json()["last_status"] in ("success", "failed")
        assert await _wait(deployed, 15)
        d = (await c.get(f"/api/deployments/{dep['id']}")).json()
        assert d["last_status"] == "success" and d["last_sha"] == "a" * 40, d
        assert d["runs"][0]["snapshot"].startswith("mmn-deploy-")
        sc = [p for meth, p in agent.calls if meth == "script.run" and "mmn-git-token" in p["script"]][-1]
        assert "ghp_secret_token" not in sc["script"] and sc["env"]["MMN_GIT_TOKEN"] == "ghp_secret_token"
        assert base64.b64decode(sc["env"]["MMN_ENV_B64"]).decode() == "API_KEY=abc123"
        assert "systemctl restart camview" in sc["script"] and "pct exec 102" in sc["script"]
        jobs = (await c.get(f"/api/nodes/{node_id}/jobs")).json()
        assert "ghp_secret_token" not in json.dumps(jobs) and "abc123" not in json.dumps(jobs)
        assert run["branch"] == "main"

        # Webhook: wrong signature, ping, push to another branch, push to main.
        hook = (await c.get(f"/api/deployments/{dep['id']}/webhook")).json()
        url = f"/api/hooks/github/{dep['id']}"

        def sig(b: bytes) -> str:
            return "sha256=" + hmac.new(hook["secret"].encode(), b, hashlib.sha256).hexdigest()

        payload = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "rhis/camview"}}).encode()
        assert (await client.post(url, content=payload, headers={"x-github-event": "push",
                                                                 "x-hub-signature-256": "sha256=00"})).status_code == 401
        ping = b"{}"
        assert (await client.post(url, content=ping, headers={"x-github-event": "ping",
                                                              "x-hub-signature-256": sig(ping)})).json()["pong"]
        other = json.dumps({"ref": "refs/heads/feature"}).encode()
        r = await client.post(url, content=other, headers={"x-github-event": "push", "x-hub-signature-256": sig(other)})
        assert r.status_code == 202 and "ignored" in r.json()
        r = await client.post(url, content=payload, headers={"x-github-event": "push",
                                                             "x-hub-signature-256": sig(payload)})
        assert r.status_code == 202 and r.json()["run_id"], r.text

        async def two_runs():
            runs = (await c.get(f"/api/deployments/{dep['id']}")).json()["runs"]
            return len(runs) == 2 and runs[0]["status"] != "running"
        assert await _wait(two_runs, 15)
        assert (await c.get(f"/api/deployments/{dep['id']}")).json()["runs"][0]["trigger"] == "webhook"

        # ---------------------------------------------------------- MCP
        tok = (await c.post("/api/tokens", json={"name": "claude-net", "scopes": ["read"]})).json()["token"]
        await _init(client, tok)
        res = (await _rpc(client, tok, "tools/call", {"name": "get_bridge_map", "arguments": {}}, 2)).json()
        assert "vmbr0" in json.dumps(res["result"])
        res = (await _rpc(client, tok, "tools/call", {"name": "apply_network_changes",
                                                      "arguments": {"node": "pve-a"}}, 3)).json()
        assert res["result"]["isError"] and "network" in json.dumps(res["result"])
        tok2 = (await c.post("/api/tokens", json={"name": "claude-net2", "scopes": ["read", "network", "deploy"]})
                ).json()["token"]
        await _init(client, tok2)
        res = (await _rpc(client, tok2, "tools/call", {"name": "apply_network_changes", "arguments": {
            "node": "pve-a", "dry_run": False, "confirm": "wrong"}}, 4)).json()
        assert res["result"]["isError"] and "confirm" in json.dumps(res["result"])
        res = (await _rpc(client, tok2, "tools/call", {"name": "get_monitoring", "arguments": {"range": "1h"}}, 5)).json()
        assert not res["result"].get("isError"), res
        res = (await _rpc(client, tok2, "tools/call", {"name": "list_deployments", "arguments": {}}, 6)).json()
        assert "camview" in json.dumps(res["result"])
        log = (await c.get("/api/settings/audit", params={"source": "mcp"})).json()
        assert any(e["action"] == "mcp.apply_network_changes" and not e["success"] for e in log)
        ui_log = (await c.get("/api/settings/audit")).json()
        assert any(e["action"] == "sdn.apply" for e in ui_log) and any(e["action"] == "network.apply" for e in ui_log)

        assert (await c.delete(f"/api/nodes/{node_id}")).status_code == 204
        runner.cancel()


def test_sdn_validation_unit():
    from mmn.services import sdn

    st = {"zones": [], "vnets": [], "subnets": [], "controllers": []}
    spec = sdn.DesignSpec(zone=sdn.ZoneSpec(id="lab", type="vxlan"),
                          vnets=[sdn.VnetSpec(id="a1", tag=None), sdn.VnetSpec(id="a2", tag=5000)],
                          subnets=[sdn.SubnetSpec(vnet="a2", cidr="10.1.0.0/24", gateway="10.2.0.1")])
    errors, _ = sdn.validate(spec, st)
    assert any("peer" in e for e in errors) and any("needs a VNI" in e for e in errors)
    assert any("outside" in e for e in errors)


def test_nic_parser():
    from mmn.services import network

    g = network.parse_nic_dump(NIC_DUMP)
    assert g[("qemu", 200)]["name"] == "win11" and len(g[("qemu", 200)]["nics"]) == 2
    nic = g[("lxc", 101)]["nics"][0]
    assert nic["tag"] == 20 and nic["firewall"] and nic["mac"] == "BC:24:11:00:00:01" and nic["name"] == "eth0"
    assert network.parse_nic("qemu", "virtio=AA:BB,bridge=vmbr0,trunks=10;20")["trunks"] == ["10", "20"]


def test_deploy_script_quotes():
    from mmn.models import Deployment
    from mmn.services import deploy

    d = Deployment(name="app", repo="rhis/app", vmid=105, path="/opt/app", channel="test", setup_cmd="echo setup",
                   deploy_cmd="echo 'hi'; make", service="app")
    s = deploy.node_script(d, "test", run_setup=False)
    assert "pct exec 105 -- bash -c" in s and "MMN_GIT_TOKEN" in s
    inner = deploy.container_script(d, "test", run_setup=False)
    assert 'if [ "$FIRST" = 1 ]; then' in inner and "systemctl restart app" in inner
    assert deploy.verify_signature("s", b"x", "sha256=" + hmac.new(b"s", b"x", hashlib.sha256).hexdigest())
    assert not deploy.verify_signature("s", b"x", None)
