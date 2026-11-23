"""Monthly customer reports (phase 10).

One report per customer and month (or any period on demand): availability of servers and Proxmox VE
nodes, incidents (monitoring alerts), updates installed and pending, Proxmox backups, workstations
and their security status, what the AI did (audit log and approvals) and recommendations from the
optimisation scan. The data is stored, and rendered as HTML (shown in the app, attached to the mail)
and as a PDF in the house style. The scheduler makes the reports of the previous month on the
configured day and mails them to the customer's contacts.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment as JinjaEnv
from jinja2 import FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.models import AiApproval, Alert, AuditLog, CustomerReport, Environment, Node, NodeJob
from mmn.services import agent_hub, customers, workstations
from mmn.services.nodes import NodeError
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.reports")

PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
DEFAULTS = {"enabled": False, "day": 1, "recipients": [], "lang": "en", "include_ai": True}

_jinja = JinjaEnv(loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "report_templates"),
                  autoescape=select_autoescape(["html"]))

# ------------------------------------------------------------------ texts (EN default, NL)
TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Monthly IT report", "period": "Period", "generated": "Generated", "customer": "Customer",
        "summary": "Summary", "availability": "Availability", "incidents": "Incidents and alerts",
        "updates": "Updates", "backups": "Backups", "workstations": "Workstations", "security": "Security",
        "ai": "AI actions", "recommendations": "Recommendations",
        "machine": "Machine", "type": "Type", "uptime": "Availability", "downtime": "Downtime",
        "installed": "Installed", "pending": "Pending", "reboot": "Reboot pending", "yes": "yes", "no": "no",
        "status": "Status", "user": "User", "issues": "Points of attention", "last_seen": "Last seen",
        "severity": "Severity", "when": "When", "what": "What", "duration": "Duration", "count": "Count",
        "tool": "Action", "target": "Target", "by": "By", "result": "Result", "ok": "OK", "failed": "Failed",
        "k_machines": "Machines", "k_availability": "Availability servers", "k_incidents": "Incidents",
        "k_updates": "Updates installed", "k_backups": "Backups OK / failed", "k_security": "Security points",
        "k_ai": "AI actions",
        "none_incidents": "No incidents in this period.", "none_updates": "No updates were installed in this period.",
        "none_backups": "No Proxmox VE backups in this customer's environment.",
        "none_ws": "No workstations with an agent.", "none_security": "No security points of attention.",
        "none_ai": "The AI did not change anything in this period.", "none_rec": "No recommendations.",
        "not_backed_up": "Guests without a backup job", "backups_error": "Could not read the backup tasks of",
        "ai_calls": "AI calls", "ai_changes": "changes", "ai_approvals": "approvals",
        "ai_rejected": "rejected", "minutes": "min", "proxmox": "Proxmox VE", "server": "Server",
        "workstation": "Workstation", "st_ok": "OK", "st_warning": "Attention", "st_critical": "Action needed",
        "st_unknown": "Unknown", "overall_ok": "Everything is in order.",
        "overall_warning": "There are points of attention; see the recommendations.",
        "overall_critical": "Action is needed; see the security points and recommendations.",
        "footer": "This report was generated automatically by {app}.",
        "mail_subject": "IT report {customer} {period}", "mail_title": "Your IT report for {period}",
        "mail_body": "Attached is the IT report for {customer} for {period}: availability, incidents, updates, "
                     "backups, security and the work that was done.",
        "mail_button": "Open the report",
        "sev_high": "High", "sev_medium": "Medium", "sev_low": "Low", "sev_info": "Info",
        "sev_critical": "Critical", "sev_warning": "Warning",
    },
    "nl": {
        "title": "Maandelijks IT-rapport", "period": "Periode", "generated": "Gemaakt", "customer": "Klant",
        "summary": "Samenvatting", "availability": "Beschikbaarheid", "incidents": "Incidenten en meldingen",
        "updates": "Updates", "backups": "Back-ups", "workstations": "Werkplekken", "security": "Beveiliging",
        "ai": "AI-acties", "recommendations": "Aanbevelingen",
        "machine": "Machine", "type": "Type", "uptime": "Beschikbaarheid", "downtime": "Uitval",
        "installed": "Geïnstalleerd", "pending": "Wachtend", "reboot": "Herstart nodig", "yes": "ja", "no": "nee",
        "status": "Status", "user": "Gebruiker", "issues": "Aandachtspunten", "last_seen": "Laatst gezien",
        "severity": "Ernst", "when": "Wanneer", "what": "Wat", "duration": "Duur", "count": "Aantal",
        "tool": "Actie", "target": "Doel", "by": "Door", "result": "Resultaat", "ok": "OK", "failed": "Mislukt",
        "k_machines": "Machines", "k_availability": "Beschikbaarheid servers", "k_incidents": "Incidenten",
        "k_updates": "Updates geïnstalleerd", "k_backups": "Back-ups OK / mislukt",
        "k_security": "Beveiligingspunten", "k_ai": "AI-acties",
        "none_incidents": "Geen incidenten in deze periode.",
        "none_updates": "In deze periode zijn geen updates geïnstalleerd.",
        "none_backups": "Geen Proxmox VE-back-ups in de omgeving van deze klant.",
        "none_ws": "Geen werkplekken met een agent.", "none_security": "Geen beveiligingspunten.",
        "none_ai": "De AI heeft in deze periode niets veranderd.", "none_rec": "Geen aanbevelingen.",
        "not_backed_up": "Gasten zonder back-uptaak", "backups_error": "Kon de back-uptaken niet lezen van",
        "ai_calls": "AI-aanroepen", "ai_changes": "wijzigingen", "ai_approvals": "goedkeuringen",
        "ai_rejected": "afgewezen", "minutes": "min", "proxmox": "Proxmox VE", "server": "Server",
        "workstation": "Werkplek", "st_ok": "OK", "st_warning": "Aandacht", "st_critical": "Actie nodig",
        "st_unknown": "Onbekend", "overall_ok": "Alles is in orde.",
        "overall_warning": "Er zijn aandachtspunten; zie de aanbevelingen.",
        "overall_critical": "Er is actie nodig; zie de beveiligingspunten en aanbevelingen.",
        "footer": "Dit rapport is automatisch gemaakt door {app}.",
        "mail_subject": "IT-rapport {customer} {period}", "mail_title": "Je IT-rapport over {period}",
        "mail_body": "In de bijlage staat het IT-rapport van {customer} over {period}: beschikbaarheid, "
                     "incidenten, updates, back-ups, beveiliging en het uitgevoerde werk.",
        "mail_button": "Rapport openen",
        "sev_high": "Hoog", "sev_medium": "Middel", "sev_low": "Laag", "sev_info": "Info",
        "sev_critical": "Kritiek", "sev_warning": "Waarschuwing",
    },
}
MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"],
          "nl": ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
                 "november", "december"]}

# Workstation check codes in the report language (the interface has its own translations).
CHECK_NL = {
    "av_off": "Geen actieve virusscanner: realtime-bescherming van Defender staat uit.",
    "signatures_old": "De virusdefinities van Defender zijn {days} dagen oud.",
    "threats_found": "Defender vond {count} bedreiging(en) in de afgelopen 30 dagen.",
    "firewall_off": "Windows Firewall staat uit voor: {profiles}.",
    "bitlocker_off": "De systeemschijf is niet beveiligd met BitLocker.",
    "luks_off": "Geen versleutelde (LUKS) schijf gevonden.",
    "updates_pending": "{count} update(s) wachten.",
    "no_recent_updates": "De laatste update is {days} dagen geleden geïnstalleerd.",
    "reboot_pending": "Een herstart is nodig om updates af te ronden.",
    "disk_unhealthy": "Schijf {disk} meldt een slechte gezondheid.",
    "disk_worn": "SSD {disk} is {wear}% versleten.",
    "disk_full": "Schijf {mount} heeft nog maar {free_pct}% vrij.",
}

# Optimisation-scan finding codes (customers._finding) in the report language: (finding, suggestion).
FINDING_NL = {
    "node_offline": ("De node is offline (geen agentverbinding).",
                     "Controleer stroom en netwerk, of de mmn-agent-service op de node."),
    "node_mem_high": ("Het geheugengebruik is {pct}%.",
                      "Verplaats een guest naar een rustigere node, verlaag het guestgeheugen of voeg RAM toe."),
    "storage_full": ("Opslag {storage} is {pct}% vol.",
                     "Verwijder oude back-ups, ISO's, snapshots of ongebruikte schijven, of vergroot de opslag."),
    "smart_bad": ("De SMART-status van {disk} ({model}) is {health}.",
                  "Vervang de schijf binnenkort en controleer of de back-ups recent zijn."),
    "cluster_imbalance": ("{busy} gebruikt {busy_pct}% geheugen terwijl {quiet} {quiet_pct}% gebruikt.",
                          "Verplaats {guest} naar {quiet} om het cluster te balanceren."),
    "cluster_imbalance_any": ("{busy} gebruikt {busy_pct}% geheugen terwijl {quiet} {quiet_pct}% gebruikt.",
                              "Verplaats een guest naar {quiet} om het cluster te balanceren."),
    "guest_disk_full": ("De rootschijf is {pct}% vol.",
                        "Ruim logs en caches in de container op of vergroot de schijf."),
    "guest_mem_high": ("Gebruikt {pct}% van het geheugen.", "Geef meer geheugen als hij traag is of swapt."),
    "guests_stopped": ("Gestopt: {names}.",
                       "Verwijder guests die niet meer nodig zijn (na een back-up) om schijfruimte vrij te maken."),
    "machine_offline": ("Offline (geen agentverbinding).", "Controleer de machine en de agent-service."),
    "workstation_offline": ("Offline of in slaapstand.", "Werkplekken worden opnieuw gecontroleerd zodra ze online komen."),
    "updates_pending": ("{count} update(s) wachten.", "Installeer ze in het onderhoudsvenster."),
    "updates_pending_sec": ("{count} update(s) wachten, waarvan {security} beveiligingsupdate(s).",
                            "Installeer ze in het onderhoudsvenster."),
    "reboot_pending": ("Na updates is een herstart nodig.", "Herstart in het onderhoudsvenster."),
    "agent_old": ("Agent {version} is ouder dan {bundled}.", "Werk de agent bij (zonder downtime)."),
    "services_failed": ("{count} mislukte service(s).",
                        "Bekijk de mislukte services en herstart of schakel ze uit."),
    "disk_full": ("Schijf {disk} is {pct}% vol.",
                  "Verwijder oude logs, tijdelijke bestanden en oude back-ups, of vergroot de schijf."),
    "machine_mem_high": ("Het geheugengebruik is {pct}%.",
                         "Zoek het proces dat het geheugen gebruikt, of voeg geheugen toe."),
    "unifi_error": ("De UniFi-verbinding faalt: {error}.", "Controleer de API-sleutel en het adres van de console."),
    "unifi_offline": ("{count} UniFi-apparaat/apparaten offline.",
                      "Controleer stroom, PoE en uplinks van die apparaten."),
    "firmware": ("Firmware-update beschikbaar voor {device} (nu {firmware}).",
                 "Werk de firmware bij op een rustig moment."),
    "guest_isolation_open": ("Het gastnetwerk ({networks}) kan het LAN bereiken.",
                             "Isoleer het gastnetwerk van het LAN met een firewall-zonebeleid."),
    "guest_isolation_partial": ("Het gastnetwerk ({networks}) kan het LAN deels bereiken.",
                                "Isoleer het gastnetwerk van het LAN met een firewall-zonebeleid."),
    "open_alerts": ("{count} openstaande monitoringmelding(en).", "Bekijk de openstaande meldingen."),
    "no_window": ("Voor deze klant is geen onderhoudsvenster ingesteld.",
                  "Spreek een onderhoudsvenster af en stel het in op de klantpagina, zodat updates gepland kunnen worden."),
}
FINDING_TARGET_NL = {"guests_stopped": "{count} gestopte guest(s)"}


def finding_texts(f: dict[str, Any], lang: str) -> tuple[str | None, str, str]:
    """(target, finding, suggestion) of an optimisation-scan finding in the report language."""
    code, params = f.get("code"), f.get("params") or {}
    if lang == "nl" and code in FINDING_NL:
        try:
            fin, sug = (x.format(**params) for x in FINDING_NL[code])
            target = FINDING_TARGET_NL[code].format(**params) if code in FINDING_TARGET_NL else f.get("target")
            return target, fin, sug
        except (KeyError, IndexError, ValueError):
            pass
    return f.get("target"), f.get("finding") or "", f.get("suggestion") or ""


def texts(lang: str) -> dict[str, str]:
    return TEXT.get(lang) or TEXT["en"]


def settings_of(e: Environment) -> dict[str, Any]:
    s = {**DEFAULTS, **(e.report or {})}
    if not s.get("recipients") and e.contact_email:
        s["default_recipients"] = [e.contact_email]
    return s


def recipients_of(e: Environment) -> list[str]:
    s = {**DEFAULTS, **(e.report or {})}
    return list(s.get("recipients") or ([e.contact_email] if e.contact_email else []))


# ------------------------------------------------------------------ periods
async def timezone(db: AsyncSession) -> ZoneInfo:
    name = (await get_setting(db, "schedules")).get("timezone") or "Europe/Amsterdam"
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def month_bounds(period: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    m = PERIOD_RE.match(period or "")
    if not m:
        raise NodeError("period must look like 2026-11")
    y, mo = int(m.group(1)), int(m.group(2))
    start = datetime(y, mo, 1, tzinfo=tz)
    end = datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def previous_period(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz)
    first = local.replace(day=1)
    prev = first - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def current_period(now: datetime, tz: ZoneInfo) -> str:
    local = now.astimezone(tz)
    return f"{local.year:04d}-{local.month:02d}"


def period_label(period: str, lang: str) -> str:
    y, m = period.split("-")
    return f"{MONTHS.get(lang, MONTHS['en'])[int(m) - 1]} {y}"


# ------------------------------------------------------------------ data
def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _check_text(c: dict[str, Any], lang: str) -> str:
    if lang == "nl" and c.get("code") in CHECK_NL:
        args = {k: (", ".join(v) if isinstance(v, list) else v) for k, v in c.items()}
        try:
            return CHECK_NL[c["code"]].format(**args)
        except (KeyError, IndexError):
            pass
    return c.get("text") or c.get("code") or ""


async def _backups(ns: list[Node], start: datetime, end: datetime) -> dict[str, Any]:
    """vzdump tasks of the period, once per cluster (through one online node of it)."""
    from mmn.services import cluster

    out: dict[str, Any] = {"clusters": [], "ok": 0, "failed": 0, "errors": [], "not_backed_up": 0}
    groups: dict[str, list[Node]] = {}
    for n in ns:
        if n.kind == "proxmox":
            groups.setdefault(n.cluster_name or n.id, []).append(n)
    for members in groups.values():
        node = next((n for n in members if agent_hub.is_online(n.id)), None)
        label = members[0].cluster_name or members[0].name
        if node is None:
            out["errors"].append(label)
            continue
        ok = failed = 0
        last: datetime | None = None
        failures: list[dict[str, Any]] = []
        try:
            for m in members:
                tasks = await cluster._get(node.id, f"/nodes/{m.pve_name or m.name}/tasks",
                                           {"typefilter": "vzdump", "since": int(start.timestamp()),
                                            "until": int(end.timestamp()), "limit": 1000}, default=[])
                for t in tasks or []:
                    if not t.get("endtime"):
                        continue
                    if str(t.get("status")) == "OK":
                        ok += 1
                        when = datetime.fromtimestamp(int(t["endtime"]), UTC)
                        last = max(last, when) if last else when
                    else:
                        failed += 1
                        failures.append({"node": m.name, "at": datetime.fromtimestamp(int(t["endtime"]), UTC),
                                         "status": str(t.get("status"))[:200], "guest": t.get("id")})
            missing = await cluster._get(node.id, "/cluster/backup-info/not-backed-up", default=[])
        except Exception as e:  # noqa: BLE001
            log.info("backup tasks of %s: %s", label, e)
            out["errors"].append(label)
            continue
        names = [f"{g.get('vmid')} {g.get('name') or ''}".strip() for g in missing or []]
        out["clusters"].append({"name": label, "ok": ok, "failed": failed, "last_ok": last,
                                "failures": failures[:10], "not_backed_up": names[:30]})
        out["ok"] += ok
        out["failed"] += failed
        out["not_backed_up"] += len(names)
    return out


def _in_period(iso: Any, start: datetime, end: datetime) -> bool:
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    dt = _aware(dt)
    return start <= dt < end


async def build(db: AsyncSession, e: Environment, period: str, lang: str = "en", *,
                live: bool = True) -> dict[str, Any]:
    """Collect the report data of one customer for one month."""
    from mmn.services import gateway

    tz = await timezone(db)
    start, end = month_bounds(period, tz)
    now = datetime.now(UTC)
    stop = min(end, now)
    ns = sorted((await db.execute(select(Node).where(Node.environment_id == e.id))).scalars(), key=lambda n: ({"proxmox": 0, "server": 1, "workstation": 2}[customers.machine_type(n)],
                                         n.name.lower()))
    ids = [n.id for n in ns]
    names = {n.id: n.name for n in ns}
    mon = await get_setting(db, "monitoring")
    offline_minutes = int((mon or {}).get("offline_minutes") or 5)

    alerts = []
    if ids:
        alerts = list((await db.execute(select(Alert).where(
            Alert.node_id.in_(ids), Alert.started_at < end,
            (Alert.resolved_at.is_(None)) | (Alert.resolved_at >= start)).order_by(Alert.started_at))).scalars())

    # Availability of servers and Proxmox VE nodes (workstations sleep and are left out).
    avail = []
    for n in ns:
        t = customers.machine_type(n)
        if t == "workstation":
            continue
        since = max(start, _aware(n.enrolled_at) or start)
        total = max(1.0, (stop - since).total_seconds())
        down = 0.0
        incidents = 0
        for a in alerts:
            if a.node_id != n.id or a.kind != "offline":
                continue
            a0 = _aware(a.started_at) - timedelta(minutes=offline_minutes)
            a1 = _aware(a.resolved_at) or now
            o = _overlap(a0, a1, since, stop)
            if o:
                down += o
                incidents += 1
        if since >= stop:
            continue
        avail.append({"machine": n.name, "type": t, "availability": round(100 * (1 - down / total), 2),
                      "downtime_minutes": int(down // 60), "outages": incidents})
    avg_avail = round(sum(a["availability"] for a in avail) / len(avail), 2) if avail else None

    # Incidents: every alert of the period, except workstations that were switched off.
    ws_ids = {n.id for n in ns if customers.machine_type(n) == "workstation"}
    inc = []
    for a in alerts:
        if a.node_id in ws_ids and a.kind == "offline":
            continue
        s0, s1 = _aware(a.started_at), _aware(a.resolved_at)
        inc.append({"machine": names.get(a.node_id or "", "?"), "kind": a.kind, "severity": a.severity,
                    "message": a.message, "started_at": s0, "resolved_at": s1,
                    "duration_minutes": int(((s1 or now) - s0).total_seconds() // 60)})
    by_kind: dict[str, int] = {}
    for i in inc:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1

    # Updates: package jobs that finished in the period, plus Windows Update / apt history.
    jobs = []
    if ids:
        jobs = list((await db.execute(select(NodeJob).where(
            NodeJob.node_id.in_(ids), NodeJob.kind == "updates.apply", NodeJob.status == "success",
            NodeJob.finished_at >= start, NodeJob.finished_at < end))).scalars())
    upd_rows = []
    total_installed = 0
    for n in ns:
        njobs = [j for j in jobs if j.node_id == n.id]
        installed = 0
        for j in njobs:
            r = j.result or {}
            installed += int(r.get("upgraded") or r.get("installed") or len(r.get("packages") or []) or 0)
        hist = [h for h in ((n.inventory or {}).get("updates") or {}).get("history") or []
                if _in_period(h.get("date"), start, end) and h.get("result") in ("Succeeded", "SucceededWithErrors")]
        if not njobs:
            installed += sum(int(h.get("packages") or 1) for h in hist)
        pending = len((n.updates or {}).get("packages") or [])
        reboot = bool((n.updates or {}).get("reboot_required")
                      or (((n.inventory or {}).get("updates") or {}).get("reboot_pending")))
        if installed or njobs or pending or reboot:
            upd_rows.append({"machine": n.name, "type": customers.machine_type(n), "installed": installed,
                             "runs": len(njobs), "pending": pending, "reboot": reboot,
                             "titles": [h.get("title") for h in hist][:5]})
        total_installed += installed

    # Workstations and security.
    ws = []
    sec: list[dict[str, Any]] = []
    for n in ns:
        if customers.machine_type(n) != "workstation":
            continue
        s = workstations.summary(n)
        items = workstations.checks(n)
        bad = [c for c in items if c["status"] in ("warning", "critical")]
        ws.append({"machine": n.name, "os": s["os"], "user": s["user"], "status": s["security"] or "unknown",
                   "last_seen": _aware(n.last_seen_at), "issues": [_check_text(c, lang) for c in bad][:6]})
        for c in bad:
            sec.append({"severity": "high" if c["status"] == "critical" else "medium", "machine": n.name,
                        "text": _check_text(c, lang)})

    scan = await customers.optimisation_scan(db, e, live=False)
    recs = [f for f in scan["findings"] if f["severity"] in ("high", "medium", "low")][:15]
    for f in scan["findings"]:
        if f["area"] in ("network", "hardware", "disk") and f["severity"] == "high":
            target, text, _sug = finding_texts(f, lang)
            sec.append({"severity": "high", "machine": target, "text": text})

    # AI actions (MCP) on this customer's machines or the customer itself.
    targets = {n.name for n in ns} | {e.name, e.id}
    ai_rows = []
    total_calls = 0
    by_risk: dict[str, int] = {}
    rows = (await db.execute(select(AuditLog).where(AuditLog.source == "mcp", AuditLog.at >= start,
                                                    AuditLog.at < end).order_by(AuditLog.at))).scalars()
    for r in rows:
        args = (r.detail or {}).get("args") or {}
        hit = (r.target in targets) or str(args.get("customer") or "") in targets or \
            any(str(x) in targets for x in (args.get("servers") or []) if isinstance(args.get("servers"), list))
        if not hit:
            continue
        tool = (r.detail or {}).get("tool") or r.action.removeprefix("mcp.")
        spec = gateway.REGISTRY.get(tool)
        risk = spec.risk if spec else "change"
        total_calls += 1
        by_risk[risk] = by_risk.get(risk, 0) + 1
        if risk != "read":
            ai_rows.append({"at": _aware(r.at), "tool": tool, "target": r.target, "risk": risk,
                            "by": (r.detail or {}).get("token") or r.username, "success": r.success})
    appr = list((await db.execute(select(AiApproval).where(AiApproval.created_at >= start,
                                                           AiApproval.created_at < end))).scalars())
    appr = [a for a in appr if e.id in (a.customer_ids or [])]
    approvals = {"total": len(appr), "approved": sum(1 for a in appr if a.status in ("executed", "approved", "running")),
                 "rejected": sum(1 for a in appr if a.status == "rejected"),
                 "expired": sum(1 for a in appr if a.status == "expired")}

    backups = await _backups(ns, start, end) if live else {"clusters": [], "ok": 0, "failed": 0, "errors": [],
                                                            "not_backed_up": 0}

    crit = sum(1 for s in sec if s["severity"] == "high") + backups["failed"]
    status = "critical" if crit else ("warning" if sec or recs or inc else "ok")
    general = await get_setting(db, "general")
    return {
        "version": 1, "lang": lang, "period": period, "period_label": period_label(period, lang),
        "start": start, "end": end, "generated_at": now, "app": general.get("app_name") or "Stackpilot",
        "customer": {"id": e.id, "name": e.name, "contact": e.contact_name, "email": e.contact_email,
                     "sla": e.sla, "maintenance_window": e.maintenance_window},
        "status": status,
        "summary": {"machines": {"proxmox": sum(1 for n in ns if n.kind == "proxmox"),
                                 "servers": sum(1 for n in ns if customers.machine_type(n) == "server"),
                                 "workstations": len(ws)},
                    "availability": avg_avail, "incidents": len(inc), "updates_installed": total_installed,
                    "backups_ok": backups["ok"], "backups_failed": backups["failed"], "security": len(sec),
                    "ai_calls": total_calls, "ai_changes": len(ai_rows)},
        "availability": avail,
        "incidents": {"items": inc[-40:], "by_kind": by_kind, "total": len(inc)},
        "updates": {"machines": upd_rows, "installed": total_installed},
        "backups": backups,
        "workstations": ws,
        "security": sec[:40],
        "ai": {"calls": total_calls, "by_risk": by_risk, "changes": ai_rows[-40:], "approvals": approvals},
        "recommendations": [dict(zip(("severity", "target", "finding", "suggestion"),
                                     (f["severity"], *finding_texts(f, lang)), strict=True)) for f in recs],
    }


# ------------------------------------------------------------------ rendering
def _fmt_dt(v: Any, tz: ZoneInfo) -> str:
    if not v:
        return "–"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return _aware(v).astimezone(tz).strftime("%d-%m-%Y %H:%M")


def render_html(data: dict[str, Any], tz: ZoneInfo) -> str:
    lang = data.get("lang") or "en"
    T = texts(lang)
    return _jinja.get_template("customer_report.html").render(
        d=data, T=T, lang=lang, dt=lambda v: _fmt_dt(v, tz), footer=T["footer"].format(app=data.get("app")))


_PDF_MAP = str.maketrans({"–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "•": "*",
                          "→": "->", "✓": "v", " ": " "})


def _latin(s: Any) -> str:
    return str(s if s is not None else "").translate(_PDF_MAP).encode("latin-1", "replace").decode("latin-1")


NACHT = (20, 33, 61)
KORAAL = (255, 107, 87)
MUTED = (79, 91, 115)
LINE = (220, 225, 235)
SOFT = (246, 247, 251)
TONE = {"ok": (28, 138, 87), "warning": (168, 107, 0), "critical": (194, 52, 52), "unknown": MUTED,
        "high": (194, 52, 52), "medium": (168, 107, 0), "low": MUTED, "info": MUTED}


def render_pdf(data: dict[str, Any], tz: ZoneInfo) -> bytes:
    """The same report as a PDF (A4) in the house style: navy header, coral accent, tables."""
    from fpdf import FPDF

    T = texts(data.get("lang") or "en")
    L = _latin

    class PDF(FPDF):
        def header(self) -> None:
            self.set_fill_color(*NACHT)
            self.rect(0, 0, 210, 18, "F")
            self.set_xy(12, 5)
            self.set_text_color(255, 255, 255)
            self.set_font("Helvetica", "B", 13)
            self.cell(0, 8, L(data.get("app")), new_x="LMARGIN", new_y="NEXT")
            self.set_fill_color(*KORAAL)
            self.ellipse(12 + self.get_string_width(L(data.get("app"))) + 2, 8.2, 2.2, 2.2, "F")
            self.set_xy(110, 5)
            self.set_font("Helvetica", "", 9)
            self.cell(88, 8, L(f"{data['customer']['name']} · {data['period_label']}".replace("·", "-")),
                      align="R")
            self.set_y(24)

        def footer(self) -> None:
            self.set_y(-14)
            self.set_draw_color(*LINE)
            self.line(12, self.get_y(), 198, self.get_y())
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTED)
            self.cell(0, 8, L(T["footer"].format(app=data.get("app"))), align="L")
            self.cell(0, 8, f"{self.page_no()}/{{nb}}", align="R")

    pdf = PDF(format="A4")
    pdf.set_margins(12, 22, 12)
    pdf.set_auto_page_break(True, margin=18)
    pdf.alias_nb_pages()
    pdf.add_page()

    def h1(text: str) -> None:
        pdf.set_text_color(*NACHT)
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(0, 10, L(text), new_x="LMARGIN", new_y="NEXT")

    def h2(text: str) -> None:
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.ln(4)
        pdf.set_fill_color(*KORAAL)
        pdf.rect(12, pdf.get_y() + 1.5, 1.4, 5, "F")
        pdf.set_x(15)
        pdf.set_text_color(*NACHT)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, L(text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def para(text: str, muted: bool = False, size: int = 10) -> None:
        pdf.set_text_color(*(MUTED if muted else NACHT))
        pdf.set_font("Helvetica", "", size)
        pdf.multi_cell(0, 5, L(text), align="L", new_x="LMARGIN", new_y="NEXT")

    def table(headers: list[str], rows: list[list[Any]], widths: list[float], tones: list[str | None] | None = None,
              tone_col: int | None = None) -> None:
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_fill_color(*SOFT)
        pdf.set_text_color(*MUTED)
        pdf.set_draw_color(*LINE)
        for h, w in zip(headers, widths, strict=True):
            pdf.cell(w, 7, L(h), border="B", fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8.5)
        for i, r in enumerate(rows):
            lines = max(len(pdf.multi_cell(w, 5, L(v), align="L", dry_run=True, output="LINES"))
                         for v, w in zip(r, widths, strict=True))
            h = 5 * lines + 1
            if pdf.get_y() + h > pdf.page_break_trigger:
                pdf.add_page()
            y0 = pdf.get_y()
            x = 12.0
            pdf.set_auto_page_break(False)
            for j, (v, w) in enumerate(zip(r, widths, strict=True)):
                pdf.set_xy(x, y0)
                tone = tones[i] if tones and tone_col == j else None
                pdf.set_text_color(*(TONE.get(tone or "", NACHT) if tone else NACHT))
                pdf.set_font("Helvetica", "B" if tone else "", 8.5)
                pdf.multi_cell(w, 5, L(v), align="L", new_x="RIGHT", new_y="TOP")
                x += w
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_auto_page_break(True, margin=18)
            pdf.set_xy(12, y0 + h)
            pdf.set_draw_color(*LINE)
            pdf.line(12, pdf.get_y(), 12 + sum(widths), pdf.get_y())
            pdf.ln(1)

    s = data["summary"]
    h1(T["title"])
    para(f"{T['customer']}: {data['customer']['name']}    {T['period']}: {data['period_label']}    "
         f"{T['generated']}: {_fmt_dt(data['generated_at'], tz)}", muted=True, size=9)
    pdf.ln(3)
    # Summary tiles
    tiles = [
        (T["k_machines"], f"{s['machines']['proxmox'] + s['machines']['servers'] + s['machines']['workstations']}"),
        (T["k_availability"], f"{s['availability']:.2f}%" if s["availability"] is not None else "–"),
        (T["k_incidents"], str(s["incidents"])),
        (T["k_updates"], str(s["updates_installed"])),
        (T["k_backups"], f"{s['backups_ok']} / {s['backups_failed']}"),
        (T["k_security"], str(s["security"])),
    ]
    w = (186 - 5 * 4) / 6
    y = pdf.get_y()
    for i, (label, value) in enumerate(tiles):
        x = 12 + i * (w + 4)
        pdf.set_fill_color(*SOFT)
        pdf.set_draw_color(*LINE)
        pdf.rect(x, y, w, 20, "DF")
        pdf.set_xy(x + 2, y + 2)
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(w - 4, 3.2, L(label), align="L")
        pdf.set_xy(x + 2, y + 11)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*NACHT)
        pdf.cell(w - 4, 7, L(value))
    pdf.set_y(y + 24)
    st = data.get("status") or "ok"
    pdf.set_text_color(*TONE.get(st, NACHT))
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, L(T[f"overall_{st}"]), new_x="LMARGIN", new_y="NEXT")

    h2(T["availability"])
    if data["availability"]:
        table([T["machine"], T["type"], T["uptime"], T["downtime"], T["count"]],
              [[a["machine"], T.get(a["type"], a["type"]), f"{a['availability']:.2f}%",
                f"{a['downtime_minutes']} {T['minutes']}", a["outages"]] for a in data["availability"]],
              [62, 34, 30, 32, 28],
              ["ok" if a["availability"] >= 99.5 else ("warning" if a["availability"] >= 98 else "critical")
               for a in data["availability"]], 2)
    else:
        para("–", muted=True)

    h2(T["incidents"])
    inc = data["incidents"]
    if inc["items"]:
        table([T["when"], T["machine"], T["severity"], T["what"], T["duration"]],
              [[_fmt_dt(i["started_at"], tz), i["machine"], T.get(f"sev_{i['severity']}", i["severity"]), i["message"],
                f"{i['duration_minutes']} {T['minutes']}"] for i in inc["items"]],
              [30, 34, 26, 70, 26], [("critical" if i["severity"] == "critical" else "warning") for i in inc["items"]], 2)
    else:
        para(T["none_incidents"], muted=True)

    h2(T["updates"])
    if data["updates"]["machines"]:
        table([T["machine"], T["installed"], T["pending"], T["reboot"]],
              [[u["machine"], u["installed"], u["pending"], T["yes"] if u["reboot"] else T["no"]]
               for u in data["updates"]["machines"]], [80, 36, 36, 34])
    else:
        para(T["none_updates"], muted=True)

    h2(T["backups"])
    b = data["backups"]
    if b["clusters"]:
        table([T["machine"], T["ok"], T["failed"], T["last_seen"], T["not_backed_up"]],
              [[c["name"], c["ok"], c["failed"], _fmt_dt(c["last_ok"], tz), len(c["not_backed_up"])] for c in b["clusters"]],
              [56, 22, 22, 40, 46], ["critical" if c["failed"] else "ok" for c in b["clusters"]], 2)
    else:
        para(T["none_backups"], muted=True)
    if b.get("errors"):
        para(f"{T['backups_error']}: {', '.join(b['errors'])}", muted=True, size=8)

    h2(T["workstations"])
    if data["workstations"]:
        table([T["machine"], "OS", T["user"], T["status"], T["issues"]],
              [[x["machine"], x["os"] or "–", x["user"] or "–", T.get(f"st_{x['status']}", x["status"]),
                " ".join(x["issues"]) or "–"] for x in data["workstations"]],
              [32, 38, 30, 22, 64], [x["status"] for x in data["workstations"]], 3)
    else:
        para(T["none_ws"], muted=True)

    h2(T["security"])
    if data["security"]:
        table([T["severity"], T["machine"], T["what"]],
              [[T.get(f"sev_{x['severity']}", x["severity"]), x["machine"], x["text"]] for x in data["security"]],
              [26, 44, 116], [x["severity"] for x in data["security"]], 0)
    else:
        para(T["none_security"], muted=True)

    ai = data["ai"]
    h2(T["ai"])
    para(f"{T['ai_calls']}: {ai['calls']}  ·  {T['ai_changes']}: {len(ai['changes'])}  ·  "
         f"{T['ai_approvals']}: {ai['approvals']['total']} ({T['ai_rejected']}: {ai['approvals']['rejected']})"
         .replace("·", "-"), size=9)
    if ai["changes"]:
        table([T["when"], T["tool"], T["target"], T["by"], T["result"]],
              [[_fmt_dt(c["at"], tz), c["tool"], c["target"] or "–", c["by"] or "–", T["ok"] if c["success"] else T["failed"]]
               for c in ai["changes"]], [30, 46, 46, 40, 24], ["ok" if c["success"] else "critical" for c in ai["changes"]], 4)
    else:
        para(T["none_ai"], muted=True)

    h2(T["recommendations"])
    if data["recommendations"]:
        table([T["severity"], T["target"], T["what"]],
              [[T.get(f"sev_{r['severity']}", r["severity"]), r["target"] or "–", f"{r['finding']} {r['suggestion']}"]
               for r in data["recommendations"]], [26, 44, 116], [r["severity"] for r in data["recommendations"]], 0)
    else:
        para(T["none_rec"], muted=True)
    return bytes(pdf.output())


# ------------------------------------------------------------------ store, list, send
def report_out(r: CustomerReport, *, with_data: bool = False) -> dict[str, Any]:
    d = {"id": r.id, "customer_id": r.environment_id, "period": r.period, "period_start": r.period_start,
         "period_end": r.period_end, "lang": r.lang, "trigger": r.trigger, "created_by": r.created_by,
         "created_at": r.created_at, "sent_to": r.sent_to or [], "sent_at": r.sent_at, "send_error": r.send_error,
         "status": (r.data or {}).get("status"), "summary": (r.data or {}).get("summary"),
         "has_pdf": bool(r.pdf)}
    if with_data:
        d["data"] = r.data
    return d


def _jsonable(v: Any) -> Any:
    import json

    return json.loads(json.dumps(v, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)))


async def generate(db: AsyncSession, e: Environment, period: str, *, lang: str | None = None, trigger: str = "manual",
                   created_by: str | None = None, live: bool = True) -> CustomerReport:
    s = settings_of(e)
    lang = lang if lang in TEXT else (s.get("lang") if s.get("lang") in TEXT else "en")
    tz = await timezone(db)
    data = await build(db, e, period, lang, live=live)
    html = render_html(data, tz)
    try:
        pdf = render_pdf(data, tz)
    except Exception as ex:  # noqa: BLE001 - the HTML report is still useful
        log.exception("PDF of report %s %s failed", e.name, period)
        pdf = None
        data["pdf_error"] = str(ex)[:300]
    r = CustomerReport(environment_id=e.id, period=period, period_start=data["start"], period_end=data["end"],
                       lang=lang, trigger=trigger, created_by=created_by, data=_jsonable(data), html=html, pdf=pdf)
    db.add(r)
    await db.flush()
    return r


async def list_reports(db: AsyncSession, environment_id: str, limit: int = 36) -> list[CustomerReport]:
    return list((await db.execute(select(CustomerReport).where(CustomerReport.environment_id == environment_id)
                                  .order_by(CustomerReport.created_at.desc()).limit(limit))).scalars())


async def get_report(db: AsyncSession, report_id: str) -> CustomerReport:
    r = await db.get(CustomerReport, report_id)
    if r is None:
        raise NodeError("report not found")
    return r


async def latest(db: AsyncSession, environment_id: str, period: str | None = None) -> CustomerReport | None:
    q = select(CustomerReport).where(CustomerReport.environment_id == environment_id)
    if period:
        q = q.where(CustomerReport.period == period)
    return (await db.execute(q.order_by(CustomerReport.created_at.desc()).limit(1))).scalars().first()


def filename(r: CustomerReport, customer: str, ext: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", customer).strip("-") or "customer"
    return f"it-report-{safe}-{r.period}.{ext}"


async def send(db: AsyncSession, r: CustomerReport, e: Environment, to: list[str] | None = None) -> list[str]:
    from mmn.services.mailer import MailContent, send_mail

    recipients = [x.strip() for x in (to or recipients_of(e)) if x and "@" in x][:20]
    if not recipients:
        raise NodeError("no recipients: set report recipients or a contact email on the customer")
    T = texts(r.lang)
    label = period_label(r.period, r.lang)
    # The customer's contacts have no account in Stackpilot: the report goes along as attachments.
    atts = [(filename(r, e.name, "html"), "text/html", r.html.encode())]
    if r.pdf:
        atts.insert(0, (filename(r, e.name, "pdf"), "application/pdf", bytes(r.pdf)))
    content = MailContent(subject=T["mail_subject"].format(customer=e.name, period=label),
                          title=T["mail_title"].format(period=label),
                          paragraphs=[T["mail_body"].format(customer=e.name, period=label)],
                          muted=[T[f"overall_{(r.data or {}).get('status') or 'ok'}"]],
                          button_url=None, button_label=None)
    try:
        await send_mail(db, recipients, content, r.lang, attachments=atts)
    except Exception as ex:
        r.send_error = str(ex)[:500]
        raise
    r.sent_to = recipients
    r.sent_at = datetime.now(UTC)
    r.send_error = None
    return recipients


async def run_schedule(now: datetime | None = None) -> list[str]:
    """Scheduler (daily): on each customer's report day, make last month's report (once) and mail it."""
    from mmn.db import sessionmaker
    from mmn.services import tenancy

    now = now or datetime.now(UTC)
    done = []
    async with sessionmaker()() as db:
        with tenancy.unrestricted():
            tz = await timezone(db)
            local = now.astimezone(tz)
            period = previous_period(now, tz)
            envs = list((await db.execute(select(Environment))).scalars())
            for e in envs:
                s = settings_of(e)
                if not s.get("enabled"):
                    continue
                day = min(max(int(s.get("day") or 1), 1), 28)
                if local.day < day:
                    continue
                if await latest_scheduled(db, e.id, period):
                    continue
                try:
                    r = await generate(db, e, period, trigger="schedule", created_by="scheduler")
                    await db.commit()
                    try:
                        await send(db, r, e)
                    except Exception as ex:  # noqa: BLE001
                        log.warning("report %s %s not mailed: %s", e.name, period, ex)
                    await db.commit()
                    done.append(e.name)
                except Exception:  # noqa: BLE001
                    log.exception("report for %s failed", e.name)
                    await db.rollback()
    return done


async def latest_scheduled(db: AsyncSession, environment_id: str, period: str) -> CustomerReport | None:
    return (await db.execute(select(CustomerReport).where(
        CustomerReport.environment_id == environment_id, CustomerReport.period == period,
        CustomerReport.trigger == "schedule").limit(1))).scalars().first()
