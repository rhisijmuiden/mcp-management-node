"""Firewall configuration changes and VPN tunnel status per customer (phase 20).

Firewall: Stackpilot keeps versions of every firewall configuration of a customer and shows each change as
a diff that an admin reviews (acknowledges):

* Proxmox VE: the cluster firewall (/etc/pve/firewall/*.fw: cluster, security groups, aliases, IP sets and
  every guest) and the host firewall of each node, read once per cluster.
* Linux servers: the nftables ruleset (or iptables-save), without counters and without the contents of
  dynamic sets (fail2ban, CrowdSec), and lines of Docker, Kubernetes and fail2ban chains left out.
* Windows servers: the profiles (on/off, default actions) and every enabled rule with its ports,
  addresses and program.
* UniFi: the firewall zones and policies of every site of a Network connection.

A change made through Stackpilot itself (a firewall action in the audit log) is marked as such and, with
the setting ``auto_ack_own``, acknowledged automatically.

VPN: tunnels and VPN servers are found and their status is followed: WireGuard peers (a peer with
persistent keepalive is down when its last handshake is too old), IPsec (strongSwan, swanctl or ipsec),
OpenVPN services on Linux; site-to-site interfaces (RRAS), VPN connections, WireGuard and OpenVPN on
Windows; the site-to-site tunnels and VPN servers of UniFi gateways (Network 9.3+). A watched tunnel that is
down for longer than ``vpn_down_minutes`` is an alert. Tunnels are watched automatically once seen up.

Everything on machines is read through the agent's existing ``exec`` command; the agent itself does not
change.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import (
    AuditLog,
    Environment,
    FirewallSource,
    FirewallVersion,
    Node,
    UnifiConnection,
    VpnTunnel,
)
from mmn.services import agent_hub
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.netsec")

FW_KINDS = ("proxmox", "linux", "windows", "unifi")
VPN_KINDS = ("wireguard", "ipsec", "openvpn", "windows_s2s", "windows_vpn", "unifi_s2s", "unifi_server")
VPN_STATUSES = ("up", "down", "idle", "missing", "unknown")
AUTO_WATCH = {"wireguard", "ipsec", "openvpn", "windows_s2s", "unifi_s2s"}
MAX_CONTENT = 400_000
MAX_DIFF = 64_000

DEFAULTS: dict[str, Any] = {
    "fw_interval_minutes": 30,
    "fw_keep_versions": 50,
    "alert_fw_changes": True,
    "auto_ack_own": True,
    "fw_ignore": ["DOCKER", r"\bdocker0\b", r"\bbr-[0-9a-f]{12}\b", r"\bveth[0-9a-f]+", r"\bcali", "KUBE-",
                  r"\bf2b-", "fail2ban", "crowdsec"],
    "vpn_interval_minutes": 5,
    "vpn_down_minutes": 10,
    "alert_vpn": True,
    "wg_handshake_seconds": 300,
}


class NetsecError(Exception):
    pass


async def settings(db: AsyncSession) -> dict[str, Any]:
    return {**DEFAULTS, **{k: v for k, v in (await get_setting(db, "netsec")).items() if k in DEFAULTS}}


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    d = _aware(d)
    return d.isoformat() if d else None


def _short(e: BaseException) -> str:
    if isinstance(e, TimeoutError | asyncio.TimeoutError):
        return "timed out"
    return (str(e) or type(e).__name__)[:500]


# ================================================================== firewall
PVE_FW_SCRIPT = r"""set +e
for f in /etc/pve/firewall/*.fw /etc/pve/nodes/*/host.fw; do
  [ -f "$f" ] || continue
  printf '### %s\n' "$f"; cat "$f"; printf '\n'
done
command -v pve-firewall >/dev/null 2>&1 && printf '#STATUS\t%s\n' "$(pve-firewall status 2>/dev/null | head -n 1)"
exit 0
"""

LINUX_FW_SCRIPT = r"""set +e
if command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1; then
  printf '#TOOL\tnft\n'; nft -s list ruleset 2>/dev/null
elif command -v iptables-save >/dev/null 2>&1; then
  printf '#TOOL\tiptables\n'; iptables-save 2>/dev/null; command -v ip6tables-save >/dev/null 2>&1 && ip6tables-save 2>/dev/null
else
  printf '#TOOL\tnone\n'
fi
command -v ufw >/dev/null 2>&1 && printf '#UFW\t%s\n' "$(ufw status 2>/dev/null | head -n 1)"
exit 0
"""

WINDOWS_FW_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
Get-NetFirewallProfile -PolicyStore ActiveStore | ForEach-Object { "PROFILE`t$($_.Name)`t$($_.Enabled)`t$($_.DefaultInboundAction)`t$($_.DefaultOutboundAction)" }
$pf = @{}; Get-NetFirewallPortFilter -All -PolicyStore ActiveStore | ForEach-Object { $pf[$_.InstanceID] = $_ }
$af = @{}; Get-NetFirewallAddressFilter -All -PolicyStore ActiveStore | ForEach-Object { $af[$_.InstanceID] = $_ }
$ap = @{}; Get-NetFirewallApplicationFilter -All -PolicyStore ActiveStore | ForEach-Object { $ap[$_.InstanceID] = $_ }
Get-NetFirewallRule -All -PolicyStore ActiveStore | Where-Object { "$($_.Enabled)" -eq 'True' } | ForEach-Object {
  $p = $pf[$_.InstanceID]; $a = $af[$_.InstanceID]; $x = $ap[$_.InstanceID]
  "RULE`t$($_.DisplayName)`t$($_.Direction)`t$($_.Action)`t$($_.Profile)`t$($p.Protocol)`t$($p.LocalPort -join ',')`t$($p.RemotePort -join ',')`t$($a.RemoteAddress -join ',')`t$($x.Program)`t$($_.PolicyStoreSourceType)"
}
"""


def _ignore_re(patterns: list[str] | None) -> re.Pattern | None:
    good = []
    for p in patterns or []:
        try:
            re.compile(p)
            good.append(f"(?:{p})")
        except re.error:
            continue
    return re.compile("|".join(good)) if good else None


def normalize_linux(stdout: str, ignore: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    """(content, summary) of LINUX_FW_SCRIPT: no counters, no dynamic set elements, ignored lines left out."""
    tool, ufw = "none", None
    body: list[str] = []
    for line in (stdout or "").splitlines():
        if line.startswith("#TOOL\t"):
            tool = line.split("\t", 1)[1].strip()
        elif line.startswith("#UFW\t"):
            ufw = line.split("\t", 1)[1].strip() or None
        else:
            body.append(line.rstrip())
    text = "\n".join(body)
    if tool == "nft":
        text = re.sub(r"elements = \{[^}]*\}", "elements = { … }", text, flags=re.S)
        text = re.sub(r"\s*counter packets \d+ bytes \d+", " counter", text)
    elif tool == "iptables":
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
        text = re.sub(r"\[\d+:\d+\]", "[0:0]", text)
    rx = _ignore_re(ignore)
    lines = [ln for ln in text.splitlines() if ln.strip() and not (rx and rx.search(ln))]
    rules = sum(1 for ln in lines if (tool == "iptables" and ln.startswith("-A ")) or
                (tool == "nft" and re.match(r"\s+(ip6?|tcp|udp|iif|oif|ct|meta|counter|accept|drop|reject|jump|"
                                            r"iifname|oifname|th|icmp|log)\b", ln) is not None))
    summary = {"tool": tool, "rules": rules, "ufw": ufw}
    return "\n".join(lines) + ("\n" if lines else ""), summary


def normalize_pve(stdout: str) -> tuple[str, dict[str, Any]]:
    status = None
    out: list[str] = []
    files = 0
    rules = 0
    enabled = None
    current = None
    for line in (stdout or "").splitlines():
        if line.startswith("#STATUS\t"):
            status = line.split("\t", 1)[1].strip() or None
            continue
        if line.startswith("### "):
            files += 1
            current = line[4:].strip()
        s = line.strip()
        if s.startswith(("IN ", "OUT ", "GROUP ", "|IN ", "|OUT ", "|GROUP ")):
            rules += 1
        if current and current.endswith("/cluster.fw") and re.match(r"enable\s*:\s*(\d)", s):
            enabled = s.split(":", 1)[1].strip() == "1"
        out.append(line.rstrip())
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else ""), {"files": files, "rules": rules, "status": status,
                                                    "cluster_enabled": enabled}


def normalize_windows(stdout: str, ignore: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    profiles, rules = [], []
    rx = _ignore_re(ignore)
    for raw in (stdout or "").splitlines():
        p = raw.rstrip("\r").split("\t")
        if p[0] == "PROFILE" and len(p) >= 5:
            profiles.append({"name": p[1], "enabled": p[2] == "True", "inbound": p[3], "outbound": p[4]})
        elif p[0] == "RULE" and len(p) >= 5:
            p += [""] * (12 - len(p))
            line = (f"{p[1]} | {p[2]} {p[3]} | profile {p[4]} | {p[5] or 'Any'} local {p[6] or 'Any'} "
                    f"remote {p[7] or 'Any'} | from {p[8] or 'Any'}" + (f" | {p[9]}" if p[9] else "") +
                    (f" | {p[10]}" if p[10] and p[10] != "Local" else ""))
            if not (rx and rx.search(line)):
                rules.append(line)
    head = [f"profile {x['name']}: {'on' if x['enabled'] else 'OFF'}, inbound {x['inbound']}, outbound {x['outbound']}"
            for x in sorted(profiles, key=lambda x: x["name"])]
    lines = head + ["", *sorted(set(rules), key=str.lower)] if rules else head
    return "\n".join(lines) + "\n", {"profiles": profiles, "rules": len(set(rules)),
                                     "disabled_profiles": [x["name"] for x in profiles if not x["enabled"]]}


_VOLATILE = re.compile(r"(?i)^(updatedAt|createdAt|lastModified|modifiedAt|hitCount|hits|statistics|metadata)$")


def _flatten(prefix: str, v: Any, out: list[str], zones: dict[str, str]) -> None:
    if isinstance(v, dict):
        for k in sorted(v):
            if _VOLATILE.match(k):
                continue
            _flatten(f"{prefix}.{k}" if prefix else k, v[k], out, zones)
    elif isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            out.append(f"{prefix}: [{', '.join(zones.get(str(x), str(x)) for x in v)}]")
        else:
            for i, x in enumerate(v):
                _flatten(f"{prefix}[{i}]", x, out, zones)
    else:
        text = zones.get(str(v), json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
        out.append(f"{prefix}: {text}")


def render_unifi(zones: list[dict], policies: list[dict], site: str) -> tuple[str, dict[str, Any]]:
    """Zones and policies of one UniFi site as readable, sorted text (zone ids replaced by their names)."""
    zmap = {str(z.get("id")): f"zone {z.get('name')}" for z in zones if z.get("id")}
    out = [f"# UniFi site {site}"]
    for z in sorted(zones, key=lambda z: str(z.get("name"))):
        lines: list[str] = []
        _flatten("", {k: v for k, v in z.items() if k not in ("id", "name")}, lines, {})
        out.append(f"[zone] {z.get('name')}")
        out += [f"  {ln}" for ln in lines]
    enabled = 0
    for p in sorted(policies, key=lambda p: (str(p.get("name") or ""), str(p.get("id")))):
        lines = []
        _flatten("", {k: v for k, v in p.items() if k not in ("id", "name")}, lines, zmap)
        out.append(f"[policy] {p.get('name') or '(no name)'} ({str(p.get('id'))[-8:]})")
        out += [f"  {ln}" for ln in lines]
        enabled += 1 if p.get("enabled", True) is not False else 0
    return "\n".join(out) + "\n", {"zones": len(zones), "rules": len(policies), "enabled": enabled}


def make_diff(old: str, new: str) -> tuple[str, int, int]:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=2))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "\n".join(lines[2:])
    if len(text) > MAX_DIFF:
        text = text[:MAX_DIFF] + "\n… (diff cut off)"
    return text, added, removed


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _made_by(db: AsyncSession, src: FirewallSource, since: datetime, now: datetime) -> str | None:
    """A firewall action through Stackpilot (interface or MCP) on a machine of this source in the window."""
    names: set[str] = set()
    q = select(Node).where(Node.environment_id == src.customer_id)
    for n in (await db.execute(q)).scalars():
        if src.kind == "proxmox" and n.kind == "proxmox" or n.id == src.node_id:
            names.add(n.name.lower())
            for g in (n.guests or {}).get("guests") or []:
                for k in ("name", "vmid"):
                    if g.get(k) is not None:
                        names.add(str(g[k]).lower())
    if not names:
        return None
    rows = (await db.execute(select(AuditLog).where(
        AuditLog.at >= since - timedelta(minutes=1), AuditLog.at <= now, AuditLog.success.is_(True),
        func.lower(AuditLog.action).like("%firewall%")).order_by(AuditLog.at.desc()).limit(50))).scalars()
    for a in rows:
        args = (a.detail or {}).get("args") or {}
        refs = {str(x).lower() for x in (a.target, args.get("node"), args.get("guest"), (a.detail or {}).get("node"))
                if x is not None}
        if refs & names or (a.target or "").lower() in names:
            who = a.username or (a.detail or {}).get("token") or a.source
            return f"{who} · {a.action}"[:300]
    return None


async def record(db: AsyncSession, src: FirewallSource, content: str, summary: dict[str, Any],
                 now: datetime, cfg: dict[str, Any]) -> FirewallVersion | None:
    """Store a new version when the configuration differs from the last one (the first is the baseline)."""
    if len(content) > MAX_CONTENT:
        content = content[:MAX_CONTENT] + "\n… (cut off)\n"
    h = _hash(content)
    src.summary, src.last_check_at, src.last_ok_at, src.last_error = summary, now, now, None
    if src.last_hash == h:
        return None
    prev = (await db.execute(select(FirewallVersion).where(FirewallVersion.source_id == src.id)
                             .order_by(FirewallVersion.taken_at.desc()).limit(1))).scalar_one_or_none()
    v = FirewallVersion(source_id=src.id, customer_id=src.customer_id, taken_at=now, hash=h, content=content)
    if prev is None:
        v.baseline = True
        v.acknowledged_at, v.acknowledged_by = now, "baseline"
    else:
        v.diff, v.added, v.removed = make_diff(prev.content, content)
        v.made_by = await _made_by(db, src, _aware(prev.taken_at) or now, now)
        if v.made_by and cfg.get("auto_ack_own"):
            v.acknowledged_at, v.acknowledged_by, v.note = now, "stackpilot", "made through Stackpilot"
    db.add(v)
    src.last_hash = h
    await db.flush()
    keep = max(5, int(cfg.get("fw_keep_versions") or 50))
    old = (await db.execute(select(FirewallVersion.id).where(FirewallVersion.source_id == src.id)
                            .order_by(FirewallVersion.taken_at.desc()).offset(keep))).scalars().all()
    if old:
        await db.execute(delete(FirewallVersion).where(FirewallVersion.id.in_(old)))
    return v


async def _source(db: AsyncSession, *, customer_id: str, key: str, kind: str, label: str, node_id: str | None = None,
                  connection_id: str | None = None) -> FirewallSource:
    src = (await db.execute(select(FirewallSource).where(FirewallSource.key == key))).scalar_one_or_none()
    if src is None:
        src = FirewallSource(customer_id=customer_id, key=key, kind=kind, label=label, node_id=node_id,
                             connection_id=connection_id, summary={})
        db.add(src)
        await db.flush()
    else:
        src.label, src.customer_id = label, customer_id
    return src


def pve_groups(nodes: list[Node]) -> list[tuple[str, str, list[Node]]]:
    """(key, label, nodes) per Proxmox VE cluster (or standalone node): the cluster firewall is read once."""
    groups: dict[str, tuple[str, list[Node]]] = {}
    for n in nodes:
        if n.kind != "proxmox":
            continue
        k = f"pve:{n.environment_id}:{n.cluster_name}" if n.cluster_name else f"pve:{n.id}"
        label = f"Proxmox VE cluster {n.cluster_name}" if n.cluster_name else f"Proxmox VE {n.name}"
        groups.setdefault(k, (label, []))[1].append(n)
    return [(k, lbl, sorted(ns, key=lambda n: n.name)) for k, (lbl, ns) in groups.items()]


async def _exec(n: Node, script: str, timeout: int = 120) -> str:
    res = await agent_hub.request(n.id, "exec", {"command": script, "timeout": timeout}, timeout=timeout + 30) or {}
    if res.get("timed_out"):
        raise NetsecError("the script timed out")
    return res.get("stdout") or ""


async def firewall_customer(env_id: str) -> dict[str, Any]:
    """Read every firewall configuration of one customer and store what changed."""
    from mmn.services.customers import machine_type

    changed, errors, sources = 0, [], 0
    async with sessionmaker()() as db:
        cfg = await settings(db)
        nodes = list((await db.execute(select(Node).where(Node.environment_id == env_id))).scalars())
        conns = list((await db.execute(select(UnifiConnection).where(
            UnifiConnection.environment_id == env_id, UnifiConnection.kind == "network",
            UnifiConnection.enabled.is_(True)))).scalars())
    jobs: list[tuple[str, str, str, str | None, Any]] = []
    for key, label, ns in pve_groups(nodes):
        online = [n for n in ns if agent_hub.is_online(n.id)]
        if online:
            jobs.append((key, "proxmox", label, online[0].id, ("pve", online[0])))
    for n in nodes:
        if n.kind == "proxmox" or machine_type(n) == "workstation" or not agent_hub.is_online(n.id):
            continue
        jobs.append((f"node:{n.id}", n.kind if n.kind in ("linux", "windows") else "linux", f"{n.name}", n.id, ("node", n)))

    async def run(job) -> None:
        nonlocal changed, sources
        key, kind, label, node_id, (how, n) = job
        now = datetime.now(UTC)
        try:
            if how == "pve":
                content, summary = normalize_pve(await _exec(n, PVE_FW_SCRIPT, 60))
            elif n.kind == "windows":
                content, summary = normalize_windows(await _exec(n, WINDOWS_FW_SCRIPT, 180), cfg["fw_ignore"])
            else:
                content, summary = normalize_linux(await _exec(n, LINUX_FW_SCRIPT, 60), cfg["fw_ignore"])
            err = None
        except Exception as e:  # noqa: BLE001
            content, summary, err = "", {}, _short(e)
        async with sessionmaker()() as db:
            src = await _source(db, customer_id=env_id, key=key, kind=kind, label=label,
                                node_id=None if how == "pve" else node_id)
            if not src.enabled:
                await db.commit()
                return
            sources += 1
            if err:
                src.last_check_at, src.last_error = now, err
                errors.append(f"{label}: {err}")
            elif await record(db, src, content, summary, now, cfg):
                changed += 1
            await db.commit()

    async def unifi(c: UnifiConnection) -> None:
        nonlocal changed, sources
        from mmn.services.unifi import service as us
        from mmn.services.unifi.service import _optional

        now = datetime.now(UTC)
        try:
            async with sessionmaker()() as db:
                c2 = await db.get(UnifiConnection, c.id)
                api = await us.network_api(db, c2)
            sites = await api.sites()
            per_site = []
            for s in sites:
                sid = s.get("id")
                zones = await _optional(api.firewall_zones(sid))
                pols = await _optional(api.firewall_policies(sid))
                if zones is None and pols is None:
                    continue
                per_site.append((s, zones or [], pols or []))
        except Exception as e:  # noqa: BLE001
            async with sessionmaker()() as db:
                src = await _source(db, customer_id=env_id, key=f"unifi:{c.id}", kind="unifi",
                                    label=f"UniFi {c.name}", connection_id=c.id)
                src.last_check_at, src.last_error = now, _short(e)
                errors.append(f"UniFi {c.name}: {_short(e)}")
                await db.commit()
            return
        async with sessionmaker()() as db:
            # A connection that could be read but has no firewall API: nothing to keep.
            old = (await db.execute(select(FirewallSource).where(FirewallSource.key == f"unifi:{c.id}"))).scalar_one_or_none()
            if old is not None:
                await db.delete(old)
            for s, zones, pols in per_site:
                sname = s.get("name") or s.get("internalReference") or s.get("id")
                content, summary = render_unifi(zones, pols, sname)
                src = await _source(db, customer_id=env_id, key=f"unifi:{c.id}:{s.get('id')}", kind="unifi",
                                    label=f"UniFi {c.name} · {sname}", connection_id=c.id)
                if not src.enabled:
                    continue
                sources += 1
                if await record(db, src, content, summary, now, cfg):
                    changed += 1
            await db.commit()

    sem = asyncio.Semaphore(6)

    async def guarded(coro):
        async with sem:
            await coro

    await asyncio.gather(*(guarded(run(j)) for j in jobs), *(guarded(unifi(c)) for c in conns))
    return {"sources": sources, "changed": changed, "errors": errors[:10]}


def version_out(v: FirewallVersion, src: FirewallSource | None = None, *, content: bool = False,
                diff: bool = True, customers: dict[str, str] | None = None) -> dict[str, Any]:
    d = {"id": v.id, "source_id": v.source_id, "customer_id": v.customer_id,
         "customer_name": (customers or {}).get(v.customer_id), "taken_at": _iso(v.taken_at), "baseline": v.baseline,
         "added": v.added, "removed": v.removed, "made_by": v.made_by, "acknowledged_at": _iso(v.acknowledged_at),
         "acknowledged_by": v.acknowledged_by, "note": v.note}
    if src is not None:
        d.update(source_label=src.label, source_kind=src.kind, node_id=src.node_id)
    if diff:
        d["diff"] = v.diff
    if content:
        d["content"] = v.content
    return d


def source_out(s: FirewallSource, *, unacked: int = 0, versions: int = 0, last_change: datetime | None = None,
               customers: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": s.id, "customer_id": s.customer_id, "customer_name": (customers or {}).get(s.customer_id),
            "key": s.key, "kind": s.kind, "label": s.label, "node_id": s.node_id, "connection_id": s.connection_id,
            "enabled": s.enabled, "summary": s.summary or {}, "last_check_at": _iso(s.last_check_at),
            "last_ok_at": _iso(s.last_ok_at), "last_error": s.last_error, "unacknowledged": unacked,
            "versions": versions, "last_change_at": _iso(last_change)}


async def _customers(db: AsyncSession) -> dict[str, str]:
    return {e.id: e.name for e in (await db.execute(select(Environment))).scalars()}


async def sources(db: AsyncSession, customer_id: str | None = None) -> list[dict[str, Any]]:
    q = select(FirewallSource).order_by(FirewallSource.label)
    if customer_id:
        q = q.where(FirewallSource.customer_id == customer_id)
    rows = list((await db.execute(q)).scalars())
    stats = {r.source_id: r for r in (await db.execute(
        select(FirewallVersion.source_id, func.count().label("n"),
               func.count().filter(FirewallVersion.acknowledged_at.is_(None)).label("open"),
               func.max(FirewallVersion.taken_at).filter(FirewallVersion.baseline.is_(False)).label("last"))
        .group_by(FirewallVersion.source_id))).all()}
    cust = await _customers(db)
    out = []
    for s in rows:
        st = stats.get(s.id)
        out.append(source_out(s, unacked=st.open if st else 0, versions=st.n if st else 0,
                              last_change=st.last if st else None, customers=cust))
    return out


async def changes(db: AsyncSession, *, customer_id: str | None = None, source_id: str | None = None,
                  unacknowledged: bool = False, days: int = 30, limit: int = 200,
                  with_diff: bool = False) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 3650)))
    q = (select(FirewallVersion, FirewallSource).join(FirewallSource, FirewallSource.id == FirewallVersion.source_id)
         .where(FirewallVersion.baseline.is_(False), FirewallVersion.taken_at >= since)
         .order_by(FirewallVersion.taken_at.desc()).limit(max(1, min(limit, 1000))))
    if customer_id:
        q = q.where(FirewallVersion.customer_id == customer_id)
    if source_id:
        q = q.where(FirewallVersion.source_id == source_id)
    if unacknowledged:
        q = q.where(FirewallVersion.acknowledged_at.is_(None))
    cust = await _customers(db)
    return [version_out(v, s, diff=with_diff, customers=cust) for v, s in (await db.execute(q)).all()]


async def acknowledge(db: AsyncSession, *, version_id: str | None = None, source_id: str | None = None,
                      customer_id: str | None = None, by: str, note: str | None = None) -> int:
    """Acknowledge one change, all open changes of a source, or all open changes of a customer."""
    q = select(FirewallVersion).where(FirewallVersion.acknowledged_at.is_(None))
    if version_id:
        q = q.where(FirewallVersion.id == version_id)
    elif source_id:
        q = q.where(FirewallVersion.source_id == source_id)
    elif customer_id:
        q = q.where(FirewallVersion.customer_id == customer_id)
    else:
        raise NetsecError("give a change, a source or a customer")
    now = datetime.now(UTC)
    n = 0
    for v in (await db.execute(q)).scalars():
        v.acknowledged_at, v.acknowledged_by = now, by[:100]
        if note:
            v.note = note.strip()[:500]
        n += 1
    return n


async def find_version(db: AsyncSession, ref: str) -> tuple[FirewallVersion, FirewallSource]:
    v = await db.get(FirewallVersion, ref)
    if v is None:
        raise NetsecError(f"firewall change {ref} not found")
    return v, await db.get(FirewallSource, v.source_id)


async def find_source(db: AsyncSession, ref: str, customer_id: str | None = None) -> FirewallSource:
    s = await db.get(FirewallSource, ref)
    if s is None:
        q = select(FirewallSource).where(func.lower(FirewallSource.label) == ref.lower().strip())
        if customer_id:
            q = q.where(FirewallSource.customer_id == customer_id)
        s = (await db.execute(q)).scalars().first()
    if s is None or (customer_id and s.customer_id != customer_id):
        raise NetsecError(f"firewall {ref} not found")
    return s


# ================================================================== VPN
LINUX_VPN_SCRIPT = r"""set +e
printf 'NOW\t%s\n' "$(date +%s)"
command -v wg >/dev/null 2>&1 && wg show all dump 2>/dev/null | awk '{print "WG\t" $0}'
if command -v swanctl >/dev/null 2>&1 && swanctl --list-conns >/dev/null 2>&1; then
  printf 'SWANCTL_CONNS\n'; swanctl --list-conns 2>/dev/null
  printf 'SWANCTL_SAS\n'; swanctl --list-sas 2>/dev/null
  printf 'SWANCTL_END\n'
elif command -v ipsec >/dev/null 2>&1; then
  printf 'IPSEC_STATUS\n'; ipsec statusall 2>/dev/null; printf 'IPSEC_END\n'
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-units --all --plain --no-legend --no-pager 'openvpn*.service' 'wg-quick@*.service' 2>/dev/null | awk '{print "UNIT\t" $1 "\t" $3 "\t" $4}'
fi
exit 0
"""

WINDOWS_VPN_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
"NOW`t$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
if (Get-Command Get-VpnS2SInterface -ErrorAction SilentlyContinue) {
  Get-VpnS2SInterface | ForEach-Object { "S2S`t$($_.Name)`t$($_.ConnectionState)`t$($_.Destination -join ',')`t$($_.AdminStatus)" }
}
Get-VpnConnection -AllUserConnection | ForEach-Object { "VPN`t$($_.Name)`t$($_.ConnectionStatus)`t$($_.ServerAddress)" }
$wg = Join-Path $env:ProgramFiles 'WireGuard\wg.exe'
if (Test-Path $wg) { & $wg show all dump | ForEach-Object { "WG`t$_" } }
Get-Service -Name 'OpenVPNService*', 'ovpn*' | ForEach-Object { "UNIT`t$($_.Name)`t$($_.Status)`t$($_.StartType)" }
"""


def parse_wg(lines: list[str], now: int, handshake_limit: int = 300) -> list[dict[str, Any]]:
    out = []
    for ln in lines:
        p = ln.split("\t")
        if len(p) != 9:
            continue  # the interface line has 5 fields
        iface, pub, _psk, endpoint, allowed, hs, rx, tx, ka = p
        try:
            hs_i, rx_i, tx_i = int(hs), int(rx), int(tx)
        except ValueError:
            continue
        keepalive = None if ka.strip() in ("off", "0", "") else int(ka) if ka.strip().isdigit() else None
        age = now - hs_i if hs_i else None
        if age is not None and age <= 180:
            status = "up"
        elif keepalive:
            status = "up" if age is not None and age <= handshake_limit else "down"
        else:
            status = "idle"
        first = allowed.split(",")[0].strip() if allowed and allowed != "(none)" else pub[:8]
        out.append({"key": f"wg:{iface}:{pub}", "kind": "wireguard", "name": f"{iface} · {first}",
                    "remote": None if endpoint in ("(none)", "") else endpoint, "status": status,
                    "detail": {"interface": iface, "public_key": pub, "allowed_ips": allowed,
                               "handshake_age": age, "rx": rx_i, "tx": tx_i, "keepalive": keepalive},
                    "auto_watch": bool(keepalive)})
    return out


def parse_swanctl(conns: str, sas: str) -> list[dict[str, Any]]:
    names: dict[str, dict[str, Any]] = {}
    cur = None
    for ln in conns.splitlines():
        m = re.match(r"^([^\s:]+):\s", ln)
        if m:
            cur = m.group(1)
            names[cur] = {"remote": None}
            continue
        m = re.match(r"^\s+remote:\s+(\S+)", ln)
        if m and cur and names[cur]["remote"] is None:
            names[cur]["remote"] = m.group(1)
    states: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    cur = None
    for ln in sas.splitlines():
        m = re.match(r"^([^\s:]+):\s+#\d+,\s+(\w+)", ln)
        if m:
            cur = m.group(1)
            if states.get(cur) != "ESTABLISHED":
                states[cur] = m.group(2).upper()
            continue
        m = re.match(r"^\s+([^\s:]+):\s+#\d+,\s+reqid\s+\d+,\s+(\w+)", ln)
        if m and cur:
            children.setdefault(cur, []).append(f"{m.group(1)} {m.group(2)}")
    out = []
    for name in sorted(set(names) | set(states)):
        st = states.get(name)
        out.append({"key": f"ipsec:{name}", "kind": "ipsec", "name": name, "remote": (names.get(name) or {}).get("remote"),
                    "status": "up" if st == "ESTABLISHED" else "down",
                    "detail": {"ike": st or "none", "children": children.get(name, [])}, "auto_watch": True})
    return out


def parse_ipsec_statusall(text: str) -> list[dict[str, Any]]:
    conns: dict[str, str | None] = {}
    states: dict[str, str] = {}
    section = None
    for ln in text.splitlines():
        if ln.startswith("Connections:"):
            section = "c"
            continue
        if ln.startswith("Security Associations"):
            section = "s"
            continue
        if section == "c":
            m = re.match(r"^\s+([^\s:\[{]+):\s+(\S+)\.\.\.(\S+)", ln)
            if m and m.group(1) not in conns:
                conns[m.group(1)] = m.group(3)
        elif section == "s":
            m = re.match(r"^\s+([^\s\[{]+)\[\d+\]:\s+(\w+)", ln)
            if m and states.get(m.group(1)) != "ESTABLISHED":
                states[m.group(1)] = m.group(2).upper()
    out = []
    for name in sorted(set(conns) | set(states)):
        st = states.get(name)
        out.append({"key": f"ipsec:{name}", "kind": "ipsec", "name": name, "remote": conns.get(name),
                    "status": "up" if st == "ESTABLISHED" else "down", "detail": {"ike": st or "none"},
                    "auto_watch": True})
    return out


def _between(lines: list[str], start: str, end: str) -> str:
    try:
        i = lines.index(start)
    except ValueError:
        return ""
    try:
        j = lines.index(end, i + 1)
    except ValueError:
        j = len(lines)
    return "\n".join(lines[i + 1:j])


def parse_linux_vpn(stdout: str, handshake_limit: int = 300) -> list[dict[str, Any]]:
    lines = [ln.rstrip("\r") for ln in (stdout or "").splitlines()]
    now = 0
    wg: list[str] = []
    out: list[dict[str, Any]] = []
    for ln in lines:
        if ln.startswith("NOW\t"):
            try:
                now = int(ln.split("\t", 1)[1])
            except ValueError:
                now = 0
        elif ln.startswith("WG\t"):
            wg.append(ln[3:])
    out += parse_wg(wg, now or int(datetime.now(UTC).timestamp()), handshake_limit)
    if "SWANCTL_CONNS" in lines:
        conns = _between(lines, "SWANCTL_CONNS", "SWANCTL_SAS")
        sas = _between(lines, "SWANCTL_SAS", "SWANCTL_END")
        out += parse_swanctl(conns, sas)
    elif "IPSEC_STATUS" in lines:
        out += parse_ipsec_statusall(_between(lines, "IPSEC_STATUS", "IPSEC_END"))
    for ln in lines:
        p = ln.split("\t")
        if p[0] != "UNIT" or len(p) < 4:
            continue
        unit, active, sub = p[1], p[2], p[3]
        if unit in ("openvpn.service",) or unit.startswith("wg-quick@"):
            continue  # the umbrella unit; WireGuard tunnels are followed per peer
        name = re.sub(r"^openvpn(-client|-server)?@", "", unit).removesuffix(".service")
        status = "up" if active == "active" and sub == "running" else "down"
        out.append({"key": f"openvpn:{unit}", "kind": "openvpn", "name": name or unit, "remote": None,
                    "status": status, "detail": {"unit": unit, "active": active, "sub": sub}, "auto_watch": True})
    return out


def parse_windows_vpn(stdout: str, handshake_limit: int = 300) -> list[dict[str, Any]]:
    lines = [ln.rstrip("\r") for ln in (stdout or "").splitlines()]
    now = 0
    wg: list[str] = []
    out: list[dict[str, Any]] = []
    for ln in lines:
        p = ln.split("\t")
        if p[0] == "NOW" and len(p) > 1:
            try:
                now = int(p[1])
            except ValueError:
                now = 0
        elif p[0] == "WG":
            wg.append("\t".join(p[1:]))
        elif p[0] == "S2S" and len(p) >= 3:
            st = p[2].lower()
            if len(p) > 4 and p[4].strip().lower() == "disabled":
                continue
            out.append({"key": f"s2s:{p[1]}", "kind": "windows_s2s", "name": p[1],
                        "remote": (p[3] if len(p) > 3 else "") or None,
                        "status": "up" if st == "connected" else "down", "detail": {"state": p[2]},
                        "auto_watch": True})
        elif p[0] == "VPN" and len(p) >= 3:
            st = p[2].lower()
            out.append({"key": f"vpn:{p[1]}", "kind": "windows_vpn", "name": p[1],
                        "remote": (p[3] if len(p) > 3 else "") or None,
                        "status": "up" if st == "connected" else "idle" if st == "disconnected" else "down",
                        "detail": {"state": p[2]}, "auto_watch": False})
        elif p[0] == "UNIT" and len(p) >= 3:
            if len(p) > 3 and p[3].strip().lower() == "disabled":
                continue
            out.append({"key": f"openvpn:{p[1]}", "kind": "openvpn", "name": p[1], "remote": None,
                        "status": "up" if p[2].lower() == "running" else "down",
                        "detail": {"service": p[1], "state": p[2], "start": p[3] if len(p) > 3 else None},
                        "auto_watch": len(p) > 3 and p[3].strip().lower() == "automatic"})
    out += parse_wg(wg, now or int(datetime.now(UTC).timestamp()), handshake_limit)
    return out


_UP = re.compile(r"(?i)^(up|connected|established|active|online|running|ok|healthy)$")
_DOWN = re.compile(r"(?i)^(down|disconnected|failed|failure|inactive|offline|error|stopped|unhealthy|disabled)$")


def _status_field(d: dict) -> str | None:
    for k, v in d.items():
        if re.search(r"(?i)(^|_)(status|state)$|connectionState|tunnelStatus|Status$|State$", k) and isinstance(v, str):
            return v
    for k in ("connected", "isConnected", "up"):
        if isinstance(d.get(k), bool):
            return "up" if d[k] else "down"
    return None


def _remote_field(d: dict) -> str | None:
    for k in ("remoteIp", "remoteIpAddress", "peerIp", "peerIpAddress", "remoteHost", "remoteAddress", "remote",
              "remoteGateway", "serverAddress"):
        v = d.get(k)
        if isinstance(v, str) and v:
            return v[:255]
        if isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str) and vv:
                    return vv[:255]
    return None


def parse_unifi_vpn(site: dict, tunnels: list[dict] | None, servers: list[dict] | None) -> list[dict[str, Any]]:
    out = []
    sid = site.get("id")
    sname = site.get("name") or site.get("internalReference") or sid
    for kind, items in (("unifi_s2s", tunnels or []), ("unifi_server", servers or [])):
        for d in items:
            if d.get("enabled") is False:
                continue
            raw = _status_field(d)
            status = "unknown" if raw is None else "up" if _UP.match(raw) else "down" if _DOWN.match(raw) else "unknown"
            if kind == "unifi_server" and raw is None:
                status = "up"  # a VPN server that is enabled is listening
            name = d.get("name") or d.get("id") or "?"
            out.append({"key": f"unifi:{sid}:{kind}:{d.get('id') or name}", "kind": kind, "name": f"{name} ({sname})",
                        "remote": _remote_field(d), "status": status,
                        "detail": {"site": sname, "type": d.get("type"), "raw_status": raw},
                        "auto_watch": kind == "unifi_s2s" and raw is not None})
    return out


def effective_watch(t: VpnTunnel) -> bool:
    if t.watched is not None:
        return bool(t.watched)
    return t.kind in AUTO_WATCH and t.last_up_at is not None and bool((t.detail or {}).get("auto_watch", True))


async def apply_tunnels(db: AsyncSession, customer_id: str, found: list[dict[str, Any]], now: datetime, *,
                        node_id: str | None = None, connection_id: str | None = None) -> int:
    """Store what one machine or UniFi connection reported; return the number of status changes."""
    q = select(VpnTunnel).where(VpnTunnel.customer_id == customer_id)
    q = q.where(VpnTunnel.node_id == node_id) if node_id else q.where(VpnTunnel.connection_id == connection_id)
    have = {t.key: t for t in (await db.execute(q)).scalars()}
    seen = set()
    changes_n = 0
    cutoff = now - timedelta(days=3)
    for f in found:
        key = f["key"][:400]
        if key in seen:
            continue
        seen.add(key)
        t = have.get(key)
        if t is None:
            t = VpnTunnel(customer_id=customer_id, key=key, kind=f["kind"], node_id=node_id, connection_id=connection_id,
                          status=f["status"], status_since=now, first_seen=now, changes=[], detail={})
            db.add(t)
        elif t.status != f["status"]:
            hist = [c for c in (t.changes or []) if (_parse(c.get("at")) or now) >= cutoff][-49:]
            hist.append({"at": now.isoformat(), "from": t.status, "to": f["status"]})
            t.changes, t.status, t.status_since = hist, f["status"], now
            changes_n += 1
        t.name, t.remote, t.kind = f["name"][:255], (f.get("remote") or None), f["kind"]
        t.detail = {**(f.get("detail") or {}), "auto_watch": bool(f.get("auto_watch", True))}
        t.last_seen = t.last_check_at = now
        if f["status"] == "up":
            t.last_up_at = now
    for key, t in have.items():
        if key in seen:
            continue
        if t.watched or effective_watch(t):
            if t.status != "missing":
                hist = list(t.changes or [])[-49:]
                hist.append({"at": now.isoformat(), "from": t.status, "to": "missing"})
                t.changes, t.status, t.status_since = hist, "missing", now
                changes_n += 1
            t.last_check_at = now
        else:
            await db.delete(t)
    return changes_n


def _parse(v: Any) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(v)))
    except (TypeError, ValueError):
        return None


async def vpn_customer(env_id: str) -> dict[str, Any]:
    from mmn.services.customers import machine_type

    errors: list[str] = []
    changed = 0
    async with sessionmaker()() as db:
        cfg = await settings(db)
        nodes = [n for n in (await db.execute(select(Node).where(Node.environment_id == env_id))).scalars()
                 if machine_type(n) != "workstation" and agent_hub.is_online(n.id)]
        conns = list((await db.execute(select(UnifiConnection).where(
            UnifiConnection.environment_id == env_id, UnifiConnection.kind == "network",
            UnifiConnection.enabled.is_(True)))).scalars())
    limit = int(cfg["wg_handshake_seconds"])

    async def node(n: Node) -> None:
        nonlocal changed
        try:
            if n.kind == "windows":
                found = parse_windows_vpn(await _exec(n, WINDOWS_VPN_SCRIPT, 60), limit)
            else:
                found = parse_linux_vpn(await _exec(n, LINUX_VPN_SCRIPT, 45), limit)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{n.name}: {_short(e)}")
            return
        async with sessionmaker()() as db:
            changed += await apply_tunnels(db, env_id, found, datetime.now(UTC), node_id=n.id)
            await db.commit()

    async def unifi(c: UnifiConnection) -> None:
        nonlocal changed
        from mmn.services.unifi import service as us
        from mmn.services.unifi.service import _optional

        try:
            async with sessionmaker()() as db:
                api = await us.network_api(db, await db.get(UnifiConnection, c.id))
            found: list[dict[str, Any]] = []
            for s in await api.sites():
                tunnels = await _optional(api.vpn_tunnels(s.get("id")))
                servers = await _optional(api.vpn_servers(s.get("id")))
                found += parse_unifi_vpn(s, tunnels, servers)
        except Exception as e:  # noqa: BLE001
            errors.append(f"UniFi {c.name}: {_short(e)}")
            return
        async with sessionmaker()() as db:
            changed += await apply_tunnels(db, env_id, found, datetime.now(UTC), connection_id=c.id)
            await db.commit()

    sem = asyncio.Semaphore(6)

    async def guarded(coro):
        async with sem:
            await coro

    await asyncio.gather(*(guarded(node(n)) for n in nodes), *(guarded(unifi(c)) for c in conns))
    return {"machines": len(nodes), "unifi": len(conns), "changed": changed, "errors": errors[:10]}


def tunnel_out(t: VpnTunnel, names: dict[str, str] | None = None, customers: dict[str, str] | None = None,
               conns: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    day = now - timedelta(hours=24)
    flaps = sum(1 for c in t.changes or [] if (_parse(c.get("at")) or now) >= day)
    return {"id": t.id, "customer_id": t.customer_id, "customer_name": (customers or {}).get(t.customer_id),
            "kind": t.kind, "name": t.name, "node_id": t.node_id, "node_name": (names or {}).get(t.node_id or ""),
            "connection_id": t.connection_id, "connection_name": (conns or {}).get(t.connection_id or ""),
            "remote": t.remote, "status": t.status, "status_since": _iso(t.status_since), "detail": t.detail or {},
            "watched": t.watched, "watching": effective_watch(t), "last_up_at": _iso(t.last_up_at),
            "last_check_at": _iso(t.last_check_at), "first_seen": _iso(t.first_seen), "changes_24h": flaps,
            "changes": (t.changes or [])[-20:]}


async def tunnels(db: AsyncSession, customer_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    from mmn.models import UptimeCheck

    q = select(VpnTunnel).order_by(VpnTunnel.name)
    if customer_id:
        q = q.where(VpnTunnel.customer_id == customer_id)
    names = {n.id: n.name for n in (await db.execute(select(Node))).scalars()}
    cust = await _customers(db)
    conns = {c.id: c.name for c in (await db.execute(select(UnifiConnection))).scalars()}
    now = datetime.now(UTC)
    rows = [tunnel_out(t, names, cust, conns, now) for t in (await db.execute(q)).scalars()]
    counts = dict.fromkeys(VPN_STATUSES, 0)
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if status:
        rows = [r for r in rows if r["status"] == status]
    uq = select(UptimeCheck).where(UptimeCheck.kind == "vpn").order_by(UptimeCheck.name)
    if customer_id:
        uq = uq.where(UptimeCheck.customer_id == customer_id)
    checks = [{"id": c.id, "name": c.name, "customer_id": c.customer_id, "customer_name": cust.get(c.customer_id),
               "target": c.target, "port": c.port, "mode": (c.config or {}).get("mode", "ssl"), "status": c.status,
               "status_since": _iso(c.status_since), "last_error": c.last_error, "enabled": c.enabled}
              for c in (await db.execute(uq)).scalars()]
    return {"tunnels": rows, "counts": counts, "uptime_checks": checks}


async def find_tunnel(db: AsyncSession, ref: str, customer_id: str | None = None) -> VpnTunnel:
    t = await db.get(VpnTunnel, ref)
    if t is None:
        q = select(VpnTunnel).where(func.lower(VpnTunnel.name) == ref.lower().strip())
        if customer_id:
            q = q.where(VpnTunnel.customer_id == customer_id)
        found = list((await db.execute(q)).scalars())
        if len(found) > 1:
            raise NetsecError(f"several tunnels are called {ref}; use the id")
        t = found[0] if found else None
    if t is None or (customer_id and t.customer_id != customer_id):
        raise NetsecError(f"VPN tunnel {ref} not found")
    return t


# ================================================================== alerts, findings
async def conditions(db: AsyncSession, now: datetime) -> dict[str, dict[str, Any]]:
    cfg = await settings(db)
    want: dict[str, dict[str, Any]] = {}
    cust = await _customers(db)
    if cfg["alert_fw_changes"]:
        rows = (await db.execute(
            select(FirewallSource, func.count(FirewallVersion.id), func.min(FirewallVersion.taken_at))
            .join(FirewallVersion, FirewallVersion.source_id == FirewallSource.id)
            .where(FirewallVersion.acknowledged_at.is_(None), FirewallSource.enabled.is_(True))
            .group_by(FirewallSource.id))).all()
        for s, n, first in rows:
            want[f"firewall:{s.id}"] = {
                "node_id": s.node_id, "kind": "firewall", "severity": "warning", "value": n,
                "detail": {"source": f"{cust.get(s.customer_id, '?')} · firewall", "customer_id": s.customer_id,
                           "firewall_source_id": s.id, "since": _iso(first)},
                "message": f"Firewall of {s.label} changed ({n} change(s) not reviewed yet)"}
    if cfg["alert_vpn"]:
        wait = timedelta(minutes=max(1, int(cfg["vpn_down_minutes"])))
        for t in (await db.execute(select(VpnTunnel).where(VpnTunnel.status.in_(("down", "missing"))))).scalars():
            if not effective_watch(t):
                continue
            if t.node_id and not agent_hub.is_online(t.node_id):
                continue  # the machine's own offline alert says enough
            since = _aware(t.status_since) or now
            if now - since < wait:
                continue
            mins = int((now - since).total_seconds() // 60)
            what = "is gone from the configuration" if t.status == "missing" else "is down"
            want[f"vpn:{t.id}"] = {
                "node_id": t.node_id, "kind": "vpn", "severity": "critical", "value": mins,
                "detail": {"source": f"{cust.get(t.customer_id, '?')} · VPN", "customer_id": t.customer_id,
                           "tunnel_id": t.id, "remote": t.remote},
                "message": f"VPN tunnel {t.name} {what} for {mins} minutes"}
    return want


async def findings(db: AsyncSession, e: Environment, now: datetime, finding) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    names = {n.id: n.name for n in (await db.execute(select(Node).where(Node.environment_id == e.id))).scalars()}
    rows = (await db.execute(
        select(FirewallSource, func.count(FirewallVersion.id), func.min(FirewallVersion.taken_at))
        .join(FirewallVersion, FirewallVersion.source_id == FirewallSource.id)
        .where(FirewallSource.customer_id == e.id, FirewallVersion.acknowledged_at.is_(None))
        .group_by(FirewallSource.id))).all()
    for s, n, first in rows:
        date = (_aware(first) or now).strftime("%Y-%m-%d %H:%M")
        out.append(finding("medium", "firewall", s.label,
                           f"The firewall of {s.label} changed {n} time(s) since {date} UTC and was not reviewed.",
                           "Review the change in Stackpilot (Security > Firewall changes): acknowledge it, or undo "
                           "it when nobody expected it.", "list_firewall_changes",
                           code="fw_changed", source=s.label, count=int(n), date=date))
    for s in (await db.execute(select(FirewallSource).where(FirewallSource.customer_id == e.id,
                                                            FirewallSource.enabled.is_(True)))).scalars():
        off = (s.summary or {}).get("disabled_profiles") or []
        if s.kind == "windows" and off:
            out.append(finding("medium", "firewall", names.get(s.node_id or "", s.label),
                               f"Windows Firewall is off for profile(s) {', '.join(off)}.",
                               "Turn the firewall on for every profile and allow what is needed with rules.",
                               code="fw_disabled", profiles=", ".join(off)))
        ok = _aware(s.last_ok_at)
        if s.last_error and (ok is None or now - ok > timedelta(days=1)):
            out.append(finding("low", "firewall", s.label,
                               f"The firewall of {s.label} could not be read: {s.last_error[:120]}.",
                               "Check the machine or connection; Stackpilot cannot see changes until it can read it again.",
                               code="fw_error", source=s.label, error=s.last_error[:120]))
    day = now - timedelta(hours=24)
    for t in (await db.execute(select(VpnTunnel).where(VpnTunnel.customer_id == e.id))).scalars():
        if not effective_watch(t):
            continue
        target = names.get(t.node_id or "", t.name)
        if t.status in ("down", "missing"):
            since = (_aware(t.status_since) or now).strftime("%Y-%m-%d %H:%M")
            out.append(finding("high", "vpn", target, f"VPN tunnel {t.name} is down since {since} UTC.",
                               "Check the tunnel on both sides (peer reachable, keys or pre-shared key, phase 1/2 "
                               "settings).", "list_vpn_tunnels", code="vpn_down", tunnel=t.name, since=since))
            continue
        flaps = sum(1 for c in t.changes or [] if (_parse(c.get("at")) or now) >= day)
        if flaps >= 4:
            out.append(finding("medium", "vpn", target, f"VPN tunnel {t.name} went down and up {flaps} times in 24 hours.",
                               "Look for an unstable internet line, DPD or rekey settings that differ, or an MTU problem.",
                               "list_vpn_tunnels", code="vpn_flapping", tunnel=t.name, count=flaps))
    return out


# ================================================================== scheduling
_last: dict[tuple[str, str], float] = {}
_busy: set[tuple[str, str]] = set()


async def refresh(env_id: str, what: str = "all") -> dict[str, Any]:
    """Read firewall, VPN and/or certificates of one customer now."""
    from mmn.services import certs

    out: dict[str, Any] = {}
    jobs = {"firewall": firewall_customer, "vpn": vpn_customer, "certificates": certs.refresh_customer}
    for name, fn in jobs.items():
        if what not in ("all", name):
            continue
        k = (env_id, name)
        if k in _busy:
            out[name] = {"busy": True}
            continue
        _busy.add(k)
        try:
            out[name] = await fn(env_id)
        finally:
            _busy.discard(k)
            _last[k] = asyncio.get_running_loop().time()
    return out


async def tick() -> int:
    """Every minute: start the firewall and VPN reads that are due, and the certificate reads."""
    from mmn.services import certs

    async with sessionmaker()() as db:
        cfg = await settings(db)
        ids = [e.id for e in (await db.execute(select(Environment))).scalars()]
    now = asyncio.get_running_loop().time()
    started = 0
    for what, minutes in (("vpn", cfg["vpn_interval_minutes"]), ("firewall", cfg["fw_interval_minutes"])):
        every = max(1, int(minutes)) * 60
        for i in ids:
            k = (i, what)
            if k in _busy or now - _last.get(k, -1e12) < every:
                continue
            _last[k] = now
            agent_hub.spawn(refresh(i, what))
            started += 1
    started += await certs.tick()
    return started
