"""Nightly checks and the morning summary (phase 12)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.db import get_db
from mmn.models import Environment, NightlyRun, User
from mmn.services import gateway, nightly, nodes, tenancy
from mmn.services.audit import audit
from mmn.services.settings_store import set_setting

router = APIRouter(prefix="/api/nightly", tags=["nightly"])

_HHMM = re.compile(r"^([01]?\d|2[0-3])[:.][0-5]\d$")


def _schedule() -> dict:
    from mmn.services import scheduler

    return scheduler.nightly_next_runs()


@router.get("")
async def latest(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    """The latest run with its findings and proposals (the morning summary)."""
    r = await nightly.latest(db, finished=True)
    current = await nightly.latest(db)
    return {"run": await nightly.detail(db, r) if r else None,
            "running": nightly.running(),
            "running_id": current.id if current and current.status == "running" and nightly.running() else None,
            "next": _schedule()}


@router.get("/runs")
async def runs(limit: int = Query(30, ge=1, le=200), db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = (await db.execute(select(NightlyRun).order_by(NightlyRun.started_at.desc()).limit(limit))).scalars()
    return [nightly.run_out(r) for r in rows]


@router.get("/runs/{rid}")
async def run_detail(rid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    r = await db.get(NightlyRun, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    return await nightly.detail(db, r)


class RunIn(BaseModel):
    customer_id: str | None = None


@router.post("/run", status_code=202)
async def run_now(body: RunIn, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    ids = None
    allowed = tenancy.allowed()
    if body.customer_id:
        try:
            ids = [(await nodes.get_environment(db, body.customer_id)).id]
        except nodes.NodeError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    elif allowed is not None:
        ids = sorted(allowed)
    try:
        rid = await nightly.start(trigger="manual", requested_by=admin.username, customer_ids=ids)
    except nightly.NightlyBusy as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    await audit(db, "nightly.run", user=admin, ip=client_ip(request), run_id=rid, customers=ids)
    await db.commit()
    return {"id": rid}


@router.post("/runs/{rid}/send")
async def send(rid: str, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    r = await db.get(NightlyRun, rid)
    if r is None or r.status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the run is not finished")
    sent = await nightly.send_summary(rid, force=True)
    await audit(db, "nightly.summary_send", user=admin, ip=client_ip(request), run_id=rid, sent=sent)
    await db.commit()
    return {"sent": sent}


class ApproveAllIn(BaseModel):
    customer_id: str | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.post("/runs/{rid}/approve")
async def approve_all(rid: str, body: ApproveAllIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    """Approve every pending proposal of a run (or of one customer in it) with one click."""
    if await db.get(NightlyRun, rid) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not_found")
    approved, started, errors = [], 0, []
    for a in await nightly.pending_approvals(db, rid, body.customer_id):
        try:
            await gateway.decide_approval(db, a, approve=True, by=admin.username, note=body.note)
        except gateway.ApprovalError as e:
            errors.append({"id": a.id, "error": str(e)})
            continue
        await audit(db, "ai.approval.approve", user=admin, target=f"{a.tool} {a.target or ''}".strip()[:255],
                    ip=client_ip(request), approval_id=a.id, note=body.note, via="nightly")
        approved.append(a)
    await db.commit()
    for a in approved:
        started += 1 if gateway.after_approved(a) else 0
    return {"approved": len(approved), "started": started, "scheduled": len(approved) - started, "errors": errors}


# ------------------------------------------------------------------ settings
class ChecksIn(BaseModel):
    updates: bool = True
    cves: bool = True
    disks: bool = True
    certificates: bool = True
    backups: bool = True
    other: bool = True


class SettingsIn(BaseModel):
    enabled: bool
    run_at: str
    summary_at: str
    notify_app: bool = True
    notify_mail: bool = True
    mail_to: str = Field(default="", max_length=1000)
    checks: ChecksIn = ChecksIn()
    cert_warn_days: int = Field(default=21, ge=1, le=120)
    cve_lookup: bool = True
    proposals: bool = True
    use_window: bool = True
    mail_when_clean: bool = True

    @field_validator("run_at", "summary_at")
    @classmethod
    def _time(cls, v: str) -> str:
        v = v.strip().replace(".", ":")
        if not _HHMM.match(v):
            raise ValueError("use HH:MM")
        h, m = v.split(":")
        return f"{int(h):02d}:{m}"


def _customer_cfg(e: Environment) -> dict:
    n = e.nightly or {}
    return {"id": e.id, "name": e.name, "kind": e.kind, "enabled": n.get("enabled", True),
            "cert_endpoints": n.get("cert_endpoints") or [], "maintenance_window": e.maintenance_window}


@router.get("/settings")
async def get_settings_(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    envs = (await db.execute(select(Environment).order_by(Environment.name))).scalars().all()
    return {"settings": await nightly.settings(db), "customers": [_customer_cfg(e) for e in envs], "next": _schedule()}


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    from mmn.services import scheduler

    await set_setting(db, "nightly", body.model_dump())
    await audit(db, "nightly.settings", user=admin, ip=client_ip(request), enabled=body.enabled, run_at=body.run_at,
                summary_at=body.summary_at)
    await db.commit()
    await scheduler.reload_nightly_jobs()
    return {"settings": await nightly.settings(db), "next": _schedule()}


class CustomerIn(BaseModel):
    enabled: bool = True
    cert_endpoints: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("cert_endpoints")
    @classmethod
    def _eps(cls, v: list[str]) -> list[str]:
        out = []
        for x in v:
            x = x.strip()
            if not x:
                continue
            if len(x) > 255 or nightly._host_port(x) is None:  # noqa: SLF001
                raise ValueError(f"not a host or host:port: {x[:60]}")
            out.append(x)
        return out


@router.put("/customers/{cid}")
async def put_customer(cid: str, body: CustomerIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        e = await nodes.get_environment(db, cid)
    except nodes.NodeError as ex:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(ex)) from ex
    e.nightly = {**(e.nightly or {}), "enabled": body.enabled, "cert_endpoints": body.cert_endpoints}
    await audit(db, "nightly.customer", user=admin, target=e.name, ip=client_ip(request), enabled=body.enabled,
                endpoints=len(body.cert_endpoints))
    await db.commit()
    return _customer_cfg(e)
