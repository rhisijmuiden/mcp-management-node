from __future__ import annotations

import json

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json",
           "mcp-protocol-version": "2025-06-18"}


async def _rpc(c, token, method, params=None, id_=1):
    r = await c.post("/mcp/", headers={**HEADERS, "authorization": f"Bearer {token}"},
                     json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    return r


async def _init(c, token):
    r = await _rpc(c, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    return r.json()


async def test_mcp_requires_token(client):
    r = await client.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "bearer" in r.headers["www-authenticate"].lower()
    r = await client.post("/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401  # no redirect for the path without a slash
    r = await _rpc(client, "mmn_bogus", "tools/list")
    assert r.status_code == 401


async def test_mcp_tools_and_scopes(admin_client, client):
    c = admin_client
    r = await c.post("/api/tokens", json={"name": "claude-read", "scopes": ["read"]})
    assert r.status_code == 201, r.text
    read_token = r.json()["token"]
    assert read_token.startswith("mmn_") and r.json()["scopes"] == ["read"]

    init = await _init(client, read_token)
    assert init["result"]["serverInfo"]["name"] == "Stackpilot"
    tools = (await _rpc(client, read_token, "tools/list", id_=2)).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"get_system_info", "run_backup", "restore_backup", "install_update", "send_test_mail"} <= names
    restore = next(t for t in tools if t["name"] == "restore_backup")
    assert restore["annotations"]["destructiveHint"] is True

    res = (await _rpc(client, read_token, "tools/call", {"name": "get_system_info", "arguments": {}}, 3)).json()
    content = res["result"]
    assert not content.get("isError"), content
    data = content.get("structuredContent") or json.loads(content["content"][0]["text"])
    assert data["name"] == "Stackpilot"

    # A read-only token cannot run a backup.
    res = (await _rpc(client, read_token, "tools/call",
                      {"name": "run_backup", "arguments": {"target_id": "x"}}, 4)).json()
    assert res["result"]["isError"] is True
    assert "backups" in json.dumps(res["result"])

    # The calls are in the audit log, marked as MCP.
    log = (await c.get("/api/settings/audit")).json()
    assert any(e["source"] == "mcp" and e["action"] == "mcp.get_system_info" for e in log)

    # Revoked tokens stop working.
    tok_id = r.json()["id"]
    assert (await c.delete(f"/api/tokens/{tok_id}")).status_code == 204
    assert (await _rpc(client, read_token, "tools/list", id_=5)).status_code == 401


async def test_destructive_tool_needs_confirm(admin_client, client):
    r = await admin_client.post("/api/tokens", json={"name": "claude-admin",
                                                     "scopes": ["read", "restore", "updates"]})
    token = r.json()["token"]
    await _init(client, token)
    res = (await _rpc(client, token, "tools/call", {"name": "restore_backup", "arguments": {
        "target_id": "t", "backup_id": "b1", "confirm": "something else"}}, 2)).json()
    assert res["result"]["isError"] is True and "confirm" in json.dumps(res["result"])
    res = (await _rpc(client, token, "tools/call", {"name": "install_update", "arguments": {
        "tag": "v9.9.9", "confirm": "v9.9.9"}}, 3)).json()
    assert res["result"]["isError"] is True and "installed system" in json.dumps(res["result"])


async def test_viewer_cannot_create_admin_scopes(admin_client):
    import httpx
    from conftest import csrf

    c = admin_client
    await c.post("/api/users", json={"username": "viewer2", "password": "viewer-password-2"})
    async with httpx.AsyncClient(transport=c._transport, base_url="http://testserver") as v:
        await v.post("/api/auth/login", json={"username": "viewer2", "password": "viewer-password-2"})
        r = await v.post("/api/tokens", json={"name": "x", "scopes": ["restore"]}, headers=csrf(v))
        assert r.status_code == 403
        r = await v.post("/api/tokens", json={"name": "x", "scopes": ["read"]}, headers=csrf(v))
        assert r.status_code == 201
