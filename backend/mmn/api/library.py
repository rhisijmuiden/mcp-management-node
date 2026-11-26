"""Template library, community scripts and scheduled tasks."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.api.deps import client_ip, current_user, require_admin
from mmn.api.proxmox import _err
from mmn.db import get_db
from mmn.models import ScheduledTask, TaskRun, User
from mmn.services import agent_hub, community, console, tasks, templates
from mmn.services.agent_hub import AgentError
from mmn.services.audit import audit
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting, set_setting

router = APIRouter(tags=["library"])


def _bad(e: Exception) -> HTTPException:
    return _err(e)


# ------------------------------------------------------------------ templates
@router.get("/api/templates")
async def list_templates(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    nl = await templates.pve_nodes(db)
    cfg = await templates.settings(db)
    return {"templates": [templates.template_out(t, nl) for t in await templates.list_templates(db)],
            "settings": cfg, "catalog_checked_at": (await templates.catalog(db))["checked_at"],
            "nodes": [{"node_id": n.id, "node": n.name, "online": agent_hub.is_online(n.id),
                       "checked_at": (n.storage_scan or {}).get("checked_at"),
                       "storages": sorted(templates.node_vztmpl_storages(n))} for n in nl]}


@router.get("/api/templates/catalog")
async def get_catalog(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return await templates.catalog(db)


@router.post("/api/templates/catalog/refresh")
async def refresh_catalog(_: User = Depends(require_admin)):
    try:
        return await templates.refresh_catalog()
    except (NodeError, AgentError) as e:
        raise _bad(e) from e


class OfficialIn(BaseModel):
    filename: str = Field(max_length=255)
    sync: bool = True
    storage: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")


@router.post("/api/templates/official", status_code=201)
async def add_official(body: OfficialIn, request: Request, db: AsyncSession = Depends(get_db),
                       admin: User = Depends(require_admin)):
    try:
        t = await templates.add_official(db, body.filename, created_by=admin.username, sync=body.sync,
                                         storage=body.storage)
    except NodeError as e:
        raise _bad(e) from e
    await audit(db, "template.add", user=admin, target=t.filename, ip=client_ip(request), kind="official")
    await db.commit()
    return templates.template_out(t, await templates.pve_nodes(db))


@router.put("/api/templates/upload", status_code=201)
async def upload(request: Request, filename: str = Query(max_length=255), name: str = Query(max_length=200),
                 description: str | None = Query(default=None, max_length=2000), replace_id: str | None = None,
                 db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """Upload a custom template as the raw request body (streamed to disk)."""
    if not templates.FNAME_RE.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="the file name must end in .tar.gz, .tar.xz or .tar.zst")
    tmp_dir = templates.templates_dir() / "_incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"upload-{hashlib.sha1((filename + admin.id).encode()).hexdigest()}.part"
    size = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8 * 1024**3:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
                f.write(chunk)
        t = await templates.store_custom_file(tmp, filename, name=name, description=description,
                                              created_by=admin.username, replace_id=replace_id)
    except NodeError as e:
        raise _bad(e) from e
    finally:
        tmp.unlink(missing_ok=True)
    await audit(db, "template.upload", user=admin, target=filename, ip=client_ip(request), size=size,
                sha256=t.sha256, replaced=replace_id)
    await db.commit()
    return templates.template_out(t, await templates.pve_nodes(db))


class FromUrlIn(BaseModel):
    url: str = Field(max_length=2000, pattern=r"^https?://\S+$")
    filename: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    replace_id: str | None = None


@router.post("/api/templates/from-url", status_code=202)
async def from_url(body: FromUrlIn, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    filename = body.filename or re.sub(r"[^A-Za-z0-9_.+-]", "_", body.url.split("?")[0].rsplit("/", 1)[-1])
    if not templates.FNAME_RE.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="the file name must end in .tar.gz, .tar.xz or .tar.zst")
    agent_hub.spawn(templates.download_custom(body.url, filename, name=body.name, description=body.description,
                                              created_by=admin.username, replace_id=body.replace_id))
    await audit(db, "template.download", user=admin, target=filename, ip=client_ip(request), url=body.url)
    await db.commit()
    return {"started": True, "filename": filename, "url": body.url}


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    sync: bool | None = None
    storage: str | None = Field(default=None, max_length=100, pattern=r"^([A-Za-z][A-Za-z0-9_.-]*)?$")
    follow_latest: bool | None = None


@router.patch("/api/templates/{tpl_id}")
async def patch_template(tpl_id: str, body: TemplatePatch, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    try:
        t = await templates.get_template(db, tpl_id)
    except NodeError as e:
        raise _bad(e) from e
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, (v or None) if k == "storage" else v)
    await audit(db, "template.update", user=admin, target=t.filename, ip=client_ip(request),
                **body.model_dump(exclude_unset=True, exclude={"description"}))
    await db.commit()
    return templates.template_out(t, await templates.pve_nodes(db))


@router.delete("/api/templates/{tpl_id}", status_code=204)
async def delete_template(tpl_id: str, request: Request, db: AsyncSession = Depends(get_db),
                          admin: User = Depends(require_admin)):
    try:
        t = await templates.get_template(db, tpl_id)
    except NodeError as e:
        raise _bad(e) from e
    name = t.filename
    await templates.delete_template(db, t)
    await audit(db, "template.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    return Response(status_code=204)


class PushIn(BaseModel):
    node_ids: list[str] = Field(default_factory=list, max_length=500)


@router.post("/api/templates/{tpl_id}/push", status_code=202)
async def push_template(tpl_id: str, body: PushIn, request: Request, db: AsyncSession = Depends(get_db),
                        admin: User = Depends(require_admin)):
    """Push one template to all (or the given) nodes now."""
    try:
        t = await templates.get_template(db, tpl_id)
    except NodeError as e:
        raise _bad(e) from e
    run_id = await templates.sync(template_ids=[t.id], node_ids=body.node_ids or None, trigger="manual",
                                  requested_by=admin.username, name=f"Push {t.name}", refresh_index=False)
    await audit(db, "template.push", user=admin, target=t.filename, ip=client_ip(request), run_id=run_id)
    await db.commit()
    return {"run_id": run_id}


@router.post("/api/templates/sync", status_code=202)
async def sync_templates(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    run_id = await templates.sync(trigger="manual", requested_by=admin.username)
    await audit(db, "template.sync", user=admin, ip=client_ip(request), run_id=run_id)
    await db.commit()
    return {"run_id": run_id}


@router.post("/api/templates/scan")
async def scan(_: User = Depends(require_admin)):
    await templates.scan_all()
    return {"ok": True}


class TemplateSettingsIn(BaseModel):
    storage: str = Field(default="local", max_length=100, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")


@router.put("/api/templates-settings")
async def put_template_settings(body: TemplateSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                                admin: User = Depends(require_admin)):
    out = await set_setting(db, "templates", body.model_dump())
    await audit(db, "template.settings", user=admin, ip=client_ip(request), storage=body.storage)
    await db.commit()
    return out


# ------------------------------------------------------------------ community scripts
@router.get("/api/community/scripts")
async def list_scripts(q: str | None = None, kind: Literal["ct", "vm"] | None = None,
                       db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    return {"state": await community.state(db), "scripts": await community.list_scripts(db, q, kind)}


@router.post("/api/community/sync")
async def sync_scripts(request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    try:
        res = await community.sync()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"sync failed: {e}") from e
    await audit(db, "community.sync", user=admin, ip=client_ip(request), **res)
    await db.commit()
    return res


@router.get("/api/community/scripts/{slug}")
async def get_script(slug: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    try:
        return await community.fetch_script(db, slug)
    except NodeError as e:
        raise _bad(e) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"could not fetch the script: {e}") from e


class InstallOptions(BaseModel):
    hostname: str | None = Field(default=None, max_length=63, pattern=r"^[A-Za-z0-9-]*$")
    vmid: int | None = Field(default=None, ge=100, le=999_999_999)
    cpu: int | None = Field(default=None, ge=1, le=128)
    ram: int | None = Field(default=None, ge=128, le=1024 * 1024)
    disk: int | None = Field(default=None, ge=1, le=64 * 1024)
    storage: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_.-]*$")
    template_storage: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_.-]*$")
    bridge: str | None = Field(default=None, max_length=16, pattern=r"^[A-Za-z0-9_.-]*$")
    vlan: int | None = Field(default=None, ge=1, le=4094)
    ip: str | None = Field(default=None, max_length=64, pattern=r"^(dhcp|[0-9a-fA-F:.]+/\d{1,3})?$")
    gateway: str | None = Field(default=None, max_length=64, pattern=r"^[0-9a-fA-F:.]*$")
    unprivileged: bool | None = None
    ssh_key: str | None = Field(default=None, max_length=5000)
    password: str | None = Field(default=None, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=20)


class InstallIn(BaseModel):
    node_id: str
    sha256: str = Field(min_length=64, max_length=64)
    mode: Literal["default", "advanced"] = "default"
    options: InstallOptions = Field(default_factory=InstallOptions)
    cols: int = Field(default=120, ge=20, le=1000)
    rows: int = Field(default=34, ge=10, le=500)


@router.post("/api/community/scripts/{slug}/install", status_code=202)
async def install_script(slug: str, body: InstallIn, request: Request, db: AsyncSession = Depends(get_db),
                         admin: User = Depends(require_admin)):
    from mmn.services import nodes

    opts = body.options.model_dump(exclude_none=True)
    try:
        node = await community.node_ok(db, body.node_id)
        if body.mode == "default":
            job = await community.start_install(db, slug, node.id, body.sha256, opts, requested_by=admin.username)
            out: dict[str, Any] = {"mode": "default", "job": nodes.job_out(job)}
        else:
            text, env = await community.interactive_env(db, slug, body.sha256, opts)
            job = await nodes.create_job(node.id, "community.install", {"slug": slug, "mode": "advanced",
                                                                        "script_sha256": body.sha256},
                                         source="ui", requested_by=admin.username, exclusive=False)
            s = await console.open_session(node.id, kind="pty", mode="script", script=text, env=env,
                                           user_id=admin.id, username=admin.username, label=slug, cols=body.cols,
                                           rows=body.rows, job_id=job.id)
            out = {"mode": "advanced", "session": s.id, "job": nodes.job_out(job)}
    except (NodeError, AgentError) as e:
        await audit(db, "community.install", user=admin, target=slug, ip=client_ip(request), success=False,
                    error=str(e), node_id=body.node_id)
        await db.commit()
        raise _bad(e) from e
    await audit(db, "community.install", user=admin, target=slug, ip=client_ip(request), node=node.name,
                mode=body.mode, script_sha256=body.sha256, job_id=out["job"]["id"],
                **{k: v for k, v in opts.items() if k not in ("password", "ssh_key")})
    await db.commit()
    return out


# ------------------------------------------------------------------ scheduled tasks
@router.get("/api/tasks")
async def list_tasks(db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    rows = (await db.execute(select(ScheduledTask).order_by(ScheduledTask.name))).scalars().all()
    return {"tasks": [tasks.task_out(t) for t in rows], "settings": await get_setting(db, "schedules")}


@router.post("/api/tasks", status_code=201)
async def create_task(body: tasks.TaskIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        t = await tasks.save_task(db, body, created_by=admin.username)
    except NodeError as e:
        raise _bad(e) from e
    await audit(db, "task.create", user=admin, target=t.name, ip=client_ip(request), kind=t.kind, cron=t.cron)
    await db.commit()
    await _reload()
    return tasks.task_out(t)


async def _reload() -> None:
    from mmn.services import scheduler

    await scheduler.reload_task_jobs()
    await scheduler.reload_nightly_jobs()


@router.post("/api/tasks/preview")
async def preview_targets(body: tasks.TaskIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await tasks.resolve_targets(db, body.kind, body.targets.model_dump(), body.options.model_dump())


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    try:
        t = await tasks.get_task(db, task_id)
    except NodeError as e:
        raise _bad(e) from e
    return {**tasks.task_out(t), "targets_now": await tasks.resolve_targets(db, t.kind, t.targets, t.options)}


@router.put("/api/tasks/{task_id}")
async def update_task(task_id: str, body: tasks.TaskIn, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        t = await tasks.save_task(db, body, await tasks.get_task(db, task_id))
    except NodeError as e:
        raise _bad(e) from e
    await audit(db, "task.update", user=admin, target=t.name, ip=client_ip(request), kind=t.kind, cron=t.cron,
                enabled=t.enabled)
    await db.commit()
    await _reload()
    return tasks.task_out(t)


@router.delete("/api/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db),
                      admin: User = Depends(require_admin)):
    try:
        t = await tasks.get_task(db, task_id)
    except NodeError as e:
        raise _bad(e) from e
    name = t.name
    await db.delete(t)
    await audit(db, "task.delete", user=admin, target=name, ip=client_ip(request))
    await db.commit()
    await _reload()
    return Response(status_code=204)


@router.post("/api/tasks/{task_id}/run", status_code=202)
async def run_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db),
                   admin: User = Depends(require_admin)):
    try:
        t = await tasks.get_task(db, task_id)
        run_id = await tasks.run_task(t.id, trigger="manual", requested_by=admin.username)
    except NodeError as e:
        raise _bad(e) from e
    await audit(db, "task.run", user=admin, target=t.name, ip=client_ip(request), run_id=run_id)
    await db.commit()
    return {"run_id": run_id}


@router.get("/api/task-runs")
async def list_runs(task_id: str | None = None, limit: int = Query(30, ge=1, le=200),
                    db: AsyncSession = Depends(get_db), _: User = Depends(current_user)):
    q = select(TaskRun).order_by(TaskRun.started_at.desc()).limit(limit)
    if task_id:
        q = q.where(TaskRun.task_id == task_id)
    return [tasks.run_out(r) for r in (await db.execute(q)).scalars()]


@router.get("/api/task-runs/{run_id}")
async def get_run(run_id: str, lang: str = Query("en", pattern=r"^(en|nl)$"), db: AsyncSession = Depends(get_db),
                  _: User = Depends(current_user)):
    r = await db.get(TaskRun, run_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    out = tasks.run_out(r)
    if r.kind == "pve_maintenance":
        from mmn.models import Environment
        from mmn.services import maintenance

        envs = {e.id: e for e in (await db.execute(select(Environment).where(
            Environment.id.in_(r.customer_ids or [])))).scalars()}
        out["maintenance"] = maintenance.run_view(r, envs, lang, await maintenance.tz_of(db))
    return out


class ResendIn(BaseModel):
    to: list[str] | None = Field(default=None, max_length=20)


@router.post("/api/task-runs/{run_id}/report/{customer_id}")
async def resend_report(run_id: str, customer_id: str, body: ResendIn, request: Request,
                        db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """Mail the maintenance report of one customer (again), to its report recipients or to ``to``."""
    from mmn.models import Environment
    from mmn.services import maintenance

    r = await db.get(TaskRun, run_id)
    e = await db.get(Environment, customer_id)
    if r is None or e is None or r.kind != "pve_maintenance" or customer_id not in (r.customer_ids or []):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if r.status in ("running", "waiting"):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="the run is not finished yet")
    saved = next((x for x in r.reports or [] if x.get("customer_id") == customer_id), {})
    rep = maintenance.build_report(r, e, maintenance._lang(e), await maintenance.tz_of(db))
    to = [x.strip().lower() for x in body.to or [] if x and "@" in x] or None
    res = await maintenance.send_report(db, r, e, rep["state"], saved.get("stats") or rep["stats"], to=to)
    if res.get("ok"):
        keep = [x for x in r.reports or [] if x.get("customer_id") != customer_id]
        r.reports = [*keep, {**saved, **res}]
    await audit(db, "maintenance.report", user=admin, target=e.name, ip=client_ip(request), run=run_id,
                success=bool(res.get("ok")), sent=len(res.get("to") or []))
    await db.commit()
    if not res.get("ok"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=res.get("error") or "send_failed")
    return {"sent_to": res["to"]}


class ScheduleSettingsIn(BaseModel):
    timezone: str = Field(max_length=64)


@router.put("/api/schedules-settings")
async def put_schedule_settings(body: ScheduleSettingsIn, request: Request, db: AsyncSession = Depends(get_db),
                                admin: User = Depends(require_admin)):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="unknown time zone") from e
    out = await set_setting(db, "schedules", body.model_dump())
    await audit(db, "schedules.settings", user=admin, ip=client_ip(request), timezone=body.timezone)
    await db.commit()
    await _reload()
    return out
