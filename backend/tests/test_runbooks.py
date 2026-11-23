"""Phase 13: runbooks: definitions and placeholders, step-by-step runs with approvals per step (app and
AI token), manual steps for a person or the AI, policies (block, approve), reject, retry, skip and
cancel, the built-in templates and the MCP tools."""

from __future__ import annotations

import asyncio

import pytest
from test_customers import _call
from test_gateway import TEST_GLOBAL, _token
from test_mcp import _init

from mmn.services import runbook_templates, runbooks


# ------------------------------------------------------------------ unit tests
def test_render_placeholders():
    ctx = {"params": {"server": "srv1", "n": 3}, "customer": {"id": "c1", "name": "Acme"}, "run": {"id": "r1"},
           "steps": {"s1": {"result": {"id": "dep9", "items": [{"x": 1}]}}}}
    assert runbooks.render("{{params.server}}", ctx) == "srv1"
    assert runbooks.render("{{ params.n }}", ctx) == 3  # one placeholder keeps the type
    assert runbooks.render("host {{params.server}} of {{customer.name}}", ctx) == "host srv1 of Acme"
    assert runbooks.render({"a": ["{{steps.s1.result.id}}", "{{steps.s1.result.items.0.x}}"]}, ctx) == {"a": ["dep9", 1]}
    with pytest.raises(runbooks.RunbookError):
        runbooks.render("{{params.missing}}", ctx)


def test_clean_definitions():
    import mmn.mcp_server  # noqa: F401 - registers the tools

    steps = runbooks.clean_steps([
        {"kind": "tool", "tool": "get_customer_context", "args": {"customer": "{{customer.id}}"}},
        {"kind": "manual", "instructions": "Call the customer", "assignee": "ai"},
        {"kind": "wait", "minutes": 5},
        {"kind": "tool", "tool": "update_customer", "approval": "always", "on_failure": "continue",
         "args": {"customer": "{{customer.id}}", "append_note": "{{steps.s1.result.name}}"}},
    ])
    assert [s["id"] for s in steps] == ["s1", "s2", "s3", "s4"]
    assert steps[0]["risk"] == "read" and steps[0]["approval"] == "policy" and steps[3]["approval"] == "always"
    assert steps[1]["assignee"] == "ai" and steps[2]["minutes"] == 5 and steps[3]["on_failure"] == "continue"
    for bad, msg in [
        ([], "at least one step"),
        ([{"kind": "tool", "tool": "no_such_tool"}], "unknown tool"),
        ([{"kind": "tool", "tool": "run_runbook"}], "cannot call"),
        ([{"kind": "wait", "minutes": 0}], "between 1 and 1440"),
        ([{"kind": "manual"}], "describe"),
        ([{"kind": "tool", "tool": "get_overview", "args": {"x": "{{steps.s2.result}}"}},
          {"kind": "tool", "tool": "get_overview"}], "earlier step"),
        ([{"id": "a", "kind": "wait", "minutes": 1}, {"id": "a", "kind": "wait", "minutes": 1}], "twice"),
    ]:
        with pytest.raises(runbooks.RunbookError, match=msg):
            runbooks.clean_steps(bad)
    assert runbooks.clean_steps([{"kind": "tool", "tool": "github__create_repository"}])[0]["tool"]  # hub tool
    params = runbooks.clean_params([{"name": "server", "required": True}, {"name": "dept", "required": False}])
    with pytest.raises(runbooks.RunbookError, match="not a parameter"):
        runbooks.check_param_refs(runbooks.clean_steps([{"kind": "tool", "tool": "get_overview",
                                                         "args": {"x": "{{params.nope}}"}}]), params)
    assert runbooks.resolve_params(params, {"server": " srv1 "}) == {"server": "srv1", "dept": None}
    with pytest.raises(runbooks.RunbookError, match="required"):
        runbooks.resolve_params(params, {})
    with pytest.raises(runbooks.RunbookError, match="unknown"):
        runbooks.resolve_params(params, {"server": "x", "other": 1})


def test_templates_are_valid():
    import mmn.mcp_server  # noqa: F401

    keys = {t["key"] for t in runbook_templates.TEMPLATES}
    assert {"patch_server", "new_employee", "github_project", "restart_service"} <= keys
    for lang in ("en", "nl"):
        for t in runbook_templates.all_localized(lang):
            assert isinstance(t["name"], str) and all(isinstance(s["title"], str) for s in t["steps"])
            steps = runbooks.clean_steps(t["steps"])
            runbooks.check_param_refs(steps, runbooks.clean_params(t["params"]))
    assert runbook_templates.get("patch_server", "nl")["name"] == "Server patchen"


# ------------------------------------------------------------------ helpers
async def _wait_run(c, rid, statuses, timeout=15, step=None):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = (await c.get(f"/api/runbooks/runs/{rid}")).json()
        if r["status"] in statuses and (step is None or r["current_step"] == step):
            return r
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run is {r['status']}: {r}")
        await asyncio.sleep(0.1)


STEPS = [
    {"id": "ctx", "kind": "tool", "tool": "get_customer_context", "title": "Read the customer",
     "args": {"customer": "{{customer.id}}"}},
    {"id": "note", "kind": "tool", "tool": "update_customer", "approval": "always", "title": "Write a note",
     "args": {"customer": "{{customer.id}}", "append_note": "Runbook for {{params.who}} at {{steps.ctx.result.name}}"}},
    {"id": "check", "kind": "manual", "title": "Check by hand", "instructions": "Ask {{params.who}} to log in"},
]


# ------------------------------------------------------------------ app flow
async def test_runbook_flow_in_the_app(admin_client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Runbook Klant"})).json()
    r = await c.post("/api/runbooks", json={"name": "Onboard", "category": "onboarding", "customer_id": cust["id"],
                                             "params": [{"name": "who", "label": "Who"}], "steps": STEPS})
    assert r.status_code == 201, r.text
    rb = r.json()
    assert rb["version"] == 1 and rb["customer_name"] == "Runbook Klant" and rb["steps"][1]["risk"] == "change"
    assert (await c.post(f"/api/runbooks/{rb['id']}/run", json={"params": {}})).status_code == 400  # who is required

    run = (await c.post(f"/api/runbooks/{rb['id']}/run", json={"params": {"who": "Piet"}, "reason": "test"})).json()
    assert run["customer_name"] == "Runbook Klant"
    run = await _wait_run(c, run["id"], {"waiting_approval"})
    ctx, note, check = run["steps"]
    assert ctx["state"]["status"] == "succeeded" and ctx["state"]["result"]["name"] == "Runbook Klant"
    assert note["state"]["status"] == "waiting_approval" and check["state"]["status"] == "pending"
    aid = note["state"]["approval_id"]
    a = (await c.get(f"/api/gateway/approvals/{aid}")).json()
    assert a["tool"] == "update_customer" and a["source"] == "runbook" and a["status"] == "pending"
    assert a["args"]["append_note"] == "Runbook for Piet at Runbook Klant"
    assert a["reasons"][0]["code"] == "runbook" and a["reasons"][0]["params"]["always"] is True
    assert a["customer_names"] == ["Runbook Klant"]

    # Editing the runbook does not change the running procedure.
    assert (await c.put(f"/api/runbooks/{rb['id']}", json={**{k: rb[k] for k in ("name", "category", "customer_id")},
                                                          "params": rb["params"], "steps": STEPS[:1]})).json()["version"] == 2

    assert (await c.post(f"/api/gateway/approvals/{aid}/approve", json={})).status_code == 200
    run = await _wait_run(c, run["id"], {"waiting_input"})
    assert run["steps"][1]["state"]["status"] == "succeeded" and run["steps"][1]["state"]["decided_by"] == "admin"
    assert run["steps"][2]["state"]["args"]["instructions"] == "Ask Piet to log in"
    assert "Runbook for Piet" in ((await c.get(f"/api/customers/{cust['id']}")).json()["notes"] or "")
    assert (await c.get("/api/runbooks/runs/count")).json()["waiting_input"] >= 1

    r = await c.post(f"/api/runbooks/runs/{run['id']}/complete", json={"ok": True, "note": "Piet is in"})
    assert r.status_code == 200, r.text
    run = await _wait_run(c, run["id"], {"succeeded"})
    assert run["steps"][2]["state"]["note"] == "Piet is in" and run["finished_at"]
    assert (await c.post(f"/api/runbooks/runs/{run['id']}/cancel")).status_code == 409

    lst = (await c.get("/api/runbooks", params={"customer_id": cust["id"]})).json()
    assert next(x for x in lst if x["id"] == rb["id"])["runs"] == 1
    rows = (await c.get("/api/settings/audit", params={"limit": 200})).json()
    actions = [x["action"] for x in rows]
    assert "runbook.run" in actions and "runbook.finished" in actions and "mcp.update_customer" in actions
    assert (await c.delete(f"/api/runbooks/{rb['id']}")).status_code == 204
    assert (await c.get(f"/api/runbooks/runs/{run['id']}")).json()["runbook_id"] is None  # history stays


async def test_reject_retry_skip_and_cancel(admin_client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Runbook Reject"})).json()
    steps = [
        {"id": "opt", "kind": "tool", "tool": "update_customer", "approval": "always", "optional": True,
         "args": {"customer": "{{customer.id}}", "append_note": "optional"}},
        {"id": "must", "kind": "tool", "tool": "update_customer", "approval": "always",
         "args": {"customer": "{{customer.id}}", "append_note": "must"}},
        {"id": "bad", "kind": "tool", "tool": "get_customer_context", "on_failure": "continue",
         "args": {"customer": "no such customer"}},
        {"id": "end", "kind": "manual", "title": "Done?", "instructions": "Look"},
    ]
    rb = (await c.post("/api/runbooks", json={"name": "Reject test", "steps": steps})).json()
    run = (await c.post(f"/api/runbooks/{rb['id']}/run", json={"customer_id": cust["id"]})).json()
    run = await _wait_run(c, run["id"], {"waiting_approval"})
    aid = run["steps"][0]["state"]["approval_id"]
    await c.post(f"/api/gateway/approvals/{aid}/reject", json={"note": "not now"})
    run = await _wait_run(c, run["id"], {"waiting_approval"}, step=1)
    assert run["steps"][0]["state"]["status"] == "skipped" and run["current_step"] == 1  # optional: skipped
    aid2 = run["steps"][1]["state"]["approval_id"]
    await c.post(f"/api/gateway/approvals/{aid2}/reject", json={"note": "no"})
    run = await _wait_run(c, run["id"], {"failed"})
    assert run["steps"][1]["state"]["status"] == "rejected" and "rejected by admin: no" in run["error"]

    # Retry runs the step again (a new approval request); skip goes on without it.
    r = await c.post(f"/api/runbooks/runs/{run['id']}/retry", json={})
    assert r.status_code == 200, r.text
    await runbooks.wait_idle()
    run = await _wait_run(c, run["id"], {"waiting_approval"})
    assert run["steps"][1]["state"]["approval_id"] not in (None, aid2)
    await c.post(f"/api/gateway/approvals/{run['steps'][1]['state']['approval_id']}/reject", json={})
    run = await _wait_run(c, run["id"], {"failed"})
    await c.post(f"/api/runbooks/runs/{run['id']}/retry", json={"skip": True})
    run = await _wait_run(c, run["id"], {"waiting_input"})
    assert run["steps"][1]["state"]["status"] == "skipped"
    assert run["steps"][2]["state"]["status"] == "failed" and run["steps"][2]["state"]["error"]  # continue

    r = await c.post(f"/api/runbooks/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["steps"][3]["state"]["status"] == "cancelled"

    # Cancelling withdraws a pending approval request.
    run2 = (await c.post(f"/api/runbooks/{rb['id']}/run", json={"customer_id": cust["id"]})).json()
    run2 = await _wait_run(c, run2["id"], {"waiting_approval"})
    aid3 = run2["steps"][0]["state"]["approval_id"]
    await c.post(f"/api/runbooks/runs/{run2['id']}/cancel")
    a = (await c.get(f"/api/gateway/approvals/{aid3}")).json()
    assert a["status"] == "rejected" and a["decision_note"] == "runbook run cancelled"
    await runbooks.wait_idle()
    assert (await c.get(f"/api/runbooks/runs/{run2['id']}")).json()["status"] == "cancelled"


async def test_policy_decides_per_step(admin_client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Runbook Policy"})).json()
    steps = [{"id": "n", "kind": "tool", "tool": "update_customer",
              "args": {"customer": "{{customer.id}}", "append_note": "policy"}}]
    rb = (await c.post("/api/runbooks", json={"name": "Policy test", "customer_id": cust["id"], "steps": steps})).json()
    try:
        # Allowed by the policy: runs right away without approval.
        run = (await c.post(f"/api/runbooks/{rb['id']}/run", json={})).json()
        run = await _wait_run(c, run["id"], {"succeeded"})
        assert run["steps"][0]["state"]["approval_id"] is None

        await c.put("/api/gateway/policies/global", json={**TEST_GLOBAL, "risks": {**TEST_GLOBAL["risks"],
                                                                                    "change": "approve"}})
        run = (await c.post(f"/api/runbooks/{rb['id']}/run", json={})).json()
        run = await _wait_run(c, run["id"], {"waiting_approval"})
        a = (await c.get(f"/api/gateway/approvals/{run['steps'][0]['state']['approval_id']}")).json()
        assert [r["code"] for r in a["reasons"]] == ["runbook", "risk"]
        await c.post(f"/api/runbooks/runs/{run['id']}/cancel")

        await c.put(f"/api/gateway/policies/customers/{cust['id']}", json={
            "risks": {}, "tools": {"update_customer": "block"}, "outside_window": "inherit", "paused": False})
        run = (await c.post(f"/api/runbooks/{rb['id']}/run", json={})).json()
        run = await _wait_run(c, run["id"], {"failed"})
        assert "blocked by policy" in run["steps"][0]["state"]["error"]
    finally:
        await c.put("/api/gateway/policies/global", json=TEST_GLOBAL)
        await c.delete(f"/api/gateway/policies/customers/{cust['id']}")


# ------------------------------------------------------------------ MCP
async def test_runbooks_over_mcp(admin_client, client):
    c = admin_client
    cust = (await c.post("/api/customers", json={"name": "Runbook MCP"})).json()
    token, tok_id = await _token(c, "claude-rb", ["read", "nodes", "runbooks"])
    await _init(client, token)
    steps = [
        {"id": "ctx", "kind": "tool", "tool": "get_customer_context", "args": {"customer": "{{customer.name}}"}},
        {"id": "ai", "kind": "manual", "assignee": "ai", "instructions": "Summarise the notes of {{customer.name}}"},
        {"id": "note", "kind": "tool", "tool": "update_customer", "approval": "always",
         "args": {"customer": "{{customer.id}}", "append_note": "AI said: {{steps.ai.note}}"}},
        {"id": "human", "kind": "manual", "instructions": "A person checks"},
    ]
    saved, err = await _call(client, token, "save_runbook", {"name": "MCP runbook", "steps": steps,
                                                             "category": "other", "customer": cust["name"]})
    assert err is None, err
    assert saved["customer_id"] == cust["id"] and saved["created_by"] == "claude-rb"
    lst, _ = await _call(client, token, "list_runbooks", {"customer": cust["name"]}, 11)
    assert any(x["name"] == "MCP runbook" for x in lst)

    # A token without the scopes of the steps cannot start it.
    weak, _ = await _token(c, "claude-weak", ["read", "runbooks"])
    await _init(client, weak)
    _, err = await _call(client, weak, "run_runbook", {"runbook": "MCP runbook", "reason": "x"}, 12)
    assert err and "nodes" in err

    out, err = await _call(client, token, "run_runbook", {"runbook": "MCP runbook", "reason": "testing"}, 13)
    assert err is None, err
    rid = out["run_id"]
    run = await _wait_run(c, rid, {"waiting_input"})
    assert run["trigger"] == "mcp" and run["token_name"] == "claude-rb" and run["customer_name"] == "Runbook MCP"
    done, err = await _call(client, token, "complete_runbook_step", {"run_id": rid, "note": "All quiet"}, 14)
    assert err is None, err
    run = await _wait_run(c, rid, {"waiting_approval"})
    a = (await c.get(f"/api/gateway/approvals/{run['steps'][2]['state']['approval_id']}")).json()
    assert a["token_name"] == "claude-rb" and a["args"]["append_note"] == "AI said: All quiet"
    assert a["ai_reason"] == "testing"
    await c.post(f"/api/gateway/approvals/{a['id']}/approve", json={})  # runs with the token's rights
    run = await _wait_run(c, rid, {"waiting_input"})
    assert run["steps"][2]["state"]["status"] == "succeeded"
    # A step for a person cannot be confirmed by the AI.
    _, err = await _call(client, token, "complete_runbook_step", {"run_id": rid, "note": "done"}, 15)
    assert err and "person" in err
    got, _ = await _call(client, token, "get_runbook_run", {"run_id": rid}, 16)
    assert got["status"] == "waiting_input" and got["steps"][1]["state"]["note"] == "All quiet"
    out, err = await _call(client, token, "cancel_runbook_run", {"run_id": rid, "reason": "enough"}, 17)
    assert err is None and out["status"] == "cancelled"
    runs, _ = await _call(client, token, "list_runbook_runs", {"customer": cust["name"]}, 18)
    assert runs[0]["id"] == rid

    _, err = await _call(client, token, "delete_runbook", {"runbook": "MCP runbook", "confirm": "nope"}, 19)
    assert err and "confirm" in err
    _, err = await _call(client, token, "delete_runbook", {"runbook": "MCP runbook", "confirm": "MCP runbook"}, 20)
    assert err is None, err


async def test_templates_and_tools_endpoints(admin_client):
    c = admin_client
    tpl = (await c.get("/api/runbooks/templates", params={"lang": "nl"})).json()
    patch = next(t for t in tpl if t["key"] == "patch_server")
    assert patch["name"] == "Server patchen"
    r = await c.post("/api/runbooks", json={"name": patch["name"], "category": patch["category"],
                                             "params": patch["params"], "steps": patch["steps"], "template": "patch_server"})
    assert r.status_code == 201, r.text
    assert r.json()["template"] == "patch_server" and r.json()["steps"][2]["wait_for_job"] is True
    tools = {t["name"]: t for t in (await c.get("/api/runbooks/tools")).json()}
    assert "run_runbook" not in tools
    assert {p["name"] for p in tools["install_node_updates"]["params"]} == {"node", "confirm"}
    assert next(p for p in tools["run_command"]["params"] if p["name"] == "timeout")["required"] is False
    await c.delete(f"/api/runbooks/{r.json()['id']}")
