"""Customers: the top layer of Stackpilot (stored as environments).

Overview of everything at a customer (Proxmox VE nodes, servers, workstations, UniFi), the
optimisation scan the AI runs per customer and the customer context (notes, maintenance window,
SLA, contacts) that is given to the AI as an MCP resource.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import Alert, ApiToken, Environment, Node, UnifiConnection
from mmn.services import agent_hub, agent_packages, nodes
from mmn.services.unifi import service as us

MEM_HIGH = 85.0
MEM_LOW = 50.0
DISK_HIGH = 85.0
STORAGE_HIGH = 80.0
GUEST_DISK_HIGH = 90.0
OK_HEALTH = {"PASSED", "OK", "UNKNOWN", ""}


def _pct(used: Any, total: Any) -> float:
    try:
        return float(used) / float(total) * 100 if total else 0.0
    except (TypeError, ValueError):
        return 0.0


def machine_type(n: Node) -> str:
    if n.kind == "proxmox":
        return "proxmox"
    return "workstation" if n.role == "workstation" else "server"


def machine_row(n: Node, bundled: str | None = None) -> dict[str, Any]:
    from mmn.services.servers import attention

    m = n.metrics or {}
    upd = n.updates or {}
    info = n.info or {}
    online = agent_hub.is_online(n.id)
    return {
        "id": n.id, "name": n.name, "type": machine_type(n), "kind": n.kind, "role": n.role or "server",
        "hostname": n.hostname, "cluster": n.cluster_name, "online": online,
        "os": f"PVE {info['pve_version']}" if info.get("pve_version") else info.get("os_pretty_name") or n.os,
        "cpu": m.get("cpu_percent") if online else None,
        "mem_pct": round(_pct(m.get("mem_used"), m.get("mem_total")), 1) if m.get("mem_total") else None,
        "updates": len(upd.get("packages") or []), "reboot_required": bool(upd.get("reboot_required")),
        "attention": attention(n),
        "agent_version": n.agent_version,
        "agent_update_available": bool(bundled and n.agent_version and n.agent_version != bundled),
        "last_seen_at": n.last_seen_at,
        "guests": (m.get("guests") or {}) if n.kind == "proxmox" else None,
    }


def _unifi_row(c: UnifiConnection) -> dict[str, Any]:
    tot = (c.snapshot or {}).get("totals") or {}
    return {"id": c.id, "name": c.name, "kind": c.kind, "status": c.status, "enabled": c.enabled,
            "last_error": c.last_error, "last_ok_at": c.last_ok_at,
            "sites": len((c.info or {}).get("sites") or []), "totals": tot}


async def summaries(db: AsyncSession) -> list[dict[str, Any]]:
    """One line per customer for the customer list: status, machines, updates, alerts, UniFi."""
    envs = await nodes.list_environments(db)
    conns = list((await db.execute(select(UnifiConnection))).scalars())
    alerts = list((await db.execute(select(Alert).where(Alert.resolved_at.is_(None)))).scalars())
    node_env = {n.id: n.environment_id for e in envs for n in e.nodes}
    out = []
    for e in envs:
        base = nodes.env_out(e)
        ns = list(e.nodes)
        online = sum(1 for n in ns if agent_hub.is_online(n.id))
        ec = [c for c in conns if c.environment_id == e.id]
        n_alerts = sum(1 for a in alerts if a.node_id and node_env.get(a.node_id) == e.id)
        attention = sum(1 for n in ns if not agent_hub.is_online(n.id)) + sum(1 for c in ec if c.status == "error")
        base.update({
            "machines": len(ns), "online": online, "alerts": n_alerts,
            "unifi": {"connections": len(ec), "errors": sum(1 for c in ec if c.status == "error"),
                      "devices": (ut := us.unifi_totals(ec))["devices"], "firmware": ut["firmware"]},
            "status": "empty" if not ns and not ec else ("attention" if attention or n_alerts else "ok"),
        })
        out.append(base)
    return out


async def overview(db: AsyncSession, e: Environment, *, user_id: str | None = None) -> dict[str, Any]:
    """Everything at one customer in one list, plus totals per type, open alerts and AI access."""
    from mmn.services import monitoring

    bundled = agent_packages.bundled_version()
    ns = sorted(e.nodes, key=lambda n: ({"proxmox": 0, "server": 1, "workstation": 2}[machine_type(n)], n.name.lower()))
    machines = [machine_row(n, bundled) for n in ns]
    conns = list((await db.execute(select(UnifiConnection).where(UnifiConnection.environment_id == e.id)
                                   .order_by(UnifiConnection.name))).scalars())
    ids = [n.id for n in ns]
    alerts = []
    if ids:
        rows = (await db.execute(select(Alert).where(Alert.resolved_at.is_(None), Alert.node_id.in_(ids))
                                 .order_by(Alert.started_at.desc()).limit(50))).scalars()
        names = {n.id: n.name for n in ns}
        alerts = [monitoring.alert_out(a, names) for a in rows]

    def count(t: str) -> dict[str, Any]:
        rows = [m for m in machines if m["type"] == t]
        return {"total": len(rows), "online": sum(1 for m in rows if m["online"]),
                "updates": sum(m["updates"] for m in rows), "attention": sum(1 for m in rows if m["attention"]),
                "with_updates": sum(1 for m in rows if m["updates"]),
                "linux": sum(1 for m in rows if m["kind"] == "linux"),
                "windows": sum(1 for m in rows if m["kind"] == "windows")}

    px = count("proxmox")
    px["guests"] = sum(((m["guests"] or {}).get("vms_total") or 0) + ((m["guests"] or {}).get("cts_total") or 0)
                       for m in machines if m["type"] == "proxmox")
    net = {"connections": len(conns), "errors": sum(1 for c in conns if c.status == "error"),
           **us.unifi_totals(conns)}
    fabrics = [us.fabric_out(c, conns) for c in conns if c.kind == "site_manager" and c.fabric]
    net["fabrics"] = [{"id": f["id"], "name": f["name"], "status": f["status"], "auto_link": f["auto_link"],
                       **f["totals"]} for f in fabrics]
    tokens = await ai_access(db, e.id, user_id)
    return {"customer": nodes.env_out(e), "machines": machines, "unifi": [_unifi_row(c) for c in conns],
            "alerts": alerts,
            "totals": {"proxmox": px, "servers": count("server"), "workstations": count("workstation"),
                       "network": net, "alerts": len(alerts)},
            "ai_access": tokens}


async def ai_access(db: AsyncSession, customer_id: str, user_id: str | None) -> list[dict[str, Any]]:
    """Active tokens (personal and OAuth) that can see this customer: tied to it, or to all customers."""
    from mmn.services import tenancy

    if tenancy.allowed() is not None:
        return []
    q = select(ApiToken).where(ApiToken.revoked.is_(False))
    if user_id:
        q = q.where(ApiToken.user_id == user_id)
    out = []
    seen_oauth: set[str] = set()
    for t in (await db.execute(q.order_by(ApiToken.created_at.desc()))).scalars():
        ids = t.customer_ids
        if ids is not None and customer_id not in ids:
            continue
        if t.kind == "oauth":
            if t.oauth_client_id in seen_oauth:
                continue
            seen_oauth.add(t.oauth_client_id or "")
        out.append({"id": t.id, "name": t.name, "kind": t.kind, "scopes": t.scopes,
                    "all_customers": ids is None, "customers": len(ids or []), "last_used_at": t.last_used_at,
                    "oauth_client_id": t.oauth_client_id})
    return out


# ------------------------------------------------------------------ AI context
def context_markdown(e: Environment) -> str:
    """The customer context for the AI: read this before working on the customer's machines."""
    lines = [f"# Customer: {e.name}", ""]
    if e.description:
        lines += [e.description, ""]
    facts = [("Type", "your own environment" if e.kind == "home" else "managed customer"),
             ("Maintenance window", e.maintenance_window), ("SLA", e.sla),
             ("Main contact", ", ".join(x for x in (e.contact_name, e.contact_email, e.contact_phone) if x) or None)]
    lines += [f"- **{k}:** {v}" for k, v in facts if v]
    if e.contacts:
        lines += ["", "## Contacts"]
        for c in e.contacts:
            bits = [c.get("name") or "", f"({c['role']})" if c.get("role") else "", c.get("email") or "", c.get("phone") or ""]
            lines.append("- " + " ".join(b for b in bits if b))
    lines += ["", "## Notes (follow these)", e.notes.strip() if e.notes else "_No notes._"]
    if e.maintenance_window:
        lines += ["", f"Only restart services, reboot machines or install updates inside the maintenance window "
                      f"({e.maintenance_window}), unless the user explicitly says otherwise."]
    return "\n".join(lines)


def context_dict(e: Environment) -> dict[str, Any]:
    return {"name": e.name, "kind": e.kind, "description": e.description, "notes": e.notes,
            "maintenance_window": e.maintenance_window, "sla": e.sla,
            "contact": {"name": e.contact_name, "email": e.contact_email, "phone": e.contact_phone},
            "contacts": e.contacts or []}


# ------------------------------------------------------------------ optimisation scan
def _finding(severity: str, area: str, target: str | None, finding: str, suggestion: str,
             tool: str | None = None, *, code: str | None = None, **params: Any) -> dict[str, Any]:
    """One finding. `code` plus `params` let the interface and the monthly report translate the
    English `finding` and `suggestion` texts (see FINDING_NL and the opt.* interface keys)."""
    d: dict[str, Any] = {"severity": severity, "area": area, "target": target, "finding": finding,
                         "suggestion": suggestion}
    if tool:
        d["next_tool"] = tool
    if code:
        d["code"] = code
        d["params"] = {k: v for k, v in params.items() if v is not None}
    return d


def _guests_by_cluster(ns: list[Node]) -> dict[str, list[dict[str, Any]]]:
    """Guests per cluster (or per standalone node), without duplicates from cluster-wide lists."""
    from mmn.services.guests import guests_of

    out: dict[str, dict[tuple, dict[str, Any]]] = {}
    for n in ns:
        key = n.cluster_name or n.id
        for g in guests_of(n):
            if g["template"]:
                continue
            out.setdefault(key, {})[(g["type"], g["vmid"])] = g
    return {k: list(v.values()) for k, v in out.items()}


def _scan_proxmox(ns: list[Node], findings: list[dict[str, Any]]) -> None:
    px = [n for n in ns if n.kind == "proxmox"]
    online = [n for n in px if agent_hub.is_online(n.id)]
    for n in px:
        if n not in online:
            findings.append(_finding("high", "proxmox", n.name, "The node is offline (no agent connection).",
                                     "Check power and network, or the mmn-agent service on the node.", "get_node",
                                     code="node_offline"))
    # Memory per node, and imbalance inside a cluster.
    clusters: dict[str, list[Node]] = {}
    for n in online:
        m = n.metrics or {}
        mem = _pct(m.get("mem_used"), m.get("mem_total"))
        if mem >= MEM_HIGH:
            findings.append(_finding("medium", "capacity", n.name, f"Memory use is {mem:.0f}%.",
                                     "Move a guest to a quieter node, lower guest memory or add RAM.",
                                     "get_node_resources", code="node_mem_high", pct=round(mem)))
        if n.cluster_name:
            clusters.setdefault(n.cluster_name, []).append(n)
        for s in (n.health or {}).get("storages") or []:
            pct = _pct(s.get("used"), s.get("total"))
            if s.get("active") and pct >= STORAGE_HIGH:
                findings.append(_finding("high" if pct >= 92 else "medium", "storage", f"{n.name}:{s.get('storage')}",
                                         f"Storage {s.get('storage')} is {pct:.0f}% full.",
                                         "Remove old backups, ISOs, snapshots or unused disks, or extend the storage.",
                                         "list_storage", code="storage_full", storage=s.get("storage"), pct=round(pct)))
        bad = [d for d in (n.health or {}).get("disks") or [] if str(d.get("health") or "").upper() not in OK_HEALTH]
        for d in bad:
            findings.append(_finding("high", "hardware", f"{n.name}:{d.get('devpath')}",
                                     f"SMART health of {d.get('devpath')} ({d.get('model') or 'disk'}) is {d.get('health')}.",
                                     "Replace the disk soon and check that backups are recent.", "get_disk_smart",
                                     code="smart_bad", disk=d.get("devpath"), model=d.get("model") or "disk",
                                     health=d.get("health")))
    for cname, members in clusters.items():
        if len(members) < 2:
            continue
        mems = sorted(((_pct((n.metrics or {}).get("mem_used"), (n.metrics or {}).get("mem_total")), n) for n in members),
                      key=lambda x: x[0])
        (low, ln), (high, hn) = mems[0], mems[-1]
        if high >= MEM_HIGH and high - low >= 35:
            guests = [g for g in _guests_by_cluster([hn]).get(hn.cluster_name or hn.id, [])
                      if g["status"] == "running" and (g.get("pve_node") in (None, hn.pve_name))]
            guests.sort(key=lambda g: g["mem"], reverse=True)
            pick = guests[0] if guests else None
            guest = f"{('CT' if pick['type'] == 'lxc' else 'VM')} {pick['vmid']} ({pick['name']})" if pick else None
            what = f"Move {guest} to {ln.name}" if guest else f"Move a guest to {ln.name}"
            findings.append(_finding("medium", "capacity", f"cluster {cname}",
                                     f"{hn.name} uses {high:.0f}% memory while {ln.name} uses {low:.0f}%.",
                                     f"{what} to balance the cluster.", "migrate_guest",
                                     code="cluster_imbalance" if guest else "cluster_imbalance_any", guest=guest,
                                     busy=hn.name, busy_pct=round(high), quiet=ln.name, quiet_pct=round(low)))
    # Guests: full disks, memory pressure, stopped guests.
    for _key, guests in _guests_by_cluster(px).items():
        stopped = [g for g in guests if g["status"] == "stopped"]
        for g in guests:
            label = f"{'CT' if g['type'] == 'lxc' else 'VM'} {g['vmid']} {g['name']}"
            if g["status"] == "running" and g["type"] == "lxc" and _pct(g["disk"], g["maxdisk"]) >= GUEST_DISK_HIGH:
                findings.append(_finding("medium", "guests", label, f"Root disk is {_pct(g['disk'], g['maxdisk']):.0f}% full.",
                                         "Clean up logs and caches in the container or grow its disk.", "run_in_container",
                                         code="guest_disk_full", pct=round(_pct(g["disk"], g["maxdisk"]))))
            if g["status"] == "running" and g["maxmem"] and _pct(g["mem"], g["maxmem"]) >= 95:
                findings.append(_finding("low", "guests", label, f"Uses {_pct(g['mem'], g['maxmem']):.0f}% of its memory.",
                                         "Give it more memory if it is slow or swaps.", "set_guest_resources",
                                         code="guest_mem_high", pct=round(_pct(g["mem"], g["maxmem"]))))
        if stopped:
            names = ", ".join(f"{g['vmid']} {g['name']}" for g in stopped[:10])
            findings.append(_finding("low", "guests", f"{len(stopped)} stopped guest(s)", f"Stopped: {names}.",
                                     "Remove guests that are no longer needed (after a backup) to free disk space.",
                                     "list_guests", code="guests_stopped", count=len(stopped), names=names))


def _scan_machines(ns: list[Node], bundled: str | None, findings: list[dict[str, Any]]) -> None:
    for n in ns:
        t = machine_type(n)
        area = "workstations" if t == "workstation" else ("proxmox" if t == "proxmox" else "servers")
        online = agent_hub.is_online(n.id)
        upd = n.updates or {}
        count = len(upd.get("packages") or [])
        if t != "proxmox" and not online:
            findings.append(_finding("low" if t == "workstation" else "high", area, n.name,
                                     "Offline (no agent connection)." if t != "workstation" else "Offline or asleep.",
                                     "Workstations are checked again when they come online." if t == "workstation"
                                     else "Check the machine and the agent service.", "diagnose_server",
                                     code="workstation_offline" if t == "workstation" else "machine_offline"))
        if count:
            sec = sum(1 for p in upd.get("packages") or []
                      if p.get("security") or "security" in str(p.get("suite") or "").lower())
            findings.append(_finding("medium" if sec or count >= 20 else "low", "updates", n.name,
                                     f"{count} pending update(s){f', {sec} security' if sec else ''}.",
                                     "Install them in the maintenance window.", "install_node_updates",
                                     code="updates_pending_sec" if sec else "updates_pending", count=count, security=sec))
        if upd.get("reboot_required"):
            findings.append(_finding("medium", "updates", n.name, "A reboot is pending after updates.",
                                     "Reboot in the maintenance window.", "reboot_node",
                                     code="reboot_pending"))
        if bundled and n.agent_version and n.agent_version != bundled and online:
            findings.append(_finding("low", "agents", n.name, f"Agent {n.agent_version} is older than {bundled}.",
                                     "Update the agent (no downtime).", "update_node_agent",
                                     code="agent_old", version=n.agent_version, bundled=bundled))
        if t == "proxmox" or not online:
            continue
        m = n.metrics or {}
        if m.get("failed_services"):
            findings.append(_finding("medium", area, n.name, f"{m['failed_services']} failed service(s).",
                                     "Look at the failed services and restart or disable them.", "list_services",
                                     code="services_failed", count=m["failed_services"]))
        disks = m.get("disks") or ([{"mount": "/", "total": m.get("disk_total"), "used": m.get("disk_used")}]
                                   if m.get("disk_total") else [])
        for d in disks:
            pct = _pct(d.get("used"), d.get("total"))
            if pct >= DISK_HIGH:
                where = d.get("mount") or d.get("name") or d.get("path") or "disk"
                findings.append(_finding("high" if pct >= 93 else "medium", "disk", f"{n.name} {where}",
                                         f"Disk {where} is {pct:.0f}% full.",
                                         "Remove old logs, temp files and old backups, or extend the disk.",
                                         "diagnose_server", code="disk_full", disk=where, pct=round(pct)))
        mem = _pct(m.get("mem_used"), m.get("mem_total"))
        if mem >= 92:
            findings.append(_finding("medium", "capacity", n.name, f"Memory use is {mem:.0f}%.",
                                     "Find the process that uses the memory, or add memory.", "diagnose_server",
                                     code="machine_mem_high", pct=round(mem)))


async def _scan_unifi(db: AsyncSession, conns: list[UnifiConnection], live: bool,
                      findings: list[dict[str, Any]]) -> None:
    from mmn.services.unifi import service as us

    for c in conns:
        if not c.enabled:
            continue
        if c.status == "error":
            findings.append(_finding("medium", "network", c.name, f"The UniFi connection fails: {c.last_error or 'error'}.",
                                     "Check the API key and the address of the console.", "list_unifi_connections",
                                     code="unifi_error", error=c.last_error or "error"))
        tot = (c.snapshot or {}).get("totals") or {}
        if tot.get("offline"):
            findings.append(_finding("medium", "network", c.name, f"{tot['offline']} UniFi device(s) offline.",
                                     "Check power, PoE and uplinks of those devices.", "get_unifi_overview",
                                     code="unifi_offline", count=tot["offline"]))
    for f in us.firmware_report(conns):
        findings.append(_finding("low", "network", f"{f['device']} ({f['type']})",
                                 f"Firmware update available for {f['model'] or f['device']} (now {f['firmware']}).",
                                 "Update the firmware in a quiet moment.", "find_firmware_updates",
                                 code="firmware", device=f["model"] or f["device"], firmware=f["firmware"]))
    if not live:
        return
    for c in conns:
        if c.kind != "network" or c.status == "error" or not c.enabled:
            continue
        for s in (c.info or {}).get("sites") or []:
            try:
                res = await asyncio.wait_for(us.guest_isolation(db, c, s["id"]), timeout=20)
            except Exception:  # noqa: BLE001 - optional check, not every console supports it
                continue
            verdict = res.get("verdict")
            if verdict in ("open", "partial"):
                nets = ", ".join(g.get("name") or "?" for g in res.get("guest_networks") or [])
                findings.append(_finding("high" if verdict == "open" else "medium", "network",
                                         f"{c.name} / {s.get('name')}",
                                         f"The guest network ({nets}) can reach the LAN ({verdict}).",
                                         "Isolate the guest network from the LAN with a firewall zone policy.",
                                         "check_guest_isolation",
                                         code="guest_isolation_open" if verdict == "open" else "guest_isolation_partial",
                                         networks=nets))


async def optimisation_scan(db: AsyncSession, e: Environment, *, live: bool = True) -> dict[str, Any]:
    """What can be improved at one customer: capacity, storage and disks, updates and reboots,
    agents, failed services, guests, UniFi devices and firmware, guest isolation."""
    bundled = agent_packages.bundled_version()
    ns = list(e.nodes)
    conns = list((await db.execute(select(UnifiConnection).where(UnifiConnection.environment_id == e.id))).scalars())
    findings: list[dict[str, Any]] = []
    _scan_proxmox(ns, findings)
    _scan_machines(ns, bundled, findings)
    await _scan_unifi(db, conns, live, findings)
    cov_at = None
    if ns:
        from mmn.services import protection

        row = await protection.stored(db, e)
        cov = protection.view(e, row) if row else None
        cov_at = cov["collected_at"] if cov else None
        findings += protection.scan_findings(cov, _finding)
    n_alerts = 0
    if ns:
        n_alerts = (await db.execute(select(func.count(Alert.id)).where(
            Alert.resolved_at.is_(None), Alert.node_id.in_([n.id for n in ns])))).scalar() or 0
    if n_alerts:
        findings.append(_finding("medium", "monitoring", e.name, f"{n_alerts} open monitoring alert(s).",
                                 "Look at the open alerts.", "list_alerts",
                                 code="open_alerts", count=n_alerts))
    if not e.maintenance_window and e.kind == "customer":
        findings.append(_finding("info", "customer", e.name, "No maintenance window is set for this customer.",
                                 "Agree on a maintenance window and set it on the customer page, so updates can be planned.",
                                 code="no_window"))
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["area"], str(f["target"])))
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    return {"customer": e.name, "context": context_dict(e),
            "checked": {"proxmox_nodes": sum(1 for n in ns if n.kind == "proxmox"),
                        "servers": sum(1 for n in ns if machine_type(n) == "server"),
                        "workstations": sum(1 for n in ns if machine_type(n) == "workstation"),
                        "unifi_connections": len(conns), "live_network_checks": live,
                        "backup_coverage_at": cov_at},
            "summary": by_sev, "findings": findings,
            "note": "Findings come from the latest data the agents and UniFi consoles reported. Respect the "
                    "customer's notes and maintenance window; ask the user before changing anything."}
