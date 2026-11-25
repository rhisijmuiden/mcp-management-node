"""Phase 20: certificates (TLS endpoints incl. STARTTLS, certificate stores on Linux and Windows), firewall
configuration versions with diffs (Proxmox VE, Linux, Windows, UniFi) and VPN tunnel status; alerts, nightly
findings, the API and the MCP tools. Machines are faked at the agent's exec command."""

from __future__ import annotations

import asyncio
import ssl
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from servers import free_port
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.services import agent_hub, certs, netsec

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _f(severity, area, target, finding, suggestion, tool=None, *, code=None, **params):
    from mmn.services.customers import _finding

    return _finding(severity, area, target, finding, suggestion, tool, code=code, **params)


def _cert(tmp: Path, cn: str, days: int = 30, sans: str | None = None) -> tuple[Path, Path]:
    import subprocess

    key, crt = tmp / f"{cn}.key", tmp / f"{cn}.pem"
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
           "-days", str(days), "-subj", f"/CN={cn}"]
    if sans:
        cmd += ["-addext", f"subjectAltName={sans}"]
    subprocess.run(cmd, check=True, capture_output=True)
    return crt, key


# ------------------------------------------------------------------ unit tests: certificates
LINUX_OUT = """BEGIN\t/etc/letsencrypt/live/web.example.nl/cert.pem
notAfter=Dec  1 10:00:00 2026 GMT
notBefore=Sep  2 10:00:00 2026 GMT
serial=04A1B2
sha256 Fingerprint=AA:BB:CC
subject=CN=web.example.nl
issuer=C=US,O=Let's Encrypt,CN=R11
X509v3 Subject Alternative Name: \n    DNS:web.example.nl, DNS:www.example.nl
KEY= Public Key Algorithm: id-ecPublicKey  Public-Key: (256 bit)
END
BEGIN\t/etc/pve/local/pve-ssl.pem
notAfter=Jan  1 00:00:00 2025 GMT
notBefore=Jan  1 00:00:00 2023 GMT
serial=01
sha256 Fingerprint=DD:EE
subject=OU=PVE Cluster Node,O=Proxmox Virtual Environment,CN=pve1.lan
issuer=CN=Proxmox Virtual Environment,OU=abc,O=PVE Cluster Manager CA
KEY= Public Key Algorithm: rsaEncryption  Public-Key: (2048 bit)
END
RENEW\tcertbot
"""

WIN_OUT = (
    "CERT\tMy\tAAA111\t2026-11-30T10:00:00Z\t2025-11-30T10:00:00Z\tCN=rdgw.klant.nl\tCN=Sectigo RSA\t0A\trdgw.klant.nl\tRSA 2048\t1\t0\r\n"
    "CERT\tMy\tBBB222\t2026-01-01T10:00:00Z\t2025-01-01T10:00:00Z\tCN=rdgw.klant.nl\tCN=Sectigo RSA\t09\trdgw.klant.nl\tRSA 2048\t0\t0\r\n"
    "CERT\tMy\tCCC333\t2027-06-01T10:00:00Z\t2026-06-01T10:00:00Z\tCN=DC01.ad.klant.nl\tCN=Klant-CA\t11\tDC01.ad.klant.nl\tRSA 2048\t0\t1\r\n"
    "garbage line\r\n")


def test_parsers_certificates():
    found, renew, err = certs.parse_linux(LINUX_OUT)
    assert err is None and renew == {"certbot"}
    le, pve = found
    assert le["common_name"] == "web.example.nl" and le["sans"] == ["web.example.nl", "www.example.nl"]
    assert le["key_info"] == "EC 256" and le["fingerprint"] == "AABBCC" and not le["self_signed"]
    assert le["not_after"] == datetime(2026, 12, 1, 10, 0, tzinfo=UTC)
    assert pve["key_info"] == "RSA 2048" and pve["common_name"] == "pve1.lan"
    assert certs.parse_linux("ERROR\topenssl is not installed\n")[2] == "openssl is not installed"

    win = certs.parse_windows(WIN_OUT)
    assert len(win) == 2  # the old rdgw certificate next to the renewed one is left out
    rd = next(c for c in win if c["common_name"] == "rdgw.klant.nl")
    assert rd["fingerprint"] == "AAA111" and rd["in_use"] and rd["renew"] is None
    dc = next(c for c in win if c["common_name"] == "DC01.ad.klant.nl")
    assert dc["renew"] == "win-autoenroll" and dc["in_use"] is False
    assert certs.machine_key("n1", rd).startswith("node:n1:My:")

    assert certs.name_matches("www.example.nl", ["*.example.nl"])
    assert not certs.name_matches("a.b.example.nl", ["*.example.nl"])
    assert not certs.name_matches("example.nl", ["*.example.nl"])
    assert certs.name_matches("10.0.0.5", ["10.0.0.5"])
    assert certs.host_port("https://x.nl/path") == ("x.nl", 443) and certs.host_port("[::1]:8443") == ("::1", 8443)
    assert certs.clean_host("HTTPS://Web.Example.NL/") == "web.example.nl"
    for bad in ("bad host", "a;rm -rf /", "*.example.nl"):
        try:
            certs.clean_host(bad)
            raise AssertionError(bad)
        except certs.CertError:
            pass
    assert certs.clean_paths(["/opt/app/tls/*.pem"]) == ["/opt/app/tls/*.pem"]
    for bad in (["relative.pem"], ["/a b.pem"], ["/etc/$(id)"], ["/x/../etc/shadow"]):
        try:
            certs.clean_paths(bad)
            raise AssertionError(bad)
        except certs.CertError:
            pass
    assert "/opt/x.pem" in certs.linux_script(["/opt/x.pem"]) and "__EXTRA__" not in certs.linux_script()
    try:
        certs.probe_script(False, "x';reboot;'", 443, "x", "none")
        raise AssertionError
    except certs.CertError:
        pass
    assert "-starttls smtp" in certs.probe_script(False, "mail.x.nl", 587, "mail.x.nl", "smtp")
    assert "SslStream" in certs.probe_script(True, "rdgw.x.nl", 443, "rdgw.x.nl", "none")


def test_parse_probe():
    with tempfile.TemporaryDirectory() as d:
        crt, _ = _cert(Path(d), "vpn.klant.nl", 40, "DNS:vpn.klant.nl")
        pem = crt.read_text()
    ok = certs.parse_probe(f"VERIFY\t0 (ok)\n{pem}", "vpn.klant.nl")
    assert ok["trusted"] is True and ok["hostname_ok"] is True and ok["self_signed"]
    bad = certs.parse_probe(f"VERIFY\t18 (self-signed certificate)\n{pem}", "other.klant.nl")
    assert bad["trusted"] is False and "self-signed" in bad["trust_error"] and bad["hostname_ok"] is False
    win = certs.parse_probe(f"VERIFY\tRemoteCertificateNameMismatch\n{pem}", "other.klant.nl")
    assert win["trusted"] is True and win["hostname_ok"] is False
    win2 = certs.parse_probe(f"VERIFY\tRemoteCertificateNameMismatch, RemoteCertificateChainErrors\n{pem}", "x")
    assert win2["trusted"] is False and win2["trust_error"] == "RemoteCertificateChainErrors"
    try:
        certs.parse_probe("ERROR\tconnect: Connection refused\n", "x")
        raise AssertionError
    except certs.CertError as e:
        assert "refused" in str(e)


async def _tls_server(crt: Path, key: Path, starttls: str | None = None):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(crt), str(key))

    async def handle(reader, writer):
        try:
            if starttls == "smtp":
                writer.write(b"220 mail.test ESMTP\r\n")
                await reader.readline()
                writer.write(b"250-mail.test\r\n250-STARTTLS\r\n250 OK\r\n")
                await reader.readline()
                writer.write(b"220 Go ahead\r\n")
                await writer.drain()
                await writer.start_tls(ctx)
            await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
        finally:
            writer.close()

    port = free_port()
    srv = await asyncio.start_server(handle, "127.0.0.1", port, ssl=None if starttls else ctx)
    return srv, port


async def test_fetch_direct_and_starttls():
    with tempfile.TemporaryDirectory() as d:
        crt, key = _cert(Path(d), "mail.test", 10, "DNS:mail.test,IP:127.0.0.1")
        srv, port = await _tls_server(crt, key)
        srv2, port2 = await _tls_server(crt, key, "smtp")
        async with srv, srv2:
            r = await certs.fetch_direct("127.0.0.1", port)
            assert r["common_name"] == "mail.test" and r["trusted"] is False and r["hostname_ok"] is True
            assert r["self_signed"] and r["key_info"] == "RSA 2048"
            assert 8 <= (r["not_after"] - datetime.now(UTC)).days <= 10
            r2 = await certs.fetch_direct("127.0.0.1", port2, sni="mail.test", starttls="smtp")
            assert r2["fingerprint"] == r["fingerprint"]
            try:
                await certs.fetch_direct("127.0.0.1", free_port(), timeout=3)
                raise AssertionError
            except OSError:
                pass


# ------------------------------------------------------------------ unit tests: firewall and VPN
NFT_1 = """#TOOL\tnft
table inet filter {
\tset blocklist {
\t\ttype ipv4_addr
\t\telements = { 1.2.3.4, 5.6.7.8 }
\t}
\tchain input {
\t\ttype filter hook input priority filter; policy drop;
\t\ttcp dport 22 counter packets 10 bytes 600 accept
\t\tiifname "docker0" accept
\t}
}
"""


def test_firewall_normalizers():
    text, summary = netsec.normalize_linux(NFT_1, netsec.DEFAULTS["fw_ignore"])
    assert "elements = { … }" in text and "packets" not in text and "docker0" not in text
    assert summary["tool"] == "nft" and summary["rules"] == 1
    again, _ = netsec.normalize_linux(NFT_1.replace("1.2.3.4", "9.9.9.9").replace("packets 10 bytes 600",
                                                                                     "packets 99 bytes 9"),
                                      netsec.DEFAULTS["fw_ignore"])
    assert again == text  # bans and counters are no change

    ipt = ("#TOOL\tiptables\n# Generated by iptables-save v1.8 on Tue\n*filter\n:INPUT DROP [5:300]\n"
           "-A INPUT -p tcp --dport 22 -j ACCEPT\n-A f2b-sshd -s 1.2.3.4/32 -j REJECT\nCOMMIT\n")
    t2, s2 = netsec.normalize_linux(ipt, netsec.DEFAULTS["fw_ignore"])
    assert ":INPUT DROP [0:0]" in t2 and "Generated" not in t2 and "f2b" not in t2 and s2["rules"] == 1

    win = ("PROFILE\tPublic\tTrue\tBlock\tAllow\r\nPROFILE\tDomain\tFalse\tBlock\tAllow\r\n"
           "RULE\tRemote Desktop\tInbound\tAllow\tAny\tTCP\t3389\tAny\tAny\t\tLocal\r\n"
           "RULE\tSQL\tInbound\tAllow\tDomain\tTCP\t1433\t\t10.0.0.0/8\tC:\\sql.exe\tGroupPolicy\r\n")
    t3, s3 = netsec.normalize_windows(win)
    assert t3.startswith("profile Domain: OFF") and "| GroupPolicy" in t3 and s3["disabled_profiles"] == ["Domain"]
    assert s3["rules"] == 2

    pve = "### /etc/pve/firewall/cluster.fw\n[OPTIONS]\nenable: 1\n\n[RULES]\nIN SSH(ACCEPT) -i vmbr0\n\n#STATUS\tStatus: enabled/running\n"
    t4, s4 = netsec.normalize_pve(pve)
    assert "#STATUS" not in t4 and s4 == {"files": 1, "rules": 1, "status": "Status: enabled/running",
                                          "cluster_enabled": True}

    zones = [{"id": "z1", "name": "Internal"}, {"id": "z2", "name": "External"}]
    pols = [{"id": "p-123456789", "name": "Block guests", "enabled": True, "action": {"type": "BLOCK"},
             "source": {"zoneId": "z1"}, "destination": {"zoneId": "z2"}, "metadata": {"origin": "USER"}}]
    t5, s5 = netsec.render_unifi(zones, pols, "Default")
    assert "source.zoneId: zone Internal" in t5 and "metadata" not in t5 and s5 == {"zones": 2, "rules": 1,
                                                                                    "enabled": 1}
    d, a, r = netsec.make_diff("a\nb\nc\n", "a\nB\nc\nd\n")
    assert (a, r) == (2, 1) and "-b" in d and "+B" in d and "+d" in d


WG = ["wg0\tPRIV\tPUBSERVER\t51820\toff",
      "wg0\tPEER1\t(none)\t203.0.113.5:51820\t10.10.0.0/24\t1000\t5000\t6000\t25",
      "wg0\tPEER2\t(none)\t(none)\t10.20.0.2/32\t0\t0\t0\toff",
      "wg0\tPEER3\t(none)\t198.51.100.7:4500\t10.30.0.0/24\t500\t1\t1\t25"]


def test_vpn_parsers():
    t = netsec.parse_wg(WG, now=1100)
    assert [x["status"] for x in t] == ["up", "idle", "down"]
    assert t[0]["name"] == "wg0 · 10.10.0.0/24" and t[0]["remote"] == "203.0.113.5:51820" and t[0]["auto_watch"]
    assert not t[1]["auto_watch"] and t[1]["remote"] is None

    conns = ("site-a: IKEv1/2, no reauthentication, rekeying every 14400s\n  local:  %any\n  remote: 198.51.100.1\n"
             "  site-a-net: TUNNEL, rekeying every 3600s\nsite-b: IKEv2\n  remote: 198.51.100.2\n")
    sas = ("site-a: #3, ESTABLISHED, IKEv2, 1234_i 5678_r*\n  local  'gw' @ 192.0.2.1[4500]\n"
           "  site-a-net: #7, reqid 1, INSTALLED, TUNNEL, ESP:AES_GCM_16-256\nsite-b: #4, CONNECTING, IKEv2\n")
    s = {x["name"]: x for x in netsec.parse_swanctl(conns, sas)}
    assert s["site-a"]["status"] == "up" and s["site-a"]["remote"] == "198.51.100.1"
    assert s["site-a"]["detail"]["children"] == ["site-a-net INSTALLED"] and s["site-b"]["status"] == "down"

    legacy = ("Status of IKE charon daemon\nConnections:\n     office:  %any...203.0.113.9  IKEv1\n"
              "     office:   local:  [gw] uses pre-shared key authentication\n"
              "Security Associations (1 up, 0 connecting):\n     office[2]: ESTABLISHED 5 minutes ago\n")
    lg = netsec.parse_ipsec_statusall(legacy)
    assert lg == [{"key": "ipsec:office", "kind": "ipsec", "name": "office", "remote": "203.0.113.9",
                   "status": "up", "detail": {"ike": "ESTABLISHED"}, "auto_watch": True}]

    lin = netsec.parse_linux_vpn("NOW\t1100\n" + "".join(f"WG\t{w}\n" for w in WG) +
                                 "UNIT\topenvpn.service\tactive\texited\nUNIT\topenvpn-client@hq.service\tfailed\tfailed\n"
                                 "UNIT\twg-quick@wg0.service\tactive\texited\n")
    kinds = [(x["kind"], x["name"], x["status"]) for x in lin]
    assert ("openvpn", "hq", "down") in kinds and len(lin) == 4

    win = netsec.parse_windows_vpn("NOW\t1100\r\nS2S\tAzure\tConnected\t20.1.2.3\tEnabled\r\n"
                                   "S2S\tOld\tDisconnected\t1.1.1.1\tDisabled\r\nVPN\tKantoor\tDisconnected\tvpn.x.nl\r\n"
                                   "UNIT\tOpenVPNService\tStopped\tAutomatic\r\n")
    w = {x["name"]: x for x in win}
    assert w["Azure"]["status"] == "up" and "Old" not in w and w["Kantoor"]["status"] == "idle"
    assert w["OpenVPNService"]["status"] == "down" and w["OpenVPNService"]["auto_watch"]

    uf = netsec.parse_unifi_vpn({"id": "s1", "name": "Default"},
                                [{"id": "t1", "name": "To DC", "status": "CONNECTED", "remoteIp": "203.0.113.20"},
                                 {"id": "t2", "name": "Off", "enabled": False},
                                 {"id": "t3", "name": "Mystery"}],
                                [{"id": "v1", "name": "WireGuard server", "type": "WIREGUARD"}])
    u = {x["name"]: x for x in uf}
    assert u["To DC (Default)"]["status"] == "up" and u["To DC (Default)"]["remote"] == "203.0.113.20"
    assert u["Mystery (Default)"]["status"] == "unknown" and not u["Mystery (Default)"]["auto_watch"]
    assert u["WireGuard server (Default)"]["status"] == "up" and "Off (Default)" not in u


# ------------------------------------------------------------------ the whole flow
class FakeMachines:
    """Answers the agent's exec command like real machines would; outputs can change between reads."""

    def __init__(self) -> None:
        self.online: set[str] = set()
        self.kind: dict[str, str] = {}
        self.out: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def what(script: str) -> str:
        if "/etc/pve/firewall" in script:
            return "pve_fw"
        if "nft -s list ruleset" in script:
            return "linux_fw"
        if "Get-NetFirewallProfile" in script:
            return "win_fw"
        if "wg show all dump" in script and "Get-VpnS2SInterface" not in script:
            return "linux_vpn"
        if "Get-VpnS2SInterface" in script:
            return "win_vpn"
        if "pveproxy-ssl.pem" in script:
            return "linux_certs"
        if "Cert:\\LocalMachine" in script:
            return "win_certs"
        if "s_client" in script or "SslStream" in script:
            return "probe"
        return "other"

    async def request(self, node_id, method, params=None, timeout=60):
        assert method == "exec"
        w = self.what(params["command"])
        self.calls.append((node_id, w))
        return {"stdout": self.out.get((node_id, w), ""), "stderr": "", "exit_code": 0}


async def test_netsec_flow(admin_client, client, monkeypatch):
    from sqlalchemy import select

    from mmn.db import sessionmaker
    from mmn.models import AuditLog, Certificate, FirewallVersion, VpnTunnel
    from mmn.services import monitoring

    c = admin_client
    fake = FakeMachines()
    monkeypatch.setattr(agent_hub, "is_online", lambda nid: nid in fake.online)
    monkeypatch.setattr(agent_hub, "request", fake.request)

    cust = (await c.post("/api/customers", json={"name": "Netsec Klant"})).json()
    cid = cust["id"]
    other = (await c.post("/api/customers", json={"name": "Netsec Ander"})).json()
    pve1 = await _machine(cid, "ns-pve1", "proxmox", cluster_name="nsclu")
    pve2 = await _machine(cid, "ns-pve2", "proxmox", cluster_name="nsclu")
    lin = await _machine(cid, "ns-web01", "linux")
    win = await _machine(cid, "NS-DC01", "windows")
    ws = await _machine(cid, "NS-PC01", "windows", role="workstation")
    fake.online |= {pve1, pve2, lin, win, ws}

    soon = (datetime.now(UTC) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(UTC) - timedelta(days=3)).strftime("%b %d %H:%M:%S %Y GMT")
    fake.out[(lin, "linux_certs")] = LINUX_OUT.replace("Dec  1 10:00:00 2026 GMT", old)
    fake.out[(win, "win_certs")] = WIN_OUT.replace("2026-11-30T10:00:00Z", soon)
    fake.out[(pve1, "linux_certs")] = ""
    pve_v1 = "### /etc/pve/firewall/cluster.fw\n[OPTIONS]\nenable: 1\n\n[RULES]\nIN SSH(ACCEPT)\n#STATUS\tStatus: enabled/running\n"
    fake.out[(pve1, "pve_fw")] = pve_v1
    fake.out[(pve2, "pve_fw")] = pve_v1
    fake.out[(lin, "linux_fw")] = NFT_1
    fake.out[(win, "win_fw")] = "PROFILE\tDomain\tFalse\tBlock\tAllow\r\nRULE\tRDP\tInbound\tAllow\tAny\tTCP\t3389\tAny\tAny\t\tLocal\r\n"
    now_s = int(datetime.now(UTC).timestamp())
    wg_up = f"NOW\t{now_s}\nWG\twg0\tPEER1\t(none)\t203.0.113.5:51820\t10.10.0.0/24\t{now_s - 30}\t1\t1\t25\n"
    fake.out[(lin, "linux_vpn")] = wg_up
    fake.out[(win, "win_vpn")] = f"NOW\t{now_s}\r\nS2S\tAzure\tConnected\t20.1.2.3\tEnabled\r\n"

    # A TLS endpoint (self-signed, expiring in 10 days) checked from Stackpilot.
    with tempfile.TemporaryDirectory() as d:
        crt, key = _cert(Path(d), "portal.test", 10, "DNS:portal.test")
        srv, port = await _tls_server(crt, key)
        async with srv:
            r = await c.post("/api/netsec/endpoints", json={"customer_id": cid, "host": "127.0.0.1", "port": port,
                                                            "name": "Portal", "sni": "portal.test"})
            assert r.status_code == 201, r.text
            ep = r.json()
            assert (await c.post("/api/netsec/endpoints", json={"customer_id": cid, "host": "127.0.0.1",
                                                               "port": port})).status_code == 400  # duplicate
            assert (await c.post("/api/netsec/endpoints", json={"customer_id": cid, "host": "x y"})).status_code == 400
            assert (await c.post("/api/netsec/endpoints", json={"customer_id": cid, "host": "a.nl", "starttls": "smtp",
                                                               "probe_node_id": win})).status_code == 400
            res = await netsec.refresh(cid, "all")
    assert res["certificates"]["machines"] == 4 and res["certificates"]["failed"] == 0, res
    assert res["firewall"]["sources"] == 3 and res["firewall"]["changed"] == 3, res  # one per cluster, not per node
    assert not any(n == ws for n, _ in fake.calls)  # workstations are left out

    lst = (await c.get(f"/api/netsec/certificates?customer_id={cid}")).json()
    by = {x["common_name"]: x for x in lst["certificates"]}
    assert by["portal.test"]["trusted"] is False and by["portal.test"]["hostname_ok"] is True
    assert by["portal.test"]["status"] == "expiring" and by["portal.test"]["endpoint_name"] == "Portal"
    assert by["web.example.nl"]["status"] == "expired" and by["web.example.nl"]["renew"] == "certbot"
    assert by["rdgw.klant.nl"]["status"] == "critical" and by["rdgw.klant.nl"]["in_use"]
    assert by["pve1.lan"]["status"] == "expired" and by["DC01.ad.klant.nl"]["status"] == "ok"
    assert lst["counts"]["expired"] == 2
    assert (await c.get(f"/api/netsec/certificates?customer_id={cid}&status=critical")).json()["certificates"][0][
        "common_name"] == "rdgw.klant.nl"

    # A certificate that disappears from the machine goes; a renewed one keeps its history.
    fake.out[(lin, "linux_certs")] = fake.out[(lin, "linux_certs")].split("BEGIN\t/etc/pve")[0] + "RENEW\tcertbot\n"
    await certs.scan_node(lin)
    async with sessionmaker()() as db:
        rows = list((await db.execute(select(Certificate).where(Certificate.node_id == lin))).scalars())
        assert [r.common_name for r in rows] == ["web.example.nl"]
    fake.out[(lin, "linux_certs")] = LINUX_OUT.replace("AA:BB:CC", "AA:BB:DD").split("BEGIN\t/etc/pve")[0]
    await certs.scan_node(lin)
    async with sessionmaker()() as db:
        row = (await db.execute(select(Certificate).where(Certificate.node_id == lin))).scalar_one()
        assert row.fingerprint == "AABBDD" and row.renewed_at is not None and row.previous_not_after is not None
        assert row.not_after.year == 2026 and row.not_after.month == 12

    # Firewall: the first read is the baseline; a change is a diff to review.
    srcs = (await c.get(f"/api/netsec/firewall/sources?customer_id={cid}")).json()
    assert {s["kind"] for s in srcs} == {"proxmox", "linux", "windows"}
    assert all(s["unacknowledged"] == 0 for s in srcs)
    assert (await c.get(f"/api/netsec/firewall/changes?customer_id={cid}")).json() == []
    fake.out[(lin, "linux_fw")] = NFT_1.replace("tcp dport 22", "tcp dport { 22, 3306 }")
    fake.out[(lin, "linux_fw")] = fake.out[(lin, "linux_fw")].replace("1.2.3.4", "4.4.4.4")  # a ban: no change
    r = await netsec.firewall_customer(cid)
    assert r["changed"] == 1
    ch = (await c.get(f"/api/netsec/firewall/changes?customer_id={cid}&unacknowledged=true")).json()
    assert len(ch) == 1 and ch[0]["source_label"] == "ns-web01" and ch[0]["added"] == 1 and ch[0]["removed"] == 1
    v = (await c.get(f"/api/netsec/firewall/versions/{ch[0]['id']}")).json()
    assert "+\t\ttcp dport { 22, 3306 }" in v["diff"] and "3306" in v["content"]
    assert (await netsec.firewall_customer(cid))["changed"] == 0  # unchanged: no new version

    # A change made through Stackpilot is marked and acknowledged automatically.
    async with sessionmaker()() as db:
        db.add(AuditLog(action="mcp.add_firewall_rule", username="reinder", source="mcp", target="ns-pve2",
                        success=True, detail={"args": {"node": "ns-pve2"}}))
        await db.commit()
    fake.out[(pve1, "pve_fw")] = pve_v1.replace("IN SSH(ACCEPT)", "IN SSH(ACCEPT)\nIN HTTPS(ACCEPT)")
    await netsec.firewall_customer(cid)
    pv = (await c.get(f"/api/netsec/firewall/changes?customer_id={cid}")).json()
    own = next(x for x in pv if x["source_kind"] == "proxmox")
    assert own["made_by"] == "reinder · mcp.add_firewall_rule" and own["acknowledged_by"] == "stackpilot"

    # VPN: found, watched once up; down for a while is an alert.
    tun = (await c.get(f"/api/netsec/vpn?customer_id={cid}")).json()
    names = {t["name"]: t for t in tun["tunnels"]}
    assert names["wg0 · 10.10.0.0/24"]["status"] == "up" and names["wg0 · 10.10.0.0/24"]["watching"]
    assert names["Azure"]["kind"] == "windows_s2s" and names["Azure"]["watching"]
    fake.out[(lin, "linux_vpn")] = wg_up.replace(f"\t{now_s - 30}\t", f"\t{now_s - 900}\t")
    await netsec.vpn_customer(cid)
    async with sessionmaker()() as db:
        t = (await db.execute(select(VpnTunnel).where(VpnTunnel.node_id == lin))).scalar_one()
        assert t.status == "down" and t.changes[-1]["from"] == "up"
        t.status_since = datetime.now(UTC) - timedelta(minutes=30)
        await db.commit()
        tid = t.id

    # Alerts: certificates, untrusted endpoint, firewall change not reviewed, VPN down.
    async with sessionmaker()() as db:
        want = await monitoring._conditions(db, await monitoring.get_settings(db))  # noqa: SLF001
    keys = {k for k in want if k.split(":")[0] in ("cert", "cert-trust", "firewall", "vpn")}
    cert_ids = {x["id"]: x["common_name"] for x in lst["certificates"]}
    alerted = {cert_ids.get(k.split(":")[1]) for k in keys if k.startswith("cert:")}
    assert {"portal.test", "rdgw.klant.nl", "web.example.nl"} <= alerted and "DC01.ad.klant.nl" not in alerted
    assert f"vpn:{tid}" in keys and want[f"vpn:{tid}"]["severity"] == "critical"
    lin_src = next(s for s in srcs if s["kind"] == "linux")
    assert f"firewall:{lin_src['id']}" in keys
    assert any(k.startswith("cert-trust:") for k in keys)
    assert not any(want[k]["detail"].get("customer_id") == other["id"] for k in keys)

    # Nightly findings.
    async with sessionmaker()() as db:
        from mmn.models import Environment

        e = await db.get(Environment, cid)
        cf = await certs.findings(db, e, datetime.now(UTC), _f)
        nf = await netsec.findings(db, e, datetime.now(UTC), _f)
    codes = {(f["code"], f["target"]) for f in cf + nf}
    assert ("cert_expiring", "NS-DC01") in codes and ("cert_untrusted", f"127.0.0.1:{port}") in codes
    assert ("fw_changed", "ns-web01") in codes and ("fw_disabled", "NS-DC01") in codes
    assert ("vpn_down", "ns-web01") in codes
    from mmn.services import nightly

    assert {nightly.category_of(f) for f in nf} == {"netsec"}
    wf = next(f for f in cf if f["target"] == "ns-web01")  # renewed but still within the warning: certbot fails?
    assert wf["code"] == "cert_expiring" and wf["renew"] == "certbot" and wf["next_tool"] == "run_command"
    from mmn.services.reports import finding_texts

    fw = next(f for f in nf if f["code"] == "fw_changed")
    assert "is 1 keer gewijzigd" in finding_texts(fw, "nl")[1]

    # Acknowledge, unwatch, ignore: the alerts go.
    r = await c.post("/api/netsec/firewall/ack", json={"source_id": lin_src["id"], "note": "planned"})
    assert r.json()["acknowledged"] == 1
    assert (await c.patch(f"/api/netsec/vpn/{tid}", json={"watched": False})).json()["watching"] is False
    rd = by["rdgw.klant.nl"]["id"]
    assert (await c.post(f"/api/netsec/certificates/{rd}/ignore", json={"ignored": True, "note": "old"})).json()["ignored"]
    async with sessionmaker()() as db:
        want = await monitoring._conditions(db, await monitoring.get_settings(db))  # noqa: SLF001
    assert f"vpn:{tid}" not in want and f"firewall:{lin_src['id']}" not in want and f"cert:{rd}" not in want

    ov = (await c.get(f"/api/netsec/overview?customer_id={cid}")).json()
    assert ov["firewall"]["unacknowledged"] == 0 and ov["certificates"]["ignored"] == 1

    # Settings: validation.
    s = (await c.get("/api/netsec/settings")).json()
    assert s["cert_warn_days"] == 21 and "DOCKER" in s["fw_ignore"]
    bad = {**s, "fw_ignore": ["("]}
    assert (await c.put("/api/netsec/settings", json=bad)).status_code == 422
    assert (await c.put("/api/netsec/settings", json={**s, "extra_paths": ["/a b"]})).status_code == 422
    assert (await c.put("/api/netsec/settings", json={**s, "cert_critical_days": 30})).status_code == 400
    r = await c.put("/api/netsec/settings", json={**s, "extra_paths": ["/opt/app/tls.pem"], "vpn_down_minutes": 5})
    assert r.status_code == 200 and r.json()["extra_paths"] == ["/opt/app/tls.pem"]

    # MCP tools with the read and netsec scopes; a token of another customer sees nothing.
    token, _ = await _token(c, "claude-netsec", ["read", "netsec"])
    await _init(client, token)
    res, err = await _call(client, token, "list_certificates", {"customer": "Netsec Klant", "within_days": 30})
    assert err is None, err
    assert {x["common_name"] for x in res["certificates"]} >= {"portal.test", "web.example.nl"}
    res, err = await _call(client, token, "list_firewall_changes", {"customer": "Netsec Klant"})
    assert err is None and len(res["changes"]) == 2 and len(res["firewalls"]) == 3
    one = next(x for x in res["changes"] if x["source_label"] == "ns-web01")
    res, err = await _call(client, token, "get_firewall_change", {"change": one["id"]})
    assert err is None and "3306" in res["diff"]
    fake.out[(win, "win_fw")] += "RULE\tAnyDesk\tInbound\tAllow\tAny\tTCP\t7070\tAny\tAny\t\tLocal\r\n"
    await netsec.firewall_customer(cid)
    res, err = await _call(client, token, "list_firewall_changes", {"customer": "Netsec Klant", "unacknowledged": True})
    assert len(res["changes"]) == 1 and res["changes"][0]["source_label"] == "NS-DC01"
    res, err = await _call(client, token, "acknowledge_firewall_change",
                           {"customer": "Netsec Klant", "all_open": True, "note": "AnyDesk for the supplier"})
    assert err is None and res["acknowledged"] == 1
    res, err = await _call(client, token, "list_vpn_tunnels", {"customer": "Netsec Klant"})
    assert err is None and res["counts"]["down"] == 1
    res, err = await _call(client, token, "set_vpn_watch", {"customer": "Netsec Klant", "tunnel": tid, "watched": None})
    assert err is None and res["watching"] is True
    res, err = await _call(client, token, "set_certificate_ignored",
                           {"customer": "Netsec Klant", "certificate": rd, "ignored": False})
    assert err is None and res["ignored"] is False
    with tempfile.TemporaryDirectory() as d:
        crt, key = _cert(Path(d), "mail.test", 50, "DNS:mail.test")
        srv, port2 = await _tls_server(crt, key, "smtp")
        async with srv:
            res, err = await _call(client, token, "save_cert_endpoint",
                                   {"customer": "Netsec Klant", "host": "127.0.0.1", "port": port2, "starttls": "smtp",
                                    "sni": "mail.test", "name": "Mail"})
    assert err is None and res["check"]["ok"] is True, (res, err)
    assert res["check"]["certificate"]["common_name"] == "mail.test"
    res, err = await _call(client, token, "delete_cert_endpoint", {"customer": "Netsec Klant",
                                                                   "endpoint": f"127.0.0.1:{port2}"})
    assert err is None
    res, err = await _call(client, token, "refresh_netsec", {"customer": "Netsec Klant", "what": "vpn"})
    assert err is None and res["vpn"]["machines"] == 4

    ro, _ = await _token(c, "claude-netsec-ro", ["read"])
    await _init(client, ro)
    _, err = await _call(client, ro, "acknowledge_firewall_change", {"customer": "Netsec Klant", "all_open": True})
    assert err and "scope" in err
    tied, _ = await _token(c, "claude-netsec-other", ["read", "netsec"], [other["id"]])
    await _init(client, tied)
    res, err = await _call(client, tied, "list_certificates", {})
    assert err is None and res["certificates"] == []
    res, err = await _call(client, tied, "list_vpn_tunnels", {})
    assert err is None and res["tunnels"] == []

    # Deleting an endpoint removes its certificate; the old nightly address list becomes endpoints.
    assert (await c.delete(f"/api/netsec/endpoints/{ep['id']}")).status_code == 204
    r = await c.put(f"/api/nightly/customers/{cid}", json={"enabled": True, "cert_endpoints": ["shop.klant.nl:8443"]})
    assert r.status_code == 200
    eps = (await c.get(f"/api/netsec/endpoints?customer_id={cid}")).json()
    assert [(x["host"], x["port"], x["source"]) for x in eps] == [("shop.klant.nl", 8443, "nightly")]
    r = await c.put(f"/api/nightly/customers/{cid}", json={"enabled": True, "cert_endpoints": []})
    assert (await c.get(f"/api/netsec/endpoints?customer_id={cid}")).json() == []
    async with sessionmaker()() as db:
        n = len(list((await db.execute(select(FirewallVersion).where(FirewallVersion.customer_id == cid))).scalars()))
        assert n >= 6

    # Clean up: other tests count the machines of all customers.
    from sqlalchemy import delete

    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.environment_id == cid))
        await db.commit()
    assert (await c.delete(f"/api/customers/{cid}")).status_code == 204


async def test_uptime_checks_become_endpoints(admin_client):
    from mmn.db import sessionmaker
    from mmn.models import CertEndpoint

    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Netsec Uptime"})).json()
    r = await c.post("/api/uptime/checks", json={"customer_id": cust["id"], "name": "Shop", "kind": "http",
                                                 "target": "https://shop.example.invalid/", "enabled": False})
    assert r.status_code == 201, r.text
    chk = r.json()
    async with sessionmaker()() as db:
        await certs.sync_uptime_endpoints(db)
        await db.commit()
    eps = (await c.get(f"/api/netsec/endpoints?customer_id={cust['id']}")).json()
    assert len(eps) == 1 and eps[0]["source"] == "uptime" and eps[0]["host"] == "shop.example.invalid"
    assert eps[0]["enabled"] is False
    assert (await c.delete(f"/api/netsec/endpoints/{eps[0]['id']}")).status_code == 400
    assert (await c.delete(f"/api/uptime/checks/{chk['id']}")).status_code == 204
    async with sessionmaker()() as db:
        await certs.sync_uptime_endpoints(db)
        await db.commit()
        from sqlalchemy import select

        assert not list((await db.execute(select(CertEndpoint).where(CertEndpoint.customer_id == cust["id"]))).scalars())
