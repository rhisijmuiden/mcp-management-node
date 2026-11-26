"""v0.29.0: UniFi Fabric keys, consoles linked through the cloud connector, joined/left consoles, counted once."""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import urlsplit

import pytest
from test_mcp import _init, _rpc
from test_unifi import FakeUnifi, _env

from mmn.db import sessionmaker
from mmn.services import monitoring
from mmn.services.unifi import transport
from mmn.services.unifi.service import _host_out
from mmn.services.unifi.transport import Target


def _host(hid, name, apps=("network",), kind="console", state="connected"):
    return {"id": hid, "type": kind, "ipAddress": "203.0.113.1", "owner": True, "isBlocked": False,
            "reportedState": {"name": name, "state": state, "version": "5.0.12",
                              "controllers": [{"name": a, "installState": "installed", "state": "active"}
                                              for a in apps]}}


class FakeCloud(FakeUnifi):
    """api.ui.com: Site Manager lists, plus the cloud connector that relays to a console's Network API."""

    def __init__(self):
        super().__init__()
        self.hosts = [_host("h-hq", "HQ Gateway"), _host("h-br", "Branch Gateway"),
                      _host("h-nvr", "Camera NVR", apps=("protect",)),
                      _host("h-self", "Old controller", kind="network-server")]
        self.down: set[str] = set()

    def cloud(self, path, query):
        if path == "/v1/hosts":
            return 200, {"data": self.hosts, "httpStatusCode": 200}
        if path == "/v1/sites":
            return 200, {"data": [{"siteId": f"s-{h['id']}", "hostId": h["id"], "meta": {"desc": "Default"},
                                   "statistics": {"counts": {"totalDevice": 2, "offlineDevice": 0},
                                                  "ispInfo": {"name": "KPN"}, "percentages": {"wanUptime": 99.9}}}
                                  for h in self.hosts]}
        if path == "/v1/devices":
            return 200, {"data": [{"hostId": h["id"], "hostName": h["reportedState"]["name"],
                                   "devices": [{"id": f"d-{h['id']}", "name": f"AP {h['id']}", "model": "U7-Pro",
                                                "status": "online", "productLine": "network"}]}
                                  for h in self.hosts]}
        return 404, {"message": "not found"}

    async def direct(self, t: Target, method, url, body, headers, timeout):
        parts = urlsplit(url)
        if parts.hostname != "api.ui.com":
            return await super().direct(t, method, url, body, headers, timeout)
        self.calls.append((method, parts.path, None, headers))
        key = {k.lower(): v for k, v in headers.items()}.get("x-api-key")
        if key == "not-owner":
            return 403, json.dumps({"message": "user is not the owner of this host"})
        if key != "fabric-key":
            return 401, json.dumps({"message": "unauthorized"})
        if parts.path.startswith("/v1/connector/consoles/"):
            host, rest = parts.path[len("/v1/connector/consoles/"):].split("/", 1)
            if host in self.down:
                return 502, "{}"
            status, res = self.network(method, "/proxy/network/integration/" + rest.split("network/integration/", 1)[1],
                                       json.loads(body) if body else None)
            return status, json.dumps(res)
        status, res = self.cloud(parts.path, parts.query)
        return status, json.dumps(res)


@pytest.fixture
def cloud(monkeypatch):
    f = FakeCloud()
    monkeypatch.setattr(transport, "_direct", f.direct)
    return f


def test_host_apps_and_connector():
    h = _host_out(_host("a", "A", apps=("network", "access")))
    assert h["apps"] == ["access", "network"] and h["network"] and h["connector"]
    assert not _host_out(_host("b", "B", apps=("protect",)))["network"]
    assert not _host_out(_host("c", "C", kind="network-server"))["connector"]
    # Unknown apps (no controllers reported): assume the Network app is there.
    assert _host_out({"id": "d", "type": "console", "reportedState": {"name": "D"}})["network"]


async def _clean(c, prefix):
    for x in (await c.get("/api/unifi/connections")).json():
        if x["name"].startswith(prefix) and x["kind"] == "network":
            assert (await c.delete(f"/api/unifi/connections/{x['id']}")).status_code == 204
    for x in (await c.get("/api/unifi/connections")).json():
        if x["name"].startswith(prefix):
            assert (await c.delete(f"/api/unifi/connections/{x['id']}")).status_code == 204


async def test_fabric_auto_link_dedupe_and_events(admin_client, cloud):
    c = admin_client
    env = await _env(c, "Fabric Klant")
    await _clean(c, "Fabric")
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "Fabric Noord",
                                                     "kind": "site_manager", "api_key": "fabric-key",
                                                     "fabric": True})
    assert r.status_code == 201, r.text
    fab = r.json()
    assert fab["fabric"] and fab["auto_link"] and fab["status"] == "ok"

    # The first poll links the two consoles with the Network app; NVR and the self-hosted controller are skipped.
    conns = {x["name"]: x for x in (await c.get("/api/unifi/connections")).json()}
    hq, _br = conns["Fabric Noord · HQ Gateway"], conns["Fabric Noord · Branch Gateway"]
    assert hq["transport"] == "cloud" and hq["console_id"] == "h-hq" and hq["cloud_connection_id"] == fab["id"]
    assert hq["status"] == "ok" and hq["totals"]["devices"] == 3 and hq["environment_id"] == env
    assert any("/v1/connector/consoles/h-hq/network/integration/v1/sites" in x[1] for x in cloud.calls)

    fabs = (await c.get(f"/api/unifi/fabrics?environment_id={env}")).json()
    assert len(fabs) == 1
    f = fabs[0]
    assert f["totals"] == {"consoles": 4, "linked": 2, "unlinked": 0, "sites": 4, "devices": 4, "offline": 0}
    reasons = {h["name"]: h["reason"] for h in f["hosts"]}
    assert reasons["Camera NVR"] == "no_network_app" and reasons["Old controller"] == "self_hosted"
    assert {h["name"]: (h["linked"] or {}).get("name") for h in f["hosts"]}["HQ Gateway"] == hq["name"]
    hq_host = next(h for h in f["hosts"] if h["id"] == "h-hq")
    assert hq_host["sites"][0]["isp"] == "KPN"

    # Counted once: the cloud devices of linked consoles are left out (they come from the Network connection).
    ov = (await c.get(f"/api/unifi/network/overview?environment_id={env}")).json()
    cloud_devs = [d for d in ov["devices"] if d["source"] == "cloud"]
    assert sorted(d["id"] for d in cloud_devs) == ["d-h-nvr", "d-h-self"]
    assert sum(1 for s in ov["sites"] if s.get("cloud")) == 2
    cust = (await c.get(f"/api/customers/{env}/overview")).json()
    net = cust["totals"]["network"]
    assert net["devices"] == 3 + 3 + 2 and net["fabrics"][0]["linked"] == 2

    # A new console joins the Fabric: an event, and it is linked by itself. One leaves: a warning.
    cloud.hosts = [h for h in cloud.hosts if h["id"] != "h-br"] + [_host("h-new", "New Shop")]
    r = await c.post(f"/api/unifi/fabrics/{fab['id']}/sync")
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["linked"] == 2  # HQ + New Shop (Branch left the Fabric)
    ev = (await c.get(f"/api/unifi/events?connection_id={fab['id']}")).json()
    kinds = {(e["kind"], e["severity"]) for e in ev}
    assert ("console_joined", "info") in kinds and ("console_left", "warning") in kinds
    assert ("console_linked", "info") in kinds
    assert any(x["name"] == "Fabric Noord · New Shop" for x in (await c.get("/api/unifi/connections")).json())

    # Auto-link off: a new console waits for a click, and the monitor asks for it.
    r = await c.patch(f"/api/unifi/connections/{fab['id']}", json={"auto_link": False})
    assert r.status_code == 200 and r.json()["auto_link"] is False
    cloud.hosts.append(_host("h-late", "Late Office"))
    f = (await c.post(f"/api/unifi/fabrics/{fab['id']}/sync")).json()
    late = next(h for h in f["hosts"] if h["id"] == "h-late")
    assert late["linkable"] and late["linked"] is None and f["totals"]["unlinked"] == 1
    async with sessionmaker()() as db:
        cfg = await monitoring.get_settings(db)
        want = await monitoring._unifi_conditions(db, cfg, dt.datetime.now(dt.UTC))
    assert f"unifi:{fab['id']}:fabric:h-late" in want
    r = await c.post(f"/api/unifi/fabrics/{fab['id']}/link", json={"host_ids": ["h-late", "h-nvr", "h-zzz"]})
    assert r.status_code == 200, r.text
    res = r.json()
    assert [x["console_id"] for x in res["linked"]] == ["h-late"]
    assert {x["id"]: x["reason"] for x in res["skipped"]} == {"h-nvr": "no_network_app", "h-zzz": "not_in_fabric"}
    # Linking again does nothing.
    res = (await c.post(f"/api/unifi/fabrics/{fab['id']}/link", json={})).json()
    assert res["linked"] == []

    # The connector cannot reach a console: a clear error on that console's connection.
    cloud.down.add("h-hq")
    r = await c.post(f"/api/unifi/connections/{hq['id']}/refresh")
    assert r.status_code == 502 and "cloud connector" in r.json()["detail"]
    log = (await c.get("/api/settings/audit")).json()
    assert any(e["action"] == "unifi.fabric.link" for e in log)


async def test_fabric_validation_and_not_owner(admin_client, cloud):
    c = admin_client
    env = await _env(c, "Fabric Klant")
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "Fabric bad", "kind": "network",
                                                     "base_url": "10.0.0.1", "api_key": "net-key", "fabric": True,
                                                     "tls_mode": "insecure"})
    assert r.status_code == 201 and r.json()["fabric"] is False  # ignored for a console
    assert (await c.delete(f"/api/unifi/connections/{r.json()['id']}")).status_code == 204
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "Fabric owner",
                                                     "kind": "site_manager", "api_key": "not-owner", "fabric": True})
    assert r.status_code == 201
    conn = r.json()
    assert conn["status"] == "error" and "owner" in conn["last_error"]
    r = await c.post(f"/api/unifi/fabrics/{conn['id']}/link", json={})
    assert r.status_code == 400 and "no consoles" in r.json()["detail"]
    assert (await c.delete(f"/api/unifi/connections/{conn['id']}")).status_code == 204


async def test_fabric_mcp(admin_client, client, cloud):
    c = admin_client
    env = await _env(c, "Fabric MCP")
    await _clean(c, "MCP Fabric")
    cloud.hosts = [_host("m-hq", "HQ Gateway"), _host("m-two", "Second Site")]
    r = await c.post("/api/unifi/connections", json={"environment_id": env, "name": "MCP Fabric",
                                                     "kind": "site_manager", "api_key": "fabric-key",
                                                     "fabric": True, "auto_link": False})
    assert r.status_code == 201, r.text
    r = await c.post("/api/tokens", json={"name": "claude-fabric", "scopes": ["unifi", "unifi_admin"]})
    tok = r.json()["token"]
    await _init(client, tok)

    async def call(name, args, id_=10):
        res = (await _rpc(client, tok, "tools/call", {"name": name, "arguments": args}, id_)).json()["result"]
        data = res.get("structuredContent") or json.loads(res["content"][0]["text"]) if not res.get("isError") else res
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return res.get("isError"), data

    err, data = await call("list_unifi_fabrics", {"customer": "Fabric MCP"})
    assert not err, data
    assert data[0]["totals"]["unlinked"] == 2
    err, data = await call("sync_unifi_fabric", {"fabric": "MCP Fabric", "consoles": ["HQ Gateway"]}, 11)
    assert not err, data
    assert [x["name"] for x in data["linked"]] == ["MCP Fabric · HQ Gateway"]
    assert data["totals"]["linked"] == 1
