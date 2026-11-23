"""Phase 12: nightly checks and the morning summary: findings per customer (updates, security updates
and CVEs, disks, certificates, backups), new and resolved since the night before, proposals as
approval requests that wait for the maintenance window, approve all with one click, execution with
the approving admin's rights, the summary mail and the MCP tools; plus unit tests of the parsers."""

from __future__ import annotations

import asyncio
import ssl
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from servers import free_port, self_signed_cert
from test_customers import _call, _machine
from test_mcp import _init

from mmn.services import nightly

NOW = datetime(2026, 11, 23, 3, 0, tzinfo=UTC)


def _f(severity, area, target, finding, suggestion, tool=None, *, code=None, **params):
    from mmn.services.customers import _finding

    return _finding(severity, area, target, finding, suggestion, tool, code=code, **params)


# ------------------------------------------------------------------ unit tests
def test_categories_and_keys():
    assert nightly.category_of({"code": "updates_pending"}) == "updates"
    assert nightly.category_of({"code": "cve_fixable"}) == "cves"
    assert nightly.category_of({"code": "disk_full"}) == "disks"
    assert nightly.category_of({"code": "cert_expiring"}) == "certificates"
    assert nightly.category_of({"code": "backup_failed", "area": "backups"}) == "backups"
    assert nightly.category_of({"code": "machine_backup_none", "area": "backups"}) == "backups"
    assert nightly.category_of({"code": "services_failed"}) == "other"
    a = nightly.finding_key({"code": "disk_full", "target": "srv /", "params": {"pct": 91}})
    b = nightly.finding_key({"code": "disk_full", "target": "srv /", "params": {"pct": 95}})
    assert a == b and a != nightly.finding_key({"code": "disk_full", "target": "srv /var"})


def test_dpkg_osv_and_names():
    out = ('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_ID="13"\n---\n'
           "libc6:amd64\tglibc\nopenssl\topenssl\nlibssl3t64:amd64\topenssl (3.5.1-1)\n")
    osr, src = nightly.parse_dpkg(out)
    assert nightly.osv_ecosystem(osr) == "Debian:13"
    assert src == {"libc6": "glibc", "openssl": "openssl", "libssl3t64": "openssl"}
    assert nightly.osv_ecosystem({"ID": "ubuntu", "VERSION_ID": "24.04", "VERSION": "24.04.1 LTS (Noble)"}) == "Ubuntu:24.04:LTS"
    assert nightly.osv_ecosystem({"ID": "rocky", "VERSION_ID": "9"}) is None
    assert nightly.vuln_name("DEBIAN-CVE-2025-1234") == "CVE-2025-1234"
    assert nightly.vuln_name("UBUNTU-CVE-2024-99") == "CVE-2024-99"
    assert nightly.vuln_name("DSA-5800-1") == "DSA-5800-1"
    assert "libc6" in nightly.dpkg_script(["libc6", "bad;rm -rf /"]) and "rm -rf" not in nightly.dpkg_script(["x;rm -rf"])


async def test_osv_lookup(monkeypatch):
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        qs = json.loads(request.content)["queries"]
        seen.extend(qs)
        res = []
        for q in qs:
            if q["version"] == "3.5.1-1":
                res.append({"vulns": [{"id": "DEBIAN-CVE-2025-0001"}, {"id": "DEBIAN-CVE-2025-0002"}]})
            elif q["version"] == "3.5.1-2":
                res.append({"vulns": [{"id": "DEBIAN-CVE-2025-0002"}]})  # not fixed by the update
            else:
                res.append({})
        return httpx.Response(200, json={"results": res})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    cache: dict = {}
    items = [("openssl", "3.5.1-1", "3.5.1-2"), ("openssl", "3.5.1-1", "3.5.1-2"), ("glibc", "2.41-11", "2.41-12")]
    out = await nightly.osv_fixed("Debian:13", items, cache)
    assert out == {"openssl": {"CVE-2025-0001"}}
    assert len(seen) == 4  # the same package and version is asked once
    await nightly.osv_fixed("Debian:13", items, cache)
    assert len(seen) == 4  # cached


def test_certificates():
    linux = ("CERT\t/etc/pve/local/pveproxy-ssl.pem\tJan  5 10:00:00 2026 GMT\tCN=pve1.example.nl\n"
             "CERT\t/etc/letsencrypt/live/web/cert.pem\tDec  1 00:00:00 2026 GMT\tCN=www.example.nl\n"
             "CERT\t/etc/nginx/ssl/fine.pem\tMar  1 00:00:00 2028 GMT\tCN=fine\nRENEW\tcertbot\n")
    certs, renew = nightly.parse_certs(linux)
    assert renew == {"certbot"} and len(certs) == 3
    win = ("CERT\tAA11\t2026-01-01T00:00:00Z\tCN=dc01.corp.local\r\n"
           "CERT\tBB22\t2027-06-01T00:00:00Z\tCN=dc01.corp.local\r\n"  # renewed: the old one does not count
           "CERT\tCC33\t2026-11-30T00:00:00Z\tCN=rds.corp.local\r\n")
    wcerts, _ = nightly.parse_certs(win)
    assert sorted(c["where"] for c in wcerts) == ["BB22", "CC33"]
    exp = nightly.cert_finding("pve1", "pve1.example.nl", certs[0]["not_after"], 21, NOW, _f)
    assert exp["code"] == "cert_expired" and exp["severity"] == "high"
    soon = nightly.cert_finding("web", "www", certs[1]["not_after"], 21, NOW, _f, "certbot")
    assert soon["code"] == "cert_expiring" and soon["severity"] == "high" and soon["params"]["days"] == 7
    assert soon["renew"] == "certbot"
    assert nightly.cert_finding("x", "fine", certs[2]["not_after"], 21, NOW, _f) is None
    assert nightly._host_port("https://portal.example.nl/login") == ("portal.example.nl", 443)
    assert nightly._host_port("mail.example.nl:993") == ("mail.example.nl", 993)
    assert nightly._host_port("[2001:db8::1]:8443") == ("2001:db8::1", 8443)
    assert nightly._host_port("bad host") is None


async def test_endpoint_certificate():
    pem = self_signed_cert(Path(tempfile.mkdtemp()))  # valid for one day
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(pem)
    port = free_port()

    async def handle(reader, writer):
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port, ssl=ctx)
    try:
        now = datetime.now(UTC)
        out = await nightly.endpoint_findings([f"127.0.0.1:{port}", f"127.0.0.1:{free_port()}"], 21, now, _f)
    finally:
        server.close()
    assert out[0]["code"] == "cert_expiring" and out[0]["severity"] == "high" and out[0]["target"] == f"127.0.0.1:{port}"
    assert out[1]["code"] == "cert_endpoint_error" and out[1]["severity"] == "low"


def test_maintenance_window_start():
    from mmn.services import gateway

    w = gateway.parse_window("Tue 22:00-02:00")
    mon = datetime(2026, 11, 23, 9, 0)  # a Monday
    assert gateway.next_window_start(w, mon) == datetime(2026, 11, 24, 22, 0)
    assert gateway.next_window_start(w, datetime(2026, 11, 25, 1, 0)) is None  # Wednesday 01:00: open
    daily = gateway.parse_window("daily 02:00-04:00")
    assert gateway.next_window_start(daily, datetime(2026, 11, 23, 5, 0)) == datetime(2026, 11, 24, 2, 0)
    assert gateway.next_window_start(daily, datetime(2026, 11, 23, 1, 0)) == datetime(2026, 11, 23, 2, 0)


def test_security_packages_and_proposals():
    from mmn.models import Node

    lin = Node(name="web01", kind="linux", role="server", updates={"packages": [
        {"name": "openssl", "current": "1", "new": "2", "suite": "trixie-security"},
        {"name": "vim", "current": "1", "new": "2", "suite": "trixie"}]})
    win = Node(name="DC01", kind="windows", role="server", updates={"packages": [
        {"name": "KB1", "new": "Cumulative", "suite": "Critical · Security Updates"},
        {"name": "KB2", "new": "Defender", "suite": "Definition Updates"}]})
    assert [p["name"] for p in nightly.security_packages(lin)] == ["openssl"]
    assert [p["name"] for p in nightly.security_packages(win)] == ["KB1"]
    by_name = {"web01": lin, "DC01": win}
    up = nightly.proposal_for({"code": "updates_pending", "target": "web01"}, lin)
    assert up == {"tool": "install_node_updates", "args": {"node": "web01", "confirm": "web01"}, "label": "install_updates"}
    disk = {"code": "disk_full", "target": "web01 /var", "params": {"disk": "/var"}}
    assert nightly.node_of(disk, by_name) is lin
    clean = nightly.proposal_for(disk, lin)
    assert clean["tool"] == "run_command" and "journalctl --vacuum-time" in clean["args"]["command"]
    assert "Dism.exe" in nightly.proposal_for({"code": "disk_full", "target": "DC01 C:"}, win)["args"]["command"]
    assert nightly.proposal_for({"code": "cert_expiring", "target": "web01", "renew": "certbot"}, lin)["args"]["command"].startswith("certbot renew")
    assert nightly.proposal_for({"code": "cert_expiring", "target": "web01"}, lin) is None
    assert nightly.proposal_for({"code": "services_failed", "target": "web01"}, lin) is None
    px = Node(name="pve1", kind="proxmox", role="server", updates={})
    assert nightly.proposal_for({"code": "reboot_pending", "target": "pve1"}, px) is None  # guests: a person decides
    assert nightly.proposal_for({"code": "reboot_pending", "target": "DC01"}, win)["tool"] == "reboot_node"


# ------------------------------------------------------------------ the whole flow
async def _wait_done(c, timeout: float = 30):
    for _ in range(int(timeout * 10)):
        d = (await c.get("/api/nightly")).json()
        if not d["running"]:
            return d
        await asyncio.sleep(0.1)
    raise AssertionError("the nightly run did not finish")


async def test_nightly_flow(admin_client, client, smtp_server):
    from mmn.db import sessionmaker
    from mmn.models import AiApproval, AuditLog, Node
    from mmn.services import gateway, scheduler
    from mmn.services.settings_store import set_setting

    c = admin_client
    # A maintenance window that is closed now: proposals that change something wait for it.
    from zoneinfo import ZoneInfo
    local = datetime.now(ZoneInfo("Europe/Amsterdam"))
    later = (local + timedelta(hours=3)).strftime("%H:00")
    end = (local + timedelta(hours=4)).strftime("%H:00")
    cust = (await c.post("/api/customers", json={"name": "Nachtwacht BV", "maintenance_window": f"daily {later}-{end}"})).json()
    cid = cust["id"]
    web = await _machine(cid, "nw-web01", "linux", updates={"packages": [
        {"name": "openssl", "current": "3.5.1-1", "new": "3.5.1-2", "suite": "trixie-security"},
        {"name": "vim", "current": "1", "new": "2", "suite": "trixie"}], "reboot_required": True})
    await _machine(cid, "NW-DC01", "windows", updates={"packages": [
        {"name": "KB5070001", "new": "2026-11 Cumulative Update", "suite": "Critical · Security Updates"}]})

    # Settings: validation, per customer endpoints.
    r = await c.put("/api/nightly/settings", json={"enabled": True, "run_at": "25:00", "summary_at": "07:00"})
    assert r.status_code == 422
    r = await c.put("/api/nightly/settings", json={
        "enabled": True, "run_at": "2.30", "summary_at": "07:00", "mail_to": "night@example.com", "cve_lookup": False,
        "checks": {"updates": True, "cves": True, "disks": True, "certificates": True, "backups": True, "other": True}})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["run_at"] == "02:30"
    assert (await c.put(f"/api/nightly/customers/{cid}", json={"cert_endpoints": ["bad host"]})).status_code == 422
    r = await c.put(f"/api/nightly/customers/{cid}", json={"enabled": True, "cert_endpoints": [f"127.0.0.1:{free_port()}"]})
    assert r.status_code == 200 and r.json()["cert_endpoints"]
    s = (await c.get("/api/nightly/settings")).json()
    assert any(x["id"] == cid and x["cert_endpoints"] for x in s["customers"])

    # First run: everything is found, nothing is "new" yet (no night before).
    r = await c.post("/api/nightly/run", json={"customer_id": cid})
    assert r.status_code == 202, r.text
    d = await _wait_done(c)
    run = d["run"]
    assert run["status"] == "done" and [x["name"] for x in run["customers"]] == ["Nachtwacht BV"]
    codes = {(f["code"], f["target"]): f for f in run["findings"]}
    assert ("updates_pending_sec", "nw-web01") in codes  # the security suite counts as security now
    assert ("security_updates", "nw-web01") in codes and codes[("security_updates", "nw-web01")]["category"] == "cves"
    win = codes[("windows_security", "NW-DC01")]
    assert win["severity"] == "high" and win["params"]["critical"] == 1
    assert ("reboot_pending", "nw-web01") in codes and ("machine_offline", "nw-web01") in codes
    assert any(f["code"] == "cert_endpoint_error" for f in run["findings"])
    assert not any(f["is_new"] for f in run["findings"])
    cs = run["customers"][0]
    assert cs["first_run"] is True and cs["proposals"] == 3  # updates web01 (twice: one request), reboot, DC01
    prop = codes[("updates_pending_sec", "nw-web01")]["proposal"]
    assert prop["tool"] == "install_node_updates" and prop["status"] == "pending" and prop["run_after"]
    assert codes[("security_updates", "nw-web01")]["proposal"]["approval_id"] == prop["approval_id"]
    assert run["pending_proposals"] >= 3

    a = (await c.get(f"/api/gateway/approvals/{prop['approval_id']}")).json()
    assert a["token_name"] == "Nightly checks" and a["source"] == "nightly" and a["run_after"]
    assert "proposed by the nightly checks" in a["reason_texts"]

    # The morning summary: app + mail.
    r = await c.post(f"/api/nightly/runs/{run['id']}/send")
    assert r.status_code == 200, r.text
    msg = None
    await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                            "security": "none", "from_address": "app@example.com"})
    r = await c.post(f"/api/nightly/runs/{run['id']}/send")
    assert r.json()["sent"]["mail"] is True
    msg = await smtp_server.wait()
    assert msg["To"] == "night@example.com" and "morning summary" in msg["Subject"]
    body = msg.get_body(("plain",)).get_content()
    assert "Nachtwacht BV" in body and "KB5070001" in body

    # Approve all with one click: they wait for the maintenance window.
    r = await c.post(f"/api/nightly/runs/{run['id']}/approve", json={"customer_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == 3 and r.json()["scheduled"] == 3
    d = (await c.get("/api/nightly")).json()
    p2 = next(f for f in d["run"]["findings"] if f["code"] == "updates_pending_sec")["proposal"]
    assert p2["status"] == "scheduled"
    assert await gateway.run_due() == 0
    # The window opens: the calls run with the rights of the admin who approved them.
    async with sessionmaker()() as db:
        a = await db.get(AiApproval, prop["approval_id"])
        a.run_after = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    assert await gateway.run_due() == 1
    for _ in range(100):
        async with sessionmaker()() as db:
            a = await db.get(AiApproval, prop["approval_id"])
            if a.status in gateway.FINAL:
                break
        await asyncio.sleep(0.05)
    assert a.status == "failed" and "not connected" in (a.error or "")  # it ran, but the machine is offline
    async with sessionmaker()() as db:
        from sqlalchemy import select

        log = (await db.execute(select(AuditLog).where(AuditLog.action == "mcp.install_node_updates")
                                .order_by(AuditLog.id.desc()))).scalars().first()
    assert log.username == "admin" and log.detail["token_kind"] == "system" and log.detail["approval_id"] == a.id

    # Second night: updates installed on web01, a new problem on DC01.
    async with sessionmaker()() as db:
        n = await db.get(Node, web)
        n.updates = {"packages": []}
        dc = (await db.execute(select(Node).where(Node.name == "NW-DC01"))).scalars().one()
        dc.updates = {**dc.updates, "reboot_required": True}
        await db.commit()
    await nightly.start(trigger="manual", customer_ids=[cid], wait=True)
    d = (await c.get("/api/nightly")).json()
    run2 = d["run"]
    cs = run2["customers"][0]
    assert cs["first_run"] is False
    resolved = {x["code"] for x in cs["resolved"]}
    assert {"updates_pending_sec", "security_updates", "reboot_pending"} <= resolved
    new = [f for f in run2["findings"] if f["is_new"]]
    assert [(f["code"], f["target"]) for f in new] == [("reboot_pending", "NW-DC01")]
    assert new[0]["proposal"]["tool"] == "reboot_node"
    hist = (await c.get("/api/nightly/runs")).json()
    assert hist[0]["id"] == run2["id"] and hist[1]["id"] == run["id"]
    assert (await c.get(f"/api/nightly/runs/{run['id']}")).json()["id"] == run["id"]

    # MCP: the summary and a run.
    r = await c.post("/api/tokens", json={"name": "claude-nightly", "scopes": ["read"]})
    token = r.json()["token"]
    await _init(client, token)
    data, err = await _call(client, token, "get_morning_summary", {"customer": "Nachtwacht BV"})
    assert err is None, err
    assert data["customers"][0]["name"] == "Nachtwacht BV" and data["findings"][0]["customer"] == "Nachtwacht BV"
    data, err = await _call(client, token, "run_nightly_checks", {"customer": "Nachtwacht BV"})
    assert err is None, err
    await _wait_done(c)

    # The schedule follows the settings.
    await scheduler.reload_nightly_jobs()
    await c.put("/api/settings/mail", json={"enabled": False})
    async with sessionmaker()() as db:
        await set_setting(db, "nightly", {"mail_to": "", "cve_lookup": True})
        await db.commit()

    # Clean up: other tests count the machines of all customers.
    from sqlalchemy import delete

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.environment_id == cid))
        await db.commit()
    assert (await c.delete(f"/api/customers/{cid}")).status_code == 204
