"""Runbooks: fixed procedures that Stackpilot runs step by step with its own tools (phase 13).

A runbook ("patch a server", "new employee", "new project via GitHub") is a list of steps:

- ``tool``: call a Stackpilot tool (or an MCP hub tool) with arguments; the arguments may hold
  placeholders: ``{{params.server}}``, ``{{customer.name}}``, ``{{customer.id}}``, ``{{run.id}}`` and
  ``{{steps.<step id>.result.<path>}}`` (the result of an earlier step);
- ``manual``: an instruction that a person (or, with ``assignee`` "ai", the AI) carries out and
  confirms;
- ``wait``: wait a number of minutes (for example for a machine to come back after a reboot).

Every tool step goes through the AI gateway like any AI call: the policy of the customer (and of the
token, when an AI started the run) decides allow, approve or block, outside the maintenance window
included. A step with ``approval`` "always" always asks an admin. Approval requests are the usual
ones (Approvals inbox, app and mail notifications); once approved, the app runs the step and the run
goes on. With ``use_window`` a step that needs approval because it is outside the maintenance window
waits for the window once approved. Every call is in the audit log with the run's id.

Runs started by an AI token use that token's rights and customers. Runs started in the app use the
rights of the admin who started them, and approvals of their steps the rights of the admin who
approves. ``start_run`` is also the entry point for automatic recovery (phase 14): it takes a
``trigger`` ("remediation") and a ``context`` (the alert or finding that the run answers).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import AiApproval, ApiToken, Environment, NodeJob, Runbook, RunbookRun
from mmn.services import events, tenancy

log = logging.getLogger("mmn.runbooks")

CATEGORIES = ("maintenance", "onboarding", "project", "remediation", "other")
KINDS = ("tool", "manual", "wait")
TRIGGERS = ("ui", "mcp", "remediation", "schedule")
FINAL = {"succeeded", "failed", "cancelled"}
STEP_FINAL = {"succeeded", "failed", "rejected", "skipped", "cancelled"}
MAX_STEPS = 50
MAX_PARAMS = 20
JOB_TIMEOUT_MIN = 180
# Tools a step may not call: runbooks do not start or change runbooks.
FORBIDDEN_TOOLS = {"run_runbook", "save_runbook", "delete_runbook", "complete_runbook_step", "cancel_runbook_run"}

_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")
_HUB_TOOL = re.compile(r"^[a-z][a-z0-9_]{0,23}__[A-Za-z0-9_-]{1,64}$")


class RunbookError(Exception):
    pass


# ------------------------------------------------------------------ definitions
def _str(v: Any, limit: int) -> str:
    return (v if isinstance(v, str) else "" if v is None else str(v)).strip()[:limit]


def clean_params(params: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out, seen = [], set()
    for p in (params or [])[:MAX_PARAMS + 1]:
        name = _str(p.get("name"), 32)
        if not _ID.match(name):
            raise RunbookError(f"parameter name '{name}': use lower case letters, digits and _")
        if name in seen:
            raise RunbookError(f"parameter '{name}' appears twice")
        seen.add(name)
        default = p.get("default")
        out.append({"name": name, "label": _str(p.get("label"), 120) or name,
                    "description": _str(p.get("description"), 500),
                    "required": bool(p.get("required", True)),
                    "default": None if default in (None, "") else _str(default, 2000)})
    if len(out) > MAX_PARAMS:
        raise RunbookError(f"at most {MAX_PARAMS} parameters")
    return out


def tool_known(tool: str) -> bool:
    from mmn.services import gateway

    return tool in gateway.REGISTRY or bool(_HUB_TOOL.match(tool))


def clean_steps(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from mmn.services import gateway

    if not steps:
        raise RunbookError("a runbook needs at least one step")
    if len(steps) > MAX_STEPS:
        raise RunbookError(f"at most {MAX_STEPS} steps")
    out, seen = [], set()
    for i, s in enumerate(steps, 1):
        kind = s.get("kind") or "tool"
        if kind not in KINDS:
            raise RunbookError(f"step {i}: unknown kind {kind}")
        sid = _str(s.get("id"), 32) or f"s{i}"
        if not _ID.match(sid):
            raise RunbookError(f"step {i}: id '{sid}': use lower case letters, digits and _")
        if sid in seen:
            raise RunbookError(f"step {i}: id '{sid}' appears twice")
        seen.add(sid)
        title = _str(s.get("title"), 200)
        st: dict[str, Any] = {"id": sid, "kind": kind, "title": title,
                              "instructions": _str(s.get("instructions"), 4000),
                              "on_failure": "continue" if s.get("on_failure") == "continue" else "stop"}
        if kind == "tool":
            tool = _str(s.get("tool"), 100)
            if not tool:
                raise RunbookError(f"step {i}: choose a tool")
            if tool in FORBIDDEN_TOOLS:
                raise RunbookError(f"step {i}: a runbook cannot call {tool}")
            if not tool_known(tool):
                raise RunbookError(f"step {i}: unknown tool {tool}")
            args = s.get("args") or {}
            if not isinstance(args, dict):
                raise RunbookError(f"step {i}: arguments must be an object")
            if len(json.dumps(args, default=str)) > 20_000:
                raise RunbookError(f"step {i}: arguments are too large")
            st.update(tool=tool, args=json.loads(json.dumps(args, default=str)),
                      approval="always" if s.get("approval") == "always" else "policy",
                      wait_for_job=bool(s.get("wait_for_job", False)),
                      # Rejecting an optional step skips it instead of stopping the run.
                      optional=bool(s.get("optional", False)))
            st["title"] = title or tool
            spec = gateway.REGISTRY.get(tool)
            st["risk"] = spec.risk if spec else "change"
        elif kind == "manual":
            if not st["instructions"] and not title:
                raise RunbookError(f"step {i}: describe what has to be done")
            st["assignee"] = "ai" if s.get("assignee") == "ai" else "human"
        else:
            try:
                minutes = int(s.get("minutes") or 0)
            except (TypeError, ValueError):
                minutes = 0
            if not 1 <= minutes <= 24 * 60:
                raise RunbookError(f"step {i}: wait between 1 and 1440 minutes")
            st["minutes"] = minutes
            st["title"] = title or f"Wait {minutes} min"
        out.append(st)
    # Placeholders may only refer to earlier steps.
    for i, st in enumerate(out):
        earlier = {x["id"] for x in out[:i]}
        for ref in _refs(st.get("args") or {}):
            parts = ref.split(".")
            if parts[0] == "steps" and (len(parts) < 2 or parts[1] not in earlier):
                raise RunbookError(f"step {i + 1}: {{{{{ref}}}}} does not refer to an earlier step")
    return out


def _refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [m.group(1) for m in _PLACEHOLDER.finditer(value)]
    if isinstance(value, dict):
        return [r for v in value.values() for r in _refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    return []


def check_param_refs(steps: list[dict[str, Any]], params: list[dict[str, Any]]) -> None:
    names = {p["name"] for p in params}
    for i, st in enumerate(steps, 1):
        for ref in _refs(st.get("args") or {}) + _refs(st.get("instructions") or ""):
            parts = ref.split(".")
            if parts[0] == "params" and (len(parts) != 2 or parts[1] not in names):
                raise RunbookError(f"step {i}: {{{{{ref}}}}} is not a parameter of this runbook")
            if parts[0] not in ("params", "customer", "run", "steps"):
                raise RunbookError(f"step {i}: unknown placeholder {{{{{ref}}}}}")


def runbook_out(rb: Runbook, customer_name: str | None = None) -> dict[str, Any]:
    return {"id": rb.id, "name": rb.name, "description": rb.description, "category": rb.category,
            "customer_id": rb.customer_id, "customer_name": customer_name, "enabled": rb.enabled,
            "params": rb.params or [], "steps": rb.steps or [], "use_window": rb.use_window,
            "triggers": rb.triggers or [], "version": rb.version, "template": rb.template,
            "created_at": rb.created_at, "created_by": rb.created_by, "updated_at": rb.updated_at,
            "updated_by": rb.updated_by}


async def find_runbook(db: AsyncSession, ref: str) -> Runbook:
    rb = await db.get(Runbook, ref)
    if rb is None:
        rows = (await db.execute(select(Runbook).order_by(Runbook.name))).scalars().all()
        matches = [r for r in rows if r.name.lower() == ref.strip().lower()]
        if len(matches) > 1:
            raise RunbookError(f"more than one runbook is called '{ref}': use its id")
        rb = matches[0] if matches else None
    if rb is None:
        raise RunbookError(f"runbook '{ref}' not found")
    return rb


async def save(db: AsyncSession, rb: Runbook | None, *, name: str, description: str | None, category: str,
               customer_id: str | None, enabled: bool, params: list[dict[str, Any]], steps: list[dict[str, Any]],
               use_window: bool, triggers: list[Any] | None, by: str | None, template: str | None = None) -> Runbook:
    name = _str(name, 120)
    if not name:
        raise RunbookError("give the runbook a name")
    if category not in CATEGORIES:
        raise RunbookError(f"category must be one of {', '.join(CATEGORIES)}")
    cp, cs = clean_params(params), clean_steps(steps)
    check_param_refs(cs, cp)
    if customer_id is not None and await db.get(Environment, customer_id) is None:
        raise RunbookError("customer not found")
    now = datetime.now(UTC)
    if rb is None:
        rb = Runbook(created_by=by, created_at=now, version=1, template=template)
        db.add(rb)
    else:
        rb.version = (rb.version or 1) + 1
    rb.name, rb.description, rb.category = name, (_str(description, 4000) or None), category
    rb.customer_id, rb.enabled, rb.params, rb.steps = customer_id, bool(enabled), cp, cs
    rb.use_window = bool(use_window)
    rb.triggers = json.loads(json.dumps(triggers or [], default=str))[:50]
    rb.updated_at, rb.updated_by = now, by
    await db.flush()
    return rb


# ------------------------------------------------------------------ placeholders
def _lookup(ctx: dict[str, Any], ref: str) -> Any:
    cur: Any = ctx
    for part in ref.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise RunbookError(f"{{{{{ref}}}}} has no value")
    return cur


def render(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill in the placeholders. A string that is exactly one placeholder keeps the value's type."""
    if isinstance(value, str):
        m = _PLACEHOLDER.fullmatch(value.strip())
        if m:
            return _lookup(ctx, m.group(1))

        def sub(mm: re.Match) -> str:
            v = _lookup(ctx, mm.group(1))
            return v if isinstance(v, str) else json.dumps(v, default=str)

        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def _context(run: RunbookRun) -> dict[str, Any]:
    steps = {}
    for sd, st in zip(run.definition.get("steps") or [], run.state or [], strict=False):
        steps[sd["id"]] = {"status": st.get("status"), "result": st.get("result"), "note": st.get("note")}
    return {"params": run.params or {}, "customer": {"id": run.customer_id, "name": run.customer_name},
            "run": {"id": run.id}, "steps": steps}


def resolve_params(defs: list[dict[str, Any]], given: dict[str, Any] | None) -> dict[str, Any]:
    given = dict(given or {})
    unknown = set(given) - {p["name"] for p in defs}
    if unknown:
        raise RunbookError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    out = {}
    for p in defs:
        v = given.get(p["name"])
        if v is None or (isinstance(v, str) and not v.strip()):
            v = p.get("default")
        if (v is None or v == "") and p.get("required", True):
            raise RunbookError(f"parameter '{p['name']}' ({p.get('label') or p['name']}) is required")
        out[p["name"]] = v.strip() if isinstance(v, str) else v
    return out


# ------------------------------------------------------------------ runs
def _step_state(sd: dict[str, Any]) -> dict[str, Any]:
    return {"id": sd["id"], "status": "pending", "approval_id": None, "job_id": None, "wait_until": None,
            "started_at": None, "finished_at": None, "args": None, "result": None, "error": None,
            "decided_by": None, "note": None}


def run_out(run: RunbookRun, *, full: bool = True) -> dict[str, Any]:
    steps = run.definition.get("steps") or []
    state = run.state or []
    done = sum(1 for s in state if s.get("status") in STEP_FINAL)
    out = {"id": run.id, "runbook_id": run.runbook_id, "runbook_name": run.runbook_name,
           "runbook_version": run.runbook_version, "customer_id": run.customer_id, "customer_name": run.customer_name,
           "status": run.status, "current_step": run.current_step, "steps_total": len(steps), "steps_done": done,
           "trigger": run.trigger, "reason": run.reason, "started_by": run.token_name or run.username,
           "username": run.username, "token_name": run.token_name, "created_at": run.created_at,
           "updated_at": run.updated_at, "finished_at": run.finished_at, "error": run.error}
    if 0 <= run.current_step < len(steps) and run.status not in FINAL:
        cur = steps[run.current_step]
        out["current"] = {"index": run.current_step, "id": cur["id"], "title": cur.get("title"), "kind": cur["kind"],
                          "status": state[run.current_step].get("status") if run.current_step < len(state) else None,
                          "approval_id": state[run.current_step].get("approval_id") if run.current_step < len(state)
                          else None}
    if full:
        out["params"] = run.params or {}
        out["context"] = run.context or {}
        out["steps"] = [{**sd, **{"state": st}} for sd, st in zip(steps, state, strict=False)]
    return out


async def start_run(db: AsyncSession, rb: Runbook, *, customer: Environment | None, params: dict[str, Any] | None,
                    trigger: str, reason: str | None, user_id: str | None, username: str | None,
                    token_id: str | None = None, token_name: str | None = None,
                    context: dict[str, Any] | None = None) -> RunbookRun:
    """Start a run (the caller commits, then calls ``kick(run.id)``)."""
    if not rb.enabled:
        raise RunbookError("this runbook is disabled")
    if rb.customer_id is not None:
        if customer is not None and customer.id != rb.customer_id:
            raise RunbookError("this runbook belongs to another customer")
        customer = customer or await db.get(Environment, rb.customer_id)
    if trigger not in TRIGGERS:
        raise RunbookError(f"unknown trigger {trigger}")
    values = resolve_params(rb.params or [], params)
    definition = {"steps": copy.deepcopy(rb.steps or []), "params": copy.deepcopy(rb.params or []),
                  "use_window": rb.use_window, "category": rb.category, "description": rb.description}
    now = datetime.now(UTC)
    run = RunbookRun(runbook_id=rb.id, runbook_name=rb.name, runbook_version=rb.version or 1, definition=definition,
                     customer_id=customer.id if customer else None, customer_name=customer.name if customer else None,
                     params=json.loads(json.dumps(values, default=str)), status="running", current_step=0,
                     state=[_step_state(sd) for sd in definition["steps"]], trigger=trigger,
                     reason=_str(reason, 2000) or None, context=json.loads(json.dumps(context or {}, default=str)),
                     user_id=user_id, username=username, token_id=token_id, token_name=token_name,
                     created_at=now, updated_at=now)
    db.add(run)
    await db.flush()
    return run


# Steps of one run never run at the same time.
_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Task] = set()


def kick(run_id: str) -> None:
    """Let a run go on (in the background)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(advance(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def wait_idle(timeout: float = 10) -> None:
    """Tests: wait until no run is busy."""
    deadline = asyncio.get_running_loop().time() + timeout
    while _tasks and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)


def _publish(run: RunbookRun) -> None:
    events.publish("runbook.changed", {"id": run.id, "status": run.status, "step": run.current_step,
                                       "runbook": run.runbook_name})


async def _principal_for(db: AsyncSession, run: RunbookRun):
    """The rights a step runs with: the token that started the run, or the admin who started it."""
    from mmn.api.tokens import SCOPES, effective_scopes
    from mmn.mcp_server import Principal
    from mmn.models import Role, User

    if run.token_id:
        tok = await db.get(ApiToken, run.token_id)
        now = datetime.now(UTC)
        if tok is None or tok.revoked or (tok.expires_at and tok.expires_at < now) or not tok.user.is_active:
            raise RunbookError("the token that started this run was revoked or expired")
        return Principal(user_id=tok.user_id, username=tok.user.username, token_id=tok.id, token_name=tok.name,
                         token_kind=tok.kind, scopes=effective_scopes(tok), ip=None,
                         client={**(tok.last_client or {}), "runbook_run": run.id},
                         customers=frozenset(tok.customer_ids) if tok.customer_ids is not None else None)
    if run.trigger == "remediation" and not run.user_id:
        # Automatic recovery (phase 14): Stackpilot itself, within the policy and the recovery guardrails.
        return Principal(user_id=None, username=None, token_id=None, token_name="Automatic recovery",
                         token_kind="remediation", scopes=set(SCOPES), ip=None,
                         client={"name": "Automatic recovery", "source": "remediation", "runbook_run": run.id},
                         customers=None)
    u = await db.get(User, run.user_id) if run.user_id else None
    if u is None or not u.is_active or u.role != Role.admin:
        raise RunbookError("the admin who started this run no longer exists or is not an admin")
    return Principal(user_id=u.id, username=u.username, token_id=None, token_name=f"Runbook: {run.runbook_name}",
                     token_kind="runbook", scopes=set(SCOPES), ip=None,
                     client={"name": f"Runbook: {run.runbook_name}", "source": "runbook", "runbook_run": run.id},
                     customers=None)


def _short(result: Any) -> Any:
    data = json.loads(json.dumps(result, default=str))
    if len(json.dumps(data)) > 50_000:
        return {"truncated": True, "preview": json.dumps(data)[:50_000]}
    return data


def _is_job(result: Any) -> str | None:
    if isinstance(result, dict) and result.get("id") and result.get("node_id") and result.get("kind") \
            and result.get("status") in ("running", "success", "failed"):
        return str(result["id"])
    return None


async def advance(run_id: str) -> None:
    lock = _locks.setdefault(run_id, asyncio.Lock())
    async with lock:
        try:
            with tenancy.unrestricted():
                await _advance(run_id)
        except Exception:  # noqa: BLE001 - a broken run must not break the scheduler
            log.exception("runbook run %s", run_id)
            with tenancy.unrestricted():
                async with sessionmaker()() as db:
                    run = await db.get(RunbookRun, run_id)
                    if run is not None and run.status not in FINAL:
                        run.status, run.error, run.finished_at = "failed", "internal error (see the app log)", datetime.now(UTC)
                        await db.commit()
                        _publish(run)


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _json(v: Any) -> Any:
    return json.loads(json.dumps(v, default=_iso))


async def _save(db: AsyncSession, run: RunbookRun, state: list[dict[str, Any]]) -> None:
    run.state = _json(state)
    run.updated_at = datetime.now(UTC)
    await db.commit()
    _publish(run)


async def _audit_run(db: AsyncSession, run: RunbookRun, action: str, success: bool = True, **detail: Any) -> None:
    from mmn.models import AuditLog

    db.add(AuditLog(action=f"runbook.{action}", user_id=run.user_id, username=run.username or
                    (run.token_name if run.trigger == "remediation" else None),
                    source="mcp" if run.token_id else "system" if run.trigger == "remediation" else "ui", target=run.runbook_name[:255], success=success,
                    detail=json.loads(json.dumps({"run_id": run.id, "customer": run.customer_name,
                                                  "token": run.token_name, **detail}, default=str))))


async def _finish(db: AsyncSession, run: RunbookRun, state: list[dict[str, Any]], status: str,
                  error: str | None = None) -> None:
    run.status, run.error, run.finished_at = status, error, datetime.now(UTC)
    await _audit_run(db, run, "finished", status == "succeeded", status=status, error=error)
    await _save(db, run, state)


async def _advance(run_id: str) -> None:  # noqa: C901 - one step machine, easier to read in one place
    from mmn.services import ai_notify, gateway

    for _ in range(MAX_STEPS * 2 + 5):
        async with sessionmaker()() as db:
            run = await db.get(RunbookRun, run_id)
            if run is None or run.status in FINAL:
                return
            steps = run.definition.get("steps") or []
            state = copy.deepcopy(run.state or [])
            idx = run.current_step
            if idx >= len(steps):
                await _finish(db, run, state, "succeeded")
                return
            sd, st = steps[idx], state[idx]
            now = datetime.now(UTC)

            if st["status"] in STEP_FINAL:
                failed = st["status"] in ("failed", "rejected")
                if failed and (st["status"] == "rejected" or sd.get("on_failure") != "continue"):
                    msg = st.get("error") or st["status"]
                    await _finish(db, run, state, "failed", f"step {idx + 1} ({sd.get('title')}): {msg}")
                    return
                run.current_step = idx + 1
                run.status = "running"
                await _save(db, run, state)
                continue

            if st["status"] == "waiting_approval":
                a = await db.get(AiApproval, st["approval_id"]) if st.get("approval_id") else None
                if a is None:
                    st.update(status="failed", error="the approval request is gone", finished_at=now)
                elif a.status in ("pending", "approved", "running"):
                    if run.status != "waiting_approval" and a.status == "pending":
                        run.status = "waiting_approval"
                        await _save(db, run, state)
                    return
                elif a.status == "executed":
                    st.update(decided_by=a.decided_by, result=_short(a.result))
                    job = _is_job(a.result) if sd.get("wait_for_job") else None
                    if job:
                        st.update(status="waiting_job", job_id=job)
                        run.status = "running"
                        await _save(db, run, state)
                        continue
                    st.update(status="succeeded", finished_at=now)
                elif a.status == "rejected":
                    note = f": {a.decision_note}" if a.decision_note else ""
                    st.update(status="skipped" if sd.get("optional") and run.status != "cancelled" else "rejected",
                              decided_by=a.decided_by, finished_at=now, error=f"rejected by {a.decided_by}{note}")
                elif a.status == "expired":
                    st.update(status="failed", error="the approval request expired", finished_at=now)
                else:
                    st.update(status="failed", decided_by=a.decided_by, error=a.error or a.status, finished_at=now)
                await _save(db, run, state)
                continue

            if st["status"] == "waiting_job":
                j = await db.get(NodeJob, st["job_id"])
                started = datetime.fromisoformat(st["started_at"]) if st.get("started_at") else now
                if j is None:
                    st.update(status="failed", error="the job is gone", finished_at=now)
                elif j.status == "running":
                    if now - started > timedelta(minutes=JOB_TIMEOUT_MIN):
                        st.update(status="failed", error=f"the job did not finish within {JOB_TIMEOUT_MIN} minutes",
                                  finished_at=now)
                    else:
                        return
                elif j.status == "success":
                    st.update(status="succeeded", finished_at=now,
                              result={**(st.get("result") or {}), "status": "success", "job_result": j.result})
                else:
                    st.update(status="failed", finished_at=now, error=j.error or "the job failed",
                              result={**(st.get("result") or {}), "status": j.status, "output": (j.output or "")[-4000:]})
                await _save(db, run, state)
                continue

            if st["status"] == "waiting_time":
                if datetime.fromisoformat(st["wait_until"]) > now:
                    return
                st.update(status="succeeded", finished_at=now)
                await _save(db, run, state)
                continue

            if st["status"] == "waiting_input":
                return

            if st["status"] == "running":
                # Only after a restart in the middle of a step: we can't tell whether it ran.
                st.update(status="failed", finished_at=now,
                          error="interrupted (Stackpilot restarted while this step ran); check and retry")
                await _save(db, run, state)
                continue

            # pending: start the step
            st["started_at"] = now.isoformat()
            ctx = _context(run)
            if sd["kind"] == "manual":
                try:
                    st["args"] = {"instructions": render(sd.get("instructions") or "", ctx)}
                except RunbookError as e:
                    st["args"] = {"instructions": sd.get("instructions") or "", "warning": str(e)}
                st["status"] = "waiting_input"
                run.status = "waiting_input"
                await _save(db, run, state)
                return
            if sd["kind"] == "wait":
                st.update(status="waiting_time",
                          wait_until=(now + timedelta(minutes=int(sd.get("minutes") or 1))).isoformat())
                run.status = "running"
                await _save(db, run, state)
                return

            # tool step
            tool = sd["tool"]
            spec = gateway.REGISTRY.get(tool)
            if run.trigger == "remediation":
                from mmn.services import remediation

                if not await remediation.active(db):
                    await cancel(db, run, by="Automatic recovery (stopped)", via="emergency stop")
                    await db.commit()
                    _publish(run)
                    return
            try:
                args = render(sd.get("args") or {}, ctx)
                if not isinstance(args, dict):
                    raise RunbookError("the arguments are not an object")
                if spec is None:
                    raise RunbookError(f"the tool {tool} does not exist (anymore); is its MCP server online?")
                principal = await _principal_for(db, run)
                if spec.scope not in principal.scopes and not (spec.source == "hub" and "hub" in principal.scopes):
                    raise RunbookError(f"the token that started this run lacks the '{spec.scope}' scope")
            except RunbookError as e:
                st.update(status="failed", error=str(e), finished_at=now)
                await _save(db, run, state)
                continue
            from mmn.mcp_server import _redact, _target_of

            st["args"] = _redact(json.loads(json.dumps(args, default=str)))
            risk = "read" if args.get("dry_run") is True else spec.risk
            recovery = run.trigger == "remediation"
            if recovery:
                why = await remediation.step_blocked(db, tool, args)
                if why:
                    st.update(status="failed", finished_at=now, error=why)
                    await _save(db, run, state)
                    continue
            # Placeholders (customer, nodes) decide which customer's policy applies; the run's own
            # customer always counts too.
            d = await gateway.decide(db, tool=tool, risk=risk,
                                     args={**args, **({"customer": run.customer_id} if run.customer_id
                                                      and spec.source != "hub" else {})},
                                     token_id=run.token_id, token_customers=principal.customers)
            if d.action in ("paused", "block"):
                st.update(status="failed", finished_at=now,
                          error=("blocked by policy: " if d.action == "block" else "refused: ")
                          + "; ".join(gateway.reason_text(r) for r in d.reasons))
                await _save(db, run, state)
                continue
            always = sd.get("approval") == "always"
            if recovery and (risk == "destructive" or (risk != "read" and (run.context or {}).get("approve_all"))):
                # Automatic recovery: destructive steps always ask an admin; in mode "approve" every
                # step that changes something does.
                always = True
            if d.action == "approve" or always:
                run_after = None
                if run.definition.get("use_window", True) and any(r.get("code") == "outside_window" for r in d.reasons):
                    run_after = await _window_start(db, d)
                a = await gateway.create_runbook_approval(
                    db, run_id=run.id, step=idx, label=f"Runbook: {run.runbook_name}", tool=tool, args=args,
                    target=_target_of(args) or run.customer_name, decision=d,
                    extra_reason={"code": "runbook", "params": {
                    "runbook": run.runbook_name, "step": idx + 1, "title": sd.get("title"), "always": always,
                    **({"recovery": ((run.context or {}).get("remediation") or {}).get("rule") or "-"}
                       if recovery else {})}},
                    token_id=run.token_id, token_name=run.token_name, user_id=run.user_id, username=run.username,
                    ai_reason=run.reason, run_after=run_after)
                st.update(status="waiting_approval", approval_id=a.id)
                run.status = "waiting_approval"
                await _save(db, run, state)
                try:
                    await ai_notify.notify(db, a)
                except Exception:  # noqa: BLE001
                    log.exception("approval notification")
                return
            st["status"] = "running"
            run.status = "running"
            await _save(db, run, state)

        # allowed: run it now with the run's rights (outside the session)
        status, result, error = await _call(run, principal, spec, args)
        async with sessionmaker()() as db:
            run = await db.get(RunbookRun, run_id)
            state = copy.deepcopy(run.state or [])
            st = state[idx]
            now = datetime.now(UTC)
            st["result"] = _short(result) if result is not None else None
            if status == "ok" and sd.get("wait_for_job") and (job := _is_job(result)):
                st.update(status="waiting_job", job_id=job)
            elif status == "ok":
                st.update(status="succeeded", finished_at=now)
            else:
                st.update(status="failed", error=error, finished_at=now)
            if run.status in FINAL:  # cancelled while the step ran
                await _save(db, run, state)
                return
            await _save(db, run, state)


async def _call(run: RunbookRun, principal: Any, spec: Any, args: dict[str, Any]) -> tuple[str, Any, str | None]:
    from mmn.mcp_server import _principal
    from mmn.services import gateway

    ptok = _principal.set(principal)
    ttok = tenancy.set_allowed(principal.customers)
    rtok = gateway.runbook_call.set(run.id)
    try:
        result = await spec.call(**args)
        if isinstance(result, dict) and result.get("isError"):
            text = " ".join(str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict))
            return "failed", result, (text or "the tool reported an error")[:4000]
        return "ok", result, None
    except Exception as e:  # noqa: BLE001 - shown on the step
        return "failed", None, (str(e) or type(e).__name__)[:4000]
    finally:
        gateway.runbook_call.reset(rtok)
        tenancy.reset(ttok)
        _principal.reset(ptok)


async def _window_start(db: AsyncSession, d: Any) -> datetime | None:
    from mmn.services import gateway

    starts = []
    now = await gateway.local_now(db)
    for cid, _ in d.customers:
        e = await db.get(Environment, cid)
        w = gateway.parse_window(e.maintenance_window) if e else None
        if w and (s := gateway.next_window_start(w, now)):
            starts.append(s)
    return max(starts).astimezone(UTC) if starts else None


# ------------------------------------------------------------------ actions on a run
async def complete_step(db: AsyncSession, run: RunbookRun, *, by: str, note: str | None, ok: bool,
                        via: str, ai: bool = False) -> RunbookRun:
    """Confirm (or fail) the manual step a run waits for."""
    steps = run.definition.get("steps") or []
    idx = run.current_step
    if run.status != "waiting_input" or idx >= len(steps):
        raise RunbookError("this run is not waiting for a manual step")
    sd = steps[idx]
    if ai and sd.get("assignee") != "ai":
        raise RunbookError("this step is for a person: they confirm it in Stackpilot (Runbooks)")
    state = copy.deepcopy(run.state)
    state[idx].update(status="succeeded" if ok else "failed", decided_by=by, note=_str(note, 4000) or None,
                      finished_at=datetime.now(UTC).isoformat(), error=None if ok else (_str(note, 4000) or "marked as failed"))
    run.status = "running"
    run.state = _json(state)
    run.updated_at = datetime.now(UTC)
    await _audit_run(db, run, "step", ok, step=idx + 1, title=sd.get("title"), by=by, via=via, note=note)
    await db.flush()
    return run


async def cancel(db: AsyncSession, run: RunbookRun, *, by: str, via: str) -> RunbookRun:
    if run.status in FINAL:
        raise RunbookError(f"this run is already {run.status}")
    state = copy.deepcopy(run.state)
    now = datetime.now(UTC)
    for st in state:
        if st.get("approval_id") and st.get("status") == "waiting_approval":
            a = await db.get(AiApproval, st["approval_id"])
            if a is not None and a.status == "pending":
                a.status, a.decided_at, a.decided_by = "rejected", now, by
                a.decision_note, a.args_enc = "runbook run cancelled", None
                events.publish("approval.changed", {"id": a.id, "status": a.status})
            elif a is not None and a.status == "approved" and a.run_after is not None:
                a.status, a.error, a.args_enc = "failed", "runbook run cancelled before it ran", None
                events.publish("approval.changed", {"id": a.id, "status": a.status})
        if st.get("status") not in STEP_FINAL and st.get("status") != "running":
            st["status"] = "cancelled"
            st["finished_at"] = st.get("finished_at") or now.isoformat()
    run.state, run.status, run.finished_at, run.updated_at = _json(state), "cancelled", now, now
    run.error = f"cancelled by {by}"
    await _audit_run(db, run, "cancel", by=by, via=via)
    await db.flush()
    return run


async def retry(db: AsyncSession, run: RunbookRun, *, by: str, skip: bool = False) -> RunbookRun:
    """Go on with a failed run: run the failed step again, or skip it."""
    if run.status != "failed":
        raise RunbookError("only a failed run can be retried")
    steps = run.definition.get("steps") or []
    idx = run.current_step
    if idx >= len(steps):
        raise RunbookError("nothing to retry")
    state = copy.deepcopy(run.state)
    if skip:
        state[idx].update(status="skipped", decided_by=by, finished_at=datetime.now(UTC).isoformat())
    else:
        state[idx] = _step_state(steps[idx])
    run.state, run.status, run.error, run.finished_at = _json(state), "running", None, None
    run.updated_at = datetime.now(UTC)
    await _audit_run(db, run, "skip" if skip else "retry", step=idx + 1, by=by)
    await db.flush()
    return run


async def tick() -> int:
    """Scheduler: go on with runs that wait for an approval, a job or the clock."""
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            ids = (await db.execute(select(RunbookRun.id).where(
                RunbookRun.status.in_(("running", "waiting_approval"))))).scalars().all()
    for rid in ids:
        if not (_locks.get(rid) and _locks[rid].locked()):
            kick(rid)
    return len(ids)


async def recover() -> None:
    """At start-up: steps that were running when the app stopped are marked interrupted."""
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            ids = (await db.execute(select(RunbookRun.id).where(RunbookRun.status == "running"))).scalars().all()
    for rid in ids:
        kick(rid)


async def prune(days: int = 365) -> int:
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=days)
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            res = await db.execute(delete(RunbookRun).where(RunbookRun.finished_at.is_not(None),
                                                            RunbookRun.finished_at < cutoff))
            await db.commit()
    return res.rowcount or 0
