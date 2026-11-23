"""AI gateway API: command center, approvals, policies and the kill switch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import AiApproval, AiPolicy, ApiToken, AuditLog, Environment, Role, User
from mmn.services import events, gateway, tenancy
from mmn.services.audit import audit
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


def _visible(a: AiApproval) -> bool:
    """With the customer switcher set, only approvals that touch that customer."""
    allowed = tenancy.allowed()
    return allowed is None or bool(set(a.customer_ids or []) & allowed)


def _settings_out(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: cfg.get(k) for k in ("paused", "pause_reads", "pause_note", "paused_by", "paused_at",
                                    "approval_wait_seconds", "approval_expiry_hours", "notify_app", "notify_mail",
                                    "notify_mail_to")}


# ------------------------------------------------------------------ command center
@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    from mmn.services import customers

    admin = user.role == Role.admin
    now = datetime.now(UTC)
    cfg = await gateway.gateway_settings(db)
    await gateway.expire_old(db)
    await db.commit()

    pending = [a for a in (await db.execute(select(AiApproval).where(AiApproval.status == "pending")
                                            .order_by(AiApproval.created_at.desc()).limit(200))).scalars()
               if _visible(a)]
    out: dict[str, Any] = {
        "paused": bool(cfg.get("paused")), "pause_note": cfg.get("pause_note"), "paused_by": cfg.get("paused_by"),
        "paused_at": cfg.get("paused_at"), "pending_count": len(pending),
        "customers": await customers.summaries(db),
    }

    # AI tools that use this app: tokens that were used in the last 30 days.
    q = select(ApiToken).where(ApiToken.revoked.is_(False), ApiToken.last_used_at.is_not(None),
                               ApiToken.last_used_at > now - timedelta(days=30)).order_by(ApiToken.last_used_at.desc())
    if not admin:
        q = q.where(ApiToken.user_id == user.id)
    out["clients"] = [{"token_id": t.id, "name": t.name, "kind": t.kind, "username": t.user.username,
                       "client": (t.last_client or {}).get("name") or None, "last_used_at": t.last_used_at,
                       "customer_ids": t.customer_ids, "active": now - t.last_used_at < timedelta(minutes=15)}
                      for t in (await db.execute(q.limit(20))).scalars()]

    gpol = await gateway.get_policy(db, "global")
    n_cust = (await db.execute(select(func.count()).select_from(AiPolicy).where(AiPolicy.scope == "customer"))).scalar()
    n_tok = (await db.execute(select(func.count()).select_from(AiPolicy).where(AiPolicy.scope == "token"))).scalar()
    risks: dict[str, int] = {}
    for t in gateway.catalog():
        risks[t["risk"]] = risks.get(t["risk"], 0) + 1
    out["guardrails"] = {"global": gateway.policy_out(gpol, "global"), "customer_policies": n_cust,
                         "token_policies": n_tok, "tools_per_risk": risks}

    if admin:
        out["approvals"] = [gateway.approval_out(a, full=False) for a in pending[:6]]
        since = now - timedelta(hours=24)
        rows = (await db.execute(select(AuditLog).where(AuditLog.source == "mcp", AuditLog.at > since)
                                 .order_by(AuditLog.at.desc()).limit(2000))).scalars().all()
        blocked = sum(1 for r in rows if not r.success and str((r.detail or {}).get("error", "")).startswith(
            ("Blocked by policy", "Refused:")))
        decided = (await db.execute(select(AiApproval.status, func.count()).where(AiApproval.decided_at > since)
                                    .group_by(AiApproval.status))).all()
        out["stats"] = {
            "calls_24h": len(rows), "failed_24h": sum(1 for r in rows if not r.success) - blocked,
            "blocked_24h": blocked,
            "changes_24h": sum(1 for r in rows if r.success
                               and ((r.detail or {}).get("result") or {}).get("status") != "pending_approval"
                               and gateway.REGISTRY.get(r.action.removeprefix("mcp."))
                               and gateway.REGISTRY[r.action.removeprefix("mcp.")].risk != "read"),
            "approved_24h": sum(c for s, c in decided if s in ("approved", "running", "executed", "failed")),
            "rejected_24h": sum(c for s, c in decided if s == "rejected"),
        }
        out["activity"] = [_activity(r) for r in rows[:15]]
        srows = (await db.execute(select(AuditLog).where(AuditLog.source == "mcp", AuditLog.at > now - timedelta(days=2))
                                  .order_by(AuditLog.at.desc()).limit(3000))).scalars().all()
        sessions = gateway.sessions_from_audit(list(srows))
        out["sessions"] = sessions[:8]
        out["stats"]["active_sessions"] = sum(1 for s in sessions if now - s["last_at"] < timedelta(minutes=15))
    return out


def _activity(r: AuditLog) -> dict[str, Any]:
    d = r.detail or {}
    tool = d.get("tool") or r.action.removeprefix("mcp.")
    spec = gateway.REGISTRY.get(tool)
    res = d.get("result") or {}
    state = "ok" if r.success else "failed"
    if res.get("status") == "pending_approval":
        state = "pending"
    elif not r.success and str(d.get("error", "")).startswith(("Blocked by policy", "Refused:")):
        state = "blocked"
    return {"id": r.id, "at": r.at, "tool": tool, "risk": spec.risk if spec else None, "target": r.target,
            "username": r.username, "token": d.get("token"), "client": (d.get("client") or {}).get("name"),
            "state": state, "approval_id": d.get("approval_id") or res.get("approval_id"),
            "error": (d.get("error") or "")[:300] or None, "duration_ms": d.get("duration_ms")}


@router.get("/sessions")
async def sessions(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db),
                   _: User = Depends(require_admin)):
    rows = (await db.execute(select(AuditLog).where(AuditLog.source == "mcp",
                                                    AuditLog.at > datetime.now(UTC) - timedelta(days=days))
                             .order_by(AuditLog.at.desc()).limit(20000))).scalars().all()
    return gateway.sessions_from_audit(list(rows))[:200]


@router.get("/activity")
async def activity(token_id: str | None = None, since: datetime | None = None, until: datetime | None = None,
                   limit: int = Query(200, ge=1, le=1000), db: AsyncSession = Depends(get_db),
                   _: User = Depends(require_admin)):
    """MCP calls, optionally of one token in a time range (one session)."""
    q = select(AuditLog).where(AuditLog.source == "mcp")
    if since:
        q = q.where(AuditLog.at >= since)
    if until:
        q = q.where(AuditLog.at <= until)
    if token_id:
        q = q.where(AuditLog.detail["token_id"].as_string() == token_id)
    rows = (await db.execute(q.order_by(AuditLog.at.desc()).limit(limit))).scalars().all()
    return [_activity(r) for r in rows]


# ------------------------------------------------------------------ approvals
@router.get("/approvals")
async def list_approvals(state: str = Query("pending"), limit: int = Query(100, ge=1, le=500),
                         db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await gateway.expire_old(db)
    await db.commit()
    q = select(AiApproval).order_by(AiApproval.created_at.desc())
    if state == "pending":
        q = q.where(AiApproval.status == "pending")
    elif state == "decided":
        q = q.where(AiApproval.status != "pending")
    rows = [a for a in (await db.execute(q.limit(limit * 3))).scalars() if _visible(a)][:limit]
    return [gateway.approval_out(a, full=False) for a in rows]


@router.get("/approvals/count")
async def approvals_count(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    if user.role != Role.admin:
        return {"pending": 0}
    rows = (await db.execute(select(AiApproval).where(AiApproval.status == "pending",
                                                      AiApproval.expires_at > datetime.now(UTC)))).scalars()
    return {"pending": sum(1 for a in rows if _visible(a))}


async def _approval(db: AsyncSession, aid: str) -> AiApproval:
    a = await db.get(AiApproval, aid)
    if a is None or not _visible(a):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return a


@router.get("/approvals/{aid}")
async def get_approval(aid: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return gateway.approval_out(await _approval(db, aid))


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.post("/approvals/{aid}/approve")
async def approve(aid: str, body: DecisionIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    a = await _approval(db, aid)
    try:
        await gateway.decide_approval(db, a, approve=True, by=admin.username, note=body.note)
    except gateway.ApprovalError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await audit(db, "ai.approval.approve", user=admin, target=f"{a.tool} {a.target or ''}".strip()[:255],
                ip=client_ip(request), approval_id=a.id, note=body.note)
    await db.commit()
    gateway.start_execution(a.id)
    return gateway.approval_out(a)


@router.post("/approvals/{aid}/reject")
async def reject(aid: str, body: DecisionIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    a = await _approval(db, aid)
    try:
        await gateway.decide_approval(db, a, approve=False, by=admin.username, note=body.note)
    except gateway.ApprovalError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await audit(db, "ai.approval.reject", user=admin, target=f"{a.tool} {a.target or ''}".strip()[:255],
                ip=client_ip(request), approval_id=a.id, note=body.note)
    await db.commit()
    return gateway.approval_out(a)


# ------------------------------------------------------------------ policies
@router.get("/policies")
async def policies(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    rows = list((await db.execute(select(AiPolicy))).scalars())
    by = {(p.scope, p.scope_id): p for p in rows}
    with tenancy.unrestricted():
        envs = list((await db.execute(select(Environment).order_by(Environment.name))).scalars())
    toks = list((await db.execute(select(ApiToken).where(ApiToken.revoked.is_(False))
                                  .order_by(ApiToken.created_at.desc()))).scalars())
    now = await gateway.local_now(db)
    customers_out = []
    for e in envs:
        windows = gateway.parse_window(e.maintenance_window)
        customers_out.append({
            "customer_id": e.id, "name": e.name, "kind": e.kind, "maintenance_window": e.maintenance_window,
            "window_understood": bool(windows) if e.maintenance_window else None,
            "in_window": gateway.in_window(windows, now) if windows else None,
            "policy": gateway.policy_out(by.get(("customer", e.id)), "customer", e.id)})
    tokens_out = [{"token_id": t.id, "name": t.name, "kind": t.kind, "username": t.user.username,
                   "client": (t.last_client or {}).get("name") or None, "last_used_at": t.last_used_at,
                   "expired": bool(t.expires_at and t.expires_at < datetime.now(UTC)),
                   "policy": gateway.policy_out(by.get(("token", t.id)), "token", t.id)} for t in toks]
    return {"global": gateway.policy_out(by.get(("global", None)), "global"), "customers": customers_out,
            "tokens": tokens_out, "tools": gateway.catalog(), "risks": list(gateway.RISKS),
            "actions": list(gateway.ACTIONS), "defaults": gateway.DEFAULT_RISKS}


class PolicyIn(BaseModel):
    risks: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)
    outside_window: str = "inherit"
    paused: bool = False


async def _save(db: AsyncSession, scope: str, sid: str | None, body: PolicyIn, admin: User, request: Request,
                target: str) -> dict[str, Any]:
    p = await gateway.save_policy(db, scope, sid, risks=body.risks, tools=body.tools,
                                  outside_window=body.outside_window, paused=body.paused, by=admin.username)
    await audit(db, f"ai.policy.{scope}", user=admin, target=target[:255], ip=client_ip(request),
                risks=p.risks, tools=p.tools, outside_window=p.outside_window, paused=p.paused)
    await db.commit()
    events.publish("gateway.policy", {"scope": scope})
    return gateway.policy_out(p, scope, sid)


@router.put("/policies/global")
async def put_global(body: PolicyIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    body.paused = False  # the global pause is the kill switch
    return await _save(db, "global", None, body, admin, request, "defaults")


@router.put("/policies/customers/{cid}")
async def put_customer(cid: str, body: PolicyIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    e = await db.get(Environment, cid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return await _save(db, "customer", cid, body, admin, request, e.name)


@router.put("/policies/tokens/{tid}")
async def put_token(tid: str, body: PolicyIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    t = await db.get(ApiToken, tid)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return await _save(db, "token", tid, body, admin, request, t.name)


@router.delete("/policies/{scope}/{sid}", status_code=204)
async def delete_policy(scope: str, sid: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    kind = {"customers": "customer", "tokens": "token"}.get(scope)
    if kind is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    p = await gateway.get_policy(db, kind, sid)
    if p is not None:
        await db.delete(p)
        await audit(db, f"ai.policy.{kind}.reset", user=admin, target=sid, ip=client_ip(request))
        await db.commit()
    return Response(status_code=204)


@router.get("/effective")
async def effective(customer_id: str | None = None, token_id: str | None = None, db: AsyncSession = Depends(get_db),
                    _: User = Depends(require_admin)):
    return await gateway.effective_for(db, customer_id, token_id)


@router.get("/window-check")
async def window_check(text: str = Query(..., max_length=200), db: AsyncSession = Depends(get_db),
                       _: User = Depends(current_user)):
    """Whether the app understands a maintenance window text, and whether now is inside it."""
    w = gateway.parse_window(text)
    return {"understood": bool(w), "in_window": gateway.in_window(w, await gateway.local_now(db)) if w else None}


# ------------------------------------------------------------------ settings and kill switch
@router.get("/settings")
async def get_settings_(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    from mmn.services import ai_notify

    out = _settings_out(await get_setting(db, "ai_gateway"))
    out["channels"] = list(ai_notify.CHANNELS)
    out["mail_enabled"] = bool((await get_setting(db, "mail")).get("enabled"))
    return out


class SettingsIn(BaseModel):
    approval_wait_seconds: int = Field(25, ge=0, le=120)
    approval_expiry_hours: int = Field(24, ge=1, le=336)
    notify_app: bool = True
    notify_mail: bool = True
    notify_mail_to: str = Field("", max_length=1000)


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    cfg = await set_setting(db, "ai_gateway", body.model_dump())
    await audit(db, "ai.settings", user=admin, ip=client_ip(request))
    await db.commit()
    return _settings_out(cfg)


@router.post("/settings/test/{channel}")
async def test_channel(channel: str, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    from mmn.services import ai_notify
    from mmn.services.mailer import MailNotConfigured

    try:
        await ai_notify.send_test(db, channel)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found") from e
    except MailNotConfigured as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="mail_disabled") from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(e)[:300]) from e
    await audit(db, "ai.notify.test", user=admin, target=channel, ip=client_ip(request))
    await db.commit()
    return {"ok": True}


class PauseIn(BaseModel):
    note: str = Field("", max_length=500)
    pause_reads: bool = False


@router.post("/pause")
async def pause(body: PauseIn, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    cfg = await set_setting(db, "ai_gateway", {"paused": True, "pause_reads": body.pause_reads, "pause_note": body.note,
                                               "paused_by": admin.username,
                                               "paused_at": datetime.now(UTC).isoformat()})
    await audit(db, "ai.pause", user=admin, ip=client_ip(request), note=body.note or None, pause_reads=body.pause_reads)
    await db.commit()
    events.publish("gateway.paused", {"paused": True})
    return _settings_out(cfg)


@router.post("/resume")
async def resume(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    cfg = await set_setting(db, "ai_gateway", {"paused": False, "pause_reads": False, "pause_note": "",
                                               "paused_by": None, "paused_at": None})
    await audit(db, "ai.resume", user=admin, ip=client_ip(request))
    await db.commit()
    events.publish("gateway.paused", {"paused": False})
    return _settings_out(cfg)


@router.get("/status")
async def gateway_status(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """Small status for the menu and the banner: paused or not."""
    cfg = await get_setting(db, "ai_gateway")
    return {"paused": bool(cfg.get("paused")), "pause_note": cfg.get("pause_note"), "paused_by": cfg.get("paused_by")}
