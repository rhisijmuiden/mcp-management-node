"""Network adapters for automatic recovery (phase 15).

A network card or uplink that stays down after a switch reboot (seen with Intel X710 ports on a
Hyper-V host) is a known problem with a known fix: restart the adapter. This module

- lists the physical wired adapters of a machine with their link state (``Get-NetAdapter`` on
  Windows, ``/sys/class/net`` on Linux) through the agent's ``exec``, so it works with every agent
  version;
- turns an adapter that was up before and is now down into a monitoring alert of kind
  ``network`` (only on machines that an enabled network rule watches);
- restarts an adapter (``Restart-NetAdapter`` or ``ip link set down/up``) and reports the state
  after; it refuses an adapter that is up or switched off on purpose unless asked explicitly;
- pushes a small watchdog configuration to agents of v0.15.0 and later for rules with the local
  fallback: when the agent cannot reach Stackpilot and a watched adapter stays down, the agent
  restarts the adapter itself (cooldown and a maximum per day), and reports what it did once it is
  back. Those repairs are recorded as recovery attempts with source ``local``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Node, RemediationAttempt, RemediationRule
from mmn.services import agent_hub, events, tenancy
from mmn.services.settings_store import get_setting, set_setting

log = logging.getLogger("mmn.netwatch")

# Adapter names and patterns: Windows names ("Ethernet 2", "SLOT 3 Port 1"), descriptions
# ("Intel(R) Ethernet Controller X710*") and Linux interfaces (eno1, enp65s0f0np0).
NAME_RE = re.compile(r"^[A-Za-z0-9 _.:()#/+@\-]{1,100}$")
PATTERN_RE = re.compile(r"^[A-Za-z0-9 _.:()#/+@*?\-]{1,120}$")
SEEN_DAYS = 30  # an adapter that was up in the last 30 days is expected to be up
CAPABILITY = "netwatch"  # agents with the local watchdog (v0.15.0+)
STATE_KEY = "netwatch"


class NetwatchError(Exception):
    pass


# ------------------------------------------------------------------ scripts and parsing
def list_script(kind: str) -> str:
    """One NIC line per physical adapter: name, description, state (up/down/disabled/...), speed,
    MAC, wireless (1/0)."""
    if kind == "windows":
        return ("$ErrorActionPreference = 'SilentlyContinue'\n"
                "Get-NetAdapter -Physical | ForEach-Object { "
                "$w = if ($_.NdisPhysicalMedium -eq 9 -or $_.InterfaceDescription -match 'Wi-?Fi|Wireless|802\\.11') "
                "{ 1 } else { 0 }; "
                "\"NIC`t$($_.Name)`t$($_.InterfaceDescription)`t$($_.Status)`t$($_.LinkSpeed)`t$($_.MacAddress)`t$w\" }")
    return ("for d in /sys/class/net/*; do n=${d##*/}; [ -e \"$d/device\" ] || continue; "
            "drv=$(basename \"$(readlink \"$d/device/driver\" 2>/dev/null)\" 2>/dev/null); "
            "fl=$(cat \"$d/flags\" 2>/dev/null); car=$(cat \"$d/carrier\" 2>/dev/null); "
            "sp=$(cat \"$d/speed\" 2>/dev/null); mac=$(cat \"$d/address\" 2>/dev/null); "
            "w=0; [ -d \"$d/wireless\" ] && w=1; "
            "if [ $(( ${fl:-0} & 1 )) -eq 0 ]; then st=disabled; elif [ \"$car\" = 1 ]; then st=up; else st=down; fi; "
            "printf 'NIC\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \"$n\" \"$drv\" \"$st\" \"${sp:-}\" \"$mac\" \"$w\"; "
            "done; exit 0")


def _state(raw: str) -> str:
    s = (raw or "").strip().lower()
    return {"up": "up", "disconnected": "down", "down": "down", "disabled": "disabled",
            "not present": "absent", "lowerlayerdown": "down"}.get(s, s or "unknown")


def _speed(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.lstrip("-").isdigit():
        n = int(raw)
        return "" if n <= 0 else f"{n // 1000} Gbps" if n >= 1000 and n % 1000 == 0 else f"{n} Mbps"
    return raw


def parse_adapters(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in (stdout or "").splitlines():
        p = line.rstrip("\r").split("\t")
        if len(p) < 4 or p[0] != "NIC" or not p[1]:
            continue
        p += [""] * (7 - len(p))
        out.append({"name": p[1], "description": p[2], "status": _state(p[3]), "speed": _speed(p[4]),
                    "mac": p[5], "wireless": p[6].strip() == "1"})
    return out


def restart_script(kind: str, name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise NetwatchError(f"adapter '{name}': use letters, digits, spaces and _ . : ( ) # / + @ -")
    if kind == "windows":
        q = "'" + name.replace("'", "''") + "'"
        return ("$ErrorActionPreference = 'Stop'\n"
                f"Restart-NetAdapter -Name {q} -Confirm:$false\n"
                f"for ($i = 0; $i -lt 20; $i++) {{ Start-Sleep -Seconds 3; "
                f"if ((Get-NetAdapter -Name {q}).Status -eq 'Up') {{ break }} }}\n"
                f"$a = Get-NetAdapter -Name {q}; \"NIC`t$($a.Name)`t$($a.InterfaceDescription)`t$($a.Status)`t"
                "$($a.LinkSpeed)`t$($a.MacAddress)`t0\"")
    q = shlex.quote(name)
    return (f"set -e; ip link set dev {q} down; sleep 2; ip link set dev {q} up; "
            f"for i in $(seq 1 20); do sleep 3; [ \"$(cat /sys/class/net/{q}/carrier 2>/dev/null)\" = 1 ] && break; done; "
            f"d=/sys/class/net/{q}; car=$(cat \"$d/carrier\" 2>/dev/null); st=down; [ \"$car\" = 1 ] && st=up; "
            "printf 'NIC\\t%s\\t%s\\t%s\\t%s\\t%s\\t0\\n' "
            f"{q} \"$(basename \"$(readlink \"$d/device/driver\")\")\" \"$st\" \"$(cat \"$d/speed\" 2>/dev/null)\" "
            "\"$(cat \"$d/address\")\"")


def glob_any(patterns: list[str], adapter: dict[str, Any]) -> bool:
    """An empty pattern list matches every adapter; patterns match the name or the description."""
    if not patterns:
        return True
    name, desc = (adapter.get("name") or "").lower(), (adapter.get("description") or "").lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) or (desc and fnmatch.fnmatchcase(desc, p.lower()))
               for p in patterns)


def clean_patterns(values: list[Any] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        v = (v if isinstance(v, str) else str(v)).strip()
        if not v:
            continue
        if not PATTERN_RE.match(v):
            raise NetwatchError(f"adapter '{v}': use letters, digits, spaces, * ? and _ . : ( ) # / + @ -")
        if v not in out:
            out.append(v)
    return out[:30]


# ------------------------------------------------------------------ reading and restarting
async def _exec(node_id: str, command: str, timeout: int = 60) -> dict[str, Any]:
    return await agent_hub.request(node_id, "exec", {"command": command, "timeout": timeout},
                                   timeout=timeout + 30) or {}


async def adapters(node_ref: str) -> dict[str, Any]:
    """The physical network adapters of a machine with their link state."""
    from mmn.services import nodes
    from mmn.services.nodes import NodeError

    async with sessionmaker()() as db:
        n = await nodes.get_node(db, node_ref)
        nid, kind, name = n.id, n.kind, n.name
    if not agent_hub.is_online(nid):
        raise NodeError(f"the agent of {name} is offline")
    res = await _exec(nid, list_script(kind))
    return {"machine": name, "kind": kind, "adapters": parse_adapters(res.get("stdout") or "")}


async def restart_adapter(node_ref: str, adapter: str, *, force: bool = False,
                          requested_by: str | None = None, source: str = "mcp") -> dict[str, Any]:
    """Restart one network adapter. Without ``force`` only an adapter that is down (no link) is
    restarted: one that is up, switched off or not present is left alone."""
    from mmn.models import AuditLog
    from mmn.services import nodes
    from mmn.services.nodes import NodeError

    adapter = (adapter or "").strip()
    if not NAME_RE.match(adapter):
        raise NodeError("give the exact name of the network adapter (see list_network_adapters)")
    async with sessionmaker()() as db:
        n = await nodes.get_node(db, node_ref)
        nid, kind, name = n.id, n.kind, n.name
    if not agent_hub.is_online(nid):
        raise NodeError(f"the agent of {name} is offline; a machine without any link can only repair itself "
                        "(turn on the local fallback of the network rule)")
    before = parse_adapters((await _exec(nid, list_script(kind))).get("stdout") or "")
    hit = next((a for a in before if a["name"].lower() == adapter.lower()), None)
    if hit is None:
        names = ", ".join(a["name"] for a in before) or "none"
        raise NodeError(f"{name} has no physical network adapter '{adapter}' (adapters: {names})")
    if not force:
        if hit["status"] == "up":
            return {"machine": name, "adapter": hit["name"], "status": "up", "restarted": False,
                    "message": "the adapter is up; nothing to do"}
        if hit["status"] in ("disabled", "absent"):
            raise NodeError(f"adapter {hit['name']} is {hit['status']} (switched off on purpose?); "
                            "restart it only with force")
    try:
        res = await _exec(nid, restart_script(kind, hit["name"]), timeout=150)
    except NetwatchError as e:
        raise NodeError(str(e)) from e
    after = parse_adapters(res.get("stdout") or "")
    now_state = after[0]["status"] if after else "unknown"
    code = res.get("exit_code", res.get("code"))
    out = {"machine": name, "adapter": hit["name"], "description": hit["description"], "before": hit["status"],
           "after": now_state, "restarted": code in (0, None), "fixed": now_state == "up",
           "speed": after[0]["speed"] if after else "",
           "output": ((res.get("stdout") or "") + (res.get("stderr") or ""))[-2000:]}
    if code not in (0, None):
        out["message"] = f"restarting the adapter failed (exit code {code})"
    elif now_state != "up":
        out["message"] = "the adapter was restarted but still has no link: check the cable, the switch port or SFP"
    else:
        out["message"] = "the adapter was restarted and is up again"
    async with sessionmaker()() as db:
        db.add(AuditLog(action="network.adapter_restart", username=requested_by, source=source, target=name,
                        success=out["fixed"], detail={"adapter": hit["name"], "before": hit["status"],
                                                      "after": now_state, "force": force}))
        await db.commit()
    return out


# ------------------------------------------------------------------ watched adapters (monitoring)
# (node id, adapter name) -> {"status", "description", "speed", "at", "expected"}
_nic_state: dict[tuple[str, str], dict[str, Any]] = {}
_last_probe: datetime | None = None


async def network_rules(db: AsyncSession) -> list[RemediationRule]:
    from mmn.services import remediation

    return [r for r in await remediation.enabled_rules(db, "alert")
            if "network" in ((r.match or {}).get("kinds") or [])]


async def watched(db: AsyncSession) -> dict[str, list[RemediationRule]]:
    """{node id: the network rules that apply to it}."""
    from mmn.services import remediation
    from mmn.services.customers import machine_type

    rules = await network_rules(db)
    out: dict[str, list[RemediationRule]] = {}
    if not rules:
        return out
    with tenancy.unrestricted():
        ns = list((await db.execute(select(Node))).scalars())
    for r in rules:
        for n in ns:
            if machine_type(n) == "workstation" and "workstation" not in ((r.match or {}).get("machine_types") or []):
                continue
            if remediation._customer_ok(r, n.environment_id) and remediation._machine_ok(r, n):  # noqa: SLF001
                out.setdefault(n.id, []).append(r)
    return out


def _wanted(rules: list[RemediationRule], a: dict[str, Any]) -> bool:
    return not a.get("wireless") and any(glob_any((r.match or {}).get("adapters") or [], a) for r in rules)


def _explicit(rules: list[RemediationRule], a: dict[str, Any]) -> bool:
    """A rule names this adapter exactly (no wildcard): it is expected up even if never seen up."""
    for r in rules:
        for p in (r.match or {}).get("adapters") or []:
            if "*" not in p and "?" not in p and p.lower() in ((a.get("name") or "").lower(),
                                                                (a.get("description") or "").lower()):
                return True
    return False


async def probe(force: bool = False) -> int:
    """Check the adapters of the watched machines (every few minutes), keep the watchdog
    configuration of the agents in step and collect what the agents repaired themselves."""
    global _last_probe
    from mmn.services import remediation

    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        cfg = await remediation.settings(db)
        every = timedelta(minutes=int(cfg.get("service_check_minutes") or 5))
        if not force and _last_probe is not None and now - _last_probe < every - timedelta(seconds=5):
            return 0
        _last_probe = now
        want = await watched(db)
        state = await get_setting(db, STATE_KEY)
        with tenancy.unrestricted():
            ids = set(want) | set(state.get("applied") or {})
            info = {n.id: (n.kind, list(n.capabilities or []), n.name)
                    for n in (await db.execute(select(Node).where(Node.id.in_(list(ids))))).scalars()} if ids else {}
    for k in [k for k in _nic_state if k[0] not in want]:
        _nic_state.pop(k, None)
    seen: dict[str, dict[str, str]] = {k: dict(v) for k, v in (state.get("seen_up") or {}).items()}
    changed = False
    checked = 0
    for nid, rules in want.items():
        if nid not in info or not agent_hub.is_online(nid):
            continue
        kind = info[nid][0]
        try:
            res = await _exec(nid, list_script(kind))
        except Exception as e:  # noqa: BLE001 - one machine must not stop the others
            log.info("network check on %s: %s", nid, e)
            continue
        present = set()
        for a in parse_adapters(res.get("stdout") or ""):
            if not _wanted(rules, a):
                continue
            present.add(a["name"])
            node_seen = seen.setdefault(nid, {})
            if a["status"] == "up":
                last = node_seen.get(a["name"])
                if not last or now - datetime.fromisoformat(last) > timedelta(hours=1):
                    node_seen[a["name"]] = now.isoformat()
                    changed = True
            last = node_seen.get(a["name"])
            expected = _explicit(rules, a) or bool(last and now - datetime.fromisoformat(last)
                                                   < timedelta(days=SEEN_DAYS))
            _nic_state[(nid, a["name"])] = {**a, "at": now, "expected": expected}
            checked += 1
        for k in [k for k in _nic_state if k[0] == nid and k[1] not in present]:
            _nic_state.pop(k, None)
    if changed:
        async with sessionmaker()() as db:
            st = await get_setting(db, STATE_KEY)
            st["seen_up"] = seen
            await set_setting(db, STATE_KEY, st)
            await db.commit()
    await sync_agents(want, info)
    return checked


async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    """Monitoring: an alert for every watched adapter that was up before and has no link now."""
    from mmn.services import remediation

    cfg = await remediation.settings(db)
    fresh = timedelta(minutes=3 * int(cfg.get("service_check_minutes") or 5) + 2)
    want: dict[str, dict[str, Any]] = {}
    for (nid, name), s in list(_nic_state.items()):
        if s.get("status") != "down" or not s.get("expected") or now - s["at"] > fresh or not agent_hub.is_online(nid):
            continue
        label = f"{name} ({s['description']})" if s.get("description") else name
        want[f"network:{nid}:{name}"] = {"node_id": nid, "kind": "network", "severity": "critical",
                                         "detail": {"adapter": name, "description": s.get("description") or "",
                                                    "state": s["status"], "mac": s.get("mac") or ""},
                                         "message": f"Network adapter {label} has no link"}
    return want


# ------------------------------------------------------------------ the agent's local watchdog
def agent_config(rules: list[RemediationRule]) -> dict[str, Any]:
    """The watchdog configuration for one machine: the union of the adapters of its rules with the
    local fallback; the strictest limits win."""
    local = [r for r in rules if (r.match or {}).get("local")]
    if not local:
        return {"enabled": False}
    patterns: list[str] = []
    every = False
    for r in local:
        ps = (r.match or {}).get("adapters") or []
        if not ps:
            every = True
        patterns += [p for p in ps if p not in patterns]
    cfg = {"enabled": True, "adapters": [] if every else patterns,
           "down_minutes": max(2, min(max(r.delay_minutes, 2) for r in local)),
           "cooldown_minutes": max(5, min(max(r.cooldown_minutes, 5) for r in local)),
           "max_per_day": max(1, min(r.max_attempts for r in local)),
           "rules": sorted(r.id for r in local)}
    return cfg


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


async def sync_agents(want: dict[str, list[RemediationRule]], info: dict[str, tuple[str, list[str], str]]) -> None:
    async with sessionmaker()() as db:
        state = await get_setting(db, STATE_KEY)
    applied: dict[str, str] = dict(state.get("applied") or {})
    unsupported: list[str] = []
    collected: list[tuple[str, dict[str, Any]]] = []
    targets = set(want) | set(applied)
    dirty = False
    for nid in targets:
        cfg = agent_config(want.get(nid, []))
        if not cfg["enabled"] and nid not in applied:
            continue
        if nid not in info or not agent_hub.is_online(nid):
            continue
        if CAPABILITY not in info[nid][1]:
            if cfg["enabled"]:
                unsupported.append(info[nid][2])
            continue
        h = config_hash(cfg)
        try:
            st = await agent_hub.request(nid, "netwatch.status", {}, timeout=30) or {}
            for act in st.get("actions") or []:
                collected.append((nid, act))
            if st.get("hash") != h:
                await agent_hub.request(nid, "netwatch.apply", {**cfg, "hash": h}, timeout=30)
            if st.get("actions"):
                await agent_hub.request(nid, "netwatch.ack", {"until": st["actions"][-1].get("at")}, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.info("network watchdog on %s: %s", nid, e)
            continue
        if cfg["enabled"]:
            if applied.get(nid) != h:
                applied[nid], dirty = h, True
        elif nid in applied:
            applied.pop(nid)
            dirty = True
    if dirty or sorted(unsupported) != sorted(state.get("unsupported") or []):
        async with sessionmaker()() as db:
            st = await get_setting(db, STATE_KEY)
            st["applied"], st["unsupported"] = applied, sorted(unsupported)
            await set_setting(db, STATE_KEY, st)
            await db.commit()
    if collected:
        await record_local(collected, want)


async def record_local(collected: list[tuple[str, dict[str, Any]]], want: dict[str, list[RemediationRule]]) -> list[str]:
    """Turn the repairs that agents did themselves into recovery attempts (source ``local``)."""
    from mmn.services import remediation

    ids: list[str] = []
    with tenancy.unrestricted():
        async with sessionmaker()() as db:
            for nid, act in collected:
                n = await db.get(Node, nid)
                if n is None:
                    continue
                name = str(act.get("adapter") or "")[:100]
                at = _parse_time(act.get("at")) or datetime.now(UTC)
                key = f"local:network:{nid}:{name}:{at.isoformat()}"
                dup = (await db.execute(select(RemediationAttempt.id).where(
                    RemediationAttempt.source_key == key[:200]))).scalars().first()
                if dup:
                    continue
                rule = next((r for r in want.get(nid, []) if (r.match or {}).get("local")), None)
                desc = str(act.get("description") or "")[:200]
                label = f"{name} ({desc})" if desc else name
                ok = bool(act.get("ok"))
                a = RemediationAttempt(
                    rule_id=rule.id if rule else None, rule_name=(rule.name if rule else "Local network watchdog"),
                    customer_id=n.environment_id, customer_name=n.environment.name if n.environment else None,
                    source="local", source_key=key[:200], source_id=None, node_id=n.id, target=n.name,
                    problem=f"Network adapter {label} had no link while Stackpilot could not reach the machine"[:500],
                    status="succeeded" if ok else "failed", resolved=ok, runbook_name=None, created_at=at,
                    finished_at=at,
                    reason={"code": "local_ok" if ok else "local_failed",
                            "params": {"adapter": name, "after": str(act.get("after") or ""),
                                       "error": str(act.get("error") or "")[:300]}})
                db.add(a)
                await db.flush()
                remediation._audit(db, "local", remediation.LABEL, target=n.name, source="agent",  # noqa: SLF001
                                   success=ok, detail={"attempt_id": a.id, "adapter": name, "action": act})
                ids.append(a.id)
            await db.commit()
    if ids:
        events.publish("remediation.changed", {"local": len(ids)})
        for aid in ids:
            await remediation.notify_attempt(aid)
    return ids


def _parse_time(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


async def status(db: AsyncSession) -> dict[str, Any]:
    st = await get_setting(db, STATE_KEY)
    return {"local_agents": len(st.get("applied") or {}), "needs_agent_update": st.get("unsupported") or []}
