"""Phase 6: UniFi Network, Site Manager and Access."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import tempfile
from urllib.parse import parse_qs, urlsplit

import pytest
from test_mcp import _init, _rpc

from mmn.services.unifi import transport
from mmn.services.unifi.transport import Target, UnifiError

SITE = "site-1"


class FakeUnifi:
    """Answers the Network Integration API and the Access developer API like a console would."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, dict]] = []
        self.ap_state = "ONLINE"
        self.policies = [
            {"id": "p2", "name": "Block hotspot to internal", "enabled": True, "index": 10, "action": {"type": "BLOCK"},
             "source": {"zoneId": "z-hot"}, "destination": {"zoneId": "z-int"}, "metadata": {"origin": "SYSTEM_DEFINED"}},
        ]

    def devices(self):
        return [
            {"id": "gw", "name": "Gateway", "model": "UCG-Ultra", "macAddress": "aa:00:00:00:00:01",
             "ipAddress": "10.0.0.1", "state": "ONLINE", "features": ["switching"], "interfaces": ["ports"]},
            {"id": "sw", "name": "Switch Core", "model": "USW-24-PoE", "macAddress": "aa:00:00:00:00:02",
             "ipAddress": "10.0.0.2", "state": "ONLINE", "features": ["switching"], "interfaces": ["ports"]},
            {"id": "ap", "name": "AP Office", "model": "U7-Pro", "macAddress": "aa:00:00:00:00:03",
             "ipAddress": "10.0.0.3", "state": self.ap_state, "features": ["accessPoint"], "interfaces": ["radios"]},
        ]

    def detail(self, did):
        d = next(x for x in self.devices() if x["id"] == did)
        extra = {"gw": {"firmwareVersion": "4.1.13", "firmwareUpdatable": False},
                 "sw": {"firmwareVersion": "7.1.26", "firmwareUpdatable": False, "uplink": {"deviceId": "gw"},
                        "interfaces": {"ports": [{"idx": 1, "state": "UP", "speedMbps": 1000, "maxSpeedMbps": 1000,
                                                  "poe": {"enabled": True, "state": "UP"}},
                                                 {"idx": 2, "state": "DOWN", "maxSpeedMbps": 1000}]}},
                 "ap": {"firmwareVersion": "8.0.24", "firmwareUpdatable": True, "uplink": {"deviceId": "sw"},
                        "interfaces": {"radios": [{"wlanStandard": "802.11be", "frequencyGHz": 5, "channel": 36}]}}}
        return {**d, **extra[did], "features": {f: {} for f in d["features"]},
                "interfaces": extra[did].get("interfaces", {})}

    def page(self, data):
        return {"offset": 0, "limit": 200, "count": len(data), "totalCount": len(data), "data": data}

    def network(self, method, path, body):
        p = path.split("/proxy/network/integration/", 1)[1]
        if p == "v1/info":
            return 200, {"applicationVersion": "10.1.84"}
        if p == "v1/sites":
            return 200, self.page([{"id": SITE, "internalReference": "default", "name": "Default"}])
        base = f"v1/sites/{SITE}/"
        if not p.startswith(base):
            return 404, {"message": "not found"}
        rest = p[len(base):]
        if rest == "devices":
            return 200, self.page(self.devices())
        if rest.startswith("devices/") and rest.endswith("/statistics/latest"):
            return 200, {"uptimeSec": 3600, "cpuUtilizationPct": 4.5, "memoryUtilizationPct": 40,
                         "uplink": {"txRateBps": 1000, "rxRateBps": 2000}}
        if rest.startswith("devices/") and rest.endswith("/actions"):
            return 200, {}
        if rest.startswith("devices/"):
            return 200, self.detail(rest.split("/")[1])
        if rest == "clients":
            return 200, self.page([
                {"type": "WIRELESS", "id": "c1", "name": "laptop-anna", "macAddress": "bb:00:00:00:00:01",
                 "ipAddress": "10.0.0.50", "uplinkDeviceId": "ap", "connectedAt": "2026-11-22T01:00:00Z",
                 "access": {"type": "DEFAULT"}},
                {"type": "WIRED", "id": "c2", "name": "nas", "macAddress": "bb:00:00:00:00:02",
                 "ipAddress": "10.0.0.10", "uplinkDeviceId": "sw", "access": {"type": "DEFAULT"}},
                {"type": "WIRELESS", "id": "c3", "name": "guest-phone", "macAddress": "bb:00:00:00:00:03",
                 "ipAddress": None, "uplinkDeviceId": "ap", "access": {"type": "GUEST", "authorized": False}},
            ])
        if rest.startswith("clients/") and rest.endswith("/actions"):
            return 200, {}
        if rest == "networks":
            return 200, self.page([
                {"id": "n-lan", "name": "Default", "vlanId": 1, "enabled": True, "management": "GATEWAY", "default": True},
                {"id": "n-guest", "name": "Guest", "vlanId": 60, "enabled": True, "management": "GATEWAY"},
            ])
        if rest == "wifi/broadcasts":
            return 200, self.page([
                {"id": "w1", "name": "Office", "enabled": True, "type": "STANDARD", "network": {"type": "NATIVE"},
                 "securityConfiguration": {"type": "WPA2_WPA3_PERSONAL"}, "hideName": False,
                 "clientIsolationEnabled": False,
                 "clientFilteringPolicy": {"action": "ALLOW", "macAddressFilter": ["BB:00:00:00:00:01"]}},
                {"id": "w2", "name": "Office-Guest", "enabled": True, "type": "STANDARD",
                 "network": {"type": "SPECIFIC", "networkId": "n-guest"},
                 "securityConfiguration": {"type": "OPEN"}, "hideName": False, "clientIsolationEnabled": True},
            ])
        if rest.startswith("wifi/broadcasts/"):
            return 200, {"id": "w1", "name": "Office", "enabled": True, "metadata": {"origin": "USER_DEFINED"},
                         "type": "STANDARD", "securityConfiguration": {"type": "WPA2_PERSONAL"}, "hideName": False,
                         "clientIsolationEnabled": False, "multicastToUnicastConversionEnabled": False,
                         "uapsdEnabled": False}
        if rest == "firewall/zones":
            return 200, self.page([{"id": "z-int", "name": "Internal", "networkIds": ["n-lan"]},
                                   {"id": "z-hot", "name": "Hotspot", "networkIds": ["n-guest"]}])
        if rest == "firewall/policies":
            return 200, self.page(self.policies)
        return 404, {"message": "not found"}

    def access(self, method, path, body, query):
        p = path.split("/api/v1/developer/", 1)[1]
        ok = lambda data: (200, {"code": "SUCCESS", "msg": "success", "data": data})  # noqa: E731
        if p == "doors":
            return ok([{"id": "d1", "name": "Server room", "full_name": "HQ - 1F - Server room", "type": "door",
                        "is_bind_hub": True, "door_lock_relay_status": "lock", "door_position_status": "close"},
                       {"id": "d2", "name": "Front door", "full_name": "HQ - GF - Front door", "type": "door",
                        "is_bind_hub": True, "door_lock_relay_status": "unlock", "door_position_status": "open"}])
        if p == "doors/d1/unlock" and method == "PUT":
            return ok(None)
        if p == "doors/d1/lock_rule":
            return ok({"type": body.get("type") if body else "schedule"})
        if p == "access_policies/schedules":
            return ok([{"id": "s1", "name": "Office hours"}])
        if p == "access_policies":
            return ok([])
        if p == "system/logs":
            hit = lambda door, who, t: {  # noqa: E731
                "@timestamp": t, "_source": {"actor": {"display_name": who, "type": "user"},
                                             "event": {"type": "access.door.unlock", "result": "ACCESS",
                                                       "display_message": "Access Granted (NFC)"},
                                             "authentication": {"credential_provider": "NFC"},
                                             "target": [{"type": "door", "id": door[0], "display_name": door[1]}]}}
            return ok({"hits": [hit(("d1", "Server room"), "Piet Jansen", "2026-11-21T23:12:00Z"),
                                hit(("d2", "Front door"), "Anna de Vries", "2026-11-21T08:01:00Z")]})
        return 404, {"code": "CODE_NOT_EXISTS", "msg": "not found"}

    async def direct(self, t: Target, method, url, body, headers, timeout):
        parts = urlsplit(url)
        data = json.loads(body) if body else None
        self.calls.append((method, parts.path, data, headers))
        if "/proxy/network/integration/" in parts.path:
            if headers.get("X-API-KEY") != "net-key" and headers.get("X-API-KEY") != "ro-key":
                return 401, json.dumps({"message": "unauthorized"})
            status, res = self.network(method, parts.path, data)
        elif "/api/v1/developer/" in parts.path:
            if headers.get("Authorization") != "Bearer access-key":
                return 401, "{}"
            status, res = self.access(method, parts.path, data, parse_qs(parts.query))
        else:
            status, res = 404, {}
        return status, json.dumps(res)


@pytest.fixture
def fake(monkeypatch):
    f = FakeUnifi()
    monkeypatch.setattr(transport, "_direct", f.direct)
    return f


async def _env(c, name="UniFi Klant"):
    envs = (await c.get("/api/environments")).json()
    e = next((x for x in envs if x["name"] == name), None)
    if e:
        return e["id"]
    r = await c.post("/api/environments", json={"name": name, "kind": "customer"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _conn(c, env, name, kind, url, key, **kw):
    existing = [x for x in (await c.get("/api/unifi/connections")).json() if x["name"] == name]
    for x in existing:
        assert (await c.delete(f"/api/unifi/connections/{x['id']}")).status_code == 204
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": name, "kind": kind,
                                                     "base_url": url, "api_key": key, "tls_mode": "insecure", **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_network_connection_overview_events_and_actions(admin_client, fake):
    c = admin_client
    env = await _env(c)
    con = await _conn(c, env, "Klant net", "network", "10.9.0.1", "net-key")
    assert con["base_url"] == "https://10.9.0.1/proxy/network/integration"
    assert con["status"] == "ok" and con["version"] == "10.1.84" and con["has_key"]
    assert "net-key" not in json.dumps(con)
    assert con["totals"] == {"sites": 1, "devices": 3, "offline": 0, "firmware": 1, "clients": 3, "aps": 1,
                             "switches": 1, "gateways": 1}

    from sqlalchemy import select

    from mmn.db import sessionmaker
    from mmn.models import UnifiConnection

    async with sessionmaker()() as db:
        row = (await db.execute(select(UnifiConnection).where(UnifiConnection.id == con["id"]))).scalar_one()
        assert row.api_key_enc and "net-key" not in row.api_key_enc

    ov = (await c.get("/api/unifi/network/overview")).json()
    devs = {d["name"]: d for d in ov["devices"] if d["connection_id"] == con["id"]}
    assert devs["Gateway"]["type"] == "gateway" and devs["AP Office"]["type"] == "ap"
    assert devs["AP Office"]["uplink_name"] == "Switch Core" and devs["AP Office"]["clients"] == 2
    assert devs["AP Office"]["firmware_updatable"] is True
    wifi = {w["name"]: w for w in ov["wifi"] if w["connection_id"] == con["id"]}
    assert wifi["Office-Guest"]["vlan"] == 60 and wifi["Office-Guest"]["guest"] and wifi["Office"]["vlan"] == 1

    fw = (await c.get("/api/unifi/network/firmware")).json()
    assert [f["device"] for f in fw if f["connection"] == "Klant net"] == ["AP Office"]

    # The AP goes offline: an event, and an alert from monitoring.
    fake.ap_state = "OFFLINE"
    assert (await c.post(f"/api/unifi/connections/{con['id']}/refresh")).status_code == 200
    evs = (await c.get(f"/api/unifi/events?connection_id={con['id']}")).json()
    assert evs[0]["kind"] == "device_offline" and evs[0]["subject"] == "AP Office"
    from mmn.services import monitoring

    await monitoring.evaluate()
    async with sessionmaker()() as db:
        al = [a for a in await monitoring.list_alerts(db, open_only=True) if a["kind"] == "unifi"]
    assert any("AP Office" in a["message"] and a["node"] == "UniFi Klant net" for a in al)
    fake.ap_state = "ONLINE"
    await c.post(f"/api/unifi/connections/{con['id']}/refresh")
    evs = (await c.get(f"/api/unifi/events?connection_id={con['id']}")).json()
    assert evs[0]["kind"] == "device_online"
    await monitoring.evaluate()

    # Device detail (live) with ports and statistics.
    d = (await c.get(f"/api/unifi/network/{con['id']}/sites/{SITE}/devices/sw")).json()
    assert d["ports_up"] == 1 and d["ports_total"] == 2 and d["stats"]["cpu_pct"] == 4.5
    assert [x["name"] for x in d["client_list"]] == ["nas"]

    # Why can't this laptop get on WiFi? A MAC that is not on the allow list is refused.
    r = (await c.get(f"/api/unifi/network/{con['id']}/sites/{SITE}/diagnose-client?q=cc:00:00:00:00:09")).json()
    assert not r["matches"]
    assert any("NOT on the list" in f["text"] and f["severity"] == "critical" for f in r["findings"])
    r = (await c.get(f"/api/unifi/network/{con['id']}/sites/{SITE}/diagnose-client?q=guest-phone")).json()
    texts = " ".join(f["text"] for f in r["findings"])
    assert "no IP address" in texts and "not authorized" in texts

    # Is guest isolated from the LAN?
    r = (await c.get(f"/api/unifi/network/{con['id']}/sites/{SITE}/guest-isolation")).json()
    assert r["verdict"] == "isolated", r
    assert r["guest_networks"][0]["ssids"] == [{"name": "Office-Guest", "client_isolation": True}]
    fake.policies.insert(0, {"id": "p1", "name": "Allow guests to printer", "enabled": True, "index": 1,
                             "action": {"type": "ALLOW"}, "source": {"zoneId": "z-hot"},
                             "destination": {"zoneId": "z-int", "trafficFilter": {"type": "IP_ADDRESS"}}})
    r = (await c.get(f"/api/unifi/network/{con['id']}/sites/{SITE}/guest-isolation")).json()
    assert r["verdict"] == "partial" and "printer" in r["guest_networks"][0]["to"][0]["why"]

    # Actions: restart and port power-cycle are logged.
    r = await c.post(f"/api/unifi/network/{con['id']}/sites/{SITE}/devices/ap/restart", json={"reason": "test"})
    assert r.status_code == 200, r.text
    assert ("POST", f"/proxy/network/integration/v1/sites/{SITE}/devices/ap/actions", {"action": "RESTART"}) in \
        [x[:3] for x in fake.calls]
    r = await c.post(f"/api/unifi/network/{con['id']}/sites/{SITE}/devices/sw/ports/1/power-cycle", json={})
    assert r.status_code == 200
    log = (await c.get("/api/settings/audit")).json()
    assert any(e["action"] == "unifi.device.restart" and e["target"] == "Klant net: ap" for e in log)

    # A read-only connection refuses actions without calling UniFi.
    ro = await _conn(c, env, "Klant net RO", "network", "https://10.9.0.5/", "ro-key", read_only=True)
    n = len(fake.calls)
    r = await c.post(f"/api/unifi/network/{ro['id']}/sites/{SITE}/devices/ap/restart", json={})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert len(fake.calls) == n

    # A wrong key: status error with a clear message.
    r = await c.patch(f"/api/unifi/connections/{ro['id']}", json={"api_key": "wrong"})
    assert r.status_code == 200
    r = await c.post(f"/api/unifi/connections/{ro['id']}/test")
    assert r.json()["ok"] is False and "rejected" in r.json()["error"]


async def test_validation(admin_client, fake):
    c = admin_client
    env = await _env(c)
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "x", "kind": "network",
                                                     "base_url": "10.0.0.1"})
    assert r.status_code == 400 and "API key" in r.json()["detail"]
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "x", "kind": "network",
                                                     "transport": "agent", "base_url": "10.0.0.1", "api_key": "k"})
    assert r.status_code == 400 and "agent" in r.json()["detail"]
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "x", "kind": "access",
                                                     "base_url": "10.0.0.1", "api_key": "k", "tls_mode": "pin",
                                                     "tls_fingerprint": "abc"})
    assert r.status_code == 400 and "SHA-256" in r.json()["detail"]
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "x", "kind": "network",
                                                     "transport": "cloud", "api_key": "k"})
    assert r.status_code == 400 and "Site Manager" in r.json()["detail"]
    # An environment with UniFi connections cannot be removed.
    await _conn(c, env, "Klant blocker", "network", "10.9.0.1", "net-key")
    r = await c.delete(f"/api/environments/{env}")
    assert r.status_code == 400 and "UniFi" in r.json()["detail"]


async def test_access_and_mcp(admin_client, client, fake):
    c = admin_client
    env = await _env(c)
    acc = await _conn(c, env, "Klant doors", "access", "10.9.0.2", "access-key")
    assert acc["base_url"] == "https://10.9.0.2:12445/api/v1/developer" and acc["status"] == "ok"
    assert acc["totals"] == {"doors": 2, "unlocked": 1, "open": 1}
    doors = (await c.get(f"/api/unifi/access/{acc['id']}/doors")).json()
    assert {d["name"]: d["lock"] for d in doors} == {"Server room": "lock", "Front door": "unlock"}
    logs = (await c.get(f"/api/unifi/access/{acc['id']}/logs?door=server")).json()
    assert [(x["actor"], x["method"]) for x in logs] == [("Piet Jansen", "NFC")]
    r = await c.post(f"/api/unifi/access/{acc['id']}/doors/d1/unlock", json={"reason": "delivery"})
    assert r.status_code == 200, r.text
    put = [x for x in fake.calls if x[1].endswith("/doors/d1/unlock")][-1]
    assert put[0] == "PUT" and "admin" in put[2]["actor_name"]
    r = await c.put(f"/api/unifi/access/{acc['id']}/emergency", json={"lockdown": True, "confirm": "nope"})
    assert r.status_code == 400

    # MCP: a token with the UniFi scopes.
    r = await c.post("/api/tokens", json={"name": "claude-unifi", "scopes": ["unifi", "doors"]})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    await _init(client, tok)

    async def call(name, args, id_=10):
        res = (await _rpc(client, tok, "tools/call", {"name": name, "arguments": args}, id_)).json()["result"]
        data = res.get("structuredContent") or json.loads(res["content"][0]["text"]) if not res.get("isError") else res
        return res.get("isError"), data

    err, data = await call("get_access_log", {"connection": "Klant doors", "door": "server room",
                                              "since": "2026-11-21T18:00:00+01:00"})
    assert not err, data
    rows = data["result"] if isinstance(data, dict) and "result" in data else data
    assert rows[0]["actor"] == "Piet Jansen"
    err, data = await call("unlock_door", {"connection": "Klant doors", "door": "server", "confirm": "server",
                                           "reason": "x"}, 11)
    assert err and "confirm must equal" in json.dumps(data)
    err, data = await call("unlock_door", {"connection": "Klant doors", "door": "server", "confirm": "Server room",
                                           "reason": "IT visit"}, 12)
    assert not err, data
    err, data = await call("diagnose_wifi_client", {"connection": "Klant blocker", "client": "laptop-anna"}, 13)
    assert err is not None
    # No unifi_admin scope: restarting a device is refused.
    err, data = await call("restart_unifi_device", {"connection": "Klant blocker", "device": "AP Office",
                                                    "confirm": "AP Office", "reason": "x"}, 14)
    assert err and "unifi_admin" in json.dumps(data)
    log = (await c.get("/api/settings/audit")).json()
    assert any(e["action"] == "mcp.unlock_door" and e["target"] == "door server" and e["success"] for e in log)


# ------------------------------------------------------------------ TLS pinning (real handshake)
def _self_signed():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unifi.local")])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    open(f"{d}/c.pem", "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(f"{d}/k.pem", "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    import hashlib

    return f"{d}/c.pem", f"{d}/k.pem", hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def test_tls_pinning():
    cert, key, fp = _self_signed()
    seen: list[bytes] = []

    async def handle(reader, writer):
        data = await reader.read(65536)
        seen.append(data)
        body = b'{"applicationVersion":"9.9"}'
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = srv.sockets[0].getsockname()[1]
    base = f"https://127.0.0.1:{port}/proxy/network/integration"
    try:
        t = Target(base_url=base, headers={"X-API-KEY": "secret"}, tls_mode="pin", fingerprint=fp.upper())
        assert (await transport.request(t, "GET", "v1/info"))["applicationVersion"] == "9.9"
        assert b"secret" in seen[-1]
        n = len(seen)
        bad = Target(base_url=base, headers={"X-API-KEY": "secret"}, tls_mode="pin", fingerprint="ab" * 32)
        with pytest.raises(UnifiError) as ei:
            await transport.request(bad, "GET", "v1/info")
        assert ei.value.code == "tls"
        assert all(b"secret" not in s for s in seen[n:])  # the key never left
        with pytest.raises(UnifiError) as ei:
            await transport.request(Target(base_url=base, headers={}), "GET", "v1/info")
        assert ei.value.code == "tls"
        info = await transport.fetch_certificate(f"https://127.0.0.1:{port}/")
        assert info["sha256"] == fp and info["self_signed"]
    finally:
        srv.close()
