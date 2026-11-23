"""Runbooks (phase 13): fixed procedures, run step by step with approvals."""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, Runbook, RunbookRun, User
from mmn.services import gateway, runbook_templates, runbooks
from mmn.services.audit import audit

router = APIRouter(prefix="/api/runbooks", tags=["runbooks"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


async def _names(db: AsyncSession) -> dict[str, str]:
    return {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}


async def _runbook(db: AsyncSession, rid: str) -> Runbook:
    rb = await db.get(Runbook, rid)
    if rb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return rb


async def _run(db: AsyncSession, rid: str) -> RunbookRun:
    r = await db.get(RunbookRun, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return r


@router.get("")
async def list_runbooks(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                        _: User = Depends(current_user)):
    q = select(Runbook).order_by(Runbook.name)
    if customer_id:
        q = q.where((Runbook.customer_id == customer_id) | Runbook.customer_id.is_(None))
    rows = (await db.execute(q)).scalars().all()
    names = await _names(db)
    stats: dict[str, dict[str, Any]] = {}
    if rows:
        ids = [r.id for r in rows]
        counts = await db.execute(select(RunbookRun.runbook_id, func.count(), func.max(RunbookRun.created_at))
                                  .where(RunbookRun.runbook_id.in_(ids)).group_by(RunbookRun.runbook_id))
        for rid, n, last in counts:
            stats[rid] = {"runs": n, "last_run_at": last}
        active = await db.execute(select(RunbookRun.runbook_id, func.count()).where(
            RunbookRun.runbook_id.in_(ids), RunbookRun.status.not_in(tuple(runbooks.FINAL)))
            .group_by(RunbookRun.runbook_id))
        for rid, n in active:
            stats.setdefault(rid, {})["active"] = n
    return [{**runbooks.runbook_out(r, names.get(r.customer_id)), "runs": stats.get(r.id, {}).get("runs", 0),
             "last_run_at": stats.get(r.id, {}).get("last_run_at"), "active": stats.get(r.id, {}).get("active", 0)}
            for r in rows]


@router.get("/templates")
async def templates(lang: str = Query("en"), _: User = Depends(current_user)):
    return runbook_templates.all_localized(lang)


def _tool_params(spec: gateway.ToolSpec) -> list[dict[str, Any]]:
    if spec.source == "hub":
        from mmn.services import hub

        h = hub.HUB_TOOLS.get(spec.name)
        schema = (h.input_schema if h else {}) or {}
        req = set(schema.get("required") or [])
        return [{"name": k, "required": k in req, "type": (v or {}).get("type"),
                 "description": ((v or {}).get("description") or "")[:200]}
                for k, v in (schema.get("properties") or {}).items()]
    if spec.call is None:
        return []
    out = []
    for p in inspect.signature(spec.call).parameters.values():
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        ann = p.annotation if isinstance(p.annotation, str) else getattr(p.annotation, "__name__", "")
        out.append({"name": p.name, "required": p.default is inspect.Parameter.empty,
                    "default": None if p.default is inspect.Parameter.empty else p.default, "type": str(ann)})
    return out


@router.get("/tools")
async def tools(_: User = Depends(require_admin)):
    """The tools a step can call, with their arguments (for the editor)."""
    import mmn.mcp_server  # noqa: F401 - registers the tools

    out = []
    for t in gateway.catalog():
        if t["name"] in runbooks.FORBIDDEN_TOOLS:
            continue
        spec = gateway.REGISTRY[t["name"]]
        out.append({**t, "params": _tool_params(spec)})
    return out


class ParamIn(BaseModel):
    name: str = Field(max_length=32)
    label: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    required: bool = True
    default: Any = None


class RunbookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    category: str = "other"
    customer_id: str | None = None
    enabled: bool = True
    use_window: bool = True
    params: list[ParamIn] = Field(default_factory=list, max_length=runbooks.MAX_PARAMS)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=runbooks.MAX_STEPS)
    triggers: list[Any] = Field(default_factory=list, max_length=50)
    template: str | None = Field(default=None, max_length=64)


async def _save(db: AsyncSession, rb: Runbook | None, body: RunbookIn, admin: User) -> Runbook:
    try:
        return await runbooks.save(db, rb, name=body.name, description=body.description, category=body.category,
                                   customer_id=body.customer_id or None, enabled=body.enabled,
                                   params=[p.model_dump() for p in body.params], steps=body.steps,
                                   use_window=body.use_window, triggers=body.triggers, by=admin.username,
                                   template=body.template)
    except runbooks.RunbookError as e:
        raise _bad(e) from e


@router.post("", status_code=201)
async def create(body: RunbookIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    rb = await _save(db, None, body, admin)
    await audit(db, "runbook.create", user=admin, target=rb.name, ip=client_ip(request), runbook_id=rb.id,
                steps=len(rb.steps), customer=rb.customer_id)
    await db.commit()
    return runbooks.runbook_out(rb, (await _names(db)).get(rb.customer_id))


@router.get("/runs")
async def list_runs(runbook_id: str | None = None, state: str = Query("all"), customer_id: str | None = None,
                    limit: int = Query(50, ge=1, le=500), db: AsyncSession = Depends(get_db),
                    _: User = Depends(current_user)):
    q = select(RunbookRun).order_by(RunbookRun.created_at.desc()).limit(limit)
    if runbook_id:
        q = q.where(RunbookRun.runbook_id == runbook_id)
    if customer_id:
        q = q.where(RunbookRun.customer_id == customer_id)
    if state == "active":
        q = q.where(RunbookRun.status.not_in(tuple(runbooks.FINAL)))
    elif state == "waiting":
        q = q.where(RunbookRun.status.in_(("waiting_approval", "waiting_input")))
    return [runbooks.run_out(r, full=False) for r in (await db.execute(q)).scalars()]


@router.get("/runs/count")
async def runs_count(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = (await db.execute(select(func.count()).select_from(RunbookRun).where(
        RunbookRun.status == "waiting_input"))).scalar_one()
    return {"waiting_input": n}


@router.get("/runs/{rid}")
async def get_run(rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return runbooks.run_out(await _run(db, rid))


class CompleteIn(BaseModel):
    ok: bool = True
    note: str | None = Field(default=None, max_length=4000)


@router.post("/runs/{rid}/complete")
async def complete(rid: str, body: CompleteIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    r = await _run(db, rid)
    try:
        await runbooks.complete_step(db, r, by=admin.username, note=body.note, ok=body.ok, via="ui")
    except runbooks.RunbookError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await db.commit()
    runbooks.kick(r.id)
    return runbooks.run_out(r)


@router.post("/runs/{rid}/cancel")
async def cancel(rid: str, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    r = await _run(db, rid)
    try:
        await runbooks.cancel(db, r, by=admin.username, via="ui")
    except runbooks.RunbookError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await db.commit()
    return runbooks.run_out(r)


class RetryIn(BaseModel):
    skip: bool = False


@router.post("/runs/{rid}/retry")
async def retry(rid: str, body: RetryIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    r = await _run(db, rid)
    try:
        await runbooks.retry(db, r, by=admin.username, skip=body.skip)
    except runbooks.RunbookError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await db.commit()
    runbooks.kick(r.id)
    return runbooks.run_out(r)


@router.get("/{rid}")
async def get_runbook(rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rb = await _runbook(db, rid)
    return runbooks.runbook_out(rb, (await _names(db)).get(rb.customer_id))


@router.put("/{rid}")
async def update(rid: str, body: RunbookIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    rb = await _save(db, await _runbook(db, rid), body, admin)
    await audit(db, "runbook.update", user=admin, target=rb.name, ip=client_ip(request), runbook_id=rb.id,
                version=rb.version)
    await db.commit()
    return runbooks.runbook_out(rb, (await _names(db)).get(rb.customer_id))


@router.delete("/{rid}", status_code=204)
async def delete(rid: str, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    rb = await _runbook(db, rid)
    active = (await db.execute(select(func.count()).select_from(RunbookRun).where(
        RunbookRun.runbook_id == rb.id, RunbookRun.status.not_in(tuple(runbooks.FINAL))))).scalar_one()
    if active:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="this runbook has runs that are not finished")
    await audit(db, "runbook.delete", user=admin, target=rb.name, ip=client_ip(request), runbook_id=rb.id)
    await db.delete(rb)
    await db.commit()


class RunIn(BaseModel):
    customer_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/{rid}/run", status_code=201)
async def start(rid: str, body: RunIn, request: Request, db: AsyncSession = Depends(get_db),
                admin: User = Depends(require_admin)):
    rb = await _runbook(db, rid)
    customer = await db.get(Environment, body.customer_id) if body.customer_id else None
    if body.customer_id and customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="customer not found")
    try:
        r = await runbooks.start_run(db, rb, customer=customer, params=body.params, trigger="ui", reason=body.reason,
                                     user_id=admin.id, username=admin.username)
    except runbooks.RunbookError as e:
        raise _bad(e) from e
    await audit(db, "runbook.run", user=admin, target=rb.name, ip=client_ip(request), run_id=r.id,
                customer=r.customer_name, params=r.params)
    await db.commit()
    runbooks.kick(r.id)
    return runbooks.run_out(r)
