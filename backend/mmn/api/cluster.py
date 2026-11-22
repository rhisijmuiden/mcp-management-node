"""High availability, replication, backup jobs and storage of a Proxmox VE cluster. Every route
takes a node of the cluster (the calls run on that node)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import current_user, require_admin
from mmn.api.network import pve_node, read, run
from mmn.db import get_db
from mmn.models import User
from mmn.services import cluster

router = APIRouter(tags=["cluster"])


# ------------------------------------------------------------------ HA
@router.get("/api/cluster/{node_id}/ha")
async def ha(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(cluster.ha_overview(n.id))


@router.post("/api/cluster/{node_id}/ha/resources", status_code=201)
async def add_ha(node_id: str, body: cluster.HaResourceIn, request: Request, db: AsyncSession = Depends(get_db),
                 admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "ha.resource_add", body.sid, cluster.save_ha_resource(n.id, body, create=True),
                     node=n.name, state=body.state, group=body.group)


@router.put("/api/cluster/{node_id}/ha/resources/{sid}")
async def update_ha(node_id: str, sid: str, body: cluster.HaResourceIn, request: Request,
                    db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    body.sid = sid
    return await run(db, request, admin, "ha.resource_update", sid, cluster.save_ha_resource(n.id, body, create=False),
                     node=n.name, state=body.state, group=body.group)


@router.delete("/api/cluster/{node_id}/ha/resources/{sid}")
async def remove_ha(node_id: str, sid: str, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "ha.resource_remove", sid, cluster.remove_ha_resource(n.id, sid), node=n.name)


@router.post("/api/cluster/{node_id}/ha/groups", status_code=201)
async def add_group(node_id: str, body: cluster.HaGroupIn, request: Request, db: AsyncSession = Depends(get_db),
                    admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "ha.group_add", body.id, cluster.create_ha_group(n.id, body), node=n.name)


@router.post("/api/cluster/{node_id}/ha/rules", status_code=201)
async def add_rule(node_id: str, body: cluster.HaRuleIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "ha.rule_add", body.id, cluster.create_ha_rule(n.id, body), node=n.name)


@router.delete("/api/cluster/{node_id}/ha/{kind}/{ident}")
async def delete_group_rule(node_id: str, kind: Literal["groups", "rules"], ident: str, request: Request,
                            db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    k = "group" if kind == "groups" else "rule"
    return await run(db, request, admin, f"ha.{k}_delete", ident, cluster.delete_ha_group_or_rule(n.id, k, ident),
                     node=n.name)


# ------------------------------------------------------------------ replication
@router.get("/api/cluster/{node_id}/replication")
async def replication(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(cluster.replication(n.id))


@router.post("/api/cluster/{node_id}/replication", status_code=201)
async def add_replication(node_id: str, body: cluster.ReplicationIn, request: Request,
                          db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "replication.add", f"{body.vmid} -> {body.target}",
                     cluster.create_replication(n.id, body), schedule=body.schedule)


@router.delete("/api/cluster/{node_id}/replication/{job_id}")
async def delete_replication(node_id: str, job_id: str, request: Request, db: AsyncSession = Depends(get_db),
                             admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "replication.delete", job_id, cluster.delete_replication(n.id, job_id))


@router.post("/api/cluster/{node_id}/replication/{job_id}/run")
async def run_replication(node_id: str, job_id: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "replication.run", job_id, cluster.run_replication(n.id, job_id))


# ------------------------------------------------------------------ backup jobs
@router.get("/api/cluster/{node_id}/backup-jobs")
async def backup_jobs(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(cluster.backup_jobs(n.id))


@router.post("/api/cluster/{node_id}/backup-jobs", status_code=201)
async def add_backup_job(node_id: str, body: cluster.BackupJobIn, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "backupjob.add", body.storage, cluster.save_backup_job(n.id, body),
                     schedule=body.schedule)


@router.put("/api/cluster/{node_id}/backup-jobs/{job_id}")
async def update_backup_job(node_id: str, job_id: str, body: cluster.BackupJobIn, request: Request,
                            db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "backupjob.update", job_id, cluster.save_backup_job(n.id, body, job_id))


@router.delete("/api/cluster/{node_id}/backup-jobs/{job_id}")
async def delete_backup_job(node_id: str, job_id: str, request: Request, db: AsyncSession = Depends(get_db),
                            admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "backupjob.delete", job_id, cluster.delete_backup_job(n.id, job_id))


@router.post("/api/cluster/{node_id}/backup-jobs/{job_id}/run", status_code=202)
async def run_backup_job(node_id: str, job_id: str, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "backupjob.run", job_id,
                     cluster.run_backup_job(db, n.id, job_id, requested_by=admin.username))


# ------------------------------------------------------------------ storage
@router.get("/api/cluster/{node_id}/storage")
async def storage(node_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    n = await pve_node(db, node_id)
    return await read(cluster.storages(n.id))


@router.post("/api/cluster/{node_id}/storage", status_code=201)
async def add_storage(node_id: str, body: cluster.StorageIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "storage.add", body.storage, cluster.add_storage(n.id, body), type=body.type)


class EnableIn(BaseModel):
    enabled: bool


@router.put("/api/cluster/{node_id}/storage/{storage}")
async def enable_storage(node_id: str, storage: str, body: EnableIn, request: Request,
                         db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "storage.enable" if body.enabled else "storage.disable", storage,
                     cluster.set_storage_enabled(n.id, storage, body.enabled))


@router.delete("/api/cluster/{node_id}/storage/{storage}")
async def remove_storage(node_id: str, storage: str, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    n = await pve_node(db, node_id)
    return await run(db, request, admin, "storage.remove", storage, cluster.remove_storage(n.id, storage))
