"""Orchestrated maintenance of Proxmox VE nodes (v0.24.0).

A scheduled task of kind ``pve_maintenance`` maintains the Proxmox VE nodes of one or more customers, one
cluster at a time and one node at a time:

1. **Checks before**: every node of the cluster online, the cluster quorate, which machines (guests) run where.
   A problem stops the cluster before anything changes.
2. **Maintenance window and approval**: a scheduled run outside the customer's maintenance window does
   nothing; when the customer's AI policy (or the task) asks for approval, the run asks an admin in the
   Approvals inbox and waits (until the window closes).
3. **Per node**: install the updates; when a restart is needed (or always, per the task), move the running
   machines to another online node of the cluster (live for VMs). A machine that can't move (no other node,
   local disks, a lock) is shut down cleanly, in reverse start-up order. Then the node restarts, Stackpilot waits
   until it is back (and the cluster quorate), starts the machines that were shut down in their start-up order
   (Proxmox ``startup: order=..,up=..``) and moves the others back. A node that does not come back stops the
   run for that cluster.
4. **Checks after**: every node online, quorum, every machine that ran before runs again.

Every step lands on the run's **timeline** in plain words ("01:52 3 machines shut down cleanly"). After the run
every customer gets a report with that timeline in their language (EN/NL) at the report recipients of the
customer (Customers → Reports), and the run is written to the change calendar (origin: schedule) so the
customer sees it in the portal as well.

The steps on the machines go through :class:`PveOps` so the tests can replace the cluster with a fake.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import AiApproval, Change, Environment, Node, ScheduledTask, TaskRun
from mmn.services import events, tenancy
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.maintenance")

KIND = "pve_maintenance"
APPROVAL_TOOL = "continue_maintenance"
APPROVAL_SOURCE = "maintenance"
# Seconds between two looks while waiting (approval, a node coming back). The tests make it small.
POLL = 10.0
MAX_UP_DELAY = 300  # at most this many seconds between two start-ups (Proxmox "up=")
QUORUM_WAIT = 300
SETTLE = 30  # seconds after a node is back before Stackpilot starts machines itself (Proxmox starts onboot ones)
MAX_NAMES = 8

# ------------------------------------------------------------------ texts
TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Maintenance report", "change_title": "Maintenance of the Proxmox servers",
        "change_desc": "Updates of the Proxmox servers. Where a restart is needed, machines are moved to another "
                       "server or shut down cleanly and started again afterwards.",
        "subject_ok": "{customer}: maintenance completed", "subject_warning": "{customer}: maintenance completed "
                      "with remarks", "subject_failed": "{customer}: maintenance did not go as planned",
        "subject_skipped": "{customer}: maintenance not carried out",
        "intro_ok": "Last night we carried out the planned maintenance on your servers. Everything went well and "
                    "all machines are running again.",
        "intro_warning": "Last night we carried out the planned maintenance on your servers. It is done, but a "
                         "few things need attention; we are following them up.",
        "intro_failed": "Last night we started the planned maintenance on your servers, but it did not go as "
                        "planned. We are following it up; you don't have to do anything.",
        "intro_skipped": "The planned maintenance on your servers was not carried out this time.",
        "summary": "{nodes} {servers} maintained, {updates} {update_word} installed, {restarts} {restart_word}.",
        "server_1": "server", "server_n": "servers", "update_1": "update", "update_n": "updates",
        "restart_1": "restart", "restart_n": "restarts",
        "timeline": "What we did, step by step:",
        "footer": "Times are in {tz}. Questions? Reply to this mail or open a ticket in the portal.",
        "button": "View in the portal",
        "and_more": "{names} and {n} more",
        "start": "Maintenance started on {cluster}",
        "outside_window": "Not started: outside the maintenance window ({window})",
        "approval_wait": "Waiting for approval of an administrator",
        "approved": "Approved by {by}",
        "rejected": "Not approved: the maintenance was skipped",
        "approval_expired": "No approval before the maintenance window closed: the maintenance was skipped",
        "blocked": "Not started: {reason}",
        "precheck_ok": "Checks done: {nodes} {servers} online, {guests} {machines} running, everything in order",
        "precheck_failed": "Checks found a problem ({problem}): the maintenance was stopped before anything "
                           "changed",
        "problem_offline": "{node} is not reachable", "problem_quorum": "the cluster has no quorum",
        "updates_none": "{node}: no updates available",
        "updating": "{node}: installing {count} {update_word}",
        "updates_done": "{node}: {count} {update_word} installed",
        "updates_failed": "{node}: installing the updates failed; the server was not restarted",
        "no_reboot": "{node}: no restart needed",
        "reboot_skipped": "{node}: a restart is needed but this task does not restart servers",
        "nothing_running": "{node}: no machines running, nothing to move",
        "migrated": "{node}: {count} {machines} moved to {target} without interruption ({names})",
        "migrated_ct": "{node}: {count} {machines} moved to {target} with a short restart ({names})",
        "migrate_failed": "{name} could not be moved and will be shut down instead",
        "shutdown": "{node}: {count} {machines} shut down cleanly ({names})",
        "shutdown_failed": "{node}: {name} could not be shut down; the server was not restarted",
        "rebooting": "{node}: restarting",
        "back": "{node}: back online after {minutes} min",
        "not_back": "{node}: not back within {minutes} minutes; the maintenance was stopped. An engineer is "
                    "following it up.",
        "no_quorum_after": "{node}: the cluster has no quorum after the restart",
        "started": "{node}: {count} {machines} started again in the right order ({names})",
        "autostarted": "{node}: {count} {machines} started by the server itself ({names})",
        "start_failed": "{name} did not start again",
        "migrated_back": "{count} {machines} moved back to {node} ({names})",
        "migrate_back_failed": "{name} could not be moved back to {node} and runs on {target}",
        "skipped_after_problem": "{node}: skipped because of the problem on an earlier server",
        "node_offline": "{node}: not reachable, skipped",
        "postcheck_ok": "Final check: all {nodes} {servers} online and all {guests} {machines} running again",
        "postcheck_issues": "Final check: {issues}",
        "issue_offline": "{node} is not reachable", "issue_not_running": "{name} is not running",
        "issue_quorum": "the cluster has no quorum",
        "done": "Maintenance completed", "done_failed": "Maintenance ended with problems",
        "machine_1": "machine", "machine_n": "machines",
        "interrupted": "The maintenance was interrupted because Stackpilot restarted",
    },
    "nl": {
        "title": "Onderhoudsrapport", "change_title": "Onderhoud van de Proxmox-servers",
        "change_desc": "Updates van de Proxmox-servers. Waar een herstart nodig is, worden machines naar een andere "
                       "server verplaatst of netjes afgesloten en daarna weer gestart.",
        "subject_ok": "{customer}: onderhoud uitgevoerd", "subject_warning": "{customer}: onderhoud uitgevoerd, "
                      "met aandachtspunten", "subject_failed": "{customer}: onderhoud verliep niet zoals gepland",
        "subject_skipped": "{customer}: onderhoud niet uitgevoerd",
        "intro_ok": "Vannacht hebben we het geplande onderhoud aan uw servers uitgevoerd. Alles is goed gegaan en "
                    "alle machines draaien weer.",
        "intro_warning": "Vannacht hebben we het geplande onderhoud aan uw servers uitgevoerd. Het is klaar, maar "
                         "een paar punten vragen aandacht; die pakken we op.",
        "intro_failed": "Vannacht zijn we begonnen met het geplande onderhoud aan uw servers, maar het verliep "
                        "niet zoals gepland. We pakken het op; u hoeft niets te doen.",
        "intro_skipped": "Het geplande onderhoud aan uw servers is deze keer niet uitgevoerd.",
        "summary": "{nodes} {servers} onderhouden, {updates} {update_word} geïnstalleerd, {restarts} "
                   "{restart_word}.",
        "server_1": "server", "server_n": "servers", "update_1": "update", "update_n": "updates",
        "restart_1": "herstart", "restart_n": "herstarts",
        "timeline": "Wat we hebben gedaan, stap voor stap:",
        "footer": "Tijden in {tz}. Vragen? Beantwoord deze mail of maak een ticket aan in het portaal.",
        "button": "Bekijken in het portaal",
        "and_more": "{names} en nog {n}",
        "start": "Onderhoud gestart op {cluster}",
        "outside_window": "Niet gestart: buiten het onderhoudsvenster ({window})",
        "approval_wait": "Wachten op goedkeuring van een beheerder",
        "approved": "Goedgekeurd door {by}",
        "rejected": "Niet goedgekeurd: het onderhoud is overgeslagen",
        "approval_expired": "Geen goedkeuring voordat het onderhoudsvenster sloot: het onderhoud is overgeslagen",
        "blocked": "Niet gestart: {reason}",
        "precheck_ok": "Controles uitgevoerd: {nodes} {servers} online, {guests} {machines} actief, alles in orde",
        "precheck_failed": "De controles vonden een probleem ({problem}): het onderhoud is gestopt voordat er iets "
                           "veranderde",
        "problem_offline": "{node} is niet bereikbaar", "problem_quorum": "het cluster heeft geen quorum",
        "updates_none": "{node}: geen updates beschikbaar",
        "updating": "{node}: {count} {update_word} installeren",
        "updates_done": "{node}: {count} {update_word} geïnstalleerd",
        "updates_failed": "{node}: het installeren van de updates is mislukt; de server is niet herstart",
        "no_reboot": "{node}: geen herstart nodig",
        "reboot_skipped": "{node}: een herstart is nodig, maar deze taak herstart geen servers",
        "nothing_running": "{node}: geen actieve machines, niets te verplaatsen",
        "migrated": "{node}: {count} {machines} zonder onderbreking verplaatst naar {target} ({names})",
        "migrated_ct": "{node}: {count} {machines} met een korte herstart verplaatst naar {target} ({names})",
        "migrate_failed": "{name} kon niet worden verplaatst en wordt in plaats daarvan afgesloten",
        "shutdown": "{node}: {count} {machines} netjes afgesloten ({names})",
        "shutdown_failed": "{node}: {name} kon niet worden afgesloten; de server is niet herstart",
        "rebooting": "{node}: herstarten",
        "back": "{node}: na {minutes} min weer online",
        "not_back": "{node}: niet binnen {minutes} minuten terug; het onderhoud is gestopt. Een beheerder pakt "
                    "het op.",
        "no_quorum_after": "{node}: het cluster heeft na de herstart geen quorum",
        "started": "{node}: {count} {machines} weer gestart in de juiste volgorde ({names})",
        "autostarted": "{node}: {count} {machines} door de server zelf gestart ({names})",
        "start_failed": "{name} is niet opnieuw gestart",
        "migrated_back": "{count} {machines} teruggezet naar {node} ({names})",
        "migrate_back_failed": "{name} kon niet worden teruggezet naar {node} en draait op {target}",
        "skipped_after_problem": "{node}: overgeslagen vanwege het probleem op een eerdere server",
        "node_offline": "{node}: niet bereikbaar, overgeslagen",
        "postcheck_ok": "Nacontrole: alle {nodes} {servers} online en alle {guests} {machines} draaien weer",
        "postcheck_issues": "Nacontrole: {issues}",
        "issue_offline": "{node} is niet bereikbaar", "issue_not_running": "{name} draait niet",
        "issue_quorum": "het cluster heeft geen quorum",
        "done": "Onderhoud afgerond", "done_failed": "Onderhoud afgerond met problemen",
        "machine_1": "machine", "machine_n": "machines",
        "interrupted": "Het onderhoud is onderbroken omdat Stackpilot herstartte",
    },
}

def texts(lang: str) -> dict[str, str]:
    return TEXT.get(lang) or TEXT["en"]


def _word(T: dict[str, str], prefix: str, n: Any) -> str:
    try:
        one = int(n) == 1
    except (TypeError, ValueError):
        one = False
    return T[f"{prefix}_1" if one else f"{prefix}_n"]


def names_text(names: list[str], lang: str) -> str:
    names = [n for n in names if n]
    if len(names) <= MAX_NAMES:
        return ", ".join(names)
    return texts(lang)["and_more"].format(names=", ".join(names[:MAX_NAMES]), n=len(names) - MAX_NAMES)


def describe(entry: dict[str, Any], lang: str) -> str:
    """One timeline entry in plain words."""
    T = texts(lang)
    p = dict(entry.get("params") or {})
    if isinstance(p.get("names"), list):
        p["names"] = names_text(p["names"], lang)
    if isinstance(p.get("issues"), list):
        p["issues"] = "; ".join(describe({"key": i["key"], "params": i.get("params") or {}}, lang)
                                for i in p["issues"])
    if isinstance(p.get("problem"), dict):
        p["problem"] = describe(p["problem"], lang)
    if "nodes" in p:
        p["servers"] = _word(T, "server", p["nodes"])
    if "count" in p:
        p["machines"] = _word(T, "machine", p["count"])
        p["update_word"] = _word(T, "update", p["count"])
    elif "guests" in p:
        p["machines"] = _word(T, "machine", p["guests"])
    p.setdefault("node", entry.get("node") or "")
    key = entry.get("key") or ""
    tpl = T.get(key) or TEXT["en"].get(key) or key
    try:
        return tpl.format(**p)
    except (KeyError, IndexError, ValueError):
        return tpl


# ------------------------------------------------------------------ start-up order
_ORDER = re.compile(r"(?:^|,)\s*order=(\d+)")
_UP = re.compile(r"(?:^|,)\s*up=(\d+)")


def startup_of(cfg: dict[str, Any] | None) -> tuple[int | None, int]:
    """(order, up delay in seconds) from a guest's Proxmox ``startup`` option (None: no order)."""
    s = str((cfg or {}).get("startup") or "")
    m, u = _ORDER.search(s), _UP.search(s)
    return (int(m.group(1)) if m else None), min(int(u.group(1)) if u else 0, MAX_UP_DELAY)


def start_order(guests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order Proxmox starts guests in: by ``order`` (lowest first), guests without one last, then VMID."""
    return sorted(guests, key=lambda g: (g.get("order") is None, g.get("order") or 0, int(g.get("vmid") or 0)))


def stop_order(guests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reverse of the start order: guests without an order first, then the highest order."""
    return list(reversed(start_order(guests)))


# ------------------------------------------------------------------ the machines
class PveOps:
    """The steps on the machines, through the agents. Every call returns plain data and never raises for an
    ordinary failure (it is reported instead)."""

    who = {"source": "schedule"}

    def __init__(self, requested_by: str | None = None) -> None:
        self.requested_by = requested_by

    def _who(self) -> dict[str, Any]:
        return {"source": "schedule", "requested_by": self.requested_by}

    def online(self, node_id: str) -> bool:
        from mmn.services import agent_hub

        return agent_hub.is_online(node_id)

    def connected_at(self, node_id: str) -> datetime | None:
        from mmn.services import agent_hub

        try:
            return agent_hub.get(node_id).connected_at
        except Exception:  # noqa: BLE001
            return None

    async def cluster(self, node_id: str) -> dict[str, Any]:
        from mmn.services import guests

        try:
            rows = await guests.api(node_id, "get", "/cluster/status")
        except Exception as e:  # noqa: BLE001
            return {"quorate": None, "members": [], "error": str(e)}
        quorate, members = None, []
        for r in rows or []:
            if r.get("type") == "cluster":
                quorate = bool(r.get("quorate"))
            elif r.get("type") == "node":
                members.append({"name": r.get("name"), "online": bool(r.get("online"))})
        if quorate is None and len(members) <= 1:
            quorate = True  # a single node without a cluster
        return {"quorate": quorate, "members": members}

    async def guests(self, node_id: str) -> list[dict[str, Any]]:
        """The guests on the node as it reports them now (the stored list when the agent can't be asked)."""
        from mmn.services import agent_hub, guests

        fresh = None
        try:
            fresh = await agent_hub.request(node_id, "guests.list", {}, timeout=45)
        except Exception:  # noqa: BLE001
            fresh = None
        async with sessionmaker()() as db:
            n = await db.get(Node, node_id)
            if n is None:
                return []
            await db.refresh(n)
            if isinstance(fresh, dict) and isinstance(fresh.get("guests"), list):
                rows = [guests._guest_out(n, g) for g in fresh["guests"]]
            else:
                rows = guests.guests_of(n)
            return [g for g in rows if not g["template"]]

    async def config(self, node_id: str, g: dict[str, Any]) -> dict[str, Any]:
        from mmn.services import guests

        try:
            return await guests.api(node_id, "get", f"/nodes/localhost/{g['type']}/{int(g['vmid'])}/config") or {}
        except Exception:  # noqa: BLE001
            return {}

    async def pending(self, node_id: str) -> dict[str, Any]:
        from mmn.services import nodes

        try:
            res = await nodes.refresh_updates(node_id)
        except Exception as e:  # noqa: BLE001
            return {"packages": [], "reboot_required": False, "error": str(e)}
        return {"packages": list(res.get("packages") or []), "reboot_required": bool(res.get("reboot_required"))}

    async def updates(self, node_id: str) -> dict[str, Any]:
        from mmn.services import guests, nodes

        try:
            job = await nodes.apply_updates(node_id, **self._who())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        status, result, err = await guests.run_and_wait(job, 3 * 3600)
        return {"ok": status == "success", "job_id": job.id, "error": err,
                "remaining": int((result or {}).get("remaining") or 0),
                "reboot_required": bool((result or {}).get("reboot_required"))}

    async def _job(self, start, timeout: float) -> dict[str, Any]:
        from mmn.services import guests

        try:
            job = await start()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        status, _, err = await guests.run_and_wait(job, timeout)
        return {"ok": status == "success", "job_id": job.id, "error": err}

    async def migrate(self, node_id: str, g: dict[str, Any], target_id: str) -> dict[str, Any]:
        from mmn.services import guests

        async def start():
            async with sessionmaker()() as db:
                node = await db.get(Node, node_id)
                return await guests.migrate(db, node, g, target_id, online=True, **self._who())

        return await self._job(start, 3600)

    async def power(self, node_id: str, g: dict[str, Any], action: str) -> dict[str, Any]:
        from mmn.services import guests

        async def start():
            async with sessionmaker()() as db:
                node = await db.get(Node, node_id)
            return await guests.power(node, g, action, **self._who())

        return await self._job(start, 900)

    async def reboot(self, node_id: str) -> dict[str, Any]:
        from mmn.services import nodes

        try:
            await nodes.reboot(node_id)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# ------------------------------------------------------------------ the run
def _now() -> datetime:
    return datetime.now(UTC)


class Run:
    """One maintenance run: the timeline, the results per node and storing them as they happen."""

    def __init__(self, run_id: str, ops: Any) -> None:
        self.run_id = run_id
        self.ops = ops
        self.timeline: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []
        self.jobs: list[str] = []
        self.migrate_back = True

    async def add(self, customer_id: str | None, key: str, level: str = "info", *, node: str | None = None,
                  cluster: str | None = None, job_ids: list[str] | None = None, **params: Any) -> dict[str, Any]:
        entry = {"at": _now().isoformat(), "customer_id": customer_id, "cluster": cluster, "node": node,
                 "key": key, "level": level, "params": params}
        if job_ids:
            entry["job_ids"] = [j for j in job_ids if j]
            self.jobs += entry["job_ids"]
        self.timeline.append(entry)
        await self.store()
        return entry

    async def store(self, status: str | None = None, **fields: Any) -> None:
        async with sessionmaker()() as db:
            r = await db.get(TaskRun, self.run_id)
            if r is None:
                return
            r.timeline = list(self.timeline)
            r.results = list(self.results)
            if status:
                r.status = status
            for k, v in fields.items():
                setattr(r, k, v)
            await db.commit()
        events.publish("task.run", {"run_id": self.run_id, "progress": len(self.timeline)})


async def plan_groups(db: AsyncSession, task: ScheduledTask) -> list[tuple[Environment, str, list[Node]]]:
    """The clusters to maintain: (customer, cluster name, Proxmox nodes), per customer by name."""
    from mmn.services.tasks import TargetsIn

    tg = TargetsIn(**(task.targets or {}))
    with tenancy.unrestricted():
        rows = list((await db.execute(select(Node).where(Node.kind == "proxmox").order_by(Node.name))).scalars())
    picked = []
    for n in rows:
        if tg.scope == "environments" and n.environment_id not in tg.environment_ids:
            continue
        if tg.scope == "nodes" and n.id not in tg.node_ids and n.name not in tg.node_ids:
            continue
        picked.append(n)
    groups: dict[tuple[str, str], list[Node]] = {}
    for n in picked:
        groups.setdefault((n.environment_id, n.cluster_name or f"node:{n.id}"), []).append(n)
    envs = {e.id: e for e in (await db.execute(select(Environment))).scalars()}
    out = []
    for (eid, cname), ns in groups.items():
        e = envs.get(eid)
        if e is None:
            continue
        out.append((e, cname if not cname.startswith("node:") else ns[0].name, ns))
    out.sort(key=lambda x: (x[0].name.lower(), x[1].lower()))
    return out


async def _window_state(db: AsyncSession, e: Environment) -> tuple[bool, datetime | None]:
    """(inside the window now, when it closes as UTC) — (True, None) when the customer has no window."""
    from mmn.services import gateway

    windows = gateway.parse_window(e.maintenance_window)
    if not windows:
        return True, None
    local = await gateway.local_now(db)
    if not gateway.in_window(windows, local):
        return False, None
    end = local
    for _ in range(14 * 24 * 12):
        end += timedelta(minutes=5)
        if not gateway.in_window(windows, end):
            break
    return True, end.astimezone(UTC)


async def _needs_approval(db: AsyncSession, e: Environment, mode: str) -> tuple[str, str | None]:
    """allow | approve | block, with the reason for block."""
    from mmn.services import gateway

    if mode == "never":
        return "allow", None
    if mode == "always":
        return "approve", None
    spec = gateway.REGISTRY.get("reboot_node")
    risk = spec.risk if spec else "destructive"
    d = await gateway.decide(db, tool="reboot_node", risk=risk, args={"customer": e.id}, token_id=None,
                             token_customers=None)
    if d.action in ("block", "paused"):
        return "block", "; ".join(gateway.reason_text(r) for r in d.reasons) or d.action
    return ("approve" if d.action == "approve" else "allow"), None


async def _ask_approval(run: Run, e: Environment, task: ScheduledTask, until: datetime | None) -> str:
    """Ask an admin in the Approvals inbox and wait. Returns approved | rejected | expired."""
    from mmn.services import gateway

    hours = 12
    if until is not None:
        hours = max(1, min(24, int((until - _now()).total_seconds() // 3600) + 1))
    async with sessionmaker()() as db:
        a, _ = await gateway.create_system_approval(
            db, source=APPROVAL_SOURCE, label=f"Maintenance: {task.name}", tool=APPROVAL_TOOL,
            args={"run_id": run.run_id, "customer": e.id}, target=e.name, customers=[(e.id, e.name)],
            reason=f"Scheduled maintenance '{task.name}' of the Proxmox servers of {e.name}: updates and, where "
                   "needed, restarts (machines are moved or shut down cleanly and started again).",
            run_after=None, expires_hours=hours)
        await db.commit()
        aid = a.id
    await run.store(status="waiting", approval_id=aid)
    await run.add(e.id, "approval_wait", "info")
    deadline = until or (_now() + timedelta(hours=hours))
    while True:
        async with sessionmaker()() as db:
            a = await db.get(AiApproval, aid)
            st, by = (a.status, a.decided_by) if a else ("expired", None)
        if st in ("approved", "running", "executed"):
            await run.store(status="running")
            await run.add(e.id, "approved", "ok", by=by or "admin")
            return "approved"
        if st in ("rejected", "failed"):
            await run.store(status="running")
            await run.add(e.id, "rejected", "warning")
            return "rejected"
        if st == "expired" or _now() >= deadline:
            break
        await run.ops.sleep(POLL)

    async with sessionmaker()() as db:
        a = await db.get(AiApproval, aid)
        if a is not None and a.status == "pending":
            a.status, a.error = "expired", "the maintenance window closed"
            await db.commit()
    await run.store(status="running")
    await run.add(e.id, "approval_expired", "warning")
    return "expired"


async def _guests_with_order(ops: Any, node_id: str, only_running: bool = True) -> list[dict[str, Any]]:
    out = []
    for g in await ops.guests(node_id):
        if only_running and g.get("status") != "running":
            continue
        cfg = await ops.config(node_id, g)
        order, up = startup_of(cfg)
        out.append({**g, "order": order, "up": up, "onboot": bool(int(cfg.get("onboot") or 0)) if cfg else False})
    return out


async def _wait_back(ops: Any, node_id: str, before: datetime | None, minutes: int) -> float | None:
    """Wait until the node's agent is gone and back again (a new connection). Minutes it took, or None."""
    t0 = _now()
    deadline = t0 + timedelta(minutes=minutes)
    while _now() < deadline:
        await ops.sleep(POLL)
        if ops.online(node_id):
            at = ops.connected_at(node_id)
            if before is None or (at is not None and at > before):
                return max(1.0, (_now() - t0).total_seconds() / 60)
    return None


async def _wait_quorum(ops: Any, node_id: str) -> bool:
    t0 = _now()
    while True:
        st = await ops.cluster(node_id)
        if st.get("quorate"):
            return True
        if (_now() - t0).total_seconds() >= QUORUM_WAIT:
            return False
        await ops.sleep(POLL)


def _pick_target(targets: list[Node], load: dict[str, int]) -> Node:
    return min(targets, key=lambda n: (load.get(n.id, 0), n.name))


async def _node(run: Run, e: Environment, cluster: str, node: Node, members: list[Node], opts: Any,
                stats: dict[str, int]) -> str:
    """One node. Returns ok | failed | stop (failed and the rest of the cluster must not go on)."""
    ops = run.ops
    cid = e.id
    add = run.add
    row: dict[str, Any] = {"target": node.name, "node_id": node.id, "status": "failed"}
    run.results.append(row)
    if not ops.online(node.id):
        await add(cid, "node_offline", "error", node=node.name, cluster=cluster)
        row.update(status="failed", error="node offline")
        return "stop"
    pend = await ops.pending(node.id)
    count = len(pend.get("packages") or [])
    reboot_required = bool(pend.get("reboot_required"))
    if count:
        await add(cid, "updating", "info", node=node.name, cluster=cluster, count=count)
        u = await ops.updates(node.id)
        if not u.get("ok"):
            await add(cid, "updates_failed", "error", node=node.name, cluster=cluster, job_ids=[u.get("job_id")],
                      error=(u.get("error") or "")[:300])
            row.update(error=u.get("error") or "updates failed", job_id=u.get("job_id"))
            return "stop"
        installed = max(0, count - int(u.get("remaining") or 0)) or count
        stats["updates"] += installed
        row["job_id"] = u.get("job_id")
        await add(cid, "updates_done", "ok", node=node.name, cluster=cluster, count=installed,
                  job_ids=[u.get("job_id")])
        reboot_required = bool(u.get("reboot_required"))
    else:
        await add(cid, "updates_none", "ok", node=node.name, cluster=cluster)
    if opts.reboot == "never" or (opts.reboot == "if_required" and not reboot_required):
        if opts.reboot == "never" and reboot_required:
            await add(cid, "reboot_skipped", "warning", node=node.name, cluster=cluster)
            row["note"] = "reboot required"
        else:
            await add(cid, "no_reboot", "ok", node=node.name, cluster=cluster)
        row["status"] = "success"
        stats["nodes"] += 1
        return "ok"

    # Move or shut down the machines that run here.
    running = await _guests_with_order(ops, node.id)
    others = [n for n in members if n.id != node.id and ops.online(n.id)] if len(members) > 1 else []
    migrated: list[tuple[dict[str, Any], Node]] = []
    to_stop: list[dict[str, Any]] = []
    if not running:
        await add(cid, "nothing_running", "info", node=node.name, cluster=cluster)
    load = {n.id: sum(1 for g in await ops.guests(n.id) if g.get("status") == "running") for n in others}
    for g in stop_order(running):
        if opts.guest_handling != "migrate" or not others or g.get("lock"):
            to_stop.append(g)
            continue
        tgt = _pick_target(others, load)
        res = await ops.migrate(node.id, g, tgt.id)
        if res.get("ok"):
            migrated.append((g, tgt))
            load[tgt.id] = load.get(tgt.id, 0) + 1
            if res.get("job_id"):
                run.jobs.append(res["job_id"])
        else:
            await add(cid, "migrate_failed", "warning", node=node.name, cluster=cluster, name=g.get("name") or
                      str(g.get("vmid")), job_ids=[res.get("job_id")], error=(res.get("error") or "")[:300])
            to_stop.append(g)
    by_target: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for g, tgt in migrated:
        by_target.setdefault((tgt.name, g.get("type") == "lxc"), []).append(g)
    for (tname, ct), gs in by_target.items():
        # VMs move live; containers restart on the other node.
        await add(cid, "migrated_ct" if ct else "migrated", "ok", node=node.name, cluster=cluster, target=tname,
                  count=len(gs), names=[x.get("name") or str(x.get("vmid")) for x in gs])
    stopped: list[dict[str, Any]] = []
    jobs: list[str] = []
    for g in stop_order(to_stop):
        res = await ops.power(node.id, g, "shutdown")
        jobs.append(res.get("job_id"))
        if not res.get("ok"):
            if stopped:
                await add(cid, "shutdown", "ok", node=node.name, cluster=cluster, count=len(stopped),
                          names=[x.get("name") or str(x.get("vmid")) for x in stopped], job_ids=jobs)
            await add(cid, "shutdown_failed", "error", node=node.name, cluster=cluster,
                      name=g.get("name") or str(g.get("vmid")), error=(res.get("error") or "")[:300])
            # Put back what was shut down already; the node is not restarted.
            await _start(run, e, cluster, node, stopped, onboot_started=[])
            await _move_back(run, e, cluster, node, migrated)
            row.update(error=f"could not shut down {g.get('name') or g.get('vmid')}")
            return "stop"
        stopped.append(g)
    if stopped:
        await add(cid, "shutdown", "ok", node=node.name, cluster=cluster, count=len(stopped),
                  names=[x.get("name") or str(x.get("vmid")) for x in stopped], job_ids=jobs)

    # Restart the node.
    before = ops.connected_at(node.id)
    await add(cid, "rebooting", "info", node=node.name, cluster=cluster)
    rb = await ops.reboot(node.id)
    if not rb.get("ok"):
        await add(cid, "not_back", "error", node=node.name, cluster=cluster, minutes=0,
                  error=(rb.get("error") or "")[:300])
        row.update(error=rb.get("error") or "reboot failed")
        await _start(run, e, cluster, node, stopped, onboot_started=[])
        await _move_back(run, e, cluster, node, migrated)
        return "stop"
    took = await _wait_back(ops, node.id, before, int(opts.boot_timeout))
    if took is None:
        await add(cid, "not_back", "error", node=node.name, cluster=cluster, minutes=int(opts.boot_timeout))
        row.update(error="node did not come back")
        return "stop"
    stats["restarts"] += 1
    await add(cid, "back", "ok", node=node.name, cluster=cluster, minutes=int(round(took)))
    if len(members) > 1 and not await _wait_quorum(ops, node.id):
        await add(cid, "no_quorum_after", "error", node=node.name, cluster=cluster)
        row.update(error="no quorum after the restart")
        return "stop"

    # Start what was shut down (Proxmox may have started the onboot ones itself), then move the rest back.
    await ops.sleep(SETTLE)
    now_running = {int(g.get("vmid") or 0) for g in await ops.guests(node.id) if g.get("status") == "running"}
    auto = [g for g in stopped if int(g.get("vmid") or 0) in now_running]
    rest = [g for g in stopped if int(g.get("vmid") or 0) not in now_running]
    ok_start = await _start(run, e, cluster, node, rest, onboot_started=auto)
    ok_back = await _move_back(run, e, cluster, node, migrated)
    row["status"] = "success" if ok_start and ok_back else "failed"
    if not (ok_start and ok_back):
        row["error"] = "not every machine is back where it was"
    stats["nodes"] += 1
    return "ok" if row["status"] == "success" else "failed"


async def _start(run: Run, e: Environment, cluster: str, node: Node, guests: list[dict[str, Any]], *,
                 onboot_started: list[dict[str, Any]]) -> bool:
    ops = run.ops
    if onboot_started:
        await run.add(e.id, "autostarted", "ok", node=node.name, cluster=cluster, count=len(onboot_started),
                      names=[g.get("name") or str(g.get("vmid")) for g in start_order(onboot_started)])
    started: list[dict[str, Any]] = []
    jobs: list[str] = []
    ok = True
    ordered = start_order(guests)
    for i, g in enumerate(ordered):
        res = await ops.power(node.id, g, "start")
        jobs.append(res.get("job_id"))
        if res.get("ok"):
            started.append(g)
            if g.get("up") and i < len(ordered) - 1:
                await ops.sleep(int(g["up"]))
        else:
            ok = False
            await run.add(e.id, "start_failed", "error", node=node.name, cluster=cluster,
                          name=g.get("name") or str(g.get("vmid")), error=(res.get("error") or "")[:300])
    if started:
        await run.add(e.id, "started", "ok", node=node.name, cluster=cluster, count=len(started),
                      names=[g.get("name") or str(g.get("vmid")) for g in started], job_ids=jobs)
    return ok


async def _move_back(run: Run, e: Environment, cluster: str, node: Node,
                     migrated: list[tuple[dict[str, Any], Node]]) -> bool:
    ops = run.ops
    if not migrated or not run.migrate_back:
        return True
    back: list[dict[str, Any]] = []
    ok = True
    for g, tgt in reversed(migrated):
        here = next((x for x in await ops.guests(tgt.id) if int(x.get("vmid") or 0) == int(g.get("vmid") or 0)),
                    None)
        res = await ops.migrate(tgt.id, here, node.id) if here else {"ok": False, "error": "not found"}
        if res.get("ok"):
            back.append(g)
            if res.get("job_id"):
                run.jobs.append(res["job_id"])
        else:
            ok = False
            await run.add(e.id, "migrate_back_failed", "warning", node=node.name, cluster=cluster,
                          name=g.get("name") or str(g.get("vmid")), target=tgt.name,
                          error=(res.get("error") or "")[:300])
    if back:
        await run.add(e.id, "migrated_back", "ok", node=node.name, cluster=cluster, count=len(back),
                      names=[g.get("name") or str(g.get("vmid")) for g in back])
    return ok


async def _precheck(run: Run, e: Environment, cluster: str, members: list[Node]) -> dict[str, Any] | None:
    """The state before: None when something is wrong (and on the timeline)."""
    ops = run.ops
    for n in members:
        if not ops.online(n.id):
            await run.add(e.id, "precheck_failed", "error", cluster=cluster,
                          problem={"key": "problem_offline", "params": {"node": n.name}})
            return None
    st = await ops.cluster(members[0].id)
    if len(members) > 1 and not st.get("quorate"):
        await run.add(e.id, "precheck_failed", "error", cluster=cluster, problem={"key": "problem_quorum"})
        return None
    running: dict[int, str] = {}
    for n in members:
        for g in await ops.guests(n.id):
            if g.get("status") == "running":
                running[int(g.get("vmid") or 0)] = g.get("name") or str(g.get("vmid"))
    await run.add(e.id, "precheck_ok", "ok", cluster=cluster, nodes=len(members), guests=len(running))
    return {"running": running}


async def _postcheck(run: Run, e: Environment, cluster: str, members: list[Node], before: dict[str, Any]) -> bool:
    ops = run.ops
    issues: list[dict[str, Any]] = []
    for n in members:
        if not ops.online(n.id):
            issues.append({"key": "issue_offline", "params": {"node": n.name}})
    if len(members) > 1 and not issues:
        st = await ops.cluster(members[0].id)
        if not st.get("quorate"):
            issues.append({"key": "issue_quorum"})
    now_running: set[int] = set()
    for n in members:
        if ops.online(n.id):
            now_running |= {int(g.get("vmid") or 0) for g in await ops.guests(n.id) if g.get("status") == "running"}
    for vmid, name in sorted(before["running"].items()):
        if vmid not in now_running:
            issues.append({"key": "issue_not_running", "params": {"name": name}})
    if issues:
        await run.add(e.id, "postcheck_issues", "warning", cluster=cluster, issues=issues)
        return False
    await run.add(e.id, "postcheck_ok", "ok", cluster=cluster, nodes=len(members), guests=len(before["running"]))
    return True


async def _customer(run: Run, task: ScheduledTask, e: Environment, clusters: list[tuple[str, list[Node]]],
                    opts: Any, trigger: str, requested_by: str | None) -> dict[str, Any]:
    """Every cluster of one customer. Returns {"state": ok|warning|failed|skipped, "stats", "change_id"}."""
    from mmn.services import changes

    stats = {"nodes": 0, "updates": 0, "restarts": 0}
    all_nodes = [n for _, ns in clusters for n in ns]

    def skip_all(why: str) -> None:
        for n in all_nodes:
            run.results.append({"target": n.name, "node_id": n.id, "status": "skipped", "error": why})

    scheduled = trigger in ("schedule", "mcp")
    async with sessionmaker()() as db:
        inside, closes = await _window_state(db, e)
        if opts.respect_window and scheduled and not inside:
            await run.add(e.id, "outside_window", "warning", window=e.maintenance_window)
            skip_all("outside the maintenance window")
            return {"state": "skipped", "stats": stats, "change_id": None, "silent": True}
        decision, why = await _needs_approval(db, e, opts.approval) if scheduled else ("allow", None)
    if decision == "block":
        await run.add(e.id, "blocked", "warning", reason=why or "")
        skip_all(why or "blocked by the policy")
        return {"state": "skipped", "stats": stats, "change_id": None, "silent": True}
    if decision == "approve":
        got = await _ask_approval(run, e, task, closes)
        if got != "approved":
            skip_all("not approved")
            return {"state": "skipped", "stats": stats, "change_id": None, "silent": got == "rejected"}

    lang = _lang(e)
    T = texts(lang)
    async with sessionmaker()() as db:
        ch = await changes.record_work(
            db, customer_id=e.id, title=T["change_title"], description=T["change_desc"], kind="update",
            impact="minor" if opts.guest_handling == "migrate" else "degraded", origin="schedule", origin_ref=task.name,
            node_ids=[n.id for n in all_nodes], task_run_id=run.run_id, task_id=task.id,
            by=requested_by or f"task:{task.name}", minutes=60 * max(1, len(all_nodes)))
        await db.commit()
        change_id = ch.id

    state = "ok"
    for cname, members in clusters:
        await run.add(e.id, "start", "info", cluster=cname)
        before = await _precheck(run, e, cname, members)
        if before is None:
            for n in members:
                run.results.append({"target": n.name, "node_id": n.id, "status": "skipped",
                                    "error": "checks before found a problem"})
            state = "failed"
            continue
        stopped = False
        for n in members:
            if stopped:
                await run.add(e.id, "skipped_after_problem", "warning", node=n.name, cluster=cname)
                run.results.append({"target": n.name, "node_id": n.id, "status": "skipped",
                                    "error": "stopped after a problem on an earlier node"})
                continue
            try:
                res = await _node(run, e, cname, n, members, opts, stats)
            except Exception as ex:  # noqa: BLE001
                log.exception("maintenance of %s", n.name)
                await run.add(e.id, "not_back", "error", node=n.name, cluster=cname, minutes=0,
                              error=f"{type(ex).__name__}: {ex}"[:300])
                res = "stop"
            if res == "stop":
                stopped, state = True, "failed"
            elif res == "failed" and state == "ok":
                state = "warning"
        ok_after = await _postcheck(run, e, cname, members, before)
        if not ok_after and state == "ok":
            state = "warning"
    await run.add(e.id, "done" if state in ("ok", "warning") else "done_failed",
                  "ok" if state == "ok" else "warning" if state == "warning" else "error")
    return {"state": state, "stats": stats, "change_id": change_id}


def _lang(e: Environment) -> str:
    lang = (e.report or {}).get("lang") or "en"
    return lang if lang in TEXT else "en"


# ------------------------------------------------------------------ report
async def tz_of(db: AsyncSession) -> ZoneInfo:
    name = (await get_setting(db, "schedules")).get("timezone") or "Europe/Amsterdam"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def report_settings(e: Environment) -> dict[str, Any]:
    r = e.report or {}
    return {"enabled": r.get("maintenance_enabled", True) is not False,
            "recipients": list(r.get("maintenance_recipients") or []),
            "on": r.get("maintenance_on") if r.get("maintenance_on") in ("always", "issues") else "always"}


def report_recipients(e: Environment) -> list[str]:
    """The maintenance report recipients of a customer: its own list, else the monthly report's, else the
    contact address."""
    from mmn.services import reports

    s = report_settings(e)
    out = s["recipients"] or reports.recipients_of(e)
    return [x for x in dict.fromkeys(str(a).strip().lower() for a in out) if x and "@" in x][:20]


def build_report(run: TaskRun, e: Environment, lang: str, tz: ZoneInfo, state: str | None = None,
                 stats: dict[str, int] | None = None) -> dict[str, Any]:
    """The report of one customer from the run's timeline, in ``lang``."""
    T = texts(lang)
    entries = [x for x in run.timeline or [] if x.get("customer_id") == e.id]
    if state is None:
        saved = next((r for r in run.reports or [] if r.get("customer_id") == e.id), {})
        state = saved.get("state") or ("failed" if any(x.get("level") == "error" for x in entries) else
                                       "warning" if any(x.get("level") == "warning" for x in entries) else "ok")
        stats = stats or saved.get("stats")
    stats = stats or {"nodes": 0, "updates": 0, "restarts": 0}
    lines = []
    for x in entries:
        try:
            at = datetime.fromisoformat(x["at"]).astimezone(tz)
        except (KeyError, ValueError):
            continue
        lines.append({"at": x["at"], "time": f"{at:%H:%M}", "text": describe(x, lang), "level": x.get("level")})
    started = run.started_at.astimezone(tz) if run.started_at else None
    summary = T["summary"].format(nodes=stats["nodes"], servers=_word(T, "server", stats["nodes"]),
                                  updates=stats["updates"], update_word=_word(T, "update", stats["updates"]),
                                  restarts=stats["restarts"], restart_word=_word(T, "restart", stats["restarts"]))
    return {"customer_id": e.id, "customer_name": e.name, "lang": lang, "state": state,
            "title": T["title"], "subject": T[f"subject_{state}"].format(customer=e.name),
            "intro": T[f"intro_{state}"], "summary": summary if state != "skipped" else None,
            "timeline_label": T["timeline"], "lines": lines, "timezone": str(tz),
            "date": started.date().isoformat() if started else None, "stats": stats,
            "footer": T["footer"].format(tz=str(tz))}


async def send_report(db: AsyncSession, run: TaskRun, e: Environment, state: str, stats: dict[str, int],
                      to: list[str] | None = None) -> dict[str, Any]:
    """Mail the report of one customer. Never raises; returns what happened."""
    from mmn.services.links import public_base_url
    from mmn.services.mailer import MailContent, try_send_mail

    lang = _lang(e)
    tz = await tz_of(db)
    rep = build_report(run, e, lang, tz, state, stats)
    recipients = to if to is not None else report_recipients(e)
    out: dict[str, Any] = {"customer_id": e.id, "at": _now().isoformat(), "state": state, "stats": stats,
                           "lang": lang, "to": recipients, "ok": False}
    if not (await get_setting(db, "mail")).get("enabled"):
        out["error"] = "mail_off"
        return out
    if not recipients:
        out["error"] = "no_recipients"
        return out
    base = await public_base_url(db)
    portal = await get_setting(db, "portal")
    url = f"{base}/portal/changes" if base and portal.get("enabled", True) else None
    T = texts(lang)
    paras = [rep["intro"]] + ([rep["summary"]] if rep["summary"] else []) + [rep["timeline_label"]]
    content = MailContent(subject=rep["subject"], title=rep["title"], paragraphs=paras, muted=[rep["footer"]],
                          button_url=url, button_label=T["button"] if url else None,
                          timeline=[{"time": x["time"], "text": x["text"], "level": x["level"] or "info"}
                                    for x in rep["lines"]])
    try:
        ok = await try_send_mail(db, recipients, content, lang)
    except Exception as ex:  # noqa: BLE001
        log.exception("maintenance report")
        ok, out["error"] = False, str(ex)[:300]
    out["ok"] = bool(ok)
    if not ok and "error" not in out:
        out["error"] = "send_failed"
    return out


# ------------------------------------------------------------------ entry points
async def run_maintenance(run_id: str, task_id: str, *, trigger: str = "schedule", requested_by: str | None = None,
                          ops: Any = None) -> None:
    """Carry out a maintenance run (started by tasks.run_task). Finishes the TaskRun itself."""
    from mmn.services import changes, tasks

    run = Run(run_id, ops)
    reports: list[dict[str, Any]] = []
    try:
        async with sessionmaker()() as db:
            task = await tasks.get_task(db, task_id)
            opts = tasks.OptionsIn(**(task.options or {}))
            groups = await plan_groups(db, task)
        # The jobs on the machines carry the task's name, like those of the other scheduled tasks.
        run.ops = ops or PveOps(f"task:{task.name}")
        run.migrate_back = bool(opts.migrate_back)
        per_customer: dict[str, tuple[Environment, list[tuple[str, list[Node]]]]] = {}
        for e, cname, ns in groups:
            per_customer.setdefault(e.id, (e, []))[1].append((cname, ns))
        await run.store(customer_ids=list(per_customer))
        if not groups:
            run.results.append({"target": "-", "status": "skipped", "error": "no Proxmox nodes match the targets"})
        for cid, (e, clusters) in per_customer.items():
            out = await _customer(run, task, e, clusters, opts, trigger, requested_by)
            await run.store()
            async with sessionmaker()() as db:
                r = await db.get(TaskRun, run_id)
                ee = await db.get(Environment, cid)
                rec: dict[str, Any] = {"customer_id": cid, "state": out["state"], "stats": out["stats"]}
                if out.get("change_id"):
                    ch = await db.get(Change, out["change_id"])
                    rec["change_id"] = out["change_id"]
                else:
                    ch = None
                rs = report_settings(ee)
                want = (opts.report and rs["enabled"] and not out.get("silent")
                        and (rs["on"] == "always" or out["state"] != "ok"))
                if want:
                    rec.update(await send_report(db, r, ee, out["state"], out["stats"]))
                    rec["state"], rec["stats"] = out["state"], out["stats"]
                if ch is not None:
                    tz = await tz_of(db)
                    rep = build_report(r, ee, _lang(ee), tz, out["state"], out["stats"])
                    note = " ".join(x for x in (rep["intro"], rep["summary"]) if x)
                    await changes.finish_work(db, ch, ok=out["state"] in ("ok", "warning"), note=note,
                                              report_sent=bool(rec.get("ok")))
                await db.commit()
            reports.append(rec)
            await run.store(reports=list(reports))
    except Exception as ex:  # noqa: BLE001
        log.exception("maintenance run %s failed", run_id)
        run.results.append({"target": "task", "status": "failed", "error": f"{type(ex).__name__}: {ex}"})
    await run.store(reports=list(reports))
    await tasks.finish_run(run_id, run.results)


async def recover() -> int:
    """At start-up: maintenance runs that were busy when Stackpilot stopped are marked failed (a run is a
    process in the app; it can't be picked up halfway, the machines may be in any state)."""
    n = 0
    async with sessionmaker()() as db:
        rows = list((await db.execute(select(TaskRun).where(TaskRun.kind == KIND,
                                                            TaskRun.status.in_(("running", "waiting"))))).scalars())
        for r in rows:
            r.timeline = [*(r.timeline or []), *({"at": _now().isoformat(), "customer_id": c, "key": "interrupted",
                                                  "level": "error", "params": {}} for c in r.customer_ids or [])]
            r.status, r.finished_at = "failed", _now()
            n += 1
            chs = (await db.execute(select(Change).where(Change.task_run_id == r.id,
                                                         Change.status == "in_progress"))).scalars()
            from mmn.services import changes

            for ch in chs:
                await changes.set_status(db, ch, "failed", by="system", note=texts(_lang(ch.customer))["interrupted"])
        await db.commit()
    return n


def run_view(r: TaskRun, envs: dict[str, Environment], lang: str, tz: ZoneInfo) -> dict[str, Any]:
    """The timeline of a run for the app: per customer, in the language of the viewer."""
    out = []
    for cid in r.customer_ids or []:
        e = envs.get(cid)
        if e is None:
            continue
        rep = build_report(r, e, lang, tz)
        saved = next((x for x in r.reports or [] if x.get("customer_id") == cid), {})
        out.append({**rep, "sent_to": saved.get("to") if saved.get("ok") else [], "sent_at": saved.get("at")
                    if saved.get("ok") else None, "send_error": saved.get("error"),
                    "change_id": saved.get("change_id")})
    return {"customers": out, "approval_id": r.approval_id}


async def task_report(db: AsyncSession, r: TaskRun, e: Environment, lang: str, tz: ZoneInfo) -> dict[str, Any]:
    """A short report of another scheduled task run (updates in containers, a command) for one customer: one
    line per machine, in the same shape as :func:`build_report`."""
    from mmn.i18n import t

    T = texts(lang)
    with tenancy.unrestricted():
        mine = {n.id for n in (await db.execute(select(Node).where(Node.environment_id == e.id))).scalars()}
    rows = [x for x in r.results or [] if x.get("node_id") in mine and x.get("status") in ("success", "failed")]
    at = (r.finished_at or r.started_at).astimezone(tz)
    ok = sum(1 for x in rows if x["status"] == "success")
    lines = [{"at": at.isoformat(), "time": f"{at:%H:%M}", "level": "ok" if x["status"] == "success" else "error",
              "text": f"{x.get('target')}: {T['done'] if x['status'] == 'success' else T['done_failed']}"}
             for x in rows]
    state = "ok" if ok == len(rows) else "failed" if not ok else "warning"
    return {"customer_id": e.id, "customer_name": e.name, "lang": lang, "state": state,
            "title": t(lang, f"task.change_{r.kind}"), "intro": t(lang, "task.change_note", ok=ok,
                                                                  failed=len(rows) - ok),
            "summary": None, "timeline_label": T["timeline"], "lines": lines, "timezone": str(tz),
            "date": at.date().isoformat(), "stats": {}}
