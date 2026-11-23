"""Phase 11: backup management per customer, against a fake Proxmox VE agent: coverage (jobs,
backups, failed runs), policy and exclusions, alerts, restores (guest and files), back up now, the
MCP tools, the report section and the scan findings; plus unit tests of the parsers and rules."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from test_agents import _csr, _dist, _secret, _wait, live_server  # noqa: F401
from test_customers import _call
from test_guests import HELLO, FakePve, _job_ok
from test_mcp import _init

NOW = int(time.time())
PBS_VOL = "pbs:backup/vm/200/2026-11-22T01:00:00Z"


class FakeBackupPve(FakePve):
    """Cluster 'lab': job 'daily' backs up 101 and 200; 102 is in no job; 103 is stopped and in no job.
    101 has a fresh backup on local, 200 one on the PBS but its latest run failed."""

    def api(self, method: str, path: str, args: dict):
        if path == "/cluster/backup":
            return [{"id": "daily", "schedule": "02:00", "storage": "pbs", "vmid": "101,200", "enabled": 1,
                     "next-run": NOW + 3600, "mode": "snapshot"}]
        if path == "/storage":
            return [{"storage": "local", "type": "dir", "content": "iso,vztmpl,backup", "path": "/var/lib/vz"},
                    {"storage": "pbs", "type": "pbs", "content": "backup", "server": "10.0.0.9", "datastore": "main"},
                    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"}]
        if path == "/storage/pbs":
            return {"storage": "pbs", "type": "pbs", "server": "10.0.0.9", "datastore": "main",
                    "username": "backup@pbs", "fingerprint": "aa:bb"}
        if path == "/nodes/pve-a/storage/local/content":
            vols = [{"volid": "local:backup/vzdump-lxc-101-2026_11_22-01_00_00.tar.zst", "vmid": 101,
                     "ctime": NOW - 3 * 3600, "size": 500 * 2**20, "format": "tar.zst", "subtype": "lxc"}]
            return [v for v in vols if not args.get("vmid") or v["vmid"] == args["vmid"]]
        if path == "/nodes/pve-a/storage/pbs/content":
            vols = [{"volid": PBS_VOL, "vmid": 200, "ctime": NOW - 50 * 3600, "size": 40 * 2**30, "format": "pbs-vm",
                     "subtype": "qemu", "verification": {"state": "ok"}}]
            return [v for v in vols if not args.get("vmid") or v["vmid"] == args["vmid"]]
        if path == "/nodes/pve-a/tasks":
            assert args["typefilter"] == "vzdump"
            return [{"upid": "UPID:pve-a:0001:0002:6740:vzdump::root@pam:", "id": "", "status": "job errors",
                     "starttime": NOW - 7200, "endtime": NOW - 7000},
                    {"upid": "UPID:pve-a:0003:0004:6741:vzdump:101:root@pam:", "id": "101", "status": "OK",
                     "starttime": NOW - 3 * 3600, "endtime": NOW - 3 * 3600 + 60}]
        if path.startswith("/nodes/pve-a/tasks/UPID:") and path.endswith("/log"):
            return [{"n": 1, "t": "INFO: starting new backup job"},
                    {"n": 2, "t": "ERROR: Backup of VM 200 failed - unable to connect to PBS 10.0.0.9"}]
        if path == "/nodes/localhost/storage/pbs/file-restore/list":
            assert args["volume"] == PBS_VOL
            return [{"filepath": "L2RyaXZlLXNjc2kwLmltZy5maWR4", "text": "drive-scsi0.img.fidx", "type": "v",
                     "leaf": False}]
        return super().api(method, path, args)

    async def guests(self) -> dict:
        from test_guests import GUESTS

        data = {"node": "pve-a", "at": "2026-11-22T00:00:00Z", "guests": GUESTS}
        await self.send({"type": "guests", "data": data})
        return data


async def _enroll_into(c, client, env_id: str) -> dict:
    _dist()
    code = (await c.post("/api/pairing", json={"environment_id": env_id, "label": "pve-a"})).json()["code"]
    _, csr = _csr()
    r = await client.post("/agent/enroll", json={"code": code, "csr": csr, "hostname": "pve-a", "agent_version": "0.3.0"},
                          headers={"x-mmn-agent-secret": _secret()})
    assert r.status_code == 200, r.text
    return r.json()


async def test_backup_management(admin_client, client, live_server):  # noqa: F811
    import websockets

    from mmn.services import monitoring

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Ijmuiden Backup BV"})).json()
    enrolled = await _enroll_into(c, client, cust["id"])
    node_id = enrolled["node_id"]
    headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS",
               "x-mmn-client-cert": quote(enrolled["certificate"])}
    async with websockets.connect(f"ws://127.0.0.1:{live_server}/agent/ws", additional_headers=headers) as ws:
        agent = FakeBackupPve(ws, live_server, headers)
        runner = asyncio.create_task(agent.run())
        await agent.send({"type": "hello", "data": HELLO})
        await agent.guests()

        async def listed():
            return len((await c.get("/api/guests", params={"node_id": node_id})).json()) == 4
        assert await _wait(listed)

        # ---------------------------------------------------------- coverage
        before = (await c.get(f"/api/protection/customers/{cust['id']}")).json()
        assert before["collected_at"] is None and before["guests"] == []
        r = await c.post(f"/api/protection/customers/{cust['id']}/refresh")
        assert r.status_code == 200, r.text
        v = r.json()
        st = {g["vmid"]: g for g in v["guests"]}
        assert st[101]["status"] == "ok" and st[101]["jobs"] == ["daily"] and st[101]["count"] == 1
        assert st[101]["age_hours"] < 4 and st[101]["storages"] == ["local"]
        assert st[102]["status"] == "no_job" and st[103]["status"] == "no_job"
        assert st[200]["status"] == "failed" and "unable to connect" in st[200]["failure"]["error"]
        assert st[200]["verified"] == "ok" and st[200]["offsite"] is True
        assert v["summary"]["guests"]["total"] == 4 and v["summary"]["failed"] == 1
        assert v["summary"]["missing"] == 2 and v["summary"]["protected"] == 1
        cl = v["clusters"][0]
        assert cl["jobs"][0]["id"] == "daily" and {s["storage"] for s in cl["storages"]} == {"local", "pbs"}
        assert cl["runs"]["ok"] == 1 and cl["runs"]["failed"] == 1
        lst = (await c.get("/api/protection")).json()
        assert next(x for x in lst if x["customer"]["id"] == cust["id"])["summary"]["failed"] == 1

        # ---------------------------------------------------------- alerts
        await monitoring.evaluate()
        alerts = (await c.get("/api/monitoring/alerts", params={"open_only": True})).json()
        mine = {a["key"]: a for a in alerts if a["key"].endswith(cust["id"])}
        assert mine[f"backup_failed:{cust['id']}"]["severity"] == "critical"
        assert "VM 200 win11" in mine[f"backup_failed:{cust['id']}"]["message"]
        assert mine[f"backup_missing:{cust['id']}"]["value"] == 2

        # ---------------------------------------------------------- policy and exclusions
        r = await c.post(f"/api/protection/customers/{cust['id']}/exclude",
                         json={"key": st[103]["key"], "excluded": True})
        assert r.status_code == 200, r.text
        assert (await c.post(f"/api/protection/customers/{cust['id']}/exclude", json={"key": "bad"})).status_code == 400
        r = await c.put(f"/api/protection/customers/{cust['id']}/policy",
                        json={**r.json(), "max_age_hours": 2, "recipients": ["ops@example.com"]})
        assert r.status_code == 200, r.text
        v = (await c.get(f"/api/protection/customers/{cust['id']}")).json()
        st = {g["vmid"]: g["status"] for g in v["guests"]}
        assert st == {101: "stale", 102: "no_job", 103: "excluded", 200: "failed"}
        assert (await c.put(f"/api/protection/customers/{cust['id']}/policy",
                            json={"max_age_hours": 26, "recipients": ["nope"]})).status_code == 422
        await c.put(f"/api/protection/customers/{cust['id']}/policy",
                    json={"max_age_hours": 26, "exclude": [v["guests"][0]["key"].rsplit(":", 1)[0] + ":103"]})

        # ---------------------------------------------------------- backups of a guest, restores
        vols = (await c.get(f"/api/protection/nodes/{node_id}/guests/200/backups")).json()
        assert [x["volid"] for x in vols] == [PBS_VOL]
        r = await c.post(f"/api/protection/nodes/{node_id}/restore", json={"volume": PBS_VOL})
        assert r.status_code == 202, r.text
        assert r.json()["vmid"] == 120 and not r.json()["overwrite"]
        call = [p for m, p in agent.calls if m == "pve.task"][-1]
        assert call["path"] == "/nodes/localhost/qemu" and call["args"] == {"vmid": 120, "archive": PBS_VOL}
        assert (await _job_ok(c, r.json()["job"]["id"]))["status"] == "success"
        # Overwriting needs the VMID as confirmation, and a stopped guest.
        r = await c.post(f"/api/protection/nodes/{node_id}/restore", json={"volume": PBS_VOL, "overwrite": True})
        assert r.status_code == 400 and r.json()["detail"] == "confirm_mismatch"
        r = await c.post(f"/api/protection/nodes/{node_id}/restore",
                         json={"volume": PBS_VOL, "overwrite": True, "confirm": "200"})
        assert r.status_code == 400 and "stop" in r.json()["detail"]
        r = await c.post(f"/api/protection/nodes/{node_id}/restore", json={"volume": PBS_VOL, "vmid": 101})
        assert r.status_code == 400 and "exists" in r.json()["detail"]
        ct_vol = "local:backup/vzdump-lxc-101-2026_11_22-01_00_00.tar.zst"
        r = await c.post(f"/api/protection/nodes/{node_id}/restore", json={"volume": ct_vol})
        assert r.status_code == 400 and "storage" in r.json()["detail"]
        r = await c.post(f"/api/protection/nodes/{node_id}/restore",
                         json={"volume": PBS_VOL, "overwrite": True, "vmid": 103, "confirm": "103"})
        assert r.status_code == 400 and "container" in r.json()["detail"]  # a VM backup over a container
        r = await c.post(f"/api/protection/nodes/{node_id}/restore",
                         json={"volume": ct_vol, "vmid": 103, "overwrite": True, "confirm": "103"})
        assert r.status_code == 202, r.text
        call = [p for m, p in agent.calls if m == "pve.task"][-1]
        assert call["path"] == "/nodes/localhost/lxc" and call["args"]["force"] == 1 and call["args"]["restore"] == 1

        # File level (PBS).
        files = (await c.get(f"/api/protection/nodes/{node_id}/files", params={"volume": PBS_VOL})).json()
        assert files["entries"][0]["path"] == "/drive-scsi0.img.fidx" and not files["entries"][0]["leaf"]
        r = await c.post(f"/api/protection/nodes/{node_id}/files/restore",
                         json={"volume": PBS_VOL, "path": "/drive-scsi0.img.fidx/part/2/etc/hosts"})
        assert r.status_code == 202, r.text
        sc = [p for m, p in agent.calls if m == "script.run"][-1]
        assert sc["env"]["MMN_SNAPSHOT"] == "vm/200/2026-11-22T01:00:00Z" and sc["env"]["MMN_STORE"] == "pbs"
        assert "proxmox-file-restore extract" in sc["script"] and r.json()["target"].endswith("/hosts")
        r = await c.post(f"/api/protection/nodes/{node_id}/files/restore",
                         json={"volume": ct_vol, "path": "/etc/hosts"})
        assert r.status_code == 400
        r = await c.post(f"/api/protection/nodes/{node_id}/files/restore",
                         json={"volume": PBS_VOL, "path": "/../etc/shadow"})
        assert r.status_code == 400

        # Back up now.
        r = await c.post(f"/api/protection/nodes/{node_id}/backup-now", json={"vmid": 102, "storage": "pbs"})
        assert r.status_code == 202, r.text
        call = [p for m, p in agent.calls if m == "pve.task"][-1]
        assert call["path"] == "/nodes/localhost/vzdump" and call["args"]["vmid"] == "102"

        # ---------------------------------------------------------- MCP
        tok = (await c.post("/api/tokens", json={"name": "backup-ai", "scopes": ["read", "cluster", "restore_guests"],
                                                 "customer_ids": [cust["id"]]})).json()["token"]
        await _init(client, tok)
        data, err = await _call(client, tok, "list_backup_coverage", {"customer": cust["name"], "problems_only": True})
        assert err is None, err
        assert {g["vmid"] for g in data["guests"]} == {102, 200}
        data, err = await _call(client, tok, "list_guest_backups", {"guest": "win11"})
        assert err is None and data[0]["volid"] == PBS_VOL
        data, err = await _call(client, tok, "restore_guest", {"node": "pve-a", "volume": PBS_VOL, "confirm": "nope"})
        assert data is None and "confirm must be 'new'" in err
        data, err = await _call(client, tok, "restore_guest", {"node": node_id, "volume": PBS_VOL, "confirm": "new"})
        assert err is None, err
        assert data["vmid"] == 120 and data["type"] == "qemu"
        data, err = await _call(client, tok, "set_backup_policy", {"customer": cust["id"], "include": [
            k for k in [g["key"] for g in v["guests"] if g["vmid"] == 103]], "max_age_hours": 48})
        assert err is None and data["max_age_hours"] == 48 and data["exclude"] == []
        data, err = await _call(client, tok, "list_backup_files", {"node": node_id, "volume": PBS_VOL})
        assert err is None and data["entries"][0]["name"] == "drive-scsi0.img.fidx"
        # A token without the restore scope cannot restore.
        tok2 = (await c.post("/api/tokens", json={"name": "backup-ro", "scopes": ["read"]})).json()["token"]
        await _init(client, tok2)
        _, err = await _call(client, tok2, "restore_guest", {"node": node_id, "volume": PBS_VOL, "confirm": "new"})
        assert "restore_guests" in err

        # ---------------------------------------------------------- scan and report
        scan = await c.post(f"/api/customers/{cust['id']}/scan", params={"live": False})
        assert scan.status_code == 200, scan.text
        codes = {f.get("code") for f in scan.json()["findings"]}
        assert {"backup_failed", "backup_no_job"} <= codes
        from mmn.db import sessionmaker
        from mmn.models import Environment
        from mmn.services import reports

        async with sessionmaker()() as db:
            e = await db.get(Environment, cust["id"])
            d = await reports.build(db, e, reports.current_period(datetime.now(UTC), await reports.timezone(db)),
                                    "nl", live=False)
        cov = d["backups"]["coverage"]
        assert cov["considered"] == 4 and cov["failed"] == 1
        assert any(x["status"] == "failed" and x["item"].startswith("VM 200") for x in cov["items"])

        runner.cancel()


# ------------------------------------------------------------------ unit tests
def test_job_covers_and_classify():
    from mmn.services import protection as p

    job = {"id": "j", "vmid": "100,101", "enabled": 1, "exclude": ""}
    assert p.job_covers(job, 100, "pve1", {}) and not p.job_covers(job, 102, "pve1", {})
    assert not p.job_covers({**job, "enabled": 0}, 100, "pve1", {})
    assert not p.job_covers({**job, "node": "pve2"}, 100, "pve1", {})
    assert p.job_covers({"all": 1, "exclude": "105"}, 104, None, {})
    assert not p.job_covers({"all": 1, "exclude": "104,105"}, 104, None, {})
    assert p.job_covers({"pool": "prod"}, 7, None, {"prod": {7}})

    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    pol = {**p.DEFAULT_POLICY, "exclude": []}
    base = {"key": "guest:c:100", "jobs": ["j"], "last_backup": (now - timedelta(hours=5)).isoformat(), "failure": None}
    assert p.classify_guest(base, pol, now)["status"] == "ok"
    assert p.classify_guest({**base, "last_backup": (now - timedelta(hours=30)).isoformat()}, pol, now)["status"] == "stale"
    assert p.classify_guest({**base, "last_backup": None}, pol, now)["status"] == "never"
    assert p.classify_guest({**base, "jobs": []}, pol, now)["status"] == "no_job"
    old_fail = {"at": (now - timedelta(hours=10)).isoformat(), "error": "x"}
    assert p.classify_guest({**base, "failure": old_fail}, pol, now)["status"] == "ok"  # a later backup worked
    new_fail = {"at": (now - timedelta(hours=1)).isoformat(), "error": "x"}
    assert p.classify_guest({**base, "failure": new_fail}, pol, now)["status"] == "failed"
    assert p.classify_guest(base, {**pol, "exclude": ["guest:c:100"]}, now)["status"] == "excluded"

    m = {"key": "machine:" + "a" * 32, "role": "server", "name": "dc01", "guest": None}
    wsb = {"policy": True, "last_success": (now - timedelta(hours=3)).isoformat(), "last_result": 0,
           "last_backup": (now - timedelta(hours=3)).isoformat(), "system_state": False}
    r = p.classify_machine({**m, "local": {"wsb": wsb}}, pol, now, {})
    assert r["status"] == "ok" and r["method"] == "wsb" and r["system_state"] is False
    failed = {**wsb, "last_result": -2147024784, "last_backup": (now - timedelta(hours=1)).isoformat()}
    r = p.classify_machine({**m, "local": {"wsb": failed}}, pol, now, {})
    assert r["status"] == "failed" and r["detail"] == "0x80070070"
    assert p.classify_machine({**m, "local": {"wsb": {"installed": False}, "agents": []}}, pol, now, {})["status"] == "none"
    assert p.classify_machine({**m, "local": None}, pol, now, {})["status"] == "unknown"
    veeam = {"veeam": {"at": (now - timedelta(hours=2)).isoformat(), "level": 2, "message": "Failed"}}
    assert p.classify_machine({**m, "local": {"wsb": {}, **veeam}}, pol, now, {})["status"] == "failed"
    ag = {"agents": [{"name": "ABService", "display": "Active Backup for Business Agent", "status": "Running"}]}
    r = p.classify_machine({**m, "local": {"wsb": {}, **ag}}, pol, now, {})
    assert r["status"] == "external" and "Active Backup" in r["detail"]
    g = {"guest:c:100": {"status": "ok", "last_backup": base["last_backup"]}}
    r = p.classify_machine({**m, "local": {"wsb": {}}, "guest": {"key": "guest:c:100"}}, pol, now, g)
    assert r["status"] == "ok" and r["method"] == "guest"
    ws = {**m, "role": "workstation", "local": {"wsb": {}}}
    assert p.classify_machine(ws, pol, now, {})["status"] == "not_monitored"
    assert p.classify_machine(ws, {**pol, "workstations": True}, now, {})["status"] == "none"
    assert p.classify_machine({**m, "local": {"pbs_server": True}}, pol, now, {})["status"] == "backup_server"


def test_parsers():
    from mmn.services import pbs, protection, restore

    out = protection.parse_windows('noise {"v":1,"wsb":{"installed":true,"policy":true,"schedule":"21:00",'
                                   '"system_state":true},"agents":{"name":"VeeamEndpointBackupSvc"},"errors":[]}')
    assert out["wsb"]["schedule"] == ["21:00"] and out["agents"][0]["name"] == "VeeamEndpointBackupSvc"
    lin = protection.parse_linux("@@@tools\nrestic\nproxmox-backup-manager\n@@@timers\n@@@cron\n0 2 * * * restic\n@@@end\n")
    assert lin["pbs_server"] and lin["tools"] == ["restic", "proxmox-backup-manager"] and lin["cron"]

    stdout = "\n".join([
        "@@@version", "proxmox-backup-server 4.0.18 running version: 4.0.18",
        "@@@datastores", json.dumps([{"name": "main", "path": "/mnt/main", "gc-schedule": "daily"}]),
        "@@@usage", json.dumps([{"store": "main", "total": 1000, "used": 850, "avail": 150}]),
        "@@@verify", json.dumps([{"id": "v1", "store": "main", "schedule": "sat 03:00", "last-run-state": "ok"}]),
        "@@@sync", "[]", "@@@prune", "[]",
        "@@@remotes", json.dumps([{"name": "offsite", "host": "pbs2", "password": "secret"}]),
        "@@@gc", "main\t" + json.dumps({"last-run-state": "ok", "removed-bytes": 5}),
        "@@@fingerprint", "Fingerprint (sha256): " + ":".join(["ab"] * 32), "@@@end"])
    ov = pbs.parse_overview(stdout)
    assert ov["datastores"][0]["used_pct"] == 85.0 and ov["datastores"][0]["gc_state"] == "ok"
    assert ov["verify_jobs"][0]["last_state"] == "ok" and "password" not in ov["remotes"][0]
    assert ov["fingerprint"] == ":".join(["ab"] * 32)
    try:
        pbs.parse_overview("@@@nopbs\n")
        raise AssertionError("expected an error")
    except Exception as e:  # noqa: BLE001
        assert "not a Proxmox Backup Server" in str(e)

    j = pbs.PbsJobIn(kind="sync", id="off1", store="main", remote="offsite", remote_store="copy", schedule="hourly")
    assert pbs.job_args(j, create=True)[:4] == ["proxmox-backup-manager", "sync-job", "create", "off1"]
    assert "--remote-store" in pbs.job_args(j, create=True)
    pr = pbs.PbsJobIn(kind="prune", id="p1", store="main", schedule="daily", keep_daily=7, keep_weekly=4)
    assert pbs.job_args(pr, create=False)[-4:] == ["--keep-daily", "7", "--keep-weekly", "4"]
    for bad in ({"kind": "prune", "id": "p", "store": "main"}, {"kind": "sync", "id": "s1", "store": "main"},
                {"kind": "verify", "id": "v;rm", "store": "main"}):
        try:
            pbs.PbsJobIn(**bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass

    assert restore.volume_type(PBS_VOL) == "qemu" and restore.volume_vmid(PBS_VOL) == 200
    assert restore.volume_type("nfs:backup/vzdump-lxc-105-2026_01_01-00_00_00.tar.zst") == "lxc"
    assert restore.volume_vmid("nfs:backup/vzdump-lxc-105-2026_01_01-00_00_00.tar.zst") == 105
    for bad in ("local:../../etc/passwd", "local:backup/x;rm -rf /", "nope"):
        try:
            restore.check_volume(bad)
            raise AssertionError(f"accepted {bad}")
        except Exception:  # noqa: BLE001
            pass


def test_scan_findings():
    from mmn.services import customers, protection

    v = {"collected_at": "2026-11-23T00:00:00+00:00", "policy": {**protection.DEFAULT_POLICY},
         "guests": [{"kind": "guest", "type": "qemu", "vmid": 100, "name": "dc", "cluster": "c", "status": "no_job"},
                    {"kind": "guest", "type": "lxc", "vmid": 101, "name": "web", "cluster": "c", "status": "no_job"}],
         "machines": [{"kind": "machine", "name": "dc01", "role": "server", "status": "none", "method": None},
                      {"kind": "machine", "name": "fs01", "role": "server", "status": "ok", "method": "wsb",
                       "system_state": False}],
         "clusters": [{"name": "c", "guest_count": 2, "storages": [{"storage": "local", "shared": False}]}],
         "pbs": [{"name": "pbs1", "datastores": [{"name": "main", "used_pct": 95}], "verify_jobs": [], "sync_jobs": []}]}
    codes = [f["code"] for f in protection.scan_findings(v, customers._finding)]
    assert codes.count("backup_no_job") == 1
    assert {"backup_local_only", "machine_backup_none", "wsb_no_system_state", "pbs_no_verify", "pbs_no_sync",
            "pbs_full"} <= set(codes)
    assert protection.scan_findings(None, customers._finding)[0]["code"] == "backup_unknown"
    from mmn.services.reports import FINDING_NL

    assert all(c in FINDING_NL for c in codes)


async def test_pbs_commands(monkeypatch):
    from mmn.services import pbs

    sent: list[str] = []

    async def fake_exec(node_id, command, timeout=120):
        sent.append(command)
        if command.startswith("export LC_ALL"):
            return {"exit_code": 0, "stdout": "@@@datastores\n" + json.dumps([{"name": "main", "path": "/mnt/m"}])
                    + "\n@@@verify\n[]\n@@@sync\n[]\n@@@prune\n[]\n@@@end\n"}
        return {"exit_code": 0, "stdout": "ok"}

    monkeypatch.setattr(pbs, "_exec", fake_exec)
    await pbs.add_remote("n", pbs.RemoteIn(name="offsite", host="pbs2.example", auth_id="sync@pbs",
                                           password="it's s3cret", fingerprint="ab:" * 31 + "ab"))
    cmd = sent[0]
    assert cmd.startswith("MMN_PBS_SECRET='it'\"'\"'s s3cret'; proxmox-backup-manager remote create offsite")
    assert '--password "$MMN_PBS_SECRET"' in cmd
    await pbs.save_job("n", pbs.PbsJobIn(kind="verify", id="weekly", store="main", schedule="sat 03:00"))
    assert "verify-job create weekly --store main --schedule 'sat 03:00'" in sent[-2]
    try:
        await pbs.save_job("n", pbs.PbsJobIn(kind="verify", id="weekly", store="other"))
        raise AssertionError("expected an error")
    except pbs.NodeError as e:
        assert "does not exist" in str(e)
    try:
        await pbs.add_datastore("n", pbs.DatastoreIn(name="x1", path="/etc"))
        raise AssertionError("expected an error")
    except pbs.NodeError:
        pass
