"""v0.27.0: software library, several GitHub tokens, repositories with rights per customer, server deployments."""

from __future__ import annotations

import httpx
import pytest
from test_customers import _machine

from mmn.services import github_library


class FakeGitHub:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.refs: list[dict] = []

    async def __call__(self, method, path, token, *, params=None, json=None):
        h = httpx.Headers({})
        if path == "/user":
            if token == "bad":
                raise github_library.GitHubError("GitHub refused the token (401)")
            login = "rhis" if token.startswith("github_pat_") else "rhis-org-bot"
            return {"login": login}, httpx.Headers({
                "x-oauth-scopes": "repo, workflow" if not token.startswith("github_pat_") else "",
                "github-authentication-token-expiration": "2026-12-01 10:00:00 UTC"})
        if path == "/user/orgs":
            return [{"login": "rhisijmuiden"}], h
        if path == "/user/repos" and method == "GET":
            return [{"full_name": "rhis/camview", "private": True, "default_branch": "main", "description": "Cams"},
                    {"full_name": "rhis/klantportaal", "private": True, "default_branch": "main"}], h
        if method == "POST" and path in ("/user/repos", "/orgs/rhisijmuiden/repos"):
            self.created.append((path, json))
            owner = "rhis" if path == "/user/repos" else "rhisijmuiden"
            return {"full_name": f"{owner}/{json['name']}", "private": json["private"], "default_branch": "main",
                    "html_url": f"https://github.com/{owner}/{json['name']}"}, h
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "a" * 40}}, h
        if method == "POST" and path.endswith("/git/refs"):
            self.refs.append(json)
            return {"ref": json["ref"]}, h
        raise github_library.GitHubError("not found")


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_library, "call", fake)
    return fake


async def test_accounts_and_legacy_token(admin_client, gh):
    c = admin_client
    from mmn.db import sessionmaker
    from mmn.security import encrypt_secret
    from mmn.services.settings_store import set_setting

    for x in (await c.get("/api/github/accounts")).json():  # left by other tests
        await c.delete(f"/api/github/accounts/{x['id']}")
    # The single token of v0.26 becomes the first account.
    async with sessionmaker()() as db:
        await set_setting(db, "github", {"token_enc": encrypt_secret("ghp_legacy"), "login": "rhis-org-bot"})
        await db.commit()
    accounts = (await c.get("/api/github/accounts")).json()
    assert [a["login"] for a in accounts] == ["rhis-org-bot"] and accounts[0]["token_kind"] == "classic"
    assert (await c.get("/api/github/accounts")).json()[0]["id"] == accounts[0]["id"]  # only once

    r = await c.post("/api/github/accounts", json={"name": "Eigen", "token": "github_pat_abc"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["login"] == "rhis" and a["token_kind"] == "fine_grained" and a["expires_at"].startswith("2026-12-01")
    assert "token" not in a and "token_enc" not in a
    assert (await c.post("/api/github/accounts", json={"token": "bad"})).status_code == 400
    owners = (await c.get(f"/api/github/accounts/{accounts[0]['id']}/owners")).json()
    assert [o["login"] for o in owners] == ["rhis-org-bot", "rhisijmuiden"]
    checked = (await c.post(f"/api/github/accounts/{accounts[0]['id']}/check")).json()
    assert checked["scopes"] == ["repo", "workflow"]
    st = (await c.get("/api/github")).json()
    assert st["configured"] is True and len(st["accounts"]) == 2

    remote = (await c.get("/api/github/remote")).json()
    assert {x["full_name"] for x in remote} == {"rhis/camview", "rhis/klantportaal"}  # no doubles over 2 tokens

    for x in (await c.get("/api/github/accounts")).json():
        assert (await c.delete(f"/api/github/accounts/{x['id']}")).status_code == 204


async def test_library_rights_and_isolation(admin_client, gh):
    c = admin_client
    acc = (await c.post("/api/github/accounts", json={"token": "github_pat_lib"})).json()
    a = (await c.post("/api/customers", json={"name": "Soft A"})).json()
    b = (await c.post("/api/customers", json={"name": "Soft B"})).json()

    # Create a repository for one customer, in an organisation, with a test branch.
    r = await c.post("/api/repos/create", json={"account_id": acc["id"], "owner": "rhisijmuiden", "name": "a-portal",
                                                "kind": "customer", "owner_customer_id": a["id"]})
    assert r.status_code == 201, r.text
    own = r.json()
    assert own["full_name"] == "rhisijmuiden/a-portal" and gh.created[-1][0] == "/orgs/rhisijmuiden/repos"
    assert gh.refs[-1]["ref"] == "refs/heads/test"
    assert [x["customer"] for x in own["access"]] == ["Soft A"]  # the owner is linked automatically
    # A customer repository cannot be given to another customer.
    bad = await c.put(f"/api/repos/{own['id']}/access", json={"customers": [{"customer_id": b["id"]}]})
    assert bad.status_code == 400
    assert (await c.post("/api/repos/create", json={"name": "x", "kind": "customer"})).status_code == 400

    # Import a product and link it to both customers, AI read only for B.
    imp = await c.post("/api/repos/import", json={"full_names": ["rhis/camview"], "customers": [
        {"customer_id": a["id"]}, {"customer_id": b["id"], "can_deploy": False, "ai_read": True}]})
    assert imp.status_code == 201, imp.text
    cam = imp.json()[0]
    assert cam["description"] == "Cams" and len(cam["access"]) == 2
    assert (await c.post("/api/repos/import", json={"full_names": ["rhis/nope"]})).status_code == 400

    lib = {x["full_name"]: x for x in (await c.get("/api/repos")).json()}
    assert {"rhisijmuiden/a-portal", "rhis/camview"} <= set(lib)
    # With customer B picked in the switcher only what is linked to B shows, and only B's link.
    only_b = (await c.get("/api/repos", headers={"X-Stackpilot-Customer": b["id"]})).json()
    names = {x["full_name"] for x in only_b}
    assert "rhis/camview" in names and "rhisijmuiden/a-portal" not in names
    assert [x["customer_id"] for x in next(x for x in only_b if x["full_name"] == "rhis/camview")["access"]] == [b["id"]]
    remote = {x["full_name"]: x for x in (await c.get("/api/github/remote")).json()}
    assert remote["rhis/camview"]["in_library"] is True and remote["rhis/klantportaal"]["in_library"] is False

    # Deploy rights: a Linux server of B may not get camview (can_deploy off) nor A's repository.
    srv_b = await _machine(b["id"], "soft-b-srv01")
    srv_a = await _machine(a["id"], "soft-a-srv01")
    ws_a = await _machine(a["id"], "soft-a-pc01", role="workstation")
    body = {"name": "cam-b", "repo": "rhis/camview", "target": "server", "node_id": srv_b, "deploy_cmd": "make"}
    r = await c.post("/api/deployments", json=body)
    assert r.status_code == 400 and "not released" in r.text
    r = await c.post("/api/deployments", json={**body, "repo": "rhisijmuiden/a-portal"})
    assert r.status_code == 400 and "another customer" in r.text
    assert (await c.post("/api/deployments", json={**body, "node_id": ws_a})).status_code == 400  # a workstation
    assert (await c.post("/api/deployments", json={**body, "target": "container", "vmid": 120})).status_code == 400
    r = await c.post("/api/deployments", json={**body, "name": "portal-a", "repo": "rhisijmuiden/a-portal",
                                               "node_id": srv_a, "snapshot": True})
    assert r.status_code == 201, r.text
    dep = r.json()
    assert dep["target"] == "server" and dep["customer_id"] == a["id"] and dep["vmid"] == 0
    assert dep["snapshot"] is False  # a server has no snapshots
    # Unknown repositories are added to the library on first deploy, linked to that customer.
    r = await c.post("/api/deployments", json={**body, "name": "new-a", "repo": "rhis/brandnew", "node_id": srv_a})
    assert r.status_code == 201, r.text
    lib = {x["full_name"]: x for x in (await c.get("/api/repos")).json()}
    assert [x["customer_id"] for x in lib["rhis/brandnew"]["access"]] == [a["id"]]
    assert lib["rhisijmuiden/a-portal"]["deployments"] == 1

    # The customer overview counts the repositories of the customer.
    links = (await c.get(f"/api/customers/{a['id']}/links")).json()
    assert links["tiles"]["software"] == {"repos": 3, "own": 1}

    # Switching to a customer repository drops other links.
    upd = await c.put(f"/api/repos/{cam['id']}", json={"kind": "customer", "owner_customer_id": a["id"]})
    assert upd.status_code == 200, upd.text
    assert [x["customer_id"] for x in upd.json()["access"]] == [a["id"]]

    # The server script writes the token to a root-only file and runs the checkout there.
    from mmn.db import sessionmaker
    from mmn.models import Deployment, Node
    from mmn.services import deploy

    async with sessionmaker()() as db:
        d = await db.get(Deployment, dep["id"])
        script = deploy.server_script(d, "main", run_setup=False)
        assert "pct exec" not in script and "umask 077" in script and "/root/.mmn-git-token" in script
        assert await github_library.token_for(db, "rhisijmuiden/a-portal") == "github_pat_lib"
        for did in [x["id"] for x in (await c.get("/api/deployments")).json() if x["node_id"] in (srv_a, srv_b)]:
            await db.delete(await db.get(Deployment, did))
        for nid in (srv_a, srv_b, ws_a):
            await db.delete(await db.get(Node, nid))
        await db.commit()
    assert (await c.delete(f"/api/repos/{own['id']}")).status_code == 204
    assert (await c.delete(f"/api/github/accounts/{acc['id']}")).status_code == 204


async def test_mcp_tools_for_repositories(admin_client, client, gh):
    from test_customers import _call
    from test_mcp import _init

    c = admin_client
    await c.post("/api/github/accounts", json={"token": "github_pat_mcp"})
    cust = (await c.post("/api/customers", json={"name": "Soft MCP"})).json()
    tok = (await c.post("/api/tokens", json={"name": "soft-mcp", "scopes": ["read", "deploy"]})).json()["token"]
    await _init(client, tok)
    c = client
    data, err = await _call(c, tok, "create_github_repo", {"name": "mcp-made", "customer": "Soft MCP"})
    assert err is None, err
    assert data["full_name"] == "rhis/mcp-made" and data["access"][0]["customer"] == "Soft MCP"
    data, err = await _call(c, tok, "link_repo_to_customer", {"repo": "rhis/klantportaal", "customer": cust["id"],
                                                                "ai_read": True})
    assert err is None, err
    assert data["access"][0]["ai_read"] is True
    data, err = await _call(c, tok, "list_software_repos", {"customer": "Soft MCP"})
    assert err is None, err
    assert {x["full_name"] for x in data} == {"rhis/mcp-made", "rhis/klantportaal"}
