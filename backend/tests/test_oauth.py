from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from test_mcp import HEADERS, _init, _rpc


async def test_oauth_flow_for_ai_clients(admin_client, client):
    # Discovery from the 401 of the MCP endpoint.
    r = await client.post("/mcp/", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in r.headers[
        "www-authenticate"]
    prm = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert prm["resource"] == "http://testserver/mcp"
    asm = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert asm["code_challenge_methods_supported"] == ["S256"]

    # Dynamic client registration.
    r = await client.post("/oauth/register", json={"client_name": "Claude",
                                                   "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]
    bad = await client.post("/oauth/register", json={"redirect_uris": ["http://evil.example.com/cb"]})
    assert bad.status_code == 400

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect = "https://claude.ai/api/mcp/auth_callback"
    r = await client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": "read backups"})
    assert r.status_code == 302 and "/connect/consent?" in r.headers["location"]

    # The logged-in admin approves on the consent page.
    info = (await admin_client.get(f"/api/oauth/clients/{client_id}")).json()
    assert info["client_name"] == "Claude"
    r = await admin_client.post("/api/oauth/approve", json={
        "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge, "state": "xyz",
        "scopes": ["read", "backups"]})
    loc = urlparse(r.json()["redirect"])
    qs = parse_qs(loc.query)
    assert qs["state"] == ["xyz"]
    code = qs["code"][0]

    # Wrong verifier fails and burns the code.
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": client_id}
    assert (await client.post("/oauth/token", data={**form, "code_verifier": "wrong"})).status_code == 400
    r = await admin_client.post("/api/oauth/approve", json={
        "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge, "scopes": ["read", "backups"]})
    code = parse_qs(urlparse(r.json()["redirect"]).query)["code"][0]
    tok = (await client.post("/oauth/token", data={**form, "code": code, "code_verifier": verifier})).json()
    assert tok["token_type"] == "Bearer" and tok["scope"] == "backups read"

    # The token works on the MCP endpoint, and the client name is recorded.
    init = await _init(client, tok["access_token"])
    assert init["result"]["serverInfo"]["name"]
    res = (await _rpc(client, tok["access_token"], "tools/call", {"name": "list_backup_runs", "arguments": {}}, 2)
           ).json()
    assert not res["result"].get("isError"), res
    log = (await admin_client.get("/api/settings/audit")).json()
    entry = next(e for e in log if e["action"] == "mcp.list_backup_runs")
    assert entry["detail"]["client"]["name"] == "test" and entry["detail"]["token_kind"] == "oauth"

    # Refresh rotates the tokens.
    new = (await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                   "refresh_token": tok["refresh_token"]})).json()
    assert new["access_token"] != tok["access_token"]
    assert (await _rpc(client, tok["access_token"], "tools/list")).status_code == 401
    assert (await _rpc(client, new["access_token"], "tools/list")).status_code == 200
    again = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                    "refresh_token": tok["refresh_token"]})
    assert again.status_code == 400
