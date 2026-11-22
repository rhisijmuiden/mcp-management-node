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


def next_runs(target_id: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for kind in ("full", "incremental"):
        job = scheduler.get_job(f"backup:{target_id}:{kind}")
        nrt = getattr(job, "next_run_time", None) if job else None
        out[kind] = nrt.isoformat() if nrt else None
    return out


async def start() -> None:
    scheduler.add_job(_update_check_job, CronTrigger(hour="*/6", minute=17, timezone="UTC"), id="update-check",
                      replace_existing=True, coalesce=True)
    scheduler.start()
    await reload_backup_jobs()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
