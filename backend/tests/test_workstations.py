"""Phase 10: workstations (inventory, security status, remote actions) and monthly customer reports."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_agents import _csr, _dist, _secret, live_server  # noqa: F401
from test_customers import _call
from test_mcp import _init
from test_servers import _enroll, _wait

from mmn.services import workstations as ws

WIN_INV = {
    "v": 1, "errors": ["secure_boot: not supported"],
    "system": {"manufacturer": "Dell Inc.", "model": "Latitude 5440", "serial": "ABC1234", "bios": "1.9.0",
               "cpu": "13th Gen Intel(R) Core(TM) i5-1345U", "cores": 10, "threads": 12, "memory": 17179869184,
               "domain": "corp.example", "part_of_domain": True, "chassis": "10", "os": "Microsoft Windows 11 Pro",
               "os_version": "10.0.26100", "build": "26100.2605", "display_version": "24H2", "arch": "64-bit",
               "install_date": "2025-02-01T10:00:00.0000000Z", "last_boot": "2026-11-20T07:59:00.0000000Z"},
    "users": "CORP\\piet",  # a single item comes through as a string
    "last_user": "CORP\\piet",
    "software": [{"name": "Google Chrome", "version": "131.0.6778.86", "publisher": "Google LLC", "installed": "20261101"},
                 {"name": "TeamViewer", "version": "15.59.3", "publisher": "TeamViewer", "installed": "20250310"},
                 {"name": "Microsoft 365 Apps for business - nl-nl", "version": "16.0.18129.20158",
                  "publisher": "Microsoft Corporation", "installed": ""}],
    "disks": {"name": "KXG80ZNV512G NVMe KIOXIA 512GB", "media": "SSD", "bus": "NVMe", "size": 512110190592,
              "health": "Healthy", "status": "OK", "temperature": 41, "wear": 93, "power_on_hours": 6200,
              "read_errors": 0},
    "volumes": [{"mount": "C:", "label": "", "fs": "NTFS", "size": 510000000000, "free": 30000000000,
                 "health": "Healthy"}],
    "defender": {"service": True, "antivirus": True, "realtime": True, "tamper": True, "mode": "Normal",
                 "signature_updated": "2026-11-10T03:00:00.0000000Z", "signature_age_days": 12,
                 "signature_version": "1.421.1", "engine": "1.1.24090.11", "quick_scan_age_days": 1,
                 "full_scan_age_days": 40, "threats_30d": 0},
    "av_products": [{"name": "Windows Defender", "enabled": True, "up_to_date": False}],
    "bitlocker": [{"mount": "C:", "type": "OperatingSystem", "status": "FullyDecrypted", "protection": "Off",
                   "percent": 0, "method": "None"}],
    "firewall": [{"profile": "Domain", "enabled": True}, {"profile": "Private", "enabled": True},
                 {"profile": "Public", "enabled": False}],
    "tpm": {"present": True, "ready": True}, "secure_boot": None,
    "update_history": [{"date": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                        "title": "2026-11 Cumulative Update for Windows 11 (KB5046617)", "result": "Succeeded"},
                       {"date": "2026-10-09T02:00:00Z", "title": "Old update", "result": "Failed"}],
    "hotfixes": [{"id": "KB5046617", "description": "Security Update", "installed": "2026-11-20"}],
    "reboot_pending": True, "battery": {"charge": 88, "status": 2},
}

LINUX_OUT = """@@@os
PRETTY_NAME="Ubuntu 24.04.1 LTS"
VERSION_ID="24.04"
@@@uname
Linux 6.8.0-49-generic x86_64
@@@dmi
sys_vendor=LENOVO
product_name=20XW0055MH
product_serial=PF2ABCDE
bios_version=N32ET86W
chassis_type=10
@@@cpu
model name	: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
8
@@@mem
MemTotal:       16123456 kB
@@@uptime
3600.5 1000.0
@@@users
jan      seat0        2026-11-22 08:01 (login screen)
jan      tty2         2026-11-22 08:01 (tty2)
@@@lsblk
{"blockdevices": [{"name": "nvme0n1", "size": 512110190592, "type": "disk", "model": "SAMSUNG MZVL2512", "rota": false,
  "mountpoint": null, "fstype": null, "tran": "nvme", "children": [
   {"name": "nvme0n1p3", "size": 500000000000, "type": "part", "model": null, "rota": false, "mountpoint": null,
    "fstype": "crypto_LUKS", "tran": null}]}]}
@@@df
Filesystem 1-blocks Used Available Capacity Mounted on
/dev/mapper/vgubuntu-root 490000000000 100000000000 390000000000 21% /
@@@smart
nvme0n1 SMART overall-health self-assessment test result: PASSED
@@@packages
firefox\t133.0+build1-0ubuntu0.24.04.1
teamviewer\t15.59.3
@@@firewall
Status: active
@@@av
clamav-daemon inactive
@@@updates
Start-Date: 2026-11-21  09:12:01
Upgrade: libssl3t64:amd64 (3.0.13-0ubuntu3.4, 3.0.13-0ubuntu3.5), openssl:amd64 (3.0.13-0ubuntu3.4, 3.0.13-0ubuntu3.5)
@@@reboot
yes
@@@secureboot
SecureBoot enabled
@@@end
""".replace("\\t", "\t")


def test_parse_windows_and_checks():
    from mmn.models import Node

    inv = ws.parse_windows("noise before\n" + json.dumps(WIN_INV))
    assert inv["users"] == ["CORP\\piet"] and inv["disks"][0]["wear"] == 93
    assert inv["os"]["display_version"] == "24H2" and inv["hardware"]["serial"] == "ABC1234"
    assert inv["updates"]["last_installed"].startswith(str((datetime.now(UTC) - timedelta(days=2)).date()))
    n = Node(name="PC1", kind="windows", role="workstation", inventory=inv,
             updates={"packages": [{"name": "KB1", "security": True}], "reboot_required": False})
    codes = {c["code"]: c for c in ws.checks(n)}
    assert {"av_on", "signatures_old", "firewall_off", "bitlocker_off", "updates_pending", "recent_updates",
            "reboot_pending", "disk_worn", "disk_full"} <= set(codes)
    assert codes["firewall_off"]["profiles"] == ["Public"] and codes["signatures_old"]["days"] == 12
    assert ws.status_of(ws.checks(n)) == "warning"
    inv["antivirus"]["defender"]["realtime"] = False
    inv["antivirus"]["products"] = []
    assert ws.status_of(ws.checks(n)) == "critical"


def test_parse_linux():
    from mmn.models import Node

    inv = ws.parse_linux(LINUX_OUT)
    assert inv["os"]["name"] == "Ubuntu 24.04.1 LTS" and inv["hardware"]["manufacturer"] == "LENOVO"
    assert inv["users"] == ["jan"] and inv["encryption"]["luks"] is True and inv["secure_boot"] is True
    assert inv["disks"][0]["health"] == "Healthy" and inv["disks"][0]["media"] == "SSD"
    assert {s["name"] for s in inv["software"]} == {"firefox", "teamviewer"}
    assert inv["updates"]["history"][0]["packages"] == 2 and inv["updates"]["reboot_pending"] is True
    codes = {c["code"] for c in ws.checks(Node(name="L", kind="linux", role="workstation", inventory=inv, updates={}))}
    assert {"luks_on", "firewall_on", "av_none_linux", "disks_healthy", "reboot_pending", "recent_updates"} <= codes


def test_action_commands_are_fixed_and_quoted():
    cmd, _ = ws.action_command("windows", "message", message="Even 'n' herstart om 12:00")
    assert cmd.startswith("msg * /TIME:600 'Even ''n'' herstart om 12:00'")
    cmd, _ = ws.action_command("linux", "message", message="it's me")
    assert cmd == "wall 'it'\"'\"'s me' && echo sent"
    with pytest.raises(ws.NodeError):
        ws.action_command("linux", "defender_update")
    with pytest.raises(ws.NodeError):
        ws.action_command("windows", "rm -rf")
    with pytest.raises(ws.NodeError):
        ws.logoff_command("windows", "piet; Remove-Item C:\\")
    assert "$u='piet'" in ws.logoff_command("windows", "CORP\\piet")
    # The PowerShell script fits the Windows command line (passed as -EncodedCommand).
    assert len(ws.WINDOWS_SCRIPT) * 2 * 4 / 3 < 30000


class WorkstationAgent:
    def __init__(self, ws_conn) -> None:
        self.ws = ws_conn
        self.commands: list[str] = []

    async def run(self) -> None:
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") != "req":
                continue
            method, p = m["method"], m.get("params") or {}
            res, err = {}, None
            if method == "exec":
                cmd = p["command"]
                self.commands.append(cmd)
                if "ConvertTo-Json -Depth 5" in cmd:
                    res = {"exit_code": 0, "stdout": json.dumps(WIN_INV), "stderr": ""}
                elif cmd.startswith("msg *"):
                    res = {"exit_code": 0, "stdout": "sent\n", "stderr": ""}
                elif "Update-MpSignature" in cmd:
                    res = {"exit_code": 0, "stdout": "signatures 1.421.9 from 23-11-2026\n", "stderr": ""}
                elif "quser" in cmd:
                    res = {"exit_code": 0, "stdout": "logged off 1 session(s)\n", "stderr": ""}
                else:
                    res = {"exit_code": 1, "stdout": "", "stderr": "unexpected"}
            elif method == "diagnose":
                res = {"findings": ["CPU 95% for 5 minutes"], "failed_services": [], "top_cpu": [{"name": "Teams"}]}
            else:
                err = f"unknown method {method}"
            await self.ws.send(json.dumps({"type": "res", "id": m["id"], "ok": err is None, "result": res,
                                           "error": err or ""}))


async def test_workstations_and_reports(admin_client, client, live_server, smtp_server):  # noqa: F811
    import websockets

    _dist()
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Tandarts Smit", "contact_email": "info@smit.example",
                                                 "maintenance_window": "za 08:00-12:00"})).json()
    r = await c.post("/api/pairing", json={"environment_id": cust["id"], "label": "PC-BALIE", "role": "workstation"})
    assert r.status_code == 201, r.text
    key, csr = _csr()
    body = {"code": r.json()["code"], "csr": csr, "hostname": "PC-BALIE", "agent_version": "0.9.0", "kind": "windows",
            "os": "windows"}
    r = await client.post("/agent/enroll", json=body, headers={"x-mmn-agent-secret": _secret()})
    assert r.status_code == 200, r.text
    from urllib.parse import quote
    e = r.json()
    wid = e["node_id"]
    headers = {"x-mmn-agent-secret": _secret(), "x-mmn-client-verify": "SUCCESS", "x-mmn-client-cert": quote(e["certificate"])}
    srv_id, srv_h = await _enroll(c, client, cust["id"], "srv-smit", "linux", "linux")
    assert srv_id

    url = f"ws://127.0.0.1:{live_server}/agent/ws"
    async with websockets.connect(url, additional_headers=headers) as wsc:
        agent = WorkstationAgent(wsc)
        task = asyncio.create_task(agent.run())
        await wsc.send(json.dumps({"type": "hello", "data": {
            "protocol": 1, "agent_version": "0.9.0", "os": "windows", "arch": "amd64", "hostname": "PC-BALIE",
            "install": "windows", "capabilities": ["metrics", "exec", "diagnose", "powershell"],
            "info": {"os_pretty_name": "Windows 11 Pro 24H2"}}}))

        async def online():
            rows = (await c.get(f"/api/workstations?customer_id={cust['id']}")).json()["workstations"]
            return rows and rows[0]["online"]

        assert await _wait(online)
        lst = (await c.get(f"/api/workstations?customer_id={cust['id']}")).json()
        assert lst["totals"]["total"] == 1 and lst["totals"]["no_inventory"] == 1  # the server is not listed

        r = await c.post(f"/api/workstations/{wid}/inventory")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["model"] == "Dell Inc. Latitude 5440" and d["user"] == "CORP\\piet" and d["security"] == "warning"
        assert "bitlocker_off" in d["issues"] and d["encryption"] == "warning" and d["antivirus"] == "Microsoft Defender"
        assert d["inventory"]["software"][1]["name"] == "TeamViewer"

        sw = (await c.get("/api/workstations/software?q=teamview*")).json()
        assert [x["machine"] for x in sw] == ["PC-BALIE"] and sw[0]["software"][0]["version"] == "15.59.3"

        r = await c.post(f"/api/workstations/{wid}/actions", json={"action": "message", "message": "Pauze om 12:00"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert agent.commands[-1] == "msg * /TIME:600 'Pauze om 12:00'; 'sent'"
        r = await c.post(f"/api/workstations/{srv_id}/actions", json={"action": "defender_update"})
        assert r.status_code in (400, 409)  # Linux server: Windows-only action (or offline)

        # MCP: list, diagnose and actions with the risk levels of the AI gateway.
        tok = (await c.post("/api/tokens", json={"name": "claude-ws", "scopes": ["read", "logs", "workstations",
                                                                                 "reports"]})).json()["token"]
        await _init(client, tok)
        data, err = await _call(client, tok, "list_workstations", {"customer": "Tandarts Smit"})
        assert err is None and data["workstations"][0]["security"] == "warning"
        data, err = await _call(client, tok, "get_workstation", {"workstation": "PC-BALIE"})
        assert err is None and "software" not in data["inventory"] and data["inventory"]["software_count"] == 3
        data, err = await _call(client, tok, "diagnose_workstation", {"workstation": "PC-BALIE"})
        assert err is None and "CPU 95% for 5 minutes" in data["findings"]
        assert any("BitLocker" in f for f in data["findings"])
        data, err = await _call(client, tok, "find_software", {"name": "chrome"})
        assert err is None and data[0]["software"][0]["name"] == "Google Chrome"
        data, err = await _call(client, tok, "workstation_action", {"workstation": "PC-BALIE", "action": "defender_update",
                                                                    "reason": "old signatures"})
        assert err is None and data["ok"] and "1.421.9" in data["output"]
        _, err = await _call(client, tok, "log_off_workstation_user", {"workstation": "PC-BALIE", "user": "piet",
                                                                       "confirm": "PC-X", "reason": "test"})
        assert err and "confirm" in err
        from mmn.services import gateway
        assert gateway.REGISTRY["workstation_action"].risk == "operate"
        assert gateway.REGISTRY["log_off_workstation_user"].risk == "destructive"
        assert gateway.REGISTRY["diagnose_workstation"].risk == "read"
        assert gateway.REGISTRY["send_customer_report"].risk == "operate"

        # Customer overview and optimisation scan see the workstation.
        ov = (await c.get(f"/api/customers/{cust['id']}/overview")).json()
        assert ov["totals"]["workstations"]["total"] == 1

        # Monthly report: settings, generate (HTML + PDF), mail with the PDF attached.
        r = await c.put(f"/api/customers/{cust['id']}/reports/settings",
                        json={"enabled": True, "day": 2, "recipients": ["praktijk@smit.example"], "lang": "nl"})
        assert r.status_code == 200 and r.json()["recipients"] == ["praktijk@smit.example"]
        info = (await c.get(f"/api/customers/{cust['id']}/reports")).json()
        period = info["current_period"]
        r = await c.post(f"/api/customers/{cust['id']}/reports", json={"period": period})
        assert r.status_code == 201, r.text
        rep = r.json()
        assert rep["has_pdf"] and rep["lang"] == "nl" and rep["summary"]["machines"]["workstations"] == 1
        assert rep["summary"]["ai_calls"] >= 4 and rep["summary"]["ai_changes"] >= 1
        full = (await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}")).json()
        assert full["data"]["workstations"][0]["machine"] == "PC-BALIE"
        assert any("BitLocker" in x["text"] for x in full["data"]["security"])
        assert any(a["tool"] == "workstation_action" for a in full["data"]["ai"]["changes"])
        html = (await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}/html")).text
        assert "Maandelijks IT-rapport" in html and "PC-BALIE" in html and "Beveiliging" in html
        pdf = await c.get(f"/api/customers/{cust['id']}/reports/{rep['id']}/pdf")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 2000

        r = await c.post(f"/api/customers/{cust['id']}/reports/{rep['id']}/send", json={})
        assert r.status_code == 409 and r.json()["detail"] == "mail_not_configured"
        await c.put("/api/settings/mail", json={"enabled": True, "host": "127.0.0.1", "port": smtp_server.port,
                                                "security": "none", "from_address": "app@example.com"})
        while not smtp_server.queue.empty():
            smtp_server.queue.get_nowait()
        r = await c.post(f"/api/customers/{cust['id']}/reports/{rep['id']}/send", json={})
        assert r.status_code == 200, r.text
        assert r.json()["sent_to"] == ["praktijk@smit.example"]
        msg = await smtp_server.wait()
        assert msg["To"] == "praktijk@smit.example" and "IT-rapport Tandarts Smit" in msg["Subject"]
        names = [p.get_filename() for p in msg.iter_attachments()]
        assert names == [f"it-report-Tandarts-Smit-{period}.pdf", f"it-report-Tandarts-Smit-{period}.html"]

        # MCP report tools.
        data, err = await _call(client, tok, "get_customer_report", {"customer": "Tandarts Smit"})
        assert err is None and data["id"] == rep["id"] and data["data"]["period"] == period
        data, err = await _call(client, tok, "get_customer_report", {"customer": "Tandarts Smit", "generate": True,
                                                                     "lang": "en"})
        assert err is None and data["id"] != rep["id"] and data["lang"] == "en"

        # The scheduler makes last month's report once on the report day and mails it.
        from mmn.services import reports
        local = datetime.now(UTC).replace(day=3, hour=9)
        done = await reports.run_schedule(local)
        assert "Tandarts Smit" in done
        msg = await smtp_server.wait()
        assert msg["To"] == "praktijk@smit.example"
        assert "Tandarts Smit" not in await reports.run_schedule(local)  # not twice
        assert "Tandarts Smit" not in await reports.run_schedule(local.replace(day=1))  # before the report day
        await c.put("/api/settings/mail", json={"enabled": False})

        task.cancel()


async def test_report_periods():
    from zoneinfo import ZoneInfo

    from mmn.services import reports

    tz = ZoneInfo("Europe/Amsterdam")
    s, e = reports.month_bounds("2026-12", tz)
    assert s.isoformat() == "2026-11-30T23:00:00+00:00" and e.isoformat() == "2026-12-31T23:00:00+00:00"
    assert reports.previous_period(datetime(2027, 1, 1, 6, tzinfo=UTC), tz) == "2026-12"
    assert reports.period_label("2026-03", "nl") == "maart 2026"
    with pytest.raises(reports.NodeError):
        reports.month_bounds("2026-13", tz)
