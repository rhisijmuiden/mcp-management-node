"""Automatic recovery ("self-healing", phase 14).

Known problems are fixed by Stackpilot itself, within the policy: a stopped service is restarted, a
full disk is cleaned up, an agent that went offline in a VM or container is started again.

How it works:

- **Detection** comes from what Stackpilot already sees: open monitoring alerts (offline agents,
  full disks and storage, memory, backups, UniFi, and services that a rule watches) and the
  findings of the nightly checks.
- **Rules** (per customer, or for every customer) link a problem to a runbook of phase 13 and fill
  in its parameters ({{machine.name}}, {{alert.detail.service}}, ...). A rule runs in mode
  ``auto`` (within the policy), ``approve`` (every changing step asks an admin) or ``notify`` (only
  report what would run).
- **Guardrails**: every step goes through the AI gateway like any call (destructive = approval,
  outside the maintenance window = approval, pauses), destructive steps always ask an admin, a
  Proxmox VE node is never rebooted and nothing is deleted. Per problem there is a cooldown and a
  maximum number of attempts per 24 hours (loop protection); for the whole app a maximum number of
  runs per hour, and too many failed recoveries in an hour pause everything (circuit breaker).
- **Emergency stop**: one switch stops all automatic recovery and cancels the runs that are busy.
- **Verification**: after a run the problem has to be gone (the alert resolved) within a few
  minutes; only then the attempt counts as fixed.
- Everything is in the audit log, in the app (live) and by mail, and in the morning summary and
  the monthly report.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import (
    Alert,
    AuditLog,
    Environment,
    NightlyFinding,
    Node,
    RemediationAttempt,
    RemediationRule,
    Runbook,
    RunbookRun,
)
from mmn.services import agent_hub, events, tenancy
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.remediation")

TRIGGERS = ("alert", "finding")
MODES = ("auto", "approve", "notify")
ALERT_KINDS = ("offline", "service", "disk", "storage", "memory", "cpu", "smart", "unifi", "backup")
# Finding codes of the nightly checks that a rule can answer (the interface offers these).
FINDING_CODES = ("disk_full", "guest_disk_full", "agent_old", "reboot_pending", "updates_pending",
                 "updates_pending_sec", "security_updates", "cve_fixable", "windows_security", "cert_expiring",
                 "cert_expired", "storage_full")
MACHINE_TYPES = ("proxmox", "server", "workstation")
SEVERITY_RANK = {"info": 0, "low": 1, "warning": 2, "medium": 2, "high": 3, "critical": 3}
ACTIVE = ("running", "waiting_approval", "verifying")
# Statuses that are a real attempt (count for the cooldown and the maximum per 24 hours).
REAL = ("running", "waiting_approval", "verifying", "succeeded", "failed", "cancelled")
FINAL = ("succeeded", "failed", "cancelled", "skipped", "notified")
LABEL = "Automatic recovery"
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.:\-]{1,100}$")
AGENT_SERVICE = "mmn-agent"
# Tools that automatic recovery never calls (whatever the policy says).
FORBIDDEN_TOOLS = {"install_update", "set_update_channel", "restore_backup", "restore_config_backup", "restore_guest",
                   "rollback_snapshot", "revert_network_changes", "apply_network_changes", "apply_sdn_design",
                   "migrate_guest", "unlock_door", "set_door_lock_rule"}


class RemediationError(Exception):
    pass


# ------------------------------------------------------------------ settings and the emergency stop
async def settings(db: AsyncSession) -> dict[str, Any]:
    return await get_setting(db, "remediation")


SETTING_FIELDS = {"enabled": bool, "max_runs_per_hour": int, "max_failures_per_hour": int,
                  "service_check_minutes": int, "verify_minutes": int, "notify_app": bool, "notify_mail": str,
                  "mail_to": str}
LIMITS = {"max_runs_per_hour": (1, 500), "max_failures_per_hour": (1, 100), "service_check_minutes": (1, 60),
          "verify_minutes": (1, 240)}


async def save_settings(db: AsyncSession, body: dict[str, Any], by: str | None) -> dict[str, Any]:
    cfg = await settings(db)
    for k, typ in SETTING_FIELDS.items():
        if k not in body or body[k] is None:
            continue
        v = body[k]
        if typ is int:
            lo, hi = LIMITS[k]
            try:
                v = int(v)
            except (TypeError, ValueError) as e:
                raise RemediationError(f"{k} must be a number") from e
            if not lo <= v <= hi:
                raise RemediationError(f"{k} must be between {lo} and {hi}")
        elif typ is bool:
            v = bool(v)
        else:
            v = str(v).strip()[:1000]
        if k == "notify_mail" and v not in ("always", "failure", "never"):
            raise RemediationError("notify_mail must be always, failure or never")
        cfg[k] = v
    await set_setting(db, "remediation", cfg)
    _audit(db, "settings", by, detail={k: cfg[k] for k in SETTING_FIELDS})
    return cfg


def _audit(db: AsyncSession, action: str, by: str | None, *, target: str | None = None, success: bool = True,
           source: str = "ui", detail: dict[str, Any] | None = None) -> None:
    db.add(AuditLog(action=f"remediation.{action}", username=by, source=source, target=(target or "")[:255] or None,
                    success=success, detail=json.loads(json.dumps(detail or {}, default=str))))


async def set_paused(db: AsyncSession, paused: bool, *, by: str, note: str | None = None,
                     source: str = "ui") -> dict[str, Any]:
    """The emergency stop. Pausing cancels the recoveries that are busy (their open approval
    requests are withdrawn); steps that already ran are not undone."""
    from mmn.services import runbooks

    cfg = await settings(db)
    now = datetime.now(UTC)
    cfg.update(paused=bool(paused), paused_by=by if paused else None, paused_at=now.isoformat() if paused else None,
               pause_note=(note or "").strip()[:500] if paused else "")
    if not paused:
        # The circuit breaker only counts failures after this.
        cfg["resumed_at"] = now.isoformat()
    await set_setting(db, "remediation", cfg)
    cancelled = 0
    if paused:
        for a in (await db.execute(select(RemediationAttempt).where(RemediationAttempt.status.in_(ACTIVE)))).scalars():
            run = await db.get(RunbookRun, a.run_id) if a.run_id else None
            if run is not None and run.status not in runbooks.FINAL:
                await runbooks.cancel(db, run, by=by, via="emergency stop")
            a.status, a.finished_at = "cancelled", now
            a.reason = {"code": "paused", "params": {"by": by}}
            cancelled += 1
    _audit(db, "pause" if paused else "resume", by, source=source, detail={"note": note, "cancelled": cancelled})
    await db.flush()
    events.publish("remediation.changed", {"paused": bool(paused)})
    return {**cfg, "cancelled": cancelled}


# ------------------------------------------------------------------ rules
def _str(v: Any, limit: int) -> str:
    return (v if isinstance(v, str) else "" if v is None else str(v)).strip()[:limit]


def clean_match(trigger: str, match: dict[str, Any] | None) -> dict[str, Any]:
    m = match or {}
    out: dict[str, Any] = {}
    if trigger == "alert":
        kinds = [k for k in (m.get("kinds") or []) if k in ALERT_KINDS]
        if not kinds:
            raise RemediationError(f"choose at least one alert kind ({', '.join(ALERT_KINDS)})")
        out["kinds"] = sorted(set(kinds), key=ALERT_KINDS.index)
        services = []
        for s in m.get("services") or []:
            s = _str(s, 100)
            if s and not UNIT_RE.match(s):
                raise RemediationError(f"service '{s}': use letters, digits and @ _ . : -")
            if s and s not in services:
                services.append(s)
        if "service" in out["kinds"] and not services:
            raise RemediationError("name the service(s) that Stackpilot watches, for example nginx")
        if services:
            out["services"] = services[:50]
    else:
        codes = [_str(c, 64) for c in (m.get("codes") or []) if _str(c, 64)]
        if not codes:
            raise RemediationError("choose at least one finding of the nightly checks")
        out["codes"] = sorted(set(codes))
    machine = _str(m.get("machine"), 200)
    if machine:
        out["machine"] = machine
    types = [t for t in (m.get("machine_types") or []) if t in MACHINE_TYPES]
    if types:
        out["machine_types"] = sorted(set(types))
    sev = _str(m.get("min_severity"), 16)
    if sev:
        if sev not in SEVERITY_RANK:
            raise RemediationError("unknown severity")
        out["min_severity"] = sev
    return out


async def save_rule(db: AsyncSession, rule: RemediationRule | None, *, name: str, description: str | None,
                    customer_id: str | None, enabled: bool, trigger: str, match: dict[str, Any] | None,
                    runbook_id: str | None, params: dict[str, Any] | None, mode: str, delay_minutes: int,
                    cooldown_minutes: int, max_attempts: int, by: str | None,
                    preset: str | None = None) -> RemediationRule:
    from mmn.services import runbooks

    name = _str(name, 120)
    if not name:
        raise RemediationError("give the rule a name")
    if trigger not in TRIGGERS:
        raise RemediationError("trigger must be alert or finding")
    if mode not in MODES:
        raise RemediationError("mode must be auto, approve or notify")
    m = clean_match(trigger, match)
    if customer_id is not None and await db.get(Environment, customer_id) is None:
        raise RemediationError("customer not found")
    rb = await db.get(Runbook, runbook_id) if runbook_id else None
    if rb is None:
        raise RemediationError("choose the runbook that fixes the problem")
    if rb.customer_id is not None and rb.customer_id != customer_id:
        raise RemediationError("this runbook belongs to another customer")
    p = {}
    names = {x["name"] for x in rb.params or []}
    for k, v in (params or {}).items():
        if k not in names:
            raise RemediationError(f"the runbook has no parameter '{k}'")
        if v not in (None, ""):
            p[k] = _str(v, 2000)
    for x in rb.params or []:
        if x.get("required", True) and x.get("default") in (None, "") and not p.get(x["name"]):
            raise RemediationError(f"fill in the runbook parameter '{x['name']}'")
    for v in p.values():
        for ref in runbooks._refs(v):  # noqa: SLF001
            if ref.split(".")[0] not in ("machine", "alert", "finding", "customer"):
                raise RemediationError(f"unknown placeholder {{{{{ref}}}}}: use machine, alert, finding or customer")
    blocked = [s["tool"] for s in rb.steps or [] if s.get("kind") == "tool" and
               (s.get("tool") in FORBIDDEN_TOOLS or str(s.get("tool")).startswith("delete_"))]
    if blocked:
        raise RemediationError(f"automatic recovery never calls {', '.join(sorted(set(blocked)))}; choose another runbook")

    def num(v: Any, lo: int, hi: int, label: str) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError) as e:
            raise RemediationError(f"{label} must be a number") from e
        if not lo <= n <= hi:
            raise RemediationError(f"{label} must be between {lo} and {hi}")
        return n

    now = datetime.now(UTC)
    if rule is None:
        rule = RemediationRule(created_at=now, created_by=by, preset=preset)
        db.add(rule)
    rule.name, rule.description, rule.customer_id, rule.enabled = name, _str(description, 2000) or None, customer_id, \
        bool(enabled)
    rule.trigger, rule.match, rule.runbook_id, rule.params, rule.mode = trigger, m, rb.id, p, mode
    rule.delay_minutes = num(delay_minutes, 0, 1440, "delay")
    rule.cooldown_minutes = num(cooldown_minutes, 1, 10080, "cooldown")
    rule.max_attempts = num(max_attempts, 1, 20, "maximum attempts")
    rule.updated_at, rule.updated_by = now, by
    await db.flush()
    _audit(db, "rule.save", by, target=rule.name, detail={"rule_id": rule.id, "mode": mode, "trigger": trigger,
                                                          "match": m, "runbook": rb.name, "enabled": rule.enabled})
    return rule


def rule_out(r: RemediationRule, names: dict[str, str] | None = None,
             runbooks_: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": r.id, "name": r.name, "description": r.description, "customer_id": r.customer_id,
            "customer_name": (names or {}).get(r.customer_id or ""), "enabled": r.enabled, "trigger": r.trigger,
            "match": r.match or {}, "runbook_id": r.runbook_id,
            "runbook_name": (runbooks_ or {}).get(r.runbook_id or ""), "params": r.params or {}, "mode": r.mode,
            "delay_minutes": r.delay_minutes, "cooldown_minutes": r.cooldown_minutes,
            "max_attempts": r.max_attempts, "preset": r.preset, "created_at": r.created_at,
            "created_by": r.created_by, "updated_at": r.updated_at, "updated_by": r.updated_by}


def attempt_out(a: RemediationAttempt, run: RunbookRun | None = None) -> dict[str, Any]:
    out = {"id": a.id, "rule_id": a.rule_id, "rule_name": a.rule_name, "customer_id": a.customer_id,
           "customer_name": a.customer_name, "source": a.source, "source_key": a.source_key,
           "source_id": a.source_id, "node_id": a.node_id, "target": a.target, "problem": a.problem,
           "status": a.status, "reason": a.reason or {}, "run_id": a.run_id, "runbook_name": a.runbook_name,
           "resolved": a.resolved, "created_at": a.created_at, "finished_at": a.finished_at}
    if run is not None:
        out["run_status"] = run.status
    return out


async def lookups(db: AsyncSession) -> tuple[dict[str, str], dict[str, str]]:
    with tenancy.unrestricted():
        names = {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}
    rbs = {r.id: r.name for r in (await db.execute(select(Runbook))).scalars()}
    return names, rbs


# ------------------------------------------------------------------ matching
def _glob(pattern: str, name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), p.strip().lower()) for p in pattern.split(",") if p.strip())


def _machine_ok(rule: RemediationRule, node: Node | None) -> bool:
    from mmn.services.customers import machine_type

    m = rule.match or {}
    if m.get("machine") and (node is None or not _glob(m["machine"], node.name)):
        return False
    if m.get("machine_types") and (node is None or machine_type(node) not in m["machine_types"]):
        return False
    return True


def _customer_ok(rule: RemediationRule, customer_id: str | None) -> bool:
    return rule.customer_id is None or rule.customer_id == customer_id


def match_alert(rule: RemediationRule, alert: Alert, node: Node | None, customer_id: str | None) -> bool:
    m = rule.match or {}
    if rule.trigger != "alert" or alert.kind not in (m.get("kinds") or []):
        return False
    if not _customer_ok(rule, customer_id) or not _machine_ok(rule, node):
        return False
    if alert.kind == "service" and (alert.detail or {}).get("service") not in (m.get("services") or []):
        return False
    if m.get("min_severity") and SEVERITY_RANK.get(alert.severity, 0) < SEVERITY_RANK[m["min_severity"]]:
        return False
    return True


def match_finding(rule: RemediationRule, f: dict[str, Any], node: Node | None, customer_id: str | None) -> bool:
    m = rule.match or {}
    if rule.trigger != "finding" or f.get("code") not in (m.get("codes") or []):
        return False
    if not _customer_ok(rule, customer_id) or not _machine_ok(rule, node):
        return False
    if m.get("min_severity") and SEVERITY_RANK.get(f.get("severity") or "info", 0) < SEVERITY_RANK[m["min_severity"]]:
        return False
    return True


def _ordered(rules: list[RemediationRule]) -> list[RemediationRule]:
    """Rules of the customer itself come before rules for every customer."""
    return sorted(rules, key=lambda r: (r.customer_id is None, r.name.lower()))


async def enabled_rules(db: AsyncSession, trigger: str) -> list[RemediationRule]:
    rows = (await db.execute(select(RemediationRule).where(RemediationRule.enabled.is_(True),
                                                           RemediationRule.trigger == trigger))).scalars().all()
    return _ordered(list(rows))


async def active(db: AsyncSession) -> bool:
    cfg = await settings(db)
    return bool(cfg.get("enabled")) and not cfg.get("paused")


# ------------------------------------------------------------------ context and parameters
def _node_ctx(n: Node | None) -> dict[str, Any]:
    from mmn.services.customers import machine_type

    if n is None:
        return {}
    return {"id": n.id, "name": n.name, "kind": n.kind, "type": machine_type(n), "hostname": n.hostname or n.name}


def alert_ctx(a: Alert) -> dict[str, Any]:
    return {"id": a.id, "key": a.key, "kind": a.kind, "severity": a.severity, "message": a.message, "value": a.value,
            "detail": a.detail or {}, "started_at": a.started_at.isoformat() if a.started_at else None}


def finding_ctx(f: NightlyFinding) -> dict[str, Any]:
    return {"id": f.id, "key": f.key, "code": f.code, "severity": f.severity, "target": f.target,
            "params": f.params or {}, "finding": f.finding, "category": f.category}


def build_params(rule: RemediationRule, ctx: dict[str, Any]) -> dict[str, Any]:
    from mmn.services import runbooks

    try:
        return {k: runbooks.render(v, ctx) for k, v in (rule.params or {}).items()}
    except runbooks.RunbookError as e:
        raise RemediationError(str(e)) from e


# ------------------------------------------------------------------ limits (loop protection)
async def _limit_reason(db: AsyncSession, cfg: dict[str, Any], rule: RemediationRule, key: str,
                        now: datetime) -> tuple[str, dict[str, Any]] | None:
    """("silent", ..) when nothing should happen now, ("skip", reason) when the attempt is refused
    and recorded once, None when a new attempt may start."""
    rows = list((await db.execute(select(RemediationAttempt).where(
        RemediationAttempt.source_key == key, RemediationAttempt.created_at >= now - timedelta(hours=24))
        .order_by(RemediationAttempt.created_at.desc()))).scalars())
    real = [a for a in rows if a.status in REAL]
    if any(a.status in ACTIVE for a in real):
        return "silent", {}
    # A rule that only reports (mode notify) reports a problem once per cooldown too.
    acted = [a for a in rows if a.status in REAL or a.status == "notified"]
    if acted and now - acted[0].created_at < timedelta(minutes=rule.cooldown_minutes):
        return "silent", {}
    last_real = real[0].created_at if real else None
    skipped_since = [a for a in rows if a.status == "skipped" and (last_real is None or a.created_at > last_real)]
    if len(real) >= rule.max_attempts:
        if any((a.reason or {}).get("code") == "max_attempts" for a in skipped_since):
            return "silent", {}
        return "skip", {"code": "max_attempts", "params": {"max": rule.max_attempts}}
    hour = (await db.execute(select(func.count()).select_from(RemediationAttempt).where(
        RemediationAttempt.status.in_(REAL), RemediationAttempt.created_at >= now - timedelta(hours=1)))).scalar_one()
    if hour >= int(cfg.get("max_runs_per_hour") or 20):
        if any((a.reason or {}).get("code") == "rate_limit" and now - a.created_at < timedelta(hours=1)
               for a in skipped_since):
            return "silent", {}
        return "skip", {"code": "rate_limit", "params": {"max": cfg.get("max_runs_per_hour")}}
    return None


async def _proxmox_target(db: AsyncSession, rule: RemediationRule, rb: Runbook, params: dict[str, Any]) -> str | None:
    """A runbook that reboots a machine never runs for a Proxmox VE node."""
    from mmn.services.customers import machine_type

    if not any(s.get("tool") == "reboot_node" for s in rb.steps or []):
        return None
    for v in params.values():
        if isinstance(v, str) and v:
            n = (await db.execute(select(Node).where(func.lower(Node.name) == v.lower()))).scalars().first()
            if n is not None and machine_type(n) == "proxmox":
                return n.name
    return None


# ------------------------------------------------------------------ starting a recovery
async def start_attempt(db: AsyncSession, cfg: dict[str, Any], rule: RemediationRule, *, source: str, key: str,
                        source_id: str | None, customer: Environment | None, node: Node | None, target: str | None,
                        problem: str, ctx: dict[str, Any], now: datetime | None = None) -> RemediationAttempt | None:
    """Start the rule's runbook for a problem (the caller commits, then calls ``kick``). Returns
    None when the limits say nothing should happen now."""
    from mmn.services import runbooks

    now = now or datetime.now(UTC)
    lim = await _limit_reason(db, cfg, rule, key, now)
    if lim is not None and lim[0] == "silent":
        return None
    rb = await db.get(Runbook, rule.runbook_id) if rule.runbook_id else None
    a = RemediationAttempt(rule_id=rule.id, rule_name=rule.name, customer_id=customer.id if customer else None,
                           customer_name=customer.name if customer else None, source=source, source_key=key[:200],
                           source_id=source_id, node_id=node.id if node else None,
                           target=(target or (node.name if node else None) or "")[:255] or None,
                           problem=(problem or "")[:500], status="running", reason={},
                           runbook_name=rb.name if rb else None, created_at=now)
    db.add(a)
    if lim is not None:
        a.status, a.reason, a.finished_at = "skipped", lim[1], now
    elif rb is None or not rb.enabled:
        a.status, a.finished_at = "failed", now
        a.reason = {"code": "no_runbook", "params": {}}
    else:
        try:
            params = build_params(rule, ctx)
            px = await _proxmox_target(db, rule, rb, params)
            if px:
                raise RemediationError(f"automatic recovery never reboots a Proxmox VE node ({px})")
            if rule.mode == "notify":
                runbooks.resolve_params(rb.params or [], params)
                a.status, a.finished_at = "notified", now
                a.reason = {"code": "notify_only", "params": {"runbook": rb.name, "params": params}}
            else:
                await db.flush()
                run = await runbooks.start_run(
                    db, rb, customer=customer, params=params, trigger="remediation",
                    reason=f"{LABEL} ({rule.name}): {problem}"[:2000], user_id=None, username=None,
                    token_id=None, token_name=LABEL,
                    context={"remediation": {"rule_id": rule.id, "rule": rule.name, "attempt_id": a.id,
                                             "mode": rule.mode},
                             "approve_all": rule.mode == "approve", source: ctx.get(source),
                             "machine": ctx.get("machine")})
                a.run_id = run.id
        except (RemediationError, runbooks.RunbookError) as e:
            a.status, a.finished_at = "failed", now
            a.reason = {"code": "error", "params": {"error": str(e)[:500]}}
    await db.flush()
    _audit(db, "start" if a.status == "running" else a.status, LABEL, target=a.target, source="system",
           success=a.status in ("running", "notified"),
           detail={"attempt_id": a.id, "rule": rule.name, "problem": a.problem, "run_id": a.run_id,
                   "customer": a.customer_name, "reason": a.reason})
    return a


def kick(a: RemediationAttempt | None) -> None:
    from mmn.services import runbooks

    if a is not None and a.run_id and a.status == "running":
        runbooks.kick(a.run_id)


async def _customer_of(db: AsyncSession, alert: Alert, node: Node | None) -> Environment | None:
    if node is not None:
        return node.environment
    cid = (alert.detail or {}).get("customer_id")
    if not cid and (alert.detail or {}).get("connection_id"):
        from mmn.models import UnifiConnection

        c = await db.get(UnifiConnection, alert.detail["connection_id"])
        cid = getattr(c, "environment_id", None) if c else None
    return await db.get(Environment, cid) if cid else None


async def scan_alerts(now: datetime | None = None) -> list[str]:
    """Start recoveries for open alerts that a rule answers. Returns the attempt ids."""
    now = now or datetime.now(UTC)
    started: list[RemediationAttempt] = []
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            cfg = await settings(db)
            if not cfg.get("enabled") or cfg.get("paused"):
                return []
            rules = await enabled_rules(db, "alert")
            if not rules:
                return []
            alerts = list((await db.execute(select(Alert).where(Alert.resolved_at.is_(None))
                                            .order_by(Alert.started_at))).scalars())
            for al in alerts:
                node = await db.get(Node, al.node_id) if al.node_id else None
                customer = await _customer_of(db, al, node)
                rule = next((r for r in rules if match_alert(r, al, node, customer.id if customer else None)), None)
                if rule is None or now - al.started_at < timedelta(minutes=rule.delay_minutes):
                    continue
                ctx = {"alert": alert_ctx(al), "machine": _node_ctx(node),
                       "customer": {"id": customer.id, "name": customer.name} if customer else {}}
                a = await start_attempt(db, cfg, rule, source="alert", key=f"alert:{al.key}", source_id=al.id,
                                        customer=customer, node=node, target=node.name if node else
                                        (al.detail or {}).get("source"), problem=al.message, ctx=ctx, now=now)
                if a is not None:
                    started.append(a)
                    await db.commit()
            ids = [a.id for a in started]
    for a in started:
        kick(a)
    if started:
        events.publish("remediation.changed", {"started": len(started)})
    for a in started:
        if a.status in FINAL:
            await notify_attempt(a.id)
    return ids


# ------------------------------------------------------------------ nightly findings
async def finding_rules(db: AsyncSession, customer_id: str) -> list[RemediationRule]:
    """The finding rules that apply to a customer now (none while paused or switched off)."""
    if not await active(db):
        return []
    return [r for r in await enabled_rules(db, "finding") if _customer_ok(r, customer_id)]


def rule_for_finding(rules: list[RemediationRule], f: dict[str, Any], node: Node | None,
                     customer_id: str) -> RemediationRule | None:
    return next((r for r in rules if match_finding(r, f, node, customer_id)), None)


async def on_findings(run_id: str, customer_id: str) -> list[str]:
    """After the nightly checks of a customer: start recoveries for findings that a rule answers."""
    from mmn.services import nightly

    started: list[RemediationAttempt] = []
    now = datetime.now(UTC)
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            rules = await finding_rules(db, customer_id)
            if not rules:
                return []
            cfg = await settings(db)
            e = await db.get(Environment, customer_id)
            if e is None:
                return []
            by_name = {n.name: n for n in e.nodes}
            rows = (await db.execute(select(NightlyFinding).where(NightlyFinding.run_id == run_id,
                                                                  NightlyFinding.environment_id == customer_id))).scalars()
            for f in rows:
                fd = {"code": f.code, "target": f.target, "params": f.params or {}, "severity": f.severity}
                node = nightly.node_of(fd, by_name)
                rule = rule_for_finding(rules, fd, node, customer_id)
                if rule is None:
                    continue
                ctx = {"finding": finding_ctx(f), "machine": _node_ctx(node), "customer": {"id": e.id, "name": e.name}}
                a = await start_attempt(db, cfg, rule, source="finding", key=f"finding:{f.key}", source_id=f.id,
                                        customer=e, node=node, target=f.target, problem=f.finding, ctx=ctx, now=now)
                if a is not None:
                    started.append(a)
                    await db.commit()
            ids = [a.id for a in started]
    for a in started:
        kick(a)
    for a in started:
        if a.status in FINAL:
            await notify_attempt(a.id)
    if started:
        events.publish("remediation.changed", {"started": len(started)})
    return ids


# ------------------------------------------------------------------ following the runs
async def sync(now: datetime | None = None) -> int:
    """Follow the runs of busy attempts: waiting for approval, verifying (is the problem gone?),
    fixed or failed. Also the circuit breaker. Returns the number of attempts that finished."""
    now = now or datetime.now(UTC)
    finished: list[str] = []
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            cfg = await settings(db)
            verify = timedelta(minutes=int(cfg.get("verify_minutes") or 15))
            rows = list((await db.execute(select(RemediationAttempt).where(
                RemediationAttempt.status.in_(ACTIVE)))).scalars())
            for a in rows:
                run = await db.get(RunbookRun, a.run_id) if a.run_id else None
                before = a.status
                if run is None:
                    a.status, a.finished_at, a.reason = "failed", now, {"code": "run_gone", "params": {}}
                elif run.status == "waiting_approval":
                    a.status = "waiting_approval"
                elif run.status in ("running", "waiting_input"):
                    a.status = "running"
                elif run.status == "cancelled":
                    a.status, a.finished_at = "cancelled", now
                    a.reason = a.reason or {"code": "cancelled", "params": {"error": run.error}}
                elif run.status == "failed":
                    a.status, a.finished_at, a.resolved = "failed", now, False
                    a.reason = {"code": "run_failed", "params": {"error": (run.error or "")[:500]}}
                elif run.status == "succeeded":
                    if a.source != "alert":
                        # A finding is checked again next night.
                        a.status, a.finished_at = "succeeded", now
                    else:
                        al = await db.get(Alert, a.source_id) if a.source_id else None
                        gone = al is None or al.resolved_at is not None
                        if gone:
                            a.status, a.finished_at, a.resolved = "succeeded", now, True
                        elif now - (run.finished_at or now) >= verify:
                            a.status, a.finished_at, a.resolved = "failed", now, False
                            a.reason = {"code": "not_fixed", "params": {"minutes": int(verify.total_seconds() // 60)}}
                        else:
                            a.status = "verifying"
                if a.status in FINAL and before not in FINAL:
                    finished.append(a.id)
                    _audit(db, a.status, LABEL, target=a.target, source="system", success=a.status == "succeeded",
                           detail={"attempt_id": a.id, "rule": a.rule_name, "run_id": a.run_id, "reason": a.reason,
                                   "resolved": a.resolved})
            await db.commit()
            tripped = False
            if not cfg.get("paused"):
                since = now - timedelta(hours=1)
                try:
                    since = max(since, datetime.fromisoformat(cfg["resumed_at"])) if cfg.get("resumed_at") else since
                except (TypeError, ValueError):
                    pass
                fails = (await db.execute(select(func.count()).select_from(RemediationAttempt).where(
                    RemediationAttempt.status == "failed", RemediationAttempt.finished_at >= since))).scalar_one()
                if fails >= int(cfg.get("max_failures_per_hour") or 5):
                    await set_paused(db, True, by=LABEL, source="system",
                                     note=f"circuit breaker: {fails} failed recoveries in the last hour")
                    await db.commit()
                    tripped = True
    if finished:
        events.publish("remediation.changed", {"finished": len(finished)})
    for aid in finished:
        await notify_attempt(aid)
    if tripped:
        await notify_paused()
    return len(finished)


async def tick() -> dict[str, int]:
    """Scheduler, every minute."""
    done = await sync()
    ids = await scan_alerts()
    return {"finished": done, "started": len(ids)}


# ------------------------------------------------------------------ watched services
# (node id, unit) -> {"state": "active"|"inactive"|"failed"|..., "at": datetime}
_service_state: dict[tuple[str, str], dict[str, Any]] = {}
_last_probe: datetime | None = None


def service_script(kind: str, units: list[str]) -> str:
    units = [u for u in units if UNIT_RE.match(u)]
    if kind == "windows":
        names = ",".join(f"'{u}'" for u in units)
        return ("$ErrorActionPreference = 'SilentlyContinue'\n"
                f"foreach ($n in @({names})) {{ $s = Get-Service -Name $n -ErrorAction SilentlyContinue; "
                "if ($s) { \"SVC`t$n`tloaded`t$($s.Status)\" } else { \"SVC`t$n`tnot-found`t\" } }")
    return ("for u in " + " ".join(units) + "; do "
            "l=$(systemctl show -p LoadState --value \"$u\" 2>/dev/null); "
            "a=$(systemctl show -p ActiveState --value \"$u\" 2>/dev/null); "
            "printf 'SVC\\t%s\\t%s\\t%s\\n' \"$u\" \"$l\" \"$a\"; done; exit 0")


def parse_services(stdout: str) -> dict[str, str | None]:
    """{unit: state}; None for a unit that does not exist on the machine."""
    out: dict[str, str | None] = {}
    for line in (stdout or "").splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) >= 3 and parts[0] == "SVC":
            out[parts[1]] = None if parts[2] in ("not-found", "") else (parts[3].strip().lower() if len(parts) > 3 else "")
    return out


def is_down(state: str | None) -> bool:
    return state in ("inactive", "failed", "stopped")


async def watched(db: AsyncSession) -> dict[str, list[str]]:
    """{node id: units} for the enabled service rules."""
    from mmn.services.customers import machine_type

    out: dict[str, list[str]] = {}
    rules = [r for r in await enabled_rules(db, "alert") if "service" in ((r.match or {}).get("kinds") or [])]
    if not rules:
        return out
    with tenancy.unrestricted():
        ns = list((await db.execute(select(Node))).scalars())
    for r in rules:
        for n in ns:
            if machine_type(n) == "workstation" and "workstation" not in ((r.match or {}).get("machine_types") or []):
                continue
            if _customer_ok(r, n.environment_id) and _machine_ok(r, n):
                lst = out.setdefault(n.id, [])
                lst += [u for u in (r.match or {}).get("services") or [] if u not in lst]
    return out


async def probe_services(force: bool = False) -> int:
    """Check the watched services on the online machines (every few minutes)."""
    global _last_probe
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        cfg = await settings(db)
        every = timedelta(minutes=int(cfg.get("service_check_minutes") or 5))
        if not force and _last_probe is not None and now - _last_probe < every - timedelta(seconds=5):
            return 0
        _last_probe = now
        want = await watched(db)
        with tenancy.unrestricted():
            kinds = {n.id: n.kind for n in (await db.execute(select(Node).where(Node.id.in_(list(want))))).scalars()} \
                if want else {}
    for k in [k for k in _service_state if k[0] not in want or k[1] not in want.get(k[0], [])]:
        _service_state.pop(k, None)
    checked = 0
    for nid, units in want.items():
        if not agent_hub.is_online(nid):
            continue
        try:
            res = await agent_hub.request(nid, "exec", {"command": service_script(kinds.get(nid, "linux"), units),
                                                        "timeout": 60}, timeout=90) or {}
        except Exception as e:  # noqa: BLE001 - one machine must not stop the others
            log.info("service check on %s: %s", nid, e)
            continue
        for unit, state in parse_services(res.get("stdout") or "").items():
            if state is None:
                _service_state.pop((nid, unit), None)
            else:
                _service_state[(nid, unit)] = {"state": state, "at": now}
            checked += 1
    return checked


async def service_conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Monitoring: an alert for every watched service that is down on an online machine."""
    cfg = await settings(db)
    fresh = timedelta(minutes=3 * int(cfg.get("service_check_minutes") or 5) + 2)
    want: dict[str, dict[str, Any]] = {}
    for (nid, unit), s in list(_service_state.items()):
        if not is_down(s.get("state")) or now - s["at"] > fresh or not agent_hub.is_online(nid):
            continue
        want[f"service:{nid}:{unit}"] = {"node_id": nid, "kind": "service", "severity": "critical",
                                         "detail": {"service": unit, "state": s["state"]},
                                         "message": f"Service {unit} is {s['state']}"}
    return want


# ------------------------------------------------------------------ repair tools
def _host_of(n: Node, pve_nodes: list[Node]) -> tuple[Node, dict[str, Any]] | None:
    """The Proxmox VE node and guest (VM or container) that a machine runs in: matched by name or
    host name, in the same customer first."""
    from mmn.services import guests

    names = {x.lower() for x in (n.name, n.hostname or "", (n.hostname or "").split(".")[0]) if x}
    hits = []
    for p in sorted(pve_nodes, key=lambda p: p.environment_id != n.environment_id):
        for g in guests.guests_of(p):
            if not g["template"] and g["name"] and g["name"].lower() in names:
                hits.append((p, g))
    return hits[0] if hits else None


async def recover_agent(node_ref: str, *, requested_by: str | None = None, source: str = "mcp") -> dict[str, Any]:
    """Bring an offline agent back: start the VM or container it runs in when that is stopped, or
    restart the agent service inside it (pct exec for a container, the QEMU guest agent for a VM).
    Never on a Proxmox VE node itself."""
    from mmn.services import guests, nodes
    from mmn.services.customers import machine_type
    from mmn.services.nodes import NodeError

    async with sessionmaker()() as db:
        n = await nodes.get_node(db, node_ref)
        if machine_type(n) == "proxmox":
            raise NodeError("this is a Proxmox VE node: automatic recovery never reboots or touches a node; check it on site")
        if agent_hub.is_online(n.id):
            return {"status": "online", "machine": n.name, "message": "the agent is connected; nothing to do"}
        pves = [p for p in (await db.execute(select(Node).where(Node.kind == "proxmox"))).scalars()
                if agent_hub.is_online(p.id)]
        hit = _host_of(n, pves)
        if hit is None:
            raise NodeError(f"{n.name} is not a VM or container on a Proxmox VE node that Stackpilot reaches; "
                            "check the machine itself")
        host, g = hit
        kind = n.kind
    vmid = int(g["vmid"])
    where = {"machine": n.name, "host": host.name, "vmid": vmid, "type": g["type"], "guest": g["name"]}
    if g["status"] != "running":
        job = await guests.power(host, g, "start", source=source, requested_by=requested_by)
        return {**where, "status": "starting", "action": "start", "job_id": job.id,
                "message": f"the {g['type']} was {g['status']}: started it"}
    if g["type"] == "lxc":
        cmd = f"pct exec {vmid} -- systemctl restart {AGENT_SERVICE} 2>&1"
    elif kind == "windows":
        cmd = (f"qm guest exec {vmid} --timeout 60 -- powershell.exe -NoProfile -Command "
               f"\"Restart-Service -Name {AGENT_SERVICE} -Force\" 2>&1")
    else:
        cmd = f"qm guest exec {vmid} --timeout 60 -- systemctl restart {AGENT_SERVICE} 2>&1"
    res = await agent_hub.request(host.id, "exec", {"command": cmd, "timeout": 90}, timeout=120) or {}
    code = res.get("exit_code", res.get("code"))
    out = ((res.get("stdout") or "") + (res.get("stderr") or ""))[-2000:]
    if code not in (0, None):
        hint = " (is the QEMU guest agent installed and running?)" if g["type"] == "qemu" else ""
        raise NodeError(f"restarting the agent in {g['type']} {vmid} on {host.name} failed{hint}: {out.strip()[:500]}")
    return {**where, "status": "restarted", "action": "restart_agent", "output": out,
            "message": "the agent service was restarted; it reconnects within a minute"}


LINUX_CLEANUP = ("df -h -x tmpfs -x devtmpfs 2>/dev/null | head -n 20; echo '--- cleaning'; "
                 "(command -v apt-get >/dev/null 2>&1 && apt-get clean); (command -v dnf >/dev/null 2>&1 && dnf clean all); "
                 "journalctl --vacuum-time=14d 2>&1 | tail -n 1; "
                 "find /tmp /var/tmp -xdev -type f -atime +7 -delete 2>/dev/null; "
                 "echo '--- after'; df -h -x tmpfs -x devtmpfs 2>/dev/null | head -n 20")
WINDOWS_CLEANUP = ("$b = Get-PSDrive -PSProvider FileSystem | Format-Table -AutoSize | Out-String; "
                   "Remove-Item \"$env:windir\\Temp\\*\" -Recurse -Force -ErrorAction SilentlyContinue; "
                   "Remove-Item \"$env:windir\\SoftwareDistribution\\Download\\*\" -Recurse -Force -ErrorAction SilentlyContinue; "
                   "Dism.exe /Online /Cleanup-Image /StartComponentCleanup /Quiet; "
                   "\"$b--- after\"; Get-PSDrive -PSProvider FileSystem | Format-Table -AutoSize | Out-String")


async def cleanup_disk(node_ref: str) -> dict[str, Any]:
    """Free disk space safely: package caches, journal older than 14 days and old temp files (Linux),
    temp files, the Windows Update download cache and the component store (Windows)."""
    from mmn.services import nodes
    from mmn.services.nodes import NodeError

    async with sessionmaker()() as db:
        n = await nodes.get_node(db, node_ref)
        nid, kind, name = n.id, n.kind, n.name
    if not agent_hub.is_online(nid):
        raise NodeError(f"the agent of {name} is offline")
    cmd, timeout = (WINDOWS_CLEANUP, 1800) if kind == "windows" else (LINUX_CLEANUP, 300)
    res = await nodes.node_call(nid, "exec", {"command": cmd, "timeout": timeout}) or {}
    return {"machine": name, "exit_code": res.get("exit_code", res.get("code")),
            "output": ((res.get("stdout") or "") + (res.get("stderr") or ""))[-8000:]}


# ------------------------------------------------------------------ runbook guardrails
async def step_blocked(db: AsyncSession, tool: str, args: dict[str, Any]) -> str | None:
    """Why a step of a recovery run may not run, whatever the policy says (None = it may)."""
    from mmn.services.customers import machine_type

    if tool in FORBIDDEN_TOOLS or tool.startswith("delete_"):
        return f"automatic recovery never calls {tool}"
    if tool == "reboot_node":
        ref = str(args.get("node") or "")
        with tenancy.unrestricted():
            n = await db.get(Node, ref) or (await db.execute(select(Node).where(
                func.lower(Node.name) == ref.lower()))).scalars().first()
        if n is None or machine_type(n) == "proxmox":
            return "automatic recovery never reboots a Proxmox VE node"
    return None


# ------------------------------------------------------------------ notifications
def _texts(a: RemediationAttempt, lang: str) -> tuple[str, list[str]]:
    from mmn.i18n import t

    what = a.target or a.customer_name or "-"
    subject = t(lang, f"remediation.subject_{a.status}", target=what, rule=a.rule_name)
    paras = [t(lang, f"remediation.body_{a.status}", target=what, rule=a.rule_name, runbook=a.runbook_name or "-",
               customer=a.customer_name or "-"),
             t(lang, "remediation.problem", problem=a.problem or "-")]
    if a.reason and a.reason.get("code"):
        paras.append(t(lang, "remediation.why", why=reason_text(a.reason, lang)))
    return subject, paras


def reason_text(r: dict[str, Any], lang: str = "en") -> str:
    from mmn.i18n import t

    code = (r or {}).get("code") or ""
    p = (r or {}).get("params") or {}
    try:
        if code in ("run_failed", "error", "cancelled"):
            return t(lang, f"remediation.reason_{code}", error=p.get("error") or "-")
        return t(lang, f"remediation.reason_{code}", **{k: v for k, v in p.items() if isinstance(v, (str, int, float))})
    except (KeyError, IndexError, ValueError):
        return code


async def notify_attempt(attempt_id: str) -> None:
    """App and mail notification of an attempt that finished. Never raises."""
    from mmn.services.ai_notify import _recipients
    from mmn.services.mailer import MailContent, send_mail

    try:
        async with sessionmaker()() as db:
            a = await db.get(RemediationAttempt, attempt_id)
            if a is None or a.notified_at is not None:
                return
            cfg = await settings(db)
            if cfg.get("notify_app"):
                events.publish("remediation.notice", {"id": a.id, "status": a.status, "target": a.target,
                                                      "rule": a.rule_name, "problem": a.problem})
            mode = cfg.get("notify_mail") or "failure"
            bad = a.status == "failed" or (a.status == "skipped" and (a.reason or {}).get("code") == "max_attempts")
            if (mode == "always" and a.status != "cancelled") or (mode == "failure" and bad):
                if (await get_setting(db, "mail")).get("enabled"):
                    url = await page_url(db)
                    by_lang: dict[str, list[str]] = {}
                    for addr, lang in await _recipients(db, {"notify_mail_to": cfg.get("mail_to") or ""}):
                        by_lang.setdefault(lang if lang in ("en", "nl") else "en", []).append(addr)
                    from mmn.i18n import t

                    for lang, to in by_lang.items():
                        subject, paras = _texts(a, lang)
                        await send_mail(db, to, MailContent(
                            subject=subject, title=t(lang, "remediation.title"), paragraphs=paras,
                            muted=[t(lang, "remediation.footer")], button_url=url,
                            button_label=t(lang, "remediation.button") if url else None), lang)
            a.notified_at = datetime.now(UTC)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("remediation notification")


async def notify_paused() -> None:
    from mmn.i18n import t
    from mmn.services.ai_notify import _recipients
    from mmn.services.mailer import MailContent, send_mail

    try:
        async with sessionmaker()() as db:
            cfg = await settings(db)
            events.publish("remediation.notice", {"status": "paused", "note": cfg.get("pause_note")})
            if cfg.get("notify_mail") == "never" or not (await get_setting(db, "mail")).get("enabled"):
                return
            url = await page_url(db)
            by_lang: dict[str, list[str]] = {}
            for addr, lang in await _recipients(db, {"notify_mail_to": cfg.get("mail_to") or ""}):
                by_lang.setdefault(lang if lang in ("en", "nl") else "en", []).append(addr)
            for lang, to in by_lang.items():
                await send_mail(db, to, MailContent(
                    subject=t(lang, "remediation.subject_paused"), title=t(lang, "remediation.title"),
                    paragraphs=[t(lang, "remediation.body_paused", note=cfg.get("pause_note") or "-")],
                    muted=[t(lang, "remediation.footer")], button_url=url,
                    button_label=t(lang, "remediation.button") if url else None), lang)
    except Exception:  # noqa: BLE001
        log.exception("remediation pause notification")


async def page_url(db: AsyncSession) -> str | None:
    from mmn.services.links import public_base_url

    base = await public_base_url(db)
    return f"{base}/remediation" if base else None


# ------------------------------------------------------------------ summaries (morning summary, monthly report)
async def summary(db: AsyncSession, since: datetime, until: datetime | None = None,
                  customer_ids: set[str] | None = None, limit: int = 30) -> dict[str, Any]:
    q = select(RemediationAttempt).where(RemediationAttempt.created_at >= since)
    if until is not None:
        q = q.where(RemediationAttempt.created_at < until)
    rows = [a for a in (await db.execute(q.order_by(RemediationAttempt.created_at.desc()))).scalars()
            if customer_ids is None or a.customer_id in customer_ids]
    counts = {s: 0 for s in ("succeeded", "failed", "waiting", "running", "skipped", "notified", "cancelled")}
    for a in rows:
        key = "waiting" if a.status == "waiting_approval" else "running" if a.status in ("running", "verifying") \
            else a.status
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(rows), "counts": counts, "items": [attempt_out(a) for a in rows[:limit]]}


async def prune(days: int = 365) -> int:
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=days)
    async with sessionmaker()() as db:
        res = await db.execute(delete(RemediationAttempt).where(RemediationAttempt.created_at < cutoff))
        await db.commit()
    return res.rowcount or 0


# ------------------------------------------------------------------ presets (recommended rules)
PRESETS: list[dict[str, Any]] = [
    {"key": "restart_service", "template": "restart_service", "trigger": "alert",
     "name": {"en": "Restart a stopped service", "nl": "Gestopte service herstarten"},
     "description": {"en": "When a watched service stops, restart it and check that it runs again.",
                     "nl": "Als een bewaakte service stopt, herstart hem en controleer dat hij weer draait."},
     "match": {"kinds": ["service"], "services": ["nginx"]},
     "params": {"server": "{{machine.name}}", "service": "{{alert.detail.service}}"},
     "delay_minutes": 2, "cooldown_minutes": 15, "max_attempts": 3},
    {"key": "free_disk_space", "template": "free_disk_space", "trigger": "alert",
     "name": {"en": "Free disk space on a full disk", "nl": "Ruimte vrijmaken op een volle schijf"},
     "description": {"en": "When the root file system of a server is nearly full, clean up package caches, old logs "
                           "and temp files.",
                     "nl": "Als het root-bestandssysteem van een server bijna vol is, ruim pakketcache, oude logs en "
                           "tijdelijke bestanden op."},
     "match": {"kinds": ["disk"], "machine_types": ["server"]},
     "params": {"server": "{{machine.name}}"},
     "delay_minutes": 10, "cooldown_minutes": 360, "max_attempts": 2},
    {"key": "recover_agent", "template": "recover_offline_agent", "trigger": "alert",
     "name": {"en": "Bring an offline agent back", "nl": "Offline agent terugbrengen"},
     "description": {"en": "When the agent of a VM or container goes offline, start the guest or restart the agent "
                           "inside it. Never on a Proxmox VE node.",
                     "nl": "Als de agent van een VM of container offline gaat, start de guest of herstart de agent "
                           "erin. Nooit op een Proxmox VE-node."},
     "match": {"kinds": ["offline"], "machine_types": ["server"]},
     "params": {"server": "{{machine.name}}"},
     "delay_minutes": 10, "cooldown_minutes": 60, "max_attempts": 2},
    {"key": "update_agent", "template": "update_agent", "trigger": "finding",
     "name": {"en": "Update outdated agents", "nl": "Verouderde agents bijwerken"},
     "description": {"en": "When the nightly checks find an outdated agent, update it.",
                     "nl": "Als de nachtelijke checks een verouderde agent vinden, werk hem bij."},
     "match": {"codes": ["agent_old"]},
     "params": {"server": "{{machine.name}}"},
     "delay_minutes": 0, "cooldown_minutes": 1440, "max_attempts": 1},
]


def preset_list(lang: str) -> list[dict[str, Any]]:
    lang = "nl" if lang == "nl" else "en"
    return [{**p, "name": p["name"][lang], "description": p["description"][lang]} for p in PRESETS]


async def add_preset(db: AsyncSession, key: str, *, customer_id: str | None, lang: str, by: str | None,
                     services: list[str] | None = None) -> RemediationRule:
    """Create a recommended rule, and the runbook it uses from the built-in template when there is
    none yet."""
    from mmn.services import runbook_templates, runbooks

    p = next((x for x in PRESETS if x["key"] == key), None)
    if p is None:
        raise RemediationError("unknown preset")
    rb = (await db.execute(select(Runbook).where(Runbook.template == p["template"], Runbook.enabled.is_(True),
                                                 (Runbook.customer_id.is_(None)) | (Runbook.customer_id == customer_id))
                           .order_by(Runbook.customer_id.is_(None)))).scalars().first()
    if rb is None:
        tpl = runbook_templates.get(p["template"], lang)
        if tpl is None:
            raise RemediationError("the runbook template is missing")
        rb = await runbooks.save(db, None, name=tpl["name"], description=tpl["description"], category=tpl["category"],
                                 customer_id=None, enabled=True, params=tpl["params"], steps=tpl["steps"],
                                 use_window=True, triggers=[], by=by, template=p["template"])
    match = dict(p["match"])
    if services:
        match["services"] = services
    lang = "nl" if lang == "nl" else "en"
    return await save_rule(db, None, name=p["name"][lang], description=p["description"][lang], customer_id=customer_id,
                           enabled=True, trigger=p["trigger"], match=match, runbook_id=rb.id, params=p["params"],
                           mode="auto", delay_minutes=p["delay_minutes"], cooldown_minutes=p["cooldown_minutes"],
                           max_attempts=p["max_attempts"], by=by, preset=key)
