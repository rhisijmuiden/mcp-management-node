"""Automatic recovery (phase 14): rules, attempts, settings and the emergency stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import current_user, require_admin
from mmn.db import get_db
from mmn.models import Alert, Node, RemediationAttempt, RemediationRule, RunbookRun, User
from mmn.services import netwatch, remediation, tenancy

router = APIRouter(prefix="/api/remediation", tags=["remediation"])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


def _visible(customer_id: str | None) -> bool:
    allowed = tenancy.allowed()
    return allowed is None or customer_id is None or customer_id in allowed


async def _rule(db: AsyncSession, rid: str) -> RemediationRule:
    r = await db.get(RemediationRule, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return r


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    cfg = await remediation.settings(db)
    allowed = tenancy.allowed()
    since = datetime.now(UTC) - timedelta(hours=24)
    s = await remediation.summary(db, since, None, set(allowed) if allowed is not None else None, limit=0)
    rules = (await db.execute(select(RemediationRule))).scalars().all()
    active = (await db.execute(select(RemediationAttempt).where(
        RemediationAttempt.status.in_(remediation.ACTIVE)))).scalars().all()
    return {"settings": cfg, "last_24h": s["counts"], "last_24h_total": s["total"],
            "rules": sum(1 for r in rules if _visible(r.customer_id)),
            "rules_enabled": sum(1 for r in rules if r.enabled and _visible(r.customer_id)),
            "active": sum(1 for a in active if _visible(a.customer_id)),
            "alert_kinds": list(remediation.ALERT_KINDS), "finding_codes": list(remediation.FINDING_CODES),
            "machine_types": list(remediation.MACHINE_TYPES), "modes": list(remediation.MODES),
            "network": await netwatch.status(db)}


class SettingsBody(BaseModel):
    enabled: bool | None = None
    max_runs_per_hour: int | None = None
    max_failures_per_hour: int | None = None
    service_check_minutes: int | None = None
    verify_minutes: int | None = None
    notify_app: bool | None = None
    notify_mail: str | None = None
    mail_to: str | None = None


@router.put("/settings")
async def put_settings(body: SettingsBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    try:
        cfg = await remediation.save_settings(db, body.model_dump(exclude_none=True), admin.username)
    except remediation.RemediationError as e:
        raise _bad(e) from e
    await db.commit()
    return cfg


class PauseBody(BaseModel):
    paused: bool
    note: str | None = Field(default=None, max_length=500)


@router.post("/pause")
async def pause(body: PauseBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """The emergency stop (and resume)."""
    cfg = await remediation.set_paused(db, body.paused, by=admin.username, note=body.note)
    await db.commit()
    if body.paused:
        await remediation.notify_paused()
    return cfg


@router.get("/rules")
async def list_rules(customer_id: str | None = None, db: AsyncSession = Depends(get_db),
                     _: User = Depends(current_user)):
    q = select(RemediationRule).order_by(RemediationRule.name)
    if customer_id:
        q = q.where((RemediationRule.customer_id == customer_id) | RemediationRule.customer_id.is_(None))
    names, rbs = await remediation.lookups(db)
    rows = [r for r in (await db.execute(q)).scalars() if _visible(r.customer_id)]
    stats: dict[str, dict[str, Any]] = {}
    if rows:
        since = datetime.now(UTC) - timedelta(days=30)
        for a in (await db.execute(select(RemediationAttempt).where(
                RemediationAttempt.rule_id.in_([r.id for r in rows]), RemediationAttempt.created_at >= since))).scalars():
            st = stats.setdefault(a.rule_id, {"attempts": 0, "succeeded": 0, "failed": 0, "last_at": None})
            st["attempts"] += 1
            st["succeeded"] += 1 if a.status == "succeeded" else 0
            st["failed"] += 1 if a.status == "failed" else 0
            st["last_at"] = max(filter(None, [st["last_at"], a.created_at]))
    return [{**remediation.rule_out(r, names, rbs), "stats": stats.get(r.id, {"attempts": 0, "succeeded": 0,
                                                                              "failed": 0, "last_at": None})}
            for r in rows]


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    customer_id: str | None = None
    enabled: bool = True
    trigger: str = "alert"
    match: dict[str, Any] = Field(default_factory=dict)
    runbook_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    mode: str = "auto"
    delay_minutes: int = 5
    cooldown_minutes: int = 30
    max_attempts: int = 3


async def _save(db: AsyncSession, r: RemediationRule | None, body: RuleBody, admin: User) -> RemediationRule:
    try:
        return await remediation.save_rule(db, r, by=admin.username, **body.model_dump())
    except remediation.RemediationError as e:
        raise _bad(e) from e


@router.post("/rules", status_code=201)
async def create_rule(body: RuleBody, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    r = await _save(db, None, body, admin)
    await db.commit()
    names, rbs = await remediation.lookups(db)
    return remediation.rule_out(r, names, rbs)


@router.get("/rules/{rid}")
async def get_rule(rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    r = await _rule(db, rid)
    if not _visible(r.customer_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    names, rbs = await remediation.lookups(db)
    return remediation.rule_out(r, names, rbs)


@router.put("/rules/{rid}")
async def update_rule(rid: str, body: RuleBody, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    r = await _save(db, await _rule(db, rid), body, admin)
    await db.commit()
    names, rbs = await remediation.lookups(db)
    return remediation.rule_out(r, names, rbs)


@router.delete("/rules/{rid}", status_code=204)
async def delete_rule(rid: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    r = await _rule(db, rid)
    remediation._audit(db, "rule.delete", admin.username, target=r.name, detail={"rule_id": r.id})  # noqa: SLF001
    await db.delete(r)
    await db.commit()


@router.get("/rules/{rid}/preview")
async def preview(rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Which open alerts this rule answers now, with the parameters it would give the runbook."""
    r = await _rule(db, rid)
    out = []
    if r.trigger == "alert":
        for al in (await db.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars():
            node = await db.get(Node, al.node_id) if al.node_id else None
            cust = await remediation._customer_of(db, al, node)  # noqa: SLF001
            if not remediation.match_alert(r, al, node, cust.id if cust else None):
                continue
            ctx = {"alert": remediation.alert_ctx(al), "machine": remediation._node_ctx(node),  # noqa: SLF001
                   "customer": {"id": cust.id, "name": cust.name} if cust else {}}
            try:
                params, error = remediation.build_params(r, ctx), None
            except remediation.RemediationError as e:
                params, error = None, str(e)
            out.append({"alert_id": al.id, "kind": al.kind, "message": al.message, "target": node.name if node else
                        (al.detail or {}).get("source"), "customer": cust.name if cust else None,
                        "started_at": al.started_at, "params": params, "error": error})
    return {"trigger": r.trigger, "matches": out}


@router.get("/presets")
async def presets(lang: str = Query("en"), _: User = Depends(current_user)):
    return remediation.preset_list(lang)


class PresetBody(BaseModel):
    customer_id: str | None = None
    lang: str = "en"
    services: list[str] | None = None
    adapters: list[str] | None = None
    local: bool | None = None


@router.post("/presets/{key}", status_code=201)
async def add_preset(key: str, body: PresetBody, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    try:
        r = await remediation.add_preset(db, key, customer_id=body.customer_id, lang=body.lang, by=admin.username,
                                         services=body.services, adapters=body.adapters,
                                         local=body.local)
    except remediation.RemediationError as e:
        raise _bad(e) from e
    await db.commit()
    names, rbs = await remediation.lookups(db)
    return remediation.rule_out(r, names, rbs)


@router.get("/attempts")
async def attempts(customer_id: str | None = None, rule_id: str | None = None, status_: str | None = Query(None, alias="status"),
                   limit: int = 100, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    q = select(RemediationAttempt).order_by(RemediationAttempt.created_at.desc()).limit(max(1, min(limit, 500)))
    if customer_id:
        q = q.where(RemediationAttempt.customer_id == customer_id)
    if rule_id:
        q = q.where(RemediationAttempt.rule_id == rule_id)
    if status_ == "active":
        q = q.where(RemediationAttempt.status.in_(remediation.ACTIVE))
    elif status_:
        q = q.where(RemediationAttempt.status == status_)
    rows = [a for a in (await db.execute(q)).scalars() if _visible(a.customer_id)]
    runs = {}
    ids = [a.run_id for a in rows if a.run_id]
    if ids:
        runs = {r.id: r for r in (await db.execute(select(RunbookRun).where(RunbookRun.id.in_(ids)))).scalars()}
    return [remediation.attempt_out(a, runs.get(a.run_id or "")) for a in rows]


@router.post("/run-now")
async def run_now(_: User = Depends(require_admin)):
    """Check the watched services and the open alerts now instead of waiting for the next minute."""
    from mmn.services import monitoring

    checked = await remediation.probe_services(force=True)
    adapters = await netwatch.probe(force=True)
    await monitoring.evaluate()
    res = await remediation.tick()
    return {**res, "services_checked": checked, "adapters_checked": adapters}
