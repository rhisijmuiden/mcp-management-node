"""Monitoring of all connected nodes: metric history (one sample per minute, 30 days), storage and
disk health (SMART) collected from Proxmox VE nodes, and alerts mailed through the app mail server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Alert, MetricSample, Node
from mmn.services import agent_hub, events
from mmn.services.agent_hub import AgentError
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.monitoring")

SAMPLE_EVERY = 55  # seconds; agents report every ~15-30 s, one row per minute is plenty
RETENTION_DAYS = 30
# range -> (look back, bucket size)
RANGES: dict[str, tuple[timedelta, timedelta]] = {
    "1h": (timedelta(hours=1), timedelta(minutes=1)),
    "24h": (timedelta(hours=24), timedelta(minutes=15)),
    "7d": (timedelta(days=7), timedelta(hours=1)),
    "30d": (timedelta(days=30), timedelta(hours=6)),
}
OK_HEALTH = {"PASSED", "OK", "UNKNOWN", ""}

_last_sample: dict[str, float] = {}


class MonitoringSettings(BaseModel):
    enabled: bool = True
    mail_to: list[str] = Field(default_factory=list, max_length=20)
    cpu_pct: int = Field(default=90, ge=10, le=100)
    cpu_minutes: int = Field(default=10, ge=1, le=120)
    mem_pct: int = Field(default=92, ge=10, le=100)
    disk_pct: int = Field(default=90, ge=10, le=100)
    storage_pct: int = Field(default=85, ge=10, le=100)
    offline_minutes: int = Field(default=5, ge=1, le=1440)
    smart: bool = True
    wearout_pct: int = Field(default=10, ge=0, le=100)  # alert when an SSD has this much life left or less


async def get_settings(db: AsyncSession) -> MonitoringSettings:
    raw = await get_setting(db, "monitoring")
    return MonitoringSettings(**{k: v for k, v in raw.items() if k in MonitoringSettings.model_fields})


async def save_settings(db: AsyncSession, body: MonitoringSettings) -> MonitoringSettings:
    await set_setting(db, "monitoring", body.model_dump())
    return body


# ------------------------------------------------------------------ samples
def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def record_sample(node_id: str, data: dict[str, Any], *, force: bool = False) -> bool:
    """Store a metrics message as a sample (at most one per node per minute)."""
    now = time.monotonic()
    if not force and now - _last_sample.get(node_id, 0) < SAMPLE_EVERY:
        return False
    _last_sample[node_id] = now
    load = data.get("load") if isinstance(data.get("load"), list) else []
    async with sessionmaker()() as db:
        db.add(MetricSample(node_id=node_id, cpu=_num(data.get("cpu_percent")),
                            load1=_num(load[0]) if load else None,
                            mem_used=int(_num(data.get("mem_used"))), mem_total=int(_num(data.get("mem_total"))),
                            swap_used=int(_num(data.get("swap_used"))),
                            disk_used=int(_num(data.get("disk_used"))), disk_total=int(_num(data.get("disk_total")))))
        await db.commit()
    return True


def _pct(used: float, total: float) -> float | None:
    return round(used * 100 / total, 1) if total else None


async def history(db: AsyncSession, node_ids: list[str], range_: str = "24h") -> dict[str, list[dict[str, Any]]]:
    """Averaged points per node for a range (1h, 24h, 7d, 30d)."""
    if range_ not in RANGES:
        raise ValueError("range must be one of 1h, 24h, 7d, 30d")
    back, bucket = RANGES[range_]
    out: dict[str, list[dict[str, Any]]] = {n: [] for n in node_ids}
    if not node_ids:
        return out
    since = datetime.now(UTC) - back
    rows = (await db.execute(text(
        "SELECT node_id, date_bin(:bucket, at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS b, avg(cpu) AS cpu, "
        "max(cpu) AS cpu_max, avg(mem_used) AS mem_used, max(mem_total) AS mem_total, avg(disk_used) AS disk_used, "
        "max(disk_total) AS disk_total, avg(load1) AS load1 FROM metric_samples "
        "WHERE at >= :since AND node_id = ANY(:ids) GROUP BY 1, 2 ORDER BY 2"
    ), {"bucket": bucket, "since": since, "ids": list(node_ids)})).all()
    for r in rows:
        out.setdefault(r.node_id, []).append({
            "at": r.b.isoformat(), "cpu": round(float(r.cpu or 0), 1), "cpu_max": round(float(r.cpu_max or 0), 1),
            "mem_pct": _pct(float(r.mem_used or 0), float(r.mem_total or 0)),
            "mem_used": int(r.mem_used or 0), "mem_total": int(r.mem_total or 0),
            "disk_pct": _pct(float(r.disk_used or 0), float(r.disk_total or 0)),
            "load1": round(float(r.load1), 2) if r.load1 is not None else None,
        })
    return out


async def prune() -> None:
    async with sessionmaker()() as db:
        await db.execute(delete(MetricSample).where(MetricSample.at < datetime.now(UTC) - timedelta(days=RETENTION_DAYS)))
        await db.execute(delete(Alert).where(Alert.resolved_at.is_not(None),
                                             Alert.resolved_at < datetime.now(UTC) - timedelta(days=90)))
        await db.commit()


# ------------------------------------------------------------------ storage and disks (SMART)
def _storage_out(s: dict[str, Any]) -> dict[str, Any]:
    total, used = int(_num(s.get("total"))), int(_num(s.get("used")))
    return {"storage": s.get("storage"), "type": s.get("type"), "content": s.get("content") or "",
            "active": bool(s.get("active")), "enabled": s.get("enabled", 1) not in (0, "0"),
            "shared": bool(s.get("shared")), "total": total, "used": used, "avail": int(_num(s.get("avail"))),
            "used_pct": _pct(used, total)}


def _disk_out(d: dict[str, Any]) -> dict[str, Any]:
    wear = d.get("wearout")
    try:
        wear_v = int(wear) if wear not in (None, "", "N/A") else None
    except (TypeError, ValueError):
        wear_v = None
    return {"devpath": d.get("devpath"), "model": d.get("model") or d.get("vendor") or "", "serial": d.get("serial"),
            "size": int(_num(d.get("size"))), "type": d.get("type") or "", "health": str(d.get("health") or "UNKNOWN"),
            "wearout": wear_v, "used": d.get("used") or ""}


async def collect_health(node_id: str) -> dict[str, Any]:
    """Storages and physical disks of a Proxmox VE node (through the agent), stored on the node."""
    from mmn.services import guests

    health: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "storages": [], "disks": [], "error": None}
    try:
        st = await guests.api(node_id, "get", "/nodes/localhost/storage", timeout=30)
        health["storages"] = [_storage_out(s) for s in (st or []) if isinstance(s, dict)]
        try:
            disks = await guests.api(node_id, "get", "/nodes/localhost/disks/list", timeout=60)
            health["disks"] = [_disk_out(d) for d in (disks or []) if isinstance(d, dict)]
        except AgentError as e:
            health["error"] = f"disks: {e}"
    except AgentError as e:
        health["error"] = str(e)
    async with sessionmaker()() as db:
        node = await db.get(Node, node_id)
        if node is not None:
            node.health = health
            await db.commit()
    events.publish("node.status", {"node_id": node_id, "online": True, "type": "health"})
    return health


async def collect_all_health() -> None:
    async with sessionmaker()() as db:
        ids = [n.id for n in (await db.execute(select(Node))).scalars() if "pve" in (n.capabilities or [])
               and agent_hub.is_online(n.id)]
    sem = asyncio.Semaphore(4)

    async def one(nid: str) -> None:
        async with sem:
            try:
                await collect_health(nid)
            except Exception:  # noqa: BLE001
                log.exception("health of node %s", nid)

    await asyncio.gather(*(one(i) for i in ids))


async def smart(node_id: str, disk: str) -> dict[str, Any]:
    from mmn.services import guests
    from mmn.services.nodes import NodeError

    if not disk.startswith("/dev/") or ".." in disk or len(disk) > 100:
        raise NodeError("disk must be a device path such as /dev/sda")
    res = await guests.api(node_id, "get", "/nodes/localhost/disks/smart", {"disk": disk}, timeout=60)
    return res or {}


# ------------------------------------------------------------------ overview
async def overview(db: AsyncSession, range_: str = "24h", environment_id: str | None = None) -> dict[str, Any]:
    from mmn.services import nodes

    nlist = await nodes.list_nodes(db, environment_id)
    hist = await history(db, [n.id for n in nlist], range_)
    open_alerts = list((await db.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars())
    rows = []
    tot = {"nodes": len(nlist), "online": 0, "cpu_sum": 0.0, "mem_used": 0, "mem_total": 0, "st_used": 0,
           "st_total": 0}
    for n in nlist:
        online = agent_hub.is_online(n.id)
        m = n.metrics or {}
        h = n.health or {}
        storages = h.get("storages") or []
        disks = h.get("disks") or []
        bad = [d for d in disks if d["health"].upper() not in OK_HEALTH]
        worst = "failed" if bad else ("ok" if disks else "unknown")
        if online:
            tot["online"] += 1
            tot["cpu_sum"] += _num(m.get("cpu_percent"))
            tot["mem_used"] += int(_num(m.get("mem_used")))
            tot["mem_total"] += int(_num(m.get("mem_total")))
        seen_shared: set[str] = set()
        for s in storages:
            if not s.get("active") or not s.get("total"):
                continue
            key = f"{n.cluster_name}:{s['storage']}" if s.get("shared") else f"{n.id}:{s['storage']}"
            if key in seen_shared:
                continue
            seen_shared.add(key)
            tot["st_used"] += s["used"]
            tot["st_total"] += s["total"]
        rows.append({
            "node_id": n.id, "name": n.name, "kind": n.kind, "environment_id": n.environment_id,
            "environment": n.environment.name if n.environment else None, "cluster": n.cluster_name, "online": online,
            "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
            "cpu": _num(m.get("cpu_percent")) if online else None, "cpus": m.get("cpus"),
            "load": m.get("load") or [], "mem_used": m.get("mem_used"), "mem_total": m.get("mem_total"),
            "disk_used": m.get("disk_used"), "disk_total": m.get("disk_total"),
            "uptime": m.get("uptime_seconds"), "trend": hist.get(n.id, []),
            "storages": storages, "disks": disks, "disk_health": worst, "health_at": h.get("at"),
            "health_error": h.get("error"),
            "alerts": sum(1 for a in open_alerts if a.node_id == n.id),
        })
    totals = {"nodes": tot["nodes"], "online": tot["online"],
              "cpu_avg": round(tot["cpu_sum"] / tot["online"], 1) if tot["online"] else None,
              "mem_used": tot["mem_used"], "mem_total": tot["mem_total"],
              "storage_used": tot["st_used"], "storage_total": tot["st_total"],
              "alerts_open": len(open_alerts)}
    return {"range": range_, "totals": totals, "nodes": rows}


def alert_out(a: Alert, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": a.id, "node_id": a.node_id, "node": (names or {}).get(a.node_id or "", None), "kind": a.kind,
            "key": a.key, "severity": a.severity, "message": a.message, "value": a.value, "detail": a.detail or {},
            "started_at": a.started_at.isoformat(), "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "notified_at": a.notified_at.isoformat() if a.notified_at else None}


async def list_alerts(db: AsyncSession, *, open_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    q = select(Alert).order_by(Alert.resolved_at.is_(None).desc(), Alert.started_at.desc()).limit(min(limit, 500))
    if open_only:
        q = q.where(Alert.resolved_at.is_(None))
    names = {n.id: n.name for n in (await db.execute(select(Node))).scalars()}
    return [alert_out(a, names) for a in (await db.execute(q)).scalars()]


# ------------------------------------------------------------------ alert evaluation
def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} EB"


async def _conditions(db: AsyncSession, cfg: MonitoringSettings) -> dict[str, dict[str, Any]]:
    """Every alert that should be open now, keyed by its alert key."""
    now = datetime.now(UTC)
    want: dict[str, dict[str, Any]] = {}
    nlist = list((await db.execute(select(Node))).scalars())
    since = now - timedelta(minutes=cfg.cpu_minutes)
    cpu_rows = (await db.execute(text(
        "SELECT node_id, avg(cpu) AS cpu, count(*) AS n FROM metric_samples WHERE at >= :since GROUP BY node_id"
    ), {"since": since})).all()
    cpu_avg = {r.node_id: (float(r.cpu or 0), int(r.n)) for r in cpu_rows}
    for n in nlist:
        online = agent_hub.is_online(n.id)
        if not online:
            seen = n.last_seen_at or n.enrolled_at
            if seen and now - seen > timedelta(minutes=cfg.offline_minutes):
                mins = int((now - seen).total_seconds() // 60)
                want[f"offline:{n.id}"] = {"node_id": n.id, "kind": "offline", "severity": "critical",
                                           "message": f"Agent not connected for {mins} minutes", "value": mins}
            continue
        m = n.metrics or {}
        avg, count = cpu_avg.get(n.id, (0.0, 0))
        if count >= max(2, cfg.cpu_minutes // 2) and avg >= cfg.cpu_pct:
            want[f"cpu:{n.id}"] = {"node_id": n.id, "kind": "cpu", "severity": "warning", "value": round(avg, 1),
                                   "message": f"CPU at {avg:.0f}% for {cfg.cpu_minutes} minutes"}
        mp = _pct(_num(m.get("mem_used")), _num(m.get("mem_total")))
        if mp is not None and mp >= cfg.mem_pct:
            want[f"memory:{n.id}"] = {"node_id": n.id, "kind": "memory", "severity": "warning", "value": mp,
                                      "message": f"Memory at {mp:.0f}%"}
        dp = _pct(_num(m.get("disk_used")), _num(m.get("disk_total")))
        if dp is not None and dp >= cfg.disk_pct:
            want[f"disk:{n.id}"] = {"node_id": n.id, "kind": "disk", "severity": "warning", "value": dp,
                                    "message": f"Root file system {m.get('disk_path') or '/'} at {dp:.0f}%"}
        h = n.health or {}
        for s in h.get("storages") or []:
            if s.get("active") and s.get("used_pct") is not None and s["used_pct"] >= cfg.storage_pct:
                key = f"storage:{n.cluster_name or n.id}:{s['storage']}" if s.get("shared") else \
                    f"storage:{n.id}:{s['storage']}"
                want.setdefault(key, {"node_id": n.id, "kind": "storage",
                                      "severity": "critical" if s["used_pct"] >= 95 else "warning",
                                      "value": s["used_pct"], "detail": {"storage": s["storage"]},
                                      "message": f"Storage {s['storage']} at {s['used_pct']:.0f}% "
                                                 f"({_fmt_bytes(s['avail'])} free)"})
        if cfg.smart:
            for d in h.get("disks") or []:
                if d["health"].upper() not in OK_HEALTH:
                    want[f"smart:{n.id}:{d['devpath']}"] = {
                        "node_id": n.id, "kind": "smart", "severity": "critical", "detail": {"disk": d["devpath"]},
                        "message": f"Disk {d['devpath']} ({d['model']}) SMART health {d['health']}"}
                elif d.get("wearout") is not None and d["wearout"] <= cfg.wearout_pct:
                    want[f"wearout:{n.id}:{d['devpath']}"] = {
                        "node_id": n.id, "kind": "smart", "severity": "warning", "value": d["wearout"],
                        "detail": {"disk": d["devpath"]},
                        "message": f"SSD {d['devpath']} ({d['model']}) has {d['wearout']}% life left"}
    return want


async def evaluate() -> dict[str, int]:
    """Open new alerts, update open ones and resolve the ones whose condition is gone."""
    async with sessionmaker()() as db:
        cfg = await get_settings(db)
        if not cfg.enabled:
            return {"opened": 0, "resolved": 0}
        want = await _conditions(db, cfg)
        open_alerts = {a.key: a for a in (await db.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars()}
        now = datetime.now(UTC)
        opened, resolved = [], []
        for key, c in want.items():
            a = open_alerts.get(key)
            if a is None:
                a = Alert(key=key, node_id=c["node_id"], kind=c["kind"], severity=c["severity"],
                          message=c["message"][:500], value=c.get("value"), detail=c.get("detail") or {})
                db.add(a)
                opened.append(a)
            else:
                a.message, a.value, a.severity = c["message"][:500], c.get("value"), c["severity"]
        for key, a in open_alerts.items():
            if key not in want:
                a.resolved_at = now
                resolved.append(a)
        await db.commit()
        names = {n.id: n.name for n in (await db.execute(select(Node))).scalars()}
        if (opened or resolved) and cfg.mail_to:
            sent = await _mail(db, cfg.mail_to, opened, resolved, names)
            if sent:
                for a in opened:
                    a.notified_at = now
                await db.commit()
    if opened or resolved:
        events.publish("monitoring.alerts", {"opened": len(opened), "resolved": len(resolved)})
    return {"opened": len(opened), "resolved": len(resolved)}


async def _mail(db: AsyncSession, to: list[str], opened: list[Alert], resolved: list[Alert],
                names: dict[str, str]) -> bool:
    from mmn.i18n import t
    from mmn.services.mailer import MailContent, try_send_mail

    general = await get_setting(db, "general")
    lang = general["default_language"]
    lines = [f"✖ {names.get(a.node_id or '', '-')}: {a.message}" for a in opened]
    lines += [f"✔ {names.get(a.node_id or '', '-')}: {t(lang, 'alert.resolved_line', message=a.message)}"
              for a in resolved]
    first = opened[0] if opened else resolved[0]
    subject = t(lang, "alert.subject_open" if opened else "alert.subject_resolved",
                node=names.get(first.node_id or "", "-"), message=first.message, count=len(opened) + len(resolved))
    base = (general.get("base_url") or "").rstrip("/")
    return await try_send_mail(db, to, MailContent(
        subject=subject, title=t(lang, "alert.title"), paragraphs=[t(lang, "alert.body"), *lines],
        muted=[t(lang, "alert.footer")], button_url=f"{base}/monitoring" if base else None,
        button_label=t(lang, "alert.button") if base else None), lang)
