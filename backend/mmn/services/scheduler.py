"""Scheduled jobs (backups, update checks). Runs inside the single app process."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from mmn.db import sessionmaker
from mmn.models import BackupTarget

log = logging.getLogger("mmn.scheduler")
scheduler = AsyncIOScheduler(timezone="UTC")


def validate_cron(expr: str) -> None:
    CronTrigger.from_crontab(expr, timezone="UTC")


async def _backup_job(target_id: str, kind: str) -> None:
    from mmn.services.backup.engine import BackupError, run_backup

    try:
        await run_backup(target_id, kind, trigger="schedule")
    except BackupError as e:
        log.warning("Scheduled %s backup for %s skipped: %s", kind, target_id, e)


async def _update_check_job() -> None:
    from mmn.services.updates import check_for_updates

    try:
        async with sessionmaker()() as db:
            await check_for_updates(db)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Automatic update check failed")


async def reload_backup_jobs() -> None:
    for job in scheduler.get_jobs():
        if job.id.startswith("backup:"):
            job.remove()
    async with sessionmaker()() as db:
        targets = (await db.execute(select(BackupTarget).where(BackupTarget.enabled.is_(True)))).scalars().all()
    for tgt in targets:
        for kind, expr in (("full", tgt.full_cron), ("incremental", tgt.incremental_cron)):
            if not expr:
                continue
            try:
                trigger = CronTrigger.from_crontab(expr, timezone="UTC")
            except ValueError:
                log.warning("Invalid cron %r on target %s", expr, tgt.name)
                continue
            scheduler.add_job(_backup_job, trigger, args=[tgt.id, kind], id=f"backup:{tgt.id}:{kind}",
                              max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)


async def _node_updates_job() -> None:
    from mmn.services.nodes import check_all_updates

    try:
        await check_all_updates()
    except Exception:  # noqa: BLE001
        log.exception("node update check failed")


async def _stale_jobs() -> None:
    from mmn.services.agent_hub import expire_stale_jobs

    await expire_stale_jobs()
    from mmn.services.servers import cleanup_spool

    try:
        await cleanup_spool()
    except Exception:  # noqa: BLE001
        log.exception("transfer cleanup failed")


def reload_node_jobs(hours: int) -> None:
    if not scheduler.running:
        return
    hours = max(1, min(int(hours or 6), 168))
    scheduler.add_job(_node_updates_job, "interval", hours=hours, id="node-updates", replace_existing=True,
                      coalesce=True, max_instances=1)


def next_runs(target_id: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for kind in ("full", "incremental"):
        job = scheduler.get_job(f"backup:{target_id}:{kind}")
        nrt = getattr(job, "next_run_time", None) if job else None
        out[kind] = nrt.isoformat() if nrt else None
    return out


# ------------------------------------------------------------------ scheduled tasks
_tz = "Europe/Amsterdam"


async def _task_job(task_id: str) -> None:
    from mmn.services import tasks
    from mmn.services.nodes import NodeError

    try:
        await tasks.run_task(task_id, trigger="schedule")
    except NodeError as e:
        log.info("scheduled task %s skipped: %s", task_id, e)
    except Exception:  # noqa: BLE001
        log.exception("scheduled task %s failed to start", task_id)


async def _community_sync_job() -> None:
    from mmn.services import community

    await community.sync_safe()


async def reload_task_jobs() -> None:
    global _tz
    from mmn.models import ScheduledTask
    from mmn.services.settings_store import get_setting

    if not scheduler.running:
        return
    for job in scheduler.get_jobs():
        if job.id.startswith("task:"):
            job.remove()
    async with sessionmaker()() as db:
        _tz = (await get_setting(db, "schedules")).get("timezone") or "UTC"
        community_cron = (await get_setting(db, "community")).get("sync_cron") or "15 3 * * *"
        rows = (await db.execute(select(ScheduledTask).where(ScheduledTask.enabled.is_(True)))).scalars().all()
    for t in rows:
        try:
            trigger = CronTrigger.from_crontab(t.cron, timezone=_tz)
        except ValueError:
            log.warning("Invalid cron %r on task %s", t.cron, t.name)
            continue
        scheduler.add_job(_task_job, trigger, args=[t.id], id=f"task:{t.id}", max_instances=1, coalesce=True,
                          misfire_grace_time=3600, replace_existing=True)
    try:
        scheduler.add_job(_community_sync_job, CronTrigger.from_crontab(community_cron, timezone=_tz),
                          id="community-sync", replace_existing=True, coalesce=True, max_instances=1)
    except ValueError:
        log.warning("Invalid community sync schedule %r", community_cron)


def next_task_run(task_id: str) -> str | None:
    job = scheduler.get_job(f"task:{task_id}") if scheduler.running else None
    nrt = getattr(job, "next_run_time", None) if job else None
    return nrt.isoformat() if nrt else None


async def _approvals_expire_job() -> None:
    from mmn.services import gateway

    async with sessionmaker()() as db:
        await gateway.expire_old(db)
        await db.commit()


async def _hub_health_job() -> None:
    from mmn.models import McpUpstream
    from mmn.services import hub, tenancy

    async with sessionmaker()() as db:
        with tenancy.unrestricted():
            rows = list((await db.execute(select(McpUpstream).where(McpUpstream.enabled.is_(True)))).scalars())
        for u in rows:
            await hub.check(db, u)
            await db.commit()
        await hub.sync(db)


async def _inventory_job() -> None:
    from mmn.services import tenancy, workstations

    try:
        with tenancy.unrestricted():
            await workstations.refresh_stale()
    except Exception:  # noqa: BLE001
        log.exception("workstation inventory refresh failed")


async def _reports_job() -> None:
    from mmn.services import reports

    try:
        done = await reports.run_schedule()
        if done:
            log.info("monthly reports made for: %s", ", ".join(done))
    except Exception:  # noqa: BLE001
        log.exception("monthly reports failed")


async def start() -> None:
    scheduler.add_job(_inventory_job, "interval", minutes=30, id="workstation-inventory", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_reports_job, CronTrigger(minute=23, timezone="UTC"), id="customer-reports",
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(_hub_health_job, "interval", minutes=5, id="hub-health", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_approvals_expire_job, "interval", minutes=5, id="ai-approvals-expire", replace_existing=True)
    scheduler.add_job(_update_check_job, CronTrigger(hour="*/6", minute=17, timezone="UTC"), id="update-check",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_stale_jobs, "interval", minutes=15, id="node-stale-jobs", replace_existing=True,
                      coalesce=True)
    scheduler.add_job(_monitor_eval_job, "interval", minutes=1, id="monitor-eval", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_monitor_health_job, "interval", minutes=10, id="monitor-health", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_monitor_prune_job, CronTrigger(hour=4, minute=41, timezone="UTC"), id="monitor-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_deploy_poll_job, "interval", minutes=5, id="deploy-poll", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_unifi_poll_job, "interval", minutes=5, id="unifi-poll", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_unifi_prune_job, CronTrigger(hour=4, minute=53, timezone="UTC"), id="unifi-prune",
                      replace_existing=True, coalesce=True)
    scheduler.start()
    await reload_backup_jobs()
    from mmn.services import tasks

    try:
        await tasks.ensure_defaults()
    except Exception:  # noqa: BLE001
        log.exception("default scheduled tasks")
    await reload_task_jobs()
    from mmn.services.settings_store import get_setting

    async with sessionmaker()() as db:
        reload_node_jobs((await get_setting(db, "agents")).get("updates_check_hours", 6))


async def _monitor_eval_job() -> None:
    from mmn.services import monitoring

    try:
        await monitoring.evaluate()
    except Exception:  # noqa: BLE001
        log.exception("monitoring evaluation failed")


async def _monitor_health_job() -> None:
    from mmn.services import monitoring

    try:
        await monitoring.collect_all_health()
    except Exception:  # noqa: BLE001
        log.exception("collecting node health failed")


async def _monitor_prune_job() -> None:
    from mmn.services import monitoring

    try:
        await monitoring.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning metrics failed")


async def _deploy_poll_job() -> None:
    from mmn.services import deploy

    try:
        await deploy.poll_all()
    except Exception:  # noqa: BLE001
        log.exception("polling deployments failed")


async def _unifi_poll_job() -> None:
    from mmn.services.unifi import service

    try:
        await service.poll_all()
    except Exception:  # noqa: BLE001
        log.exception("polling UniFi failed")


async def _unifi_prune_job() -> None:
    from mmn.services.unifi import service

    try:
        await service.prune_events()
    except Exception:  # noqa: BLE001
        log.exception("pruning UniFi events failed")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
