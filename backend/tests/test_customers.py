"""Phase 7: customers central, customer isolation for MCP tokens and the customer switcher."""

from __future__ import annotations

import json

from test_mcp import _init, _rpc


async def _machine(env_id: str, name: str, kind: str = "linux", role: str = "server", **extra) -> str:
    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        n = Node(environment_id=env_id, name=name, kind=kind, role=role, capabilities=[], info={}, metrics={},
                 updates=extra.pop("updates", {}), governor={}, **extra)
        db.add(n)
        await db.commit()
        return n.id


async def _call(client, token, name, args, id_=10):
    res = (await _rpc(client, token, "tools/call", {"name": name, "arguments": args}, id_)).json()["result"]
    if res.get("isError"):
        return None, json.dumps(res)
    data = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    if isinstance(data, dict) and list(data) == ["result"]:
        data = data["result"]
    return data, None


async def test_customers_and_isolation(admin_client, client):
    c = admin_client
    r = await c.post("/api/customers", json={
        "name": "Van Dijk Accountants", "contact_name": "J. van Dijk", "contact_email": "jan@vandijk.example",
        "maintenance_window": "Tue 22:00-02:00", "sla": "Next business day",
        "notes": "Accounting software on vd-sql01 must not restart during office hours (08:00-18:00).",
        "contacts": [{"name": "Piet", "role": "Office manager", "phone": "+31 6 12345678"}]})
    assert r.status_code == 201, r.text
    vd = r.json()
    assert vd["maintenance_window"] == "Tue 22:00-02:00" and vd["contacts"][0]["name"] == "Piet"
    other = (await c.post("/api/customers", json={"name": "Bakkerij Jansen"})).json()
    assert (await c.post("/api/customers", json={"name": "bakkerij jansen"})).status_code == 409

    sql = await _machine(vd["id"], "vd-sql01", "windows",
                         updates={"packages": [{"name": "KB5", "new": "1"}], "reboot_required": True})
    pc = await _machine(vd["id"], "PC-RECEPTIE", "windows", role="workstation")
    await _machine(vd["id"], "pve-vd01", "proxmox")
    secret = await _machine(other["id"], "jansen-nas", "linux")

    # Customer list and overview.
    rows = (await c.get("/api/customers")).json()
    row = next(x for x in rows if x["id"] == vd["id"])
    assert row["machines"] == 3 and row["counts"] == {"proxmox": 1, "servers": 1, "workstations": 1}
    assert row["status"] == "attention"  # machines are offline in the test
    ov = (await c.get(f"/api/customers/{vd['id']}/overview")).json()
    assert {m["name"] for m in ov["machines"]} == {"vd-sql01", "PC-RECEPTIE", "pve-vd01"}
    assert ov["totals"]["workstations"]["total"] == 1 and ov["totals"]["servers"]["updates"] == 1
    ctx = (await c.get(f"/api/customers/{vd['id']}/context")).json()
    assert "office hours" in ctx["markdown"] and "Tue 22:00-02:00" in ctx["markdown"]

    # A machine can be marked as a workstation.
    r = await c.patch(f"/api/nodes/{sql}", json={"role": "workstation"})
    assert r.status_code == 200 and r.json()["role"] == "workstation"
    await c.patch(f"/api/nodes/{sql}", json={"role": "server"})
    srv = (await c.get(f"/api/servers/overview?environment_id={vd['id']}&role=workstation")).json()
    assert [s["name"] for s in srv["servers"]] == ["PC-RECEPTIE"]

    # Optimisation scan.
    scan = (await c.post(f"/api/customers/{vd['id']}/scan?live=false")).json()
    texts = json.dumps(scan["findings"])
    assert "reboot" in texts and "pending update" in texts and scan["checked"]["workstations"] == 1

    # The customer switcher in the web interface limits every query.
    hdr = {"x-stackpilot-customer": other["id"]}
    names = {n["name"] for n in (await c.get("/api/nodes", headers=hdr)).json()}
    assert names == {"jansen-nas"}
    assert (await c.get(f"/api/nodes/{sql}", headers=hdr)).status_code == 404
    assert len((await c.get("/api/customers", headers=hdr)).json()) >= 3  # the switcher still lists all

    # A token tied to one customer only sees that customer.
    r = await c.post("/api/tokens", json={"name": "vandijk-ops", "scopes": ["read", "logs", "nodes"],
                                          "customer_ids": [vd["id"]]})
    assert r.status_code == 201, r.text
    assert r.json()["customer_ids"] == [vd["id"]]
    tok = r.json()["token"]
    assert (await c.post("/api/tokens", json={"name": "x", "customer_ids": ["nope"]})).status_code == 400
    await _init(client, tok)
    data, _ = await _call(client, tok, "list_customers", {})
    assert [x["name"] for x in data] == ["Van Dijk Accountants"]
    data, _ = await _call(client, tok, "list_nodes", {})
    assert secret not in {n["id"] for n in data} and {n["id"] for n in data} == {sql, pc, (await _id(c, "pve-vd01"))}
    data, err = await _call(client, tok, "get_node", {"node": "jansen-nas"})
    assert data is None and "not found" in err
    data, err = await _call(client, tok, "get_node", {"node": secret})
    assert data is None and "not found" in err
    data, err = await _call(client, tok, "get_customer_overview", {"customer": "Bakkerij Jansen"})
    assert data is None and "not found" in err
    data, err = await _call(client, tok, "read_file", {"node": "jansen-nas", "path": "/etc/passwd"})
    assert data is None and "not found" in err
    data, err = await _call(client, tok, "get_audit_log", {})
    assert data is None and "across customers" in err
    data, _ = await _call(client, tok, "get_customer_overview", {"customer": "van dijk accountants"})
    assert data["context"]["maintenance_window"] == "Tue 22:00-02:00"
    data, _ = await _call(client, tok, "optimisation_scan", {"customer": vd["id"], "live_network_checks": False})
    assert data["customer"] == "Van Dijk Accountants" and data["findings"]
    data, _ = await _call(client, tok, "update_customer", {"customer": vd["id"], "append_note": "Backups go to the NAS."})
    assert data["notes"].endswith("Backups go to the NAS.") and "office hours" in data["notes"]
    data, err = await _call(client, tok, "update_customer", {"customer": other["id"], "notes": "hacked"})
    assert data is None

    # Customer notes are an MCP resource; the token only sees its own customer.
    res = (await _rpc(client, tok, "resources/read", {"uri": "stackpilot://customers"}, 20)).json()
    text = res["result"]["contents"][0]["text"]
    assert "Van Dijk" in text and "Jansen" not in text
    res = (await _rpc(client, tok, "resources/read", {"uri": f"stackpilot://customers/{vd['id']}"}, 21)).json()
    assert "office hours" in res["result"]["contents"][0]["text"]
    res = (await _rpc(client, tok, "resources/read", {"uri": f"stackpilot://customers/{other['id']}"}, 22)).json()
    assert "error" in res or res.get("result", {}).get("isError")

    # The AI access tab lists the token; tying it to all customers again widens it.
    ov = (await c.get(f"/api/customers/{vd['id']}/overview")).json()
    assert any(t["name"] == "vandijk-ops" and not t["all_customers"] for t in ov["ai_access"])
    tid = r.json()["id"]
    assert (await c.put(f"/api/tokens/{tid}/customers", json={"customer_ids": None})).json()["customer_ids"] is None
    data, _ = await _call(client, tok, "list_customers", {})
    assert len(data) >= 3

    # A customer with machines cannot be deleted; an empty one can.
    assert (await c.delete(f"/api/customers/{vd['id']}")).status_code == 409
    empty = (await c.post("/api/customers", json={"name": "Leeg"})).json()
    assert (await c.delete(f"/api/customers/{empty['id']}")).status_code == 204

    # Leave the shared test database as it was for the other tests.
    from sqlalchemy import delete

    from mmn.db import sessionmaker
    from mmn.models import Node

    async with sessionmaker()() as db:
        await db.execute(delete(Node).where(Node.environment_id.in_([vd["id"], other["id"]])))
        await db.commit()
    for cid in (vd["id"], other["id"]):
        assert (await c.delete(f"/api/customers/{cid}")).status_code == 204


async def _id(c, name: str) -> str:
    return next(n["id"] for n in (await c.get("/api/nodes")).json() if n["name"] == name)
