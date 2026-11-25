"""Nightly checks and the morning summary (phase 12).

Every night Stackpilot checks every customer: pending updates and reboots, security updates with
the CVEs they fix (looked up at OSV.dev), full disks and storage, SMART health, certificates that
expire (on the machines through the agent, and TLS endpoints the admin lists per customer), backups
that failed or are missing, and the rest of the optimisation scan. Findings are compared with the
night before (new, resolved). Where a fix is a single tool call (install updates, reboot, update the
agent, clean up a disk, renew a certificate) the finding gets a proposal: an approval request of the
AI gateway (phase 8) that an admin approves with one click. Changes then wait for the customer's
maintenance window. In the morning one summary goes out in the app and by mail.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import AiApproval, Environment, NightlyFinding, NightlyRun, Node
from mmn.services import agent_hub, events
from mmn.services.settings_store import get_setting

log = logging.getLogger("mmn.nightly")

CATEGORIES = ("updates", "cves", "disks", "certificates", "backups", "identity", "other")
SEVERITIES = ("high", "medium", "low", "info")
_SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}
SOURCE = "nightly"
LABEL = "Nightly checks"

_CODE_CATEGORY = {
    "updates_pending": "updates", "updates_pending_sec": "updates", "reboot_pending": "updates",
    "agent_old": "updates", "firmware": "updates",
    "cve_fixable": "cves", "security_updates": "cves", "windows_security": "cves",
    "disk_full": "disks", "storage_full": "disks", "guest_disk_full": "disks", "smart_bad": "disks",
    "pbs_full": "disks",
    "cert_expired": "certificates", "cert_expiring": "certificates", "cert_endpoint_error": "certificates",
}


def category_of(f: dict[str, Any]) -> str:
    code = f.get("code") or ""
    if code in _CODE_CATEGORY:
        return _CODE_CATEGORY[code]
    if f.get("area") == "backups" or code.startswith("backup_") or code.startswith("machine_backup"):
        return "backups"
    if f.get("area") == "identity" or code.startswith("m365_"):
        return "identity"
    return "other"


def finding_key(f: dict[str, Any]) -> str:
    """Stable over nights: the same problem on the same target has the same key."""
    raw = f"{f.get('code') or f.get('finding')}|{f.get('target') or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


async def settings(db: AsyncSession) -> dict[str, Any]:
    from mmn.services.settings_store import DEFAULTS

    cfg = await get_setting(db, "nightly")
    cfg["checks"] = {**DEFAULTS["nightly"]["checks"], **(cfg.get("checks") or {})}
    return cfg


def parse_hhmm(text: str | None, default: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", default)
    return int(m.group(1)), int(m.group(2))


async def _tz(db: AsyncSession) -> ZoneInfo:
    tz = (await get_setting(db, "schedules")).get("timezone") or "Europe/Amsterdam"
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


# ------------------------------------------------------------------ security updates and CVEs
_WIN_SEC = re.compile(r"critical|important|security", re.I)


def security_packages(n: Node) -> list[dict[str, Any]]:
    """Pending updates that are security updates: apt/dnf packages from a security suite, Windows
    updates with an MSRC severity or in the Security Updates category."""
    pkgs = (n.updates or {}).get("packages") or []
    if n.kind == "windows":
        return [p for p in pkgs if _WIN_SEC.search(str(p.get("suite") or ""))]
    return [p for p in pkgs if p.get("security") or "security" in str(p.get("suite") or "").lower()]


def dpkg_script(names: list[str]) -> str:
    safe = [x for x in names if re.fullmatch(r"[a-z0-9][a-z0-9+.\-:]*", x)]
    return ("cat /etc/os-release 2>/dev/null; echo '---'; dpkg-query -W -f='${binary:Package}\\t${source:Package}\\n' "
            + " ".join(safe) + " 2>/dev/null")


def parse_dpkg(stdout: str) -> tuple[dict[str, str], dict[str, str]]:
    """(os-release fields, {binary package: source package})."""
    head, _, tail = stdout.partition("---")
    osr: dict[str, str] = {}
    for line in head.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            osr[k.strip()] = v.strip().strip('"')
    src: dict[str, str] = {}
    for line in tail.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 2 and parts[0]:
            src[parts[0].split(":")[0]] = parts[1].split(" ")[0] or parts[0].split(":")[0]
    return osr, src


def osv_ecosystem(osr: dict[str, str]) -> str | None:
    os_id, ver = (osr.get("ID") or "").lower(), osr.get("VERSION_ID") or ""
    if os_id == "debian" and ver:
        return f"Debian:{ver.split('.')[0]}"
    if os_id == "ubuntu" and ver:
        return f"Ubuntu:{ver}:LTS" if "LTS" in (osr.get("VERSION") or "") else f"Ubuntu:{ver}"
    return None


def vuln_name(vid: str) -> str:
    """OSV ids to the names people know: DEBIAN-CVE-2025-1234 and UBUNTU-CVE-... become CVE-2025-1234."""
    m = re.search(r"CVE-\d{4}-\d+", vid)
    return m.group(0) if m else vid


OSV_URL = "https://api.osv.dev/v1/querybatch"


async def osv_fixed(ecosystem: str, items: list[tuple[str, str, str]],
                    cache: dict[tuple, frozenset[str]]) -> dict[str, set[str]]:
    """For (source package, installed version, new version): the vulnerabilities that affect the
    installed version but not the new one, i.e. the ones the pending update fixes."""
    import httpx

    queries: list[tuple[tuple, dict[str, Any]]] = []
    for src, cur, new in items:
        for ver in (cur, new):
            key = (ecosystem, src, ver)
            if key not in cache and ver:
                queries.append((key, {"package": {"name": src, "ecosystem": ecosystem}, "version": ver}))
    seen: set[tuple] = set()
    queries = [q for q in queries if not (q[0] in seen or seen.add(q[0]))]
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(queries), 500):
            chunk = queries[i:i + 500]
            r = await client.post(OSV_URL, json={"queries": [q[1] for q in chunk]})
            r.raise_for_status()
            results = r.json().get("results") or []
            for (key, _), res in zip(chunk, results, strict=False):
                cache[key] = frozenset(v.get("id") for v in (res or {}).get("vulns") or [] if v.get("id"))
    out: dict[str, set[str]] = {}
    for src, cur, new in items:
        fixed = set(cache.get((ecosystem, src, cur), frozenset())) - set(cache.get((ecosystem, src, new), frozenset()))
        if fixed:
            out.setdefault(src, set()).update(vuln_name(v) for v in fixed)
    return out


async def cve_findings(n: Node, cfg: dict[str, Any], cache: dict[tuple, frozenset[str]], finding) -> list[dict[str, Any]]:
    sec = security_packages(n)
    if not sec:
        return []
    if n.kind == "windows":
        critical = sum(1 for p in sec if "critical" in str(p.get("suite") or "").lower())
        kbs = ", ".join(str(p.get("name")) for p in sec[:8]) + (" …" if len(sec) > 8 else "")
        return [finding("high" if critical else "medium", "security", n.name,
                        f"{len(sec)} security update(s) pending ({critical} critical): {kbs}.",
                        "Install them in the maintenance window.", "install_node_updates",
                        code="windows_security", count=len(sec), critical=critical, names=kbs)]
    names = ", ".join(str(p.get("name")) for p in sec[:8]) + (" …" if len(sec) > 8 else "")
    vulns: set[str] = set()
    if cfg.get("cve_lookup") and agent_hub.is_online(n.id) and (n.updates or {}).get("packages"):
        try:
            res = await agent_hub.request(n.id, "exec", {"command": dpkg_script([p["name"] for p in sec]),
                                                         "timeout": 60}, timeout=90) or {}
            osr, src = parse_dpkg(res.get("stdout") or "")
            eco = osv_ecosystem(osr)
            if eco:
                items = [(src.get(p["name"], p["name"]), p.get("current") or "", p.get("new") or "") for p in sec]
                for ids in (await osv_fixed(eco, items, cache)).values():
                    vulns |= ids
        except Exception as e:  # noqa: BLE001 - the lookup is optional; the finding stays without CVE numbers
            log.info("CVE lookup for %s skipped: %s", n.name, e)
    if vulns:
        cves = sorted(vulns, key=lambda v: (not v.startswith("CVE-"), v))
        shown = ", ".join(cves[:8]) + (" …" if len(cves) > 8 else "")
        return [finding("high", "security", n.name,
                        f"{len(sec)} security update(s) fix {len(cves)} known vulnerabilities: {shown}.",
                        "Install the updates in the maintenance window.", "install_node_updates",
                        code="cve_fixable", count=len(sec), cves=len(cves), list=shown, names=names)]
    return [finding("medium", "security", n.name, f"{len(sec)} security update(s) pending: {names}.",
                    "Install them in the maintenance window.", "install_node_updates",
                    code="security_updates", count=len(sec), names=names)]


# ------------------------------------------------------------------ certificates
LINUX_CERT_SCRIPT = r"""set +e
files=""
for f in /etc/pve/local/pveproxy-ssl.pem /etc/pve/local/pve-ssl.pem /etc/proxmox-backup/proxy.pem /etc/letsencrypt/live/*/cert.pem; do
  [ -f "$f" ] && files="$files
$f"
done
for d in /etc/nginx /etc/apache2 /etc/httpd; do
  [ -d "$d" ] && files="$files
$(grep -rhoE '^[[:space:]]*(ssl_certificate|SSLCertificateFile)[[:space:]]+[^;#]+' "$d" 2>/dev/null | awk '{print $2}' | tr -d '";')"
done
printf '%s\n' "$files" | sort -u | while read -r f; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  end=$(openssl x509 -noout -enddate -in "$f" 2>/dev/null | cut -d= -f2)
  subj=$(openssl x509 -noout -subject -nameopt RFC2253 -in "$f" 2>/dev/null | sed 's/^subject= *//')
  [ -n "$end" ] && printf 'CERT\t%s\t%s\t%s\n' "$f" "$end" "$subj"
done
command -v certbot >/dev/null 2>&1 && printf 'RENEW\tcertbot\n'
command -v pvenode >/dev/null 2>&1 && pvenode config get --property acme 2>/dev/null | grep -q . && printf 'RENEW\tpve-acme\n'
exit 0
"""

WINDOWS_CERT_SCRIPT = r"""$ErrorActionPreference = 'SilentlyContinue'
Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.HasPrivateKey } | ForEach-Object {
  "CERT`t$($_.Thumbprint)`t$($_.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))`t$($_.Subject)"
}
"""


def _parse_date(text: str) -> datetime | None:
    text = " ".join(text.split())
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _cn(subject: str) -> str:
    m = re.search(r"CN=([^,]+)", subject or "")
    return m.group(1).strip() if m else (subject or "").strip()


def parse_certs(stdout: str) -> tuple[list[dict[str, Any]], set[str]]:
    """(certificates, renew tools). Of certificates with the same subject (a renewed one next to
    the old one) only the newest counts."""
    certs: list[dict[str, Any]] = []
    renew: set[str] = set()
    for line in stdout.splitlines():
        parts = line.rstrip("\r").split("\t")
        if parts[0] == "RENEW" and len(parts) > 1:
            renew.add(parts[1].strip())
        elif parts[0] == "CERT" and len(parts) >= 3:
            end = _parse_date(parts[2])
            if end:
                certs.append({"where": parts[1].strip(), "not_after": end, "subject": parts[3].strip() if len(parts) > 3 else ""})
    newest: dict[str, dict[str, Any]] = {}
    for c in certs:
        k = c["subject"] or c["where"]
        if k not in newest or c["not_after"] > newest[k]["not_after"]:
            newest[k] = c
    # Files on Linux are each their own certificate (a renewed file replaces the old one).
    by_file = [c for c in certs if c["where"].startswith("/")]
    return (by_file if by_file else list(newest.values())), renew


def cert_finding(target: str, name: str, not_after: datetime, warn_days: int, now: datetime, finding,
                 renew: str | None = None) -> dict[str, Any] | None:
    days = (not_after - now).total_seconds() / 86400
    date = not_after.strftime("%Y-%m-%d")
    if days < 0:
        f = finding("high", "certificates", target, f"Certificate {name} expired on {date}.",
                    "Renew or replace the certificate.", "run_command" if renew else None,
                    code="cert_expired", name=name, date=date)
    elif days <= warn_days:
        f = finding("high" if int(days) <= 7 else "medium", "certificates", target,
                    f"Certificate {name} expires in {int(days)} day(s), on {date}.",
                    "Renew the certificate before it expires." + (" Automatic renewal seems not to work." if renew else ""),
                    "run_command" if renew else None, code="cert_expiring", name=name, date=date, days=int(days))
    else:
        return None
    if renew:
        f["renew"] = renew
    return f


async def machine_cert_findings(n: Node, warn_days: int, now: datetime, finding) -> list[dict[str, Any]]:
    from mmn.services.customers import machine_type

    if not agent_hub.is_online(n.id) or machine_type(n) == "workstation":
        return []
    script = WINDOWS_CERT_SCRIPT if n.kind == "windows" else LINUX_CERT_SCRIPT
    try:
        res = await agent_hub.request(n.id, "exec", {"command": script, "timeout": 60}, timeout=90) or {}
    except Exception as e:  # noqa: BLE001
        log.info("certificate check on %s skipped: %s", n.name, e)
        return []
    certs, renew = parse_certs(res.get("stdout") or "")
    out = []
    for c in certs:
        how = None
        if c["where"].startswith("/etc/letsencrypt/") and "certbot" in renew:
            how = "certbot"
        elif c["where"] == "/etc/pve/local/pveproxy-ssl.pem" and "pve-acme" in renew:
            how = "pve-acme"
        name = _cn(c["subject"]) or c["where"]
        if c["where"].startswith("/"):
            name = f"{name} ({c['where']})" if name != c["where"] else name
        f = cert_finding(n.name, name, c["not_after"], warn_days, now, finding, how)
        if f:
            out.append(f)
    return out


def _host_port(text: str) -> tuple[str, int] | None:
    """"host", "host:8443", "https://host/path" or "[::1]:443" to (host, port)."""
    text = re.sub(r"^[a-z]+://", "", text.strip(), flags=re.I).split("/")[0].strip()
    m = re.fullmatch(r"\[([^\]]+)\](?::(\d{1,5}))?", text) or re.fullmatch(r"([^:\s]+)(?::(\d{1,5}))?", text)
    if not m:
        return None
    return m.group(1), int(m.group(2) or 443)


async def endpoint_cert(host: str, port: int, timeout: float = 10) -> datetime:
    """The expiry date of the certificate a TLS endpoint presents (also when it is invalid)."""
    from cryptography import x509

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host), timeout)
    try:
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    finally:
        writer.close()
    return x509.load_der_x509_certificate(der).not_valid_after_utc


async def endpoint_findings(endpoints: list[str], warn_days: int, now: datetime, finding) -> list[dict[str, Any]]:
    out = []
    for ep in endpoints[:50]:
        hp = _host_port(ep)
        if not hp:
            continue
        label = f"{hp[0]}:{hp[1]}"
        try:
            end = await endpoint_cert(*hp)
        except Exception as e:  # noqa: BLE001
            out.append(finding("low", "certificates", label, f"The certificate of {label} could not be checked: {str(e)[:120] or type(e).__name__}.",
                               "Check that Stackpilot can reach the address, or remove it from the list.",
                               code="cert_endpoint_error", endpoint=label, error=(str(e) or type(e).__name__)[:120]))
            continue
        f = cert_finding(label, hp[0], end, warn_days, now, finding)
        if f:
            out.append(f)
    return out


# ------------------------------------------------------------------ proposals
LINUX_CLEANUP = ("(command -v apt-get >/dev/null 2>&1 && apt-get clean); (command -v dnf >/dev/null 2>&1 && dnf clean all); "
                 "journalctl --vacuum-time=14d 2>&1 | tail -n 1; df -h -x tmpfs -x devtmpfs")
WINDOWS_CLEANUP = ("Remove-Item \"$env:windir\\Temp\\*\" -Recurse -Force -ErrorAction SilentlyContinue; "
                   "Dism.exe /Online /Cleanup-Image /StartComponentCleanup /Quiet; "
                   "Get-PSDrive -PSProvider FileSystem | Format-Table -AutoSize | Out-String")
RENEW = {"certbot": "certbot renew 2>&1", "pve-acme": "pvenode acme cert renew 2>&1"}


def node_of(f: dict[str, Any], by_name: dict[str, Node]) -> Node | None:
    """The machine a finding is about: its target is the machine name, or "<name> <disk>"."""
    target = f.get("target") or ""
    if target in by_name:
        return by_name[target]
    disk = str((f.get("params") or {}).get("disk") or "")
    if disk and target.endswith(" " + disk):
        return by_name.get(target[: -len(disk) - 1])
    return None


def proposal_for(f: dict[str, Any], n: Node | None) -> dict[str, Any] | None:
    """The one tool call that fixes a finding, or None when it needs a person."""
    from mmn.services.customers import machine_type

    code = f.get("code")
    if n is None:
        return None
    name = n.name
    online = agent_hub.is_online(n.id)
    if code in ("updates_pending", "updates_pending_sec", "cve_fixable", "security_updates", "windows_security"):
        return {"tool": "install_node_updates", "args": {"node": name, "confirm": name}, "label": "install_updates"}
    if code == "reboot_pending" and machine_type(n) != "proxmox":
        return {"tool": "reboot_node", "args": {"node": name, "confirm": name}, "label": "reboot"}
    if code == "agent_old" and online:
        return {"tool": "update_node_agent", "args": {"node": name}, "label": "update_agent"}
    if code == "disk_full" and machine_type(n) != "proxmox":
        cmd, timeout = (WINDOWS_CLEANUP, 1800) if n.kind == "windows" else (LINUX_CLEANUP, 300)
        return {"tool": "run_command", "label": "cleanup_disk",
                "args": {"node": name, "command": cmd, "timeout": timeout,
                         "reason": "Nightly checks: free disk space (package cache, old logs, temp files)"}}
    if code in ("cert_expired", "cert_expiring") and f.get("renew") in RENEW:
        return {"tool": "run_command", "label": "renew_cert",
                "args": {"node": name, "command": RENEW[f["renew"]], "timeout": 300,
                         "reason": "Nightly checks: renew a certificate that expires"}}
    return None


async def run_after_for(db: AsyncSession, e: Environment, risk: str, cfg: dict[str, Any]) -> datetime | None:
    """Changes wait for the customer's maintenance window (UTC), when there is one."""
    from mmn.services import gateway

    if not cfg.get("use_window") or risk not in gateway.WINDOW_RISKS:
        return None
    windows = gateway.parse_window(e.maintenance_window)
    if not windows:
        return None
    start = gateway.next_window_start(windows, await gateway.local_now(db))
    return start.astimezone(UTC) if start else None


# ------------------------------------------------------------------ running
class NightlyBusy(Exception):
    pass


_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()


def running() -> bool:
    return _lock.locked() or any(not t.done() for t in _tasks)


async def start(*, trigger: str = "manual", requested_by: str | None = None,
                customer_ids: list[str] | None = None, wait: bool = False) -> str:
    """Start a run in the background (or wait for it). Returns the run id."""
    if running():
        raise NightlyBusy("the nightly checks are already running")
    async with sessionmaker()() as db:
        # A run that was still "running" when the app restarted never finishes.
        for old in (await db.execute(select(NightlyRun).where(NightlyRun.status == "running"))).scalars():
            old.status, old.error, old.finished_at = "failed", "interrupted (the app restarted)", datetime.now(UTC)
        run = NightlyRun(trigger=trigger, requested_by=requested_by, customer_ids=customer_ids, status="running",
                         summary={})
        db.add(run)
        await db.commit()
        run_id = run.id
    events.publish("nightly.changed", {"id": run_id, "status": "running"})
    if wait:
        await execute(run_id)
    else:
        task = asyncio.create_task(execute(run_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return run_id


async def _previous(db: AsyncSession, run_id: str, env_id: str) -> tuple[NightlyRun | None, list[NightlyFinding]]:
    runs = (await db.execute(select(NightlyRun).where(NightlyRun.status == "done", NightlyRun.id != run_id)
                             .order_by(NightlyRun.started_at.desc()).limit(20))).scalars().all()
    for r in runs:
        if env_id in ((r.summary or {}).get("customers") or {}):
            rows = (await db.execute(select(NightlyFinding).where(NightlyFinding.run_id == r.id,
                                                                  NightlyFinding.environment_id == env_id))).scalars().all()
            return r, list(rows)
    return None, []


async def _refresh(e: Environment, cfg: dict[str, Any]) -> None:
    """Fresh data before the checks: package lists of the online machines and the backup coverage."""
    from mmn.services import nodes, protection

    online = [n for n in e.nodes if agent_hub.is_online(n.id)]
    checks = cfg["checks"]
    if online and (checks.get("updates") or checks.get("cves")):
        async def one(nid: str) -> None:
            try:
                await nodes.refresh_updates(nid)
            except Exception as ex:  # noqa: BLE001
                log.info("update check on %s: %s", nid, ex)

        try:
            await asyncio.wait_for(asyncio.gather(*(one(n.id) for n in online)), timeout=330)
        except TimeoutError:
            log.info("update checks of %s took too long", e.name)
    if checks.get("identity"):
        from mmn.services import m365

        await m365.refresh_customer(e.id)
    if e.nodes and checks.get("backups"):
        try:
            await asyncio.wait_for(protection.collect(e.id), timeout=600)
        except Exception as ex:  # noqa: BLE001
            log.info("backup coverage of %s: %s", e.name, ex)


async def check_customer(run_id: str, env_id: str, cfg: dict[str, Any], cache: dict[tuple, frozenset[str]],
                         *, refresh: bool = True) -> dict[str, Any]:
    from mmn.services import customers, gateway

    finding = customers._finding  # noqa: SLF001 - the same finding shape as the optimisation scan
    if refresh:
        async with sessionmaker()() as db:
            e = await db.get(Environment, env_id)
            if e is not None:
                await _refresh(e, cfg)
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        e = await db.get(Environment, env_id)
        if e is None:
            return {}
        scan = await customers.optimisation_scan(db, e, live=False)
        found = [f for f in scan["findings"]]
        errors: list[str] = []
        checks = cfg["checks"]
        ns = list(e.nodes)
        if checks.get("cves"):
            for n in ns:
                try:
                    found += await cve_findings(n, cfg, cache, finding)
                except Exception as ex:  # noqa: BLE001
                    errors.append(f"{n.name}: CVE check failed ({ex})"[:300])
        if checks.get("certificates"):
            warn = max(1, min(int(cfg.get("cert_warn_days") or 21), 120))
            certs = await asyncio.gather(*(machine_cert_findings(n, warn, now, finding) for n in ns),
                                         return_exceptions=True)
            for n, res in zip(ns, certs, strict=True):
                if isinstance(res, BaseException):
                    errors.append(f"{n.name}: certificate check failed ({res})"[:300])
                else:
                    found += res
            found += await endpoint_findings(list((e.nightly or {}).get("cert_endpoints") or []), warn, now, finding)
        if checks.get("identity"):
            from mmn.services import m365

            try:
                found += await m365.findings(db, e, now, finding)
            except Exception as ex:  # noqa: BLE001
                errors.append(f"Microsoft 365 check failed ({ex})"[:300])
        found = [f for f in found if checks.get(category_of(f), True)]

        from mmn.services import remediation

        recovery_rules = await remediation.finding_rules(db, e.id)
        recovered = 0
        prev_run, prev = await _previous(db, run_id, e.id)
        prev_by_key = {p.key: p for p in prev}
        by_name = {n.name: n for n in ns}
        seen: set[str] = set()
        rows: list[NightlyFinding] = []
        approval_ids: set[str] = set()
        for f in found:
            key = finding_key(f)
            if key in seen:
                continue
            seen.add(key)
            old = prev_by_key.get(key)
            row = NightlyFinding(run_id=run_id, environment_id=e.id, key=key, category=category_of(f),
                                 severity=f.get("severity") or "info", area=f.get("area") or "", target=(f.get("target") or None),
                                 code=f.get("code"), params=f.get("params") or {}, finding=f.get("finding") or "",
                                 suggestion=f.get("suggestion") or "", next_tool=f.get("next_tool"),
                                 is_new=prev_run is not None and old is None,
                                 first_seen_at=old.first_seen_at if old else now)
            if row.target and len(row.target) > 255:
                row.target = row.target[:255]
            # A finding that an automatic recovery rule answers is fixed by that rule (after the
            # commit below), not proposed.
            fixer = remediation.rule_for_finding(recovery_rules, f, node_of(f, by_name), e.id) if recovery_rules else None
            recovered += 1 if fixer else 0
            ready = f.pop("proposal", None)  # findings that bring their own proposal (Microsoft 365)
            prop = (ready or proposal_for(f, node_of(f, by_name))) if cfg.get("proposals") and not fixer else None
            if prop and prop["tool"] in gateway.REGISTRY:
                spec = gateway.REGISTRY[prop["tool"]]
                run_after = await run_after_for(db, e, spec.risk, cfg)
                reason = f"{f.get('finding')} {f.get('suggestion')}".strip()
                try:
                    a, created = await gateway.create_system_approval(
                        db, source=SOURCE, label=LABEL, tool=prop["tool"], args=prop["args"],
                        target=prop["args"].get("node") or prop["args"].get("user"), customers=[(e.id, e.name)],
                        reason=reason,
                        run_after=run_after, expires_hours=20)
                except gateway.ApprovalError as ex:
                    errors.append(str(ex))
                else:
                    row.approval_id = a.id
                    row.proposal = {**prop, "args": a.args_view, "risk": spec.risk,
                                    "run_after": run_after.isoformat() if run_after else None}
                    approval_ids.add(a.id)
            rows.append(row)
            db.add(row)
        resolved = []
        if prev_run is not None:
            for p in prev:
                if p.key not in seen:
                    resolved.append({"key": p.key, "category": p.category, "severity": p.severity, "target": p.target,
                                     "code": p.code, "params": p.params or {}, "finding": p.finding})
        counts = {s: 0 for s in SEVERITIES}
        by_cat = {c: 0 for c in CATEGORIES}
        for r in rows:
            counts[r.severity] = counts.get(r.severity, 0) + 1
            by_cat[r.category] = by_cat.get(r.category, 0) + 1
        summary = {"id": e.id, "name": e.name, "kind": e.kind, "counts": counts, "by_category": by_cat,
                   "total": len(rows), "new": sum(1 for r in rows if r.is_new), "resolved": resolved[:100],
                   "proposals": len(approval_ids), "errors": errors[:20], "checked": scan.get("checked") or {},
                   "maintenance_window": e.maintenance_window, "first_run": prev_run is None,
                   "recovery": recovered}
        await db.commit()
    if recovered:
        try:
            await remediation.on_findings(run_id, env_id)
        except Exception:  # noqa: BLE001 - the checks themselves are done
            log.exception("automatic recovery after the nightly checks of %s", env_id)
    return summary


def _totals(customers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    t: dict[str, Any] = {"customers": len(customers), "findings": 0, "new": 0, "resolved": 0, "proposals": 0,
                         "counts": {s: 0 for s in SEVERITIES}, "by_category": {c: 0 for c in CATEGORIES}}
    for c in customers.values():
        t["findings"] += c.get("total", 0)
        t["new"] += c.get("new", 0)
        t["resolved"] += len(c.get("resolved") or [])
        t["proposals"] += c.get("proposals", 0)
        for k, v in (c.get("counts") or {}).items():
            t["counts"][k] = t["counts"].get(k, 0) + v
        for k, v in (c.get("by_category") or {}).items():
            t["by_category"][k] = t["by_category"].get(k, 0) + v
    return t


async def execute(run_id: str, *, refresh: bool = True) -> None:
    from mmn.services import tenancy

    async with _lock:
        status, error = "done", None
        results: dict[str, dict[str, Any]] = {}
        try:
            async with sessionmaker()() as db:
                run = await db.get(NightlyRun, run_id)
                cfg = await settings(db)
                with tenancy.unrestricted():
                    envs = list((await db.execute(select(Environment).order_by(Environment.name))).scalars())
                ids = set(run.customer_ids) if run and run.customer_ids is not None else None
                todo = [e.id for e in envs if (ids is None or e.id in ids)
                        and (ids is not None or (e.nightly or {}).get("enabled", True))]
            cache: dict[tuple, frozenset[str]] = {}
            for eid in todo:
                try:
                    with tenancy.unrestricted():
                        res = await check_customer(run_id, eid, cfg, cache, refresh=refresh)
                except Exception as ex:  # noqa: BLE001 - one customer must not stop the others
                    log.exception("nightly checks of %s failed", eid)
                    res = {"id": eid, "errors": [f"{type(ex).__name__}: {ex}"[:300]], "total": 0, "counts": {},
                           "by_category": {}, "new": 0, "resolved": [], "proposals": 0}
                if res:
                    results[eid] = res
                async with sessionmaker()() as db:
                    run = await db.get(NightlyRun, run_id)
                    run.summary = {"customers": results, "totals": _totals(results)}
                    await db.commit()
        except Exception as ex:  # noqa: BLE001
            log.exception("nightly run failed")
            status, error = "failed", f"{type(ex).__name__}: {ex}"[:2000]
        async with sessionmaker()() as db:
            run = await db.get(NightlyRun, run_id)
            run.status, run.error, run.finished_at = status, error, datetime.now(UTC)
            run.summary = {"customers": results, "totals": _totals(results)}
            trigger = run.trigger
            await db.commit()
            late = trigger == "schedule" and await _past_summary_time(db)
    events.publish("nightly.changed", {"id": run_id, "status": status})
    if late:
        await send_summary(run_id)


async def _past_summary_time(db: AsyncSession) -> bool:
    cfg = await settings(db)
    h, m = parse_hhmm(cfg.get("summary_at"), "07:00")
    now = datetime.now(await _tz(db))
    return (now.hour, now.minute) >= (h, m)


# ------------------------------------------------------------------ the summary
def _visible(env_id: str) -> bool:
    from mmn.services import tenancy

    allowed = tenancy.allowed()
    return allowed is None or env_id in allowed


def finding_out(f: NightlyFinding, a: AiApproval | None) -> dict[str, Any]:
    prop = None
    if f.proposal:
        prop = {**f.proposal, "approval_id": f.approval_id,
                "status": a.status if a else "missing", "run_after": (a.run_after if a else None) or f.proposal.get("run_after"),
                "expires_at": a.expires_at if a else None, "error": a.error if a else None,
                "decided_by": a.decided_by if a else None}
        if a and a.status in ("approved",) and a.run_after:
            prop["status"] = "scheduled" if a.run_after > datetime.now(UTC) else "approved"
        if a:
            prop["risk"] = a.risk
    return {"id": f.id, "customer_id": f.environment_id, "key": f.key, "category": f.category, "severity": f.severity,
            "area": f.area, "target": f.target, "code": f.code, "params": f.params or {}, "finding": f.finding,
            "suggestion": f.suggestion, "next_tool": f.next_tool, "is_new": f.is_new, "first_seen_at": f.first_seen_at,
            "proposal": prop}


def run_out(r: NightlyRun, *, visible_only: bool = True) -> dict[str, Any]:
    cust = {k: v for k, v in ((r.summary or {}).get("customers") or {}).items() if not visible_only or _visible(k)}
    return {"id": r.id, "started_at": r.started_at, "finished_at": r.finished_at, "status": r.status,
            "trigger": r.trigger, "requested_by": r.requested_by, "summary_sent_at": r.summary_sent_at,
            "error": r.error, "customers": list(cust.values()), "totals": _totals(cust)}


async def detail(db: AsyncSession, r: NightlyRun) -> dict[str, Any]:
    out = run_out(r)
    rows = [f for f in (await db.execute(select(NightlyFinding).where(NightlyFinding.run_id == r.id))).scalars()
            if _visible(f.environment_id)]
    ids = [f.approval_id for f in rows if f.approval_id]
    approvals = {a.id: a for a in (await db.execute(select(AiApproval).where(AiApproval.id.in_(ids)))).scalars()} if ids else {}
    rows.sort(key=lambda f: (_SEV_ORDER.get(f.severity, 9), not f.is_new, f.category, str(f.target)))
    out["findings"] = [finding_out(f, approvals.get(f.approval_id)) for f in rows]
    out["pending_proposals"] = sum(1 for f in out["findings"] if (f["proposal"] or {}).get("status") == "pending")
    out["remediation"] = await remediation_summary(db, r)
    return out


async def remediation_summary(db: AsyncSession, r: NightlyRun) -> dict[str, Any]:
    """What automatic recovery did in the 24 hours before the summary (phase 14)."""
    from mmn.services import remediation, tenancy

    allowed = tenancy.allowed()
    end = r.finished_at or datetime.now(UTC)
    return await remediation.summary(db, end - timedelta(hours=24), None,
                                     set(allowed) if allowed is not None else None)


async def latest(db: AsyncSession, *, finished: bool = False) -> NightlyRun | None:
    q = select(NightlyRun).order_by(NightlyRun.started_at.desc())
    if finished:
        q = q.where(NightlyRun.status != "running")
    return (await db.execute(q.limit(1))).scalars().first()


async def pending_approvals(db: AsyncSession, run_id: str, customer_id: str | None = None) -> list[AiApproval]:
    rows = (await db.execute(select(NightlyFinding).where(NightlyFinding.run_id == run_id,
                                                          NightlyFinding.approval_id.is_not(None)))).scalars().all()
    ids = {f.approval_id for f in rows if _visible(f.environment_id) and (customer_id is None or f.environment_id == customer_id)}
    if not ids:
        return []
    return list((await db.execute(select(AiApproval).where(AiApproval.id.in_(ids), AiApproval.status == "pending",
                                                           AiApproval.expires_at > datetime.now(UTC)))).scalars())


def _sev_line(counts: dict[str, int], lang: str) -> str:
    from mmn.i18n import t

    parts = [t(lang, f"nightly.sev_{s}", n=counts.get(s, 0)) for s in ("high", "medium", "low") if counts.get(s)]
    return ", ".join(parts) or t(lang, "nightly.all_clear")


async def mail_content(db: AsyncSession, r: NightlyRun, lang: str, url: str | None):
    from mmn.i18n import t
    from mmn.services.mailer import MailContent
    from mmn.services.reports import finding_texts

    d = run_out(r, visible_only=False)
    tot = d["totals"]
    app = (await get_setting(db, "general"))["app_name"]
    paras = [t(lang, "nightly.mail_intro", customers=tot["customers"], findings=tot["findings"], new=tot["new"],
               resolved=tot["resolved"])]
    for c in sorted(d["customers"], key=lambda c: (-(c.get("counts") or {}).get("high", 0), c.get("name") or "")):
        line = t(lang, "nightly.mail_customer", name=c.get("name") or "?", sev=_sev_line(c.get("counts") or {}, lang),
                 new=c.get("new", 0), resolved=len(c.get("resolved") or []), proposals=c.get("proposals", 0))
        paras.append(line)
    rows = (await db.execute(select(NightlyFinding).where(NightlyFinding.run_id == r.id))).scalars().all()
    names = {c["id"]: c.get("name") for c in d["customers"]}
    top = sorted([f for f in rows if f.severity in ("high", "medium") and (f.is_new or f.severity == "high")],
                 key=lambda f: (_SEV_ORDER.get(f.severity, 9), not f.is_new))[:10]
    if top:
        paras.append(t(lang, "nightly.mail_top"))
        for f in top:
            target, text, _ = finding_texts({"code": f.code, "params": f.params, "target": f.target, "finding": f.finding,
                                             "suggestion": f.suggestion}, lang)
            new = f" ({t(lang, 'nightly.new')})" if f.is_new else ""
            paras.append(f"• {t(lang, 'nightly.sev_name_' + f.severity)}{new} · {names.get(f.environment_id) or ''} · "
                         f"{target or ''}: {text}")
    rem = await remediation_summary(db, r)
    if rem["total"]:
        c = rem["counts"]
        paras.append(t(lang, "nightly.mail_recovery", fixed=c.get("succeeded", 0), failed=c.get("failed", 0),
                       waiting=c.get("waiting", 0), skipped=c.get("skipped", 0)))
        for a in [x for x in rem["items"] if x["status"] in ("succeeded", "failed", "waiting_approval", "skipped")][:8]:
            paras.append(f"• {t(lang, 'remediation.status_' + a['status'])} · {a.get('customer_name') or ''} · "
                         f"{a.get('target') or ''}: {a.get('problem') or ''} ({a.get('rule_name')})")
    pending = len(await pending_approvals(db, r.id))
    if pending:
        paras.append(t(lang, "nightly.mail_proposals", count=pending))
    muted = [t(lang, "nightly.mail_footer")]
    subject = (t(lang, "nightly.subject_attention", app=app, high=tot["counts"].get("high", 0), new=tot["new"],
                 proposals=pending) if tot["findings"] else t(lang, "nightly.subject_clean", app=app))
    return MailContent(subject=subject, title=t(lang, "nightly.title"), paragraphs=paras, muted=muted,
                       button_url=url, button_label=t(lang, "nightly.button") if url else None)


async def summary_url(db: AsyncSession) -> str | None:
    from mmn.services.links import public_base_url

    base = await public_base_url(db)
    return f"{base}/ai/nightly" if base else None


class MailDisabled(Exception):
    pass


async def send_summary(run_id: str, *, force: bool = False) -> dict[str, bool]:
    """The morning summary: an event in the app and a mail. Never raises."""
    from mmn.services.ai_notify import _recipients
    from mmn.services.mailer import send_mail

    sent: dict[str, bool] = {}
    async with sessionmaker()() as db:
        r = await db.get(NightlyRun, run_id)
        if r is None or r.status == "running" or (r.summary_sent_at and not force):
            return sent
        cfg = await settings(db)
        d = run_out(r, visible_only=False)
        if cfg.get("notify_app"):
            events.publish("nightly.summary", {"id": r.id, "findings": d["totals"]["findings"], "new": d["totals"]["new"],
                                               "high": d["totals"]["counts"].get("high", 0),
                                               "proposals": d["totals"]["proposals"]})
            sent["app"] = True
        clean = not d["totals"]["findings"]
        if cfg.get("notify_mail") and (not clean or cfg.get("mail_when_clean") or force):
            try:
                if not (await get_setting(db, "mail")).get("enabled"):
                    raise MailDisabled("mail is disabled")
                url = await summary_url(db)
                by_lang: dict[str, list[str]] = {}
                for addr, lang in await _recipients(db, {"notify_mail_to": cfg.get("mail_to") or ""}):
                    by_lang.setdefault(lang if lang in ("en", "nl") else "en", []).append(addr)
                for lang, to in by_lang.items():
                    await send_mail(db, to, await mail_content(db, r, lang, url), lang)
                sent["mail"] = bool(by_lang)
            except MailDisabled:
                log.info("morning summary not mailed: mail is disabled")
                sent["mail"] = False
            except Exception:  # noqa: BLE001
                log.exception("morning summary mail failed")
                sent["mail"] = False
        r.summary_sent_at = datetime.now(UTC)
        await db.commit()
    return sent


async def summary_job() -> None:
    """Scheduler, at the summary time: send the summary of last night's run (once)."""
    async with sessionmaker()() as db:
        r = await latest(db)
        if r is None or r.trigger != "schedule" or r.summary_sent_at or r.status == "running":
            return
        if r.started_at < datetime.now(UTC) - timedelta(hours=20):
            return
        run_id = r.id
    await send_summary(run_id)


async def schedule_job() -> None:
    async with sessionmaker()() as db:
        if not (await settings(db)).get("enabled"):
            return
    try:
        await start(trigger="schedule", wait=True)
    except NightlyBusy:
        log.info("nightly checks already running")


async def prune(days: int = 90) -> None:
    from sqlalchemy import delete

    async with sessionmaker()() as db:
        await db.execute(delete(NightlyRun).where(NightlyRun.started_at < datetime.now(UTC) - timedelta(days=days)))
        await db.commit()
