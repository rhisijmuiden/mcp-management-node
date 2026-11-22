from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, require_admin
from mmn.db import get_db
from mmn.models import BackupTarget, User
from mmn.security import verify_password
from mmn.services import scheduler
from mmn.services.audit import audit
from mmn.services.backup import engine
from mmn.services.backup.targets import (
    TargetIn,
    apply_target,
    list_runs,
    list_targets,
    spawn,
    target_out,
)
from mmn.services.backup.transports import friendly_error, make_transport

router = APIRouter(prefix="/api/backups", tags=["backups"])


async def _target(db: AsyncSession, target_id: str) -> BackupTarget:
    t = await db.get(BackupTarget, target_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return t


@router.get("/targets")
async def get_targets(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return [target_out(t) | {"next_runs": scheduler.next_runs(t.id)} for t in await list_targets(db)]


@router.post("/targets", status_code=201)
async def create_target(body: TargetIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = BackupTarget(options={})
    apply_target(t, body, body.model_fields_set)
    db.add(t)
    await db.flush()
    await audit(db, "backup.target.create", user=admin, target=t.name, ip=client_ip(request))
    await db.commit()
    await scheduler.reload_backup_jobs()
    return target_out(t)


@router.put("/targets/{target_id}")
async def update_target(target_id: str, body: TargetIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = await _target(db, target_id)
    apply_target(t, body, body.model_fields_set)
    await audit(db, "backup.target.update", user=admin, target=t.name, ip=client_ip(request))
    await db.commit()
    await scheduler.reload_backup_jobs()
    return target_out(t)


@router.delete("/targets/{target_id}", status_code=204)
async def delete_target(target_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    t = await _target(db, target_id)
    await db.delete(t)
    await audit(db, "backup.target.delete", user=admin, target=t.name, ip=client_ip(request))
    await db.commit()
    await scheduler.reload_backup_jobs()
    return Response(status_code=204)


@router.post("/targets/{target_id}/test")
async def test_target(target_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    t = await _target(db, target_id)
    try:
        info = await make_transport(engine.target_config(t)).test()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": friendly_error(e)}
    if info.get("host_key"):
        opts = dict(t.options or {})
        known = opts.get("host_key")
        if known and known != info["host_key"]:
            return {"ok": False, "error": "host key mismatch: the server key changed",
                    "fingerprint": info.get("fingerprint")}
        opts["host_key"], opts["host_key_fingerprint"] = info["host_key"], info.get("fingerprint")
        t.options = opts
        await db.commit()
    return {"ok": True, **{k: v for k, v in info.items() if k != "host_key"}}


@router.post("/targets/{target_id}/forget-host-key")
async def forget_host_key(target_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    t = await _target(db, target_id)
    opts = dict(t.options or {})
    opts.pop("host_key", None)
    opts.pop("host_key_fingerprint", None)
    t.options = opts
    await db.commit()
    return target_out(t)


class RunIn(BaseModel):
    kind: str = "auto"


@router.post("/targets/{target_id}/run", status_code=202)
async def run_now(target_id: str, body: RunIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    if body.kind not in ("auto", "full", "incremental"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid_kind")
    t = await _target(db, target_id)
    if engine.maintenance["active"]:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="restore_in_progress")
    await audit(db, "backup.run", user=admin, target=t.name, ip=client_ip(request), kind=body.kind)
    await db.commit()
    spawn(engine.run_backup(target_id, body.kind, trigger="manual"))
    return {"status": "started"}


@router.get("/runs")
async def runs(limit: int = Query(50, ge=1, le=500), target_id: str | None = None,
               db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await list_runs(db, limit, target_id)


@router.get("/targets/{target_id}/remote")
async def remote_backups(target_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    t = await _target(db, target_id)
    try:
        return await engine.list_remote_backups(t)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=friendly_error(e)) from e


class RestoreIn(BaseModel):
    backup_id: str
    password: str  # the admin confirms with their own password


@router.post("/targets/{target_id}/restore")
async def restore(target_id: str, body: RestoreIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    if not verify_password(admin.password_hash, body.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="wrong_password")
    await _target(db, target_id)
    username, user_id, ip = admin.username, admin.id, client_ip(request)
    await db.close()
    try:
        result = await engine.restore_backup(target_id, body.backup_id, user_id=user_id, username=username)
    except engine.BackupError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=friendly_error(e)) from e
    del ip
    await scheduler.reload_backup_jobs()
    return result
