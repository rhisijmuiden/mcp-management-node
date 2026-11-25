"""Scheduled jobs (backups, update checks). Runs inside the single app process."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

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


async def _approvals_due_job() -> None:
    from mmn.services import gateway

    try:
        await gateway.run_due()
    except Exception:  # noqa: BLE001
        log.exception("starting approved calls failed")


async def _runbooks_job() -> None:
    from mmn.services import runbooks

    try:
        await runbooks.tick()
    except Exception:  # noqa: BLE001
        log.exception("runbooks tick failed")


async def _runbooks_prune_job() -> None:
    from mmn.services import runbooks

    try:
        await runbooks.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning runbook runs failed")


async def _remediation_job() -> None:
    from mmn.services import remediation

    try:
        await remediation.probe_services()
    except Exception:  # noqa: BLE001
        log.exception("service checks of automatic recovery failed")
    try:
        from mmn.services import netwatch

        await netwatch.probe()
    except Exception:  # noqa: BLE001
        log.exception("network checks of automatic recovery failed")
    try:
        await remediation.tick()
    except Exception:  # noqa: BLE001
        log.exception("automatic recovery tick failed")


async def _remediation_prune_job() -> None:
    from mmn.services import remediation

    try:
        await remediation.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning recovery attempts failed")


async def _nightly_job() -> None:
    from mmn.services import nightly

    try:
        await nightly.schedule_job()
    except Exception:  # noqa: BLE001
        log.exception("nightly checks failed")


async def _nightly_summary_job() -> None:
    from mmn.services import nightly

    try:
        await nightly.summary_job()
    except Exception:  # noqa: BLE001
        log.exception("morning summary failed")


async def _nightly_prune_job() -> None:
    from mmn.services import nightly

    try:
        await nightly.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning nightly runs failed")


async def reload_nightly_jobs() -> None:
    """(Re)schedule the nightly checks and the morning summary at the configured local times."""
    from mmn.services import nightly
    from mmn.services.settings_store import get_setting

    if not scheduler.running:
        return
    async with sessionmaker()() as db:
        cfg = await nightly.settings(db)
        tz = (await get_setting(db, "schedules")).get("timezone") or "UTC"
    for jid in ("nightly-run", "nightly-summary"):
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)
    if not cfg.get("enabled"):
        return
    rh, rm = nightly.parse_hhmm(cfg.get("run_at"), "02:30")
    sh, sm = nightly.parse_hhmm(cfg.get("summary_at"), "07:00")
    try:
        run_trigger = CronTrigger(hour=rh, minute=rm, timezone=tz)
        sum_trigger = CronTrigger(hour=sh, minute=sm, timezone=tz)
    except Exception:  # noqa: BLE001 - unknown time zone
        run_trigger = CronTrigger(hour=rh, minute=rm, timezone="UTC")
        sum_trigger = CronTrigger(hour=sh, minute=sm, timezone="UTC")
    scheduler.add_job(_nightly_job, run_trigger, id="nightly-run", replace_existing=True, coalesce=True,
                      max_instances=1, misfire_grace_time=3 * 3600)
    scheduler.add_job(_nightly_summary_job, sum_trigger, id="nightly-summary", replace_existing=True, coalesce=True,
                      max_instances=1, misfire_grace_time=6 * 3600)


def nightly_next_runs() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, jid in (("run", "nightly-run"), ("summary", "nightly-summary")):
        job = scheduler.get_job(jid) if scheduler.running else None
        nrt = getattr(job, "next_run_time", None) if job else None
        out[key] = nrt.isoformat() if nrt else None
    return out


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


async def _backup_coverage_job() -> None:
    from mmn.services import protection, tenancy

    try:
        with tenancy.unrestricted():
            await protection.collect_all()
    except Exception:  # noqa: BLE001
        log.exception("collecting backup coverage failed")


async def _reports_job() -> None:
    from mmn.services import reports

    try:
        done = await reports.run_schedule()
        if done:
            log.info("monthly reports made for: %s", ", ".join(done))
    except Exception:  # noqa: BLE001
        log.exception("monthly reports failed")


async def _tickets_close_job() -> None:
    from mmn.services import tickets

    try:
        n = await tickets.close_stale()
        if n:
            log.info("closed %d resolved tickets without a reply", n)
    except Exception:  # noqa: BLE001
        log.exception("closing resolved tickets failed")


async def start() -> None:
    scheduler.add_job(_inventory_job, "interval", minutes=30, id="workstation-inventory", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_backup_coverage_job, "interval", minutes=30, id="backup-coverage", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(minutes=2))
    scheduler.add_job(_reports_job, CronTrigger(minute=23, timezone="UTC"), id="customer-reports",
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(_hub_health_job, "interval", minutes=5, id="hub-health", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_approvals_expire_job, "interval", minutes=5, id="ai-approvals-expire", replace_existing=True)
    scheduler.add_job(_approvals_due_job, "interval", minutes=1, id="ai-approvals-due", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_runbooks_job, "interval", seconds=30, id="runbooks-tick", replace_existing=True,
                      coalesce=True, max_instances=1)
    scheduler.add_job(_remediation_job, "interval", minutes=1, id="remediation-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=40))
    scheduler.add_job(_remediation_prune_job, CronTrigger(hour=4, minute=59, timezone="UTC"), id="remediation-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_runbooks_prune_job, CronTrigger(hour=4, minute=53, timezone="UTC"), id="runbooks-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_nightly_prune_job, CronTrigger(hour=4, minute=47, timezone="UTC"), id="nightly-prune",
                      replace_existing=True, coalesce=True)
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
    scheduler.add_job(_uptime_job, "interval", seconds=15, id="uptime-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=20))
    scheduler.add_job(_uptime_prune_job, CronTrigger(hour=5, minute=7, timezone="UTC"), id="uptime-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_m365_job, "interval", minutes=1, id="m365-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=45))
    scheduler.add_job(_costs_job, "interval", hours=1, id="costs-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=105))
    scheduler.add_job(_changes_job, "interval", minutes=1, id="changes-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=50))
    scheduler.add_job(_netsec_job, "interval", minutes=1, id="netsec-tick", replace_existing=True,
                      coalesce=True, max_instances=1, next_run_time=datetime.now(UTC) + timedelta(seconds=75))
    scheduler.add_job(_m365_prune_job, CronTrigger(hour=5, minute=17, timezone="UTC"), id="m365-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_unifi_prune_job, CronTrigger(hour=4, minute=53, timezone="UTC"), id="unifi-prune",
                      replace_existing=True, coalesce=True)
    scheduler.add_job(_tickets_close_job, CronTrigger(minute=37, timezone="UTC"), id="tickets-close",
                      replace_existing=True, coalesce=True)
    scheduler.start()
    await reload_backup_jobs()
    from mmn.services import tasks

    try:
        await tasks.ensure_defaults()
    except Exception:  # noqa: BLE001
        log.exception("default scheduled tasks")
    await reload_task_jobs()
    try:
        from mmn.services import runbooks

        await runbooks.recover()
    except Exception:  # noqa: BLE001
        log.exception("recovering runbook runs")
    try:
        await reload_nightly_jobs()
    except Exception:  # noqa: BLE001
        log.exception("scheduling the nightly checks")
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


async def _uptime_job() -> None:
    from mmn.services import uptime

    try:
        await uptime.tick()
    except Exception:  # noqa: BLE001
        log.exception("uptime checks failed")


async def _uptime_prune_job() -> None:
    from mmn.services import uptime

    try:
        await uptime.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning uptime results failed")


async def _m365_job() -> None:
    from mmn.services import m365

    try:
        await m365.tick()
    except Exception:  # noqa: BLE001
        log.exception("Microsoft 365 sync failed")


async def _m365_prune_job() -> None:
    from mmn.services import m365

    try:
        await m365.prune()
    except Exception:  # noqa: BLE001
        log.exception("pruning Microsoft 365 sign-ins failed")


async def _netsec_job() -> None:
    from mmn.services import netsec

    try:
        await netsec.tick()
    except Exception:  # noqa: BLE001
        log.exception("certificate, firewall and VPN checks failed")


async def _costs_job() -> None:
    from mmn.services import costs

    try:
        await costs.tick()
    except Exception:  # noqa: BLE001
        log.exception("reading the usage of the customers for the costs failed")


async def _changes_job() -> None:
    from mmn.services import changes

    try:
        await changes.tick()
    except Exception:  # noqa: BLE001
        log.exception("change calendar tick failed")


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
