"""Backup management per customer (phase 11): coverage, policy, restores, Proxmox Backup Server.

Proxmox VE backup jobs and storages stay under /api/cluster/{node}/backup-jobs and /storage."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import current_user, require_admin
from mmn.api.network import pve_node, read, run
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import Environment, Node, User
from mmn.services import nodes, pbs, protection, restore
from mmn.services.agent_hub import AgentError

router = APIRouter(prefix="/api/protection", tags=["backups"])


async def _customer(db: AsyncSession, cid: str) -> Environment:
    try:
        return await nodes.get_environment(db, cid)
    except nodes.NodeError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e)) from e


async def _pbs_node(db: AsyncSession, node_id: str) -> Node:
    try:
        n = await nodes.get_node(db, node_id)
    except nodes.NodeError as e:
        raise _err(e) from e
    if n.kind != "linux":
        raise _err(nodes.NodeError("a Proxmox Backup Server is a Linux server with the agent"))
    return n


# ------------------------------------------------------------------ coverage and policy
@router.get("")
async def overview(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await protection.overview_all(db)


@router.get("/customers/{cid}")
async def coverage(cid: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    e = await _customer(db, cid)
    return protection.view(e, await protection.stored(db, e))


@router.post("/customers/{cid}/refresh")
async def refresh(cid: str, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    e = await _customer(db, cid)
    return await run(db, request, admin, "backups.coverage_refresh", e.name, protection.collect(e.id, force=True))


@router.put("/customers/{cid}/policy")
async def save_policy(cid: str, body: protection.PolicyIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    e = await _customer(db, cid)
    return await run(db, request, admin, "backups.policy", e.name, protection.save_policy(db, e, body),
                     max_age_hours=body.max_age_hours, excluded=len(body.exclude))


class ExcludeIn(BaseModel):
    key: str = Field(max_length=200)
    excluded: bool = True


@router.post("/customers/{cid}/exclude")
async def exclude(cid: str, body: ExcludeIn, request: Request, db: AsyncSession = Depends(get_db),
                  admin: User = Depends(require_admin)):
    e = await _customer(db, cid)
    return await run(db, request, admin, "backups.exclude" if body.excluded else "backups.include", body.key,
                     protection.set_excluded(db, e, body.key, body.excluded), customer=e.name)


# ------------------------------------------------------------------ backups of a guest, restores
@router.get("/nodes/{node_id}/guests/{vmid}/backups")
async def guest_backups(node_id: str, vmid: int, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(restore.guest_backups(db, n, vmid))


class RestoreBody(restore.RestoreIn):
    confirm: str = Field(default="", max_length=20)  # overwrite: the VMID that is overwritten


@router.post("/nodes/{node_id}/restore", status_code=202)
async def restore_guest(node_id: str, body: RestoreBody, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    if body.overwrite:
        try:
            expected = str(body.vmid or restore.volume_vmid(body.volume) or "")
        except nodes.NodeError as e:
            raise _err(e) from e
        if not expected or body.confirm.strip() != expected:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="confirm_mismatch")

    async def go():
        job, info = await restore.restore_guest(db, n, body, requested_by=admin.username)
        return {"job": nodes.job_out(job, with_output=False), **info}

    return await run(db, request, admin, "backups.restore", body.volume, go(), node=n.name, vmid=body.vmid,
                     overwrite=body.overwrite, storage=body.storage)


@router.get("/nodes/{node_id}/files")
async def list_files(node_id: str, volume: str, path: str = "/", db: AsyncSession = Depends(get_db),
                     _: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await read(restore.list_files(n, volume, path))


@router.post("/nodes/{node_id}/files/restore", status_code=202)
async def restore_files(node_id: str, body: restore.FileRestoreIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)

    async def go():
        job, target = await restore.restore_files(n, body, requested_by=admin.username)
        return {"job": nodes.job_out(job, with_output=False), "target": target, "node": n.name}

    return await run(db, request, admin, "backups.file_restore", body.volume, go(), node=n.name, path=body.path)


@router.post("/nodes/{node_id}/backup-now", status_code=202)
async def backup_now(node_id: str, body: restore.BackupNowIn, request: Request, db: AsyncSession = Depends(get_db),
                     admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)

    async def go():
        return nodes.job_out(await restore.backup_now(db, n, body, requested_by=admin.username), with_output=False)

    return await run(db, request, admin, "backups.backup_now", str(body.vmid), go(), node=n.name, storage=body.storage)


# ------------------------------------------------------------------ Proxmox Backup Server
@router.get("/pbs/{node_id}")
async def pbs_overview(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await _pbs_node(db, node_id)
    try:
        return {"node_id": n.id, "name": n.name, **await pbs.overview(n.id)}
    except (nodes.NodeError, AgentError) as e:
        raise _err(e) from e


@router.post("/pbs/{node_id}/jobs")
async def pbs_save_job(node_id: str, body: pbs.PbsJobIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    n = await _pbs_node(db, node_id)
    return await run(db, request, admin, f"pbs.{body.kind}_job_save", f"{n.name}:{body.id}", pbs.save_job(n.id, body),
                     store=body.store, schedule=body.schedule)


@router.delete("/pbs/{node_id}/jobs/{kind}/{job_id}")
async def pbs_delete_job(node_id: str, kind: Literal["verify", "sync", "prune"], job_id: str, request: Request,
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await _pbs_node(db, node_id)
    return await run(db, request, admin, f"pbs.{kind}_job_delete", f"{n.name}:{job_id}", pbs.delete_job(n.id, kind, job_id))


@router.post("/pbs/{node_id}/jobs/{kind}/{job_id}/run")
async def pbs_run_job(node_id: str, kind: Literal["verify", "sync", "prune"], job_id: str, request: Request,
                      db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await _pbs_node(db, node_id)
    return await run(db, request, admin, f"pbs.{kind}_job_run", f"{n.name}:{job_id}", pbs.run_job(n.id, kind, job_id))


@router.post("/pbs/{node_id}/datastores")
async def pbs_add_datastore(node_id: str, body: pbs.DatastoreIn, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    n = await _pbs_node(db, node_id)
    return await run(db, request, admin, "pbs.datastore_add", f"{n.name}:{body.name}", pbs.add_datastore(n.id, body),
                     path=body.path)


@router.post("/pbs/{node_id}/remotes")
async def pbs_add_remote(node_id: str, body: pbs.RemoteIn, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    n = await _pbs_node(db, node_id)
    return await run(db, request, admin, "pbs.remote_add", f"{n.name}:{body.name}", pbs.add_remote(n.id, body),
                     host=body.host)
