"""Phase 18: customer documentation (versions, attachments) and the password vault (encryption at rest,
unlock, access log, key rotation) with AI use only after approval."""

from __future__ import annotations

import asyncio

import pytest
from test_customers import _call, _machine
from test_gateway import _token
from test_mcp import _init

from mmn.services import vault

PW = "correct horse battery"


def test_crypto_roundtrip_and_binding():
    kid, wrapped, ct = vault.seal("item", "a" * 32, b"geheim wachtwoord")
    assert b"geheim" not in ct and b"geheim" not in wrapped
    assert vault.unseal("item", "a" * 32, kid, wrapped, ct) == b"geheim wachtwoord"
    # The ciphertext is bound to its item: moving it to another item fails.
    with pytest.raises(vault.VaultError):
        vault.unseal("item", "b" * 32, kid, wrapped, ct)
    blob = vault.seal_blob("attachment", "c" * 32, b"%PDF netwerkplan")
    assert blob.startswith(vault.BLOB_MAGIC) and b"netwerkplan" not in blob
    assert vault.open_blob("attachment", "c" * 32, blob) == b"%PDF netwerkplan"
    assert vault.scrub("user=bob pw=Sup3r!x ok", ["Sup3r!x", "bob"]) == "user=*** pw=*** ok"


def test_password_generator():
    pw = vault.generate_password(32)
    assert len(pw) == 32 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw)
    assert not set(pw) & vault.AMBIGUOUS
    assert len(vault.generate_password(24, symbols=False)) == 24
    phrase = vault.generate_password(words=5)
    assert phrase.count("-") == 5


async def test_documents(admin_client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Docs Klant"})).json()
    srv = await _machine(cust["id"], "docs-srv01")
    r = await c.post("/api/documents", json={"customer_id": cust["id"], "title": "Netwerkplan", "category": "network",
                                             "body": "# Netwerk\nVLAN 10 kantoor 10.0.10.0/24", "tags": ["LAN", "lan"],
                                             "node_ids": [srv]})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["tags"] == ["lan"] and d["machines"][0]["name"] == "docs-srv01" and d["version"] == 1
    other = (await c.post("/api/customers", json={"name": "Docs Ander"})).json()
    bad = await c.post("/api/documents", json={"customer_id": other["id"], "title": "x", "node_ids": [srv]})
    assert bad.status_code == 400

    r = await c.patch(f"/api/documents/{d['id']}", json={"body": "# Netwerk\nVLAN 20 gasten", "base_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2
    # Saving on top of an old version is a conflict.
    r = await c.patch(f"/api/documents/{d['id']}", json={"body": "oud", "base_version": 1})
    assert r.status_code == 409
    full = (await c.get(f"/api/documents/{d['id']}")).json()
    assert len(full["revisions"]) == 1 and full["revisions"][0]["version"] == 1
    rev = (await c.get(f"/api/documents/{d['id']}/revisions/{full['revisions'][0]['id']}")).json()
    assert "VLAN 10" in rev["body"]

    found = (await c.get("/api/documents", params={"q": "gasten", "customer_id": cust["id"]})).json()
    assert [x["id"] for x in found["documents"]] == [d["id"]] and "gasten" in found["documents"][0]["snippets"][0]

    # Attachments are stored encrypted.
    r = await c.post(f"/api/documents/{d['id']}/attachments",
                     files={"file": ("plan.pdf", b"%PDF-1.4 serverkast rij 3", "application/pdf")})
    assert r.status_code == 201, r.text
    att = r.json()
    assert vault.attachment_path(att["id"]).exists()
    assert b"serverkast" not in vault.attachment_path(att["id"]).read_bytes()
    r = await c.get(f"/api/documents/{d['id']}/attachments/{att['id']}")
    assert r.content == b"%PDF-1.4 serverkast rij 3" and r.headers["content-type"] == "application/octet-stream"
    r = await c.get(f"/api/documents/{d['id']}/attachments/{att['id']}", params={"inline": True})
    assert r.headers["content-type"] == "application/pdf"

    restored = (await c.post(f"/api/documents/{d['id']}/revisions/{full['revisions'][0]['id']}/restore")).json()
    assert "VLAN 10" in restored["body"] and restored["version"] == 3
    tpl = (await c.get("/api/documents/templates", params={"lang": "nl"})).json()
    assert any(t["key"] == "network" and t["title"] == "Netwerkplan" for t in tpl)

    assert (await c.delete(f"/api/documents/{d['id']}")).status_code == 204
    assert not vault.attachment_path(att["id"]).exists()


async def _unlock(c):
    r = await c.post("/api/vault/unlock", json={"password": PW})
    assert r.status_code == 200, r.text


async def test_vault_items_unlock_history_and_rotation(admin_client):
    c = admin_client
    await c.post("/api/vault/lock")
    cust = (await c.post("/api/customers", json={"name": "Kluis Klant"})).json()
    body = {"customer_id": cust["id"], "name": "Firewall admin", "kind": "network", "username": "admin",
            "password": "Fw-Geheim-123!", "url": "https://10.0.0.1", "notes": "MFA op telefoon",
            "fields": [{"name": "Recovery code", "value": "RC-9999", "secret": True},
                       {"name": "Model", "value": "UDM Pro", "secret": False}], "rotate_days": 90}
    r = await c.post("/api/vault/items", json=body)
    assert r.status_code == 403 and r.json()["detail"] == "vault_locked"
    r = await c.post("/api/vault/unlock", json={"password": "wrong"})
    assert r.status_code == 403
    await _unlock(c)
    r = await c.post("/api/vault/items", json=body)
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["username"] == "admin" and "password" not in item and item["has_password"]
    assert {f["name"]: f["value"] for f in item["fields"]} == {"Recovery code": None, "Model": "UDM Pro"}

    # Nothing readable in the database.
    from sqlalchemy import text

    from mmn.db import sessionmaker

    async with sessionmaker()() as db:
        raw = (await db.execute(text("SELECT ciphertext, wrapped_key FROM vault_items WHERE id = :i"),
                                {"i": item["id"]})).one()
    assert b"Fw-Geheim" not in raw[0] and b"admin" not in raw[0]

    listed = (await c.get("/api/vault/items", params={"customer_id": cust["id"], "q": "firewall"})).json()
    assert [i["id"] for i in listed["items"]] == [item["id"]] and "password" not in listed["items"][0]
    r = await c.post(f"/api/vault/items/{item['id']}/reveal", json={"action": "copy", "field": "password"})
    assert r.status_code == 200 and r.json()["password"] == "Fw-Geheim-123!"
    assert {f["name"]: f["value"] for f in r.json()["fields"]}["Recovery code"] == "RC-9999"

    # Changing the password keeps the old one; an empty secret field keeps its value.
    r = await c.patch(f"/api/vault/items/{item['id']}", json={
        "password": "Nieuw-456!", "fields": [{"name": "Recovery code", "value": "", "secret": True}]})
    assert r.status_code == 200, r.text
    full = (await c.get(f"/api/vault/items/{item['id']}")).json()
    assert len(full["history"]) == 1
    old = (await c.post(f"/api/vault/items/{item['id']}/history/{full['history'][0]['id']}/reveal")).json()
    assert old["password"] == "Fw-Geheim-123!"
    now = (await c.post(f"/api/vault/items/{item['id']}/reveal", json={})).json()
    assert now["password"] == "Nieuw-456!" and now["fields"][0]["value"] == "RC-9999"

    log = (await c.get("/api/vault/access", params={"item_id": item["id"]})).json()
    actions = [a["action"] for a in log]
    assert {"create", "copy", "update", "reveal"} <= set(actions)

    # Key rotation: everything stays readable with the new key, the old key is gone.
    before = vault.key_info()["active"]
    r = await c.post("/api/vault/rotate-key")
    assert r.status_code == 200, r.text
    assert r.json()["key_id"] != before and r.json()["items"] >= 1 and vault.key_info()["keys"] == 1
    again = (await c.post(f"/api/vault/items/{item['id']}/reveal", json={})).json()
    assert again["password"] == "Nieuw-456!"
    old = (await c.post(f"/api/vault/items/{item['id']}/history/{full['history'][0]['id']}/reveal")).json()
    assert old["password"] == "Fw-Geheim-123!"

    await c.post("/api/vault/lock")
    r = await c.post(f"/api/vault/items/{item['id']}/reveal", json={})
    assert r.status_code == 403
    failed = (await c.get("/api/vault/access", params={"item_id": item["id"]})).json()[0]
    assert failed["action"] == "reveal" and failed["success"] is False


async def _approve_latest(c, tool: str) -> str:
    rows = (await c.get("/api/gateway/approvals", params={"state": "pending"})).json()
    rows = rows["approvals"] if isinstance(rows, dict) else rows
    a = next(x for x in rows if x["tool"] == tool)
    assert "vault" in [r["code"] for r in a["reasons"]]
    r = await c.post(f"/api/gateway/approvals/{a['id']}/approve", json={"note": "ok"})
    assert r.status_code == 200, r.text
    for _ in range(100):
        st = (await c.get(f"/api/gateway/approvals/{a['id']}")).json()["status"]
        if st in ("executed", "failed"):
            break
        await asyncio.sleep(0.05)
    return a["id"]


async def test_ai_uses_vault_only_after_approval(admin_client, client, monkeypatch):
    from mmn.services import agent_hub

    c = admin_client
    await c.put("/api/gateway/settings", json={"approval_wait_seconds": 0, "approval_expiry_hours": 24,
                                               "notify_app": False, "notify_mail": False, "notify_mail_to": ""})
    a = (await c.post("/api/customers", json={"name": "AI Kluis A"})).json()
    b = (await c.post("/api/customers", json={"name": "AI Kluis B"})).json()
    srv = await _machine(a["id"], "aik-srv01")
    await _unlock(c)
    item = (await c.post("/api/vault/items", json={
        "customer_id": a["id"], "name": "SQL sa", "kind": "database", "username": "sa", "password": "Sql-Pw-777",
        "node_ids": [srv]})).json()
    never = (await c.post("/api/vault/items", json={
        "customer_id": a["id"], "name": "Bank token", "password": "nooit", "ai_access": "never"})).json()
    await c.post("/api/documents", json={"customer_id": a["id"], "title": "SQL procedure", "category": "procedure",
                                         "body": "Herstart SQL alleen in het onderhoudsvenster.", "node_ids": [srv]})
    await c.post("/api/documents", json={"customer_id": a["id"], "title": "Geheim", "body": "x", "ai_visible": False})

    token, _ = await _token(c, "claude-vault", ["read", "vault", "docs", "exec"])
    await _init(client, token)
    no_scope, _ = await _token(c, "claude-novault", ["read"])
    await _init(client, no_scope)
    other, _ = await _token(c, "claude-b", ["read", "vault"], customer_ids=[b["id"]])
    await _init(client, other)

    # Documentation for the AI (hidden pages are left out); writing needs the docs scope.
    res, err = await _call(client, token, "list_documents", {"customer": "AI Kluis A", "query": "onderhoudsvenster"})
    assert err is None and [d["title"] for d in res["documents"]] == ["SQL procedure"]
    res, err = await _call(client, token, "list_documents", {"customer": "AI Kluis A"})
    assert "Geheim" not in [d["title"] for d in res["documents"]]
    _, err = await _call(client, token, "get_document", {"document": "Geheim", "customer": "AI Kluis A"})
    assert err and "not found" in err
    res, err = await _call(client, token, "save_document", {"customer": "AI Kluis A", "document": "SQL procedure",
                                                           "title": "SQL procedure", "body": "- 2026-11-25: getest",
                                                           "append": True})
    assert err is None and res["version"] == 2
    _, err = await _call(client, no_scope, "save_document", {"customer": "AI Kluis A", "title": "x"})
    assert err and "docs" in err
    ctx, _ = await _call(client, token, "get_customer_context", {"customer": "AI Kluis A"})
    assert "SQL procedure" in [d["title"] for d in ctx["documentation"]]

    # Listing shows no secrets; other customers' tokens see nothing.
    res, err = await _call(client, token, "list_credentials", {"customer": "AI Kluis A"})
    assert err is None
    by_name = {i["name"]: i for i in res["credentials"]}
    assert by_name["SQL sa"]["username"] == "sa" and "password" not in by_name["SQL sa"]
    assert by_name["Bank token"]["ai_may_use"] is False
    _, err = await _call(client, no_scope, "list_credentials", {})
    assert err and "vault" in err
    res, _ = await _call(client, other, "list_credentials", {})
    assert res["credentials"] == []
    _, err = await _call(client, other, "reveal_credential", {"credential": item["id"], "reason": "test"})
    assert err and "not found" in err

    # Reveal: pending first (even though the test policy allows destructive tools), then once after approval.
    res, err = await _call(client, token, "reveal_credential", {"credential": "SQL sa", "reason": "check DB",
                                                               "customer": "AI Kluis A"})
    assert err is None and res["status"] == "pending_approval", res
    assert "Sql-Pw-777" not in str(res)
    aid = await _approve_latest(c, "reveal_credential")
    appr = (await c.get(f"/api/gateway/approvals/{aid}")).json()
    assert appr["status"] == "executed" and "Sql-Pw-777" not in str(appr)
    res, err = await _call(client, token, "reveal_credential", {"credential": "SQL sa", "reason": "check DB",
                                                               "customer": "AI Kluis A"})
    assert err is None and res["password"] == "Sql-Pw-777" and res["reads_left"] == 0
    res, err = await _call(client, token, "reveal_credential", {"credential": "SQL sa", "reason": "check DB",
                                                               "customer": "AI Kluis A"})
    assert res["status"] == "pending_approval"  # the grant is used up
    _, err = await _call(client, token, "reveal_credential", {"credential": "Bank token", "reason": "x",
                                                             "customer": "AI Kluis A"})
    assert err and "never" in err

    # The audit log never holds the secret.
    audit = (await c.get("/api/settings/audit", params={"limit": 200}))
    if audit.status_code == 200:
        assert "Sql-Pw-777" not in audit.text

    # run_with_credential: the script gets the secret in its environment; it is wiped from the output.
    calls = []

    async def fake_request(node_id, method, params, timeout=60):
        calls.append((method, {**params, "env": dict(params.get("env") or {})}))
        if method == "script.run":
            async def finish():
                await asyncio.sleep(0.05)
                agent_hub.add_line(node_id, params["job_id"], f"login {params['env']['SP_USERNAME']} "
                                                              f"{params['env']['SP_PASSWORD']} ok")
                await agent_hub._job_message(node_id, {"job_id": params["job_id"], "event": "done",
                                                       "status": "success", "result": {"exit_code": 0}})
            asyncio.create_task(finish())
            return {"started": True}
        return {}

    monkeypatch.setattr(agent_hub, "request", fake_request)
    args = {"server": "aik-srv01", "credential": "SQL sa", "customer": "AI Kluis A", "reason": "test login",
            "script": 'sqlcmd -U "$SP_USERNAME" -P "$SP_PASSWORD" -Q "select 1"'}
    res, err = await _call(client, token, "run_with_credential", args)
    assert err is None and res["status"] == "pending_approval"
    aid = await _approve_latest(c, "run_with_credential")
    appr = (await c.get(f"/api/gateway/approvals/{aid}")).json()
    assert appr["status"] == "executed", appr
    i = str(appr).find("Sql-Pw-777")
    assert i < 0, str(appr)[max(0, i - 300):i + 50]
    assert appr["result"]["exit_code"] == 0 and "login sa *** ok" in appr["result"]["output"]
    sent = [p for m, p in calls if m == "script.run"][-1]
    assert sent["env"]["SP_PASSWORD"] == "Sql-Pw-777"
    from mmn.db import sessionmaker
    from mmn.models import NodeJob

    async with sessionmaker()() as db:
        job = await db.get(NodeJob, appr["result"]["job_id"])
        assert "Sql-Pw-777" not in (job.output or "") and "Sql-Pw-777" not in str(job.params)

    log = (await c.get("/api/vault/access", params={"item_id": item["id"], "actor": "ai"})).json()
    actions = [x["action"] for x in log]
    assert {"ai_request", "ai_approved", "ai_reveal", "ai_use"} <= set(actions)
    assert all(x["token_name"] == "claude-vault" for x in log if x["action"] != "ai_denied")
    _ = never
