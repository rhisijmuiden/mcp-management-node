"""Workstations (phase 10): PCs and laptops of a customer as first-class machines.

The same agent runs on workstations as on servers (``Node.role == "workstation"``). On top of the
server tools, Stackpilot keeps an inventory per workstation: hardware, operating system, logged-in
users, installed software, disks and their health, patch status, antivirus (Microsoft Defender or
another product), BitLocker/LUKS, the firewall, TPM and Secure Boot. It is collected with a fixed,
read-only script through the agent's command channel, so every agent version that is installed
today works without an agent update. The scheduler refreshes it for online workstations; the
interface and the AI can refresh it on demand.

From the inventory a security status is derived (ok / warning / critical with codes the interface
translates), and a few remote actions are offered (Defender scan and signature update, Group Policy
refresh, a message to the user, logging off a user), each through the agent and the audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmn.db import sessionmaker
from mmn.models import Node
from mmn.services import agent_hub, nodes
from mmn.services.agent_hub import AgentError
from mmn.services.nodes import NodeError

log = logging.getLogger("mmn.workstations")

KINDS = ("linux", "windows")
STALE_AFTER = timedelta(hours=6)
SOFTWARE_MAX = 1000

# ------------------------------------------------------------------ collection scripts
# Windows PowerShell 5.1, read-only. Every section is wrapped, so a missing cmdlet (no Defender on a
# server, no BitLocker on Home editions) only adds a line to "errors". Keep it short: the agent passes
# it as -EncodedCommand, and the Windows command line is limited to 32k characters.
WINDOWS_SCRIPT = r"""$ErrorActionPreference='Stop'
$r=[ordered]@{v=1;errors=@()}
function T($n,[scriptblock]$b){try{$r[$n]=(& $b)}catch{$r.errors+=("${n}: "+$_.Exception.Message)}}
function L($n,[scriptblock]$b){try{$r[$n]=@(& $b)}catch{$r.errors+=("${n}: "+$_.Exception.Message)}}
function D($d){if($d){([datetime]$d).ToUniversalTime().ToString('o')}}
T 'system' {$c=Get-CimInstance Win32_ComputerSystem;$b=Get-CimInstance Win32_BIOS;$p=@(Get-CimInstance Win32_Processor)[0];$o=Get-CimInstance Win32_OperatingSystem
$cv=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
[ordered]@{manufacturer=$c.Manufacturer;model=$c.Model;serial=$b.SerialNumber;bios=$b.SMBIOSBIOSVersion;cpu=$p.Name;cores=$p.NumberOfCores;threads=$p.NumberOfLogicalProcessors;memory=[int64]$c.TotalPhysicalMemory;domain=$c.Domain;part_of_domain=$c.PartOfDomain;chassis=(@(Get-CimInstance Win32_SystemEnclosure)[0].ChassisTypes -join ',');os=$o.Caption;os_version=$o.Version;build="$($cv.CurrentBuild).$($cv.UBR)";display_version=$cv.DisplayVersion;arch=$o.OSArchitecture;install_date=(D $o.InstallDate);last_boot=(D $o.LastBootUpTime)}}
L 'users' {$u=@();$cs=(Get-CimInstance Win32_ComputerSystem).UserName;if($cs){$u+=$cs}
foreach($p in @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'")){$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner;if($o.User){$u+=("$($o.Domain)\$($o.User)")}}
$u|Sort-Object -Unique}
T 'last_user' {(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI' -ErrorAction SilentlyContinue).LastLoggedOnUser}
L 'software' {$k='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
Get-ItemProperty $k -ErrorAction SilentlyContinue|?{$_.DisplayName -and -not $_.SystemComponent -and -not $_.ParentKeyName}|%{[pscustomobject]@{name=[string]$_.DisplayName;version=[string]$_.DisplayVersion;publisher=[string]$_.Publisher;installed=[string]$_.InstallDate}}|Sort-Object name,version -Unique|Select-Object -First 1000}
L 'disks' {Get-PhysicalDisk|%{$rc=$_|Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
[pscustomobject]@{name=$_.FriendlyName;media=[string]$_.MediaType;bus=[string]$_.BusType;size=[int64]$_.Size;health=[string]$_.HealthStatus;status=($_.OperationalStatus -join ',');temperature=$rc.Temperature;wear=$rc.Wear;power_on_hours=$rc.PowerOnHours;read_errors=$rc.ReadErrorsUncorrected}}}
L 'volumes' {Get-Volume|?{$_.DriveLetter -and "$($_.DriveType)" -eq 'Fixed'}|%{[pscustomobject]@{mount="$($_.DriveLetter):";label=$_.FileSystemLabel;fs=$_.FileSystem;size=[int64]$_.Size;free=[int64]$_.SizeRemaining;health=[string]$_.HealthStatus}}}
T 'defender' {$m=Get-MpComputerStatus
[ordered]@{service=$m.AMServiceEnabled;antivirus=$m.AntivirusEnabled;realtime=$m.RealTimeProtectionEnabled;tamper=$m.IsTamperProtected;mode=[string]$m.AMRunningMode;signature_updated=(D $m.AntivirusSignatureLastUpdated);signature_age_days=$m.AntivirusSignatureAge;signature_version=$m.AntivirusSignatureVersion;engine=$m.AMEngineVersion;quick_scan_age_days=$m.QuickScanAge;full_scan_age_days=$m.FullScanAge;threats_30d=@(Get-MpThreatDetection -ErrorAction SilentlyContinue|?{$_.InitialDetectionTime -gt (Get-Date).AddDays(-30)}).Count}}
L 'av_products' {Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue|%{$s=[int]$_.productState;[pscustomobject]@{name=$_.displayName;enabled=((($s -shr 12) -band 0xF) -eq 1);up_to_date=(($s -band 0xFF) -eq 0)}}}
L 'bitlocker' {if(Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue){Get-BitLockerVolume|%{[pscustomobject]@{mount=$_.MountPoint;type=[string]$_.VolumeType;status=[string]$_.VolumeStatus;protection=[string]$_.ProtectionStatus;percent=$_.EncryptionPercentage;method=[string]$_.EncryptionMethod}}}
else{Get-CimInstance -Namespace root/cimv2/Security/MicrosoftVolumeEncryption -ClassName Win32_EncryptableVolume|%{[pscustomobject]@{mount=$_.DriveLetter;type=$null;status=$null;protection=@('Off','On','Unknown')[[int]$_.ProtectionStatus];percent=$null;method=$null}}}}
L 'firewall' {Get-NetFirewallProfile|%{[pscustomobject]@{profile=$_.Name;enabled=("$($_.Enabled)" -eq 'True')}}}
T 'tpm' {$t=Get-Tpm;[ordered]@{present=$t.TpmPresent;ready=$t.TpmReady}}
T 'secure_boot' {try{Confirm-SecureBootUEFI}catch{$null}}
L 'update_history' {$q=(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher();$n=$q.GetTotalHistoryCount()
if($n -gt 0){$q.QueryHistory(0,[Math]::Min($n,60))|?{$_.Operation -eq 1}|%{[pscustomobject]@{date=(D $_.Date);title=$_.Title;result=@('NotStarted','InProgress','Succeeded','SucceededWithErrors','Failed','Aborted')[$_.ResultCode]}}}}
L 'hotfixes' {Get-HotFix|?{$_.InstalledOn}|Sort-Object InstalledOn -Descending|Select-Object -First 20|%{[pscustomobject]@{id=$_.HotFixID;description=$_.Description;installed=$_.InstalledOn.ToString('yyyy-MM-dd')}}}
T 'reboot_pending' {(Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')}
T 'battery' {$b=@(Get-CimInstance Win32_Battery);if($b.Count){[ordered]@{charge=$b[0].EstimatedChargeRemaining;status=$b[0].BatteryStatus}}}
$r|ConvertTo-Json -Depth 5 -Compress"""

# POSIX sh, read-only: prints sections that are parsed here, so neither jq nor Python is needed.
LINUX_SCRIPT = r"""export LC_ALL=C
s(){ echo "@@@$1"; }
s os; cat /etc/os-release 2>/dev/null
s uname; uname -srm
s dmi; for f in sys_vendor product_name product_serial bios_version chassis_type; do printf '%s=%s\n' "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null)"; done
s cpu; grep -m1 'model name' /proc/cpuinfo; nproc 2>/dev/null
s mem; grep MemTotal /proc/meminfo
s uptime; cat /proc/uptime
s users; who 2>/dev/null
s lsblk; lsblk -J -b -o NAME,SIZE,TYPE,MODEL,ROTA,MOUNTPOINT,FSTYPE,TRAN 2>/dev/null
s df; df -P -B1 -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>/dev/null
s smart; if command -v smartctl >/dev/null 2>&1; then for d in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print $1}'); do printf '%s ' "$d"; smartctl -H "/dev/$d" 2>/dev/null | grep -Ei 'overall-health|SMART Health Status' || echo; done; fi
s packages; if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W -f '${Package}\t${Version}\n' 2>/dev/null | head -4000; elif command -v rpm >/dev/null 2>&1; then rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\n' 2>/dev/null | head -4000; fi
s firewall; command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -1; command -v firewall-cmd >/dev/null 2>&1 && echo "firewalld $(firewall-cmd --state 2>&1)"; command -v nft >/dev/null 2>&1 && echo "nft_rules $(nft list ruleset 2>/dev/null | grep -c .)"
s av; for u in clamav-daemon clamav-freshclam mdatp falcon-sensor; do echo "$u $(systemctl is-active $u 2>/dev/null)"; done
s updates; [ -f /var/log/apt/history.log ] && grep -E '^(Start-Date|Upgrade|Install):' /var/log/apt/history.log | tail -80 | cut -c1-4000
s reboot; [ -f /var/run/reboot-required ] && echo yes || echo no
s secureboot; command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null
s end
"""


def collect_script(kind: str) -> str:
    return WINDOWS_SCRIPT if kind == "windows" else LINUX_SCRIPT


# ------------------------------------------------------------------ parsing
def _list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_windows(stdout: str) -> dict[str, Any]:
    raw = (stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        raise NodeError("the inventory script returned no data: " + raw[:300])
    data = json.loads(raw[start:])
    sysd = data.get("system") or {}
    inv: dict[str, Any] = {
        "collected_at": datetime.now(UTC).isoformat(), "platform": "windows",
        "hardware": {k: sysd.get(k) for k in ("manufacturer", "model", "serial", "bios", "cpu", "cores", "threads",
                                              "memory", "chassis")},
        "os": {"name": sysd.get("os"), "version": sysd.get("os_version"), "build": sysd.get("build"),
               "display_version": sysd.get("display_version"), "arch": sysd.get("arch"),
               "install_date": sysd.get("install_date"), "last_boot": sysd.get("last_boot"),
               "domain": sysd.get("domain"), "part_of_domain": sysd.get("part_of_domain")},
        "users": [u for u in _list(data.get("users")) if u],
        "last_user": data.get("last_user"),
        "software": [s for s in _list(data.get("software")) if isinstance(s, dict) and s.get("name")][:SOFTWARE_MAX],
        "disks": _list(data.get("disks")),
        "volumes": _list(data.get("volumes")),
        "antivirus": {"defender": data.get("defender"), "products": _list(data.get("av_products"))},
        "encryption": {"kind": "bitlocker", "volumes": _list(data.get("bitlocker"))},
        "firewall": {"profiles": _list(data.get("firewall"))},
        "tpm": data.get("tpm"), "secure_boot": data.get("secure_boot"),
        "updates": {"history": _list(data.get("update_history")), "hotfixes": _list(data.get("hotfixes")),
                    "reboot_pending": bool(data.get("reboot_pending"))},
        "battery": data.get("battery"),
        "errors": [str(e)[:300] for e in _list(data.get("errors"))][:20],
    }
    hist = [h for h in inv["updates"]["history"] if h.get("result") in ("Succeeded", "SucceededWithErrors")]
    dates = [h["date"] for h in hist if h.get("date")] + [f"{h['installed']}T00:00:00+00:00"
                                                          for h in inv["updates"]["hotfixes"] if h.get("installed")]
    inv["updates"]["last_installed"] = max(dates) if dates else None
    return inv


def _sections(stdout: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in (stdout or "").splitlines():
        if line.startswith("@@@"):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


_APT_DATE = re.compile(r"^Start-Date:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")


def parse_linux(stdout: str) -> dict[str, Any]:
    sec = _sections(stdout)
    if "end" not in sec and "os" not in sec:
        raise NodeError("the inventory script returned no data: " + (stdout or "")[:300])
    osr = {}
    for ln in sec.get("os", []):
        if "=" in ln:
            k, v = ln.split("=", 1)
            osr[k] = v.strip().strip('"')
    dmi = {}
    for ln in sec.get("dmi", []):
        if "=" in ln:
            k, v = ln.split("=", 1)
            dmi[k] = v.strip() or None
    cpu = sec.get("cpu", [])
    model = next((ln.split(":", 1)[1].strip() for ln in cpu if ln.startswith("model name")), None)
    threads = next((_int(ln) for ln in cpu if ln.strip().isdigit()), None)
    mem = next((_int(ln.split()[1]) for ln in sec.get("mem", []) if ln.startswith("MemTotal")), None)
    up = sec.get("uptime", ["0"])
    boot = None
    try:
        boot = (datetime.now(UTC) - timedelta(seconds=float(up[0].split()[0]))).isoformat()
    except (ValueError, IndexError):
        pass
    users = sorted({ln.split()[0] for ln in sec.get("users", []) if ln.strip()})
    disks, volumes, luks = [], [], False
    try:
        tree = json.loads("\n".join(sec.get("lsblk", [])) or "{}").get("blockdevices") or []
    except ValueError:
        tree = []

    def walk(devs: list[dict]) -> None:
        nonlocal luks
        for d in devs:
            if d.get("fstype") == "crypto_LUKS":
                luks = True
            walk(d.get("children") or [])

    walk(tree)
    smart = {}
    for ln in sec.get("smart", []):
        parts = ln.split(None, 1)
        if len(parts) == 2:
            m = re.search(r"(PASSED|FAILED|OK)\b", parts[1])
            smart[parts[0]] = m.group(1) if m else None
    for d in tree:
        if d.get("type") != "disk" or str(d.get("name")).startswith(("zram", "loop")) or not _int(d.get("size")):
            continue
        h = smart.get(d.get("name"))
        disks.append({"name": d.get("model") or d.get("name"), "device": f"/dev/{d.get('name')}",
                      "media": "HDD" if str(d.get("rota")) in ("1", "True", "true") else "SSD",
                      "bus": (d.get("tran") or "").upper() or None, "size": _int(d.get("size")),
                      "health": {"PASSED": "Healthy", "OK": "Healthy", "FAILED": "Unhealthy"}.get(h or "", "Unknown")})
    for ln in sec.get("df", [])[1:]:
        p = ln.split()
        if len(p) >= 6 and p[0].startswith("/"):
            volumes.append({"mount": p[5], "fs": None, "label": p[0], "size": _int(p[1]),
                            "free": _int(p[3])})
    software = []
    for ln in sec.get("packages", []):
        if "\t" in ln:
            n, v = ln.split("\t", 1)
            software.append({"name": n, "version": v})
    fw = {"ufw": None, "firewalld": None, "nft_rules": None}
    for ln in sec.get("firewall", []):
        if ln.startswith("Status:"):
            fw["ufw"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("firewalld "):
            fw["firewalld"] = ln.split(" ", 1)[1].strip()
        elif ln.startswith("nft_rules "):
            fw["nft_rules"] = _int(ln.split()[1])
    av = [{"name": p[0], "enabled": p[1] == "active"} for p in (ln.split() for ln in sec.get("av", [])) if len(p) == 2]
    history, start = [], None
    for ln in sec.get("updates", []):
        m = _APT_DATE.match(ln)
        if m:
            start = f"{m.group(1)}T{m.group(2)}"
            continue
        if start and (ln.startswith("Upgrade:") or ln.startswith("Install:")):
            count = len(re.findall(r"\), |\)$", ln.strip())) or 1
            history.append({"date": start, "title": f"{ln.split(':', 1)[0]}: {count} package(s)",
                            "result": "Succeeded", "packages": count})
    history.reverse()
    sb = " ".join(sec.get("secureboot", [])).lower()
    inv = {
        "collected_at": datetime.now(UTC).isoformat(), "platform": "linux",
        "hardware": {"manufacturer": dmi.get("sys_vendor"), "model": dmi.get("product_name"),
                     "serial": dmi.get("product_serial"), "bios": dmi.get("bios_version"), "cpu": model,
                     "cores": None, "threads": threads, "memory": mem * 1024 if mem else None,
                     "chassis": dmi.get("chassis_type")},
        "os": {"name": osr.get("PRETTY_NAME"), "version": osr.get("VERSION_ID"),
               "build": (sec.get("uname") or [""])[0], "last_boot": boot},
        "users": users, "last_user": users[0] if users else None,
        "software": software[:SOFTWARE_MAX * 3], "disks": disks, "volumes": volumes,
        "antivirus": {"defender": None, "products": av},
        "encryption": {"kind": "luks", "luks": luks},
        "firewall": fw, "tpm": None,
        "secure_boot": True if "enabled" in sb else (False if "disabled" in sb else None),
        "updates": {"history": history[:60], "reboot_pending": (sec.get("reboot") or ["no"])[0].strip() == "yes"},
        "battery": None, "errors": [],
    }
    inv["updates"]["last_installed"] = history[0]["date"] if history else None
    return inv


def parse(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    out = result.get("stdout") or ""
    if result.get("timed_out"):
        raise NodeError("the inventory script timed out")
    try:
        return parse_windows(out) if kind == "windows" else parse_linux(out)
    except ValueError as e:
        raise NodeError(f"could not read the inventory: {e}; {(result.get('stderr') or '')[:300]}") from e


# ------------------------------------------------------------------ security status
SEVERITY = {"ok": 0, "info": 1, "warning": 2, "critical": 3}


def _days_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 86400


def _pct_free(v: dict[str, Any]) -> float | None:
    size, free = v.get("size"), v.get("free")
    try:
        return float(free) / float(size) * 100 if size else None
    except (TypeError, ValueError):
        return None


def checks(n: Node) -> list[dict[str, Any]]:
    """Security and health checks of one workstation. Each check: key, status (ok, info, warning,
    critical), code (translated in the interface), text (English, for the AI and the report)."""
    inv = n.inventory or {}
    if not inv:
        return []
    out: list[dict[str, Any]] = []

    def add(key: str, status: str, code: str, text: str, **extra: Any) -> None:
        out.append({"key": key, "status": status, "code": code, "text": text, **extra})

    win = inv.get("platform") == "windows"
    av = inv.get("antivirus") or {}
    d = av.get("defender") or {}
    others = [p for p in av.get("products") or [] if p.get("enabled") and "defender" not in str(p.get("name")).lower()]
    if win:
        if d.get("realtime") and d.get("antivirus"):
            add("antivirus", "ok", "av_on", "Microsoft Defender real-time protection is on.")
            age = d.get("signature_age_days")
            if isinstance(age, int | float) and age > 3:
                add("signatures", "warning", "signatures_old", f"Defender signatures are {int(age)} days old.",
                    days=int(age))
            else:
                add("signatures", "ok", "signatures_ok", "Defender signatures are up to date.")
        elif others:
            add("antivirus", "ok", "av_other", f"Antivirus: {', '.join(str(p.get('name')) for p in others)}.",
                product=others[0].get("name"))
        else:
            add("antivirus", "critical", "av_off", "No active antivirus: Defender real-time protection is off.")
        threats = d.get("threats_30d") or 0
        if threats:
            add("threats", "critical", "threats_found", f"Defender found {threats} threat(s) in the last 30 days.",
                count=threats)
    else:
        if any(p.get("enabled") for p in av.get("products") or []):
            add("antivirus", "ok", "av_other", "An antivirus service is running.")
        else:
            add("antivirus", "info", "av_none_linux", "No antivirus service found (common on Linux).")
    # Firewall
    fw = inv.get("firewall") or {}
    if win:
        off = [p.get("profile") for p in fw.get("profiles") or [] if p.get("enabled") is False]
        if off:
            add("firewall", "warning", "firewall_off", f"Windows Firewall is off for: {', '.join(map(str, off))}.",
                profiles=off)
        elif fw.get("profiles"):
            add("firewall", "ok", "firewall_on", "Windows Firewall is on for all profiles.")
    else:
        on = (str(fw.get("ufw") or "").lower() == "active" or str(fw.get("firewalld") or "").lower() == "running"
              or (fw.get("nft_rules") or 0) >= 5)
        add("firewall", "ok" if on else "info", "firewall_on" if on else "firewall_none_linux",
            "A firewall is active." if on else "No active firewall found (ufw, firewalld or nftables rules).")
    # Disk encryption
    enc = inv.get("encryption") or {}
    if win:
        vols = enc.get("volumes") or []
        osv = next((v for v in vols if str(v.get("type")) == "OperatingSystem"), None) or \
            next((v for v in vols if str(v.get("mount")).upper().startswith("C:")), None)
        if osv and str(osv.get("protection")) == "On":
            add("encryption", "ok", "bitlocker_on", "BitLocker protects the system drive.")
        elif osv and str(osv.get("status")) in ("EncryptionInProgress",):
            add("encryption", "info", "bitlocker_progress", "BitLocker encryption is in progress.")
        else:
            add("encryption", "warning", "bitlocker_off", "The system drive is not protected by BitLocker.")
    else:
        add("encryption", "ok" if enc.get("luks") else "warning", "luks_on" if enc.get("luks") else "luks_off",
            "The disk is encrypted (LUKS)." if enc.get("luks") else "No encrypted (LUKS) disk found.")
    # Updates
    upd = n.updates or {}
    pending = upd.get("packages") or []
    sec = sum(1 for p in pending if p.get("security"))
    if pending:
        add("updates", "warning" if sec or len(pending) >= 10 else "info", "updates_pending",
            f"{len(pending)} update(s) pending{f', {sec} security' if sec else ''}.", count=len(pending), security=sec)
    last = (inv.get("updates") or {}).get("last_installed")
    age = _days_since(last)
    if age is not None and age > 35:
        add("patching", "warning", "no_recent_updates", f"The last update was installed {int(age)} days ago.",
            days=int(age))
    elif age is not None:
        add("patching", "ok", "recent_updates", f"Updates were installed {int(age)} day(s) ago.", days=int(age))
    if upd.get("reboot_required") or (inv.get("updates") or {}).get("reboot_pending"):
        add("reboot", "warning", "reboot_pending", "A reboot is pending to finish updates.")
    # Disks
    bad = [x for x in inv.get("disks") or [] if str(x.get("health") or "").lower() not in ("healthy", "unknown", "")]
    for x in bad:
        add("disk_health", "critical", "disk_unhealthy", f"Disk {x.get('name')} reports health {x.get('health')}.",
            disk=x.get("name"))
    for x in inv.get("disks") or []:
        if isinstance(x.get("wear"), int | float) and x["wear"] >= 90:
            add("disk_wear", "warning", "disk_worn", f"SSD {x.get('name')} is {int(x['wear'])}% worn.",
                disk=x.get("name"), wear=int(x["wear"]))
    if inv.get("disks") and not bad:
        add("disk_health", "ok", "disks_healthy", "All disks report healthy.")
    for v in inv.get("volumes") or []:
        pf = _pct_free(v)
        if pf is not None and pf < 10 and (v.get("size") or 0) > 2 * 1024**3:
            add("disk_space", "critical" if pf < 3 else "warning", "disk_full",
                f"Drive {v.get('mount')} has only {pf:.0f}% free.", mount=v.get("mount"), free_pct=round(pf, 1))
    if win:
        tpm = inv.get("tpm") or {}
        if tpm and not tpm.get("present"):
            add("tpm", "info", "no_tpm", "No TPM found (needed for BitLocker without a PIN and Windows 11).")
        if inv.get("secure_boot") is False:
            add("secure_boot", "info", "secure_boot_off", "Secure Boot is off.")
    return out


def status_of(items: list[dict[str, Any]]) -> str:
    worst = max((SEVERITY.get(c["status"], 0) for c in items), default=0)
    return {0: "ok", 1: "ok", 2: "warning", 3: "critical"}[worst]


# ------------------------------------------------------------------ output
def _defender_line(inv: dict[str, Any]) -> str | None:
    av = inv.get("antivirus") or {}
    d = av.get("defender") or {}
    if d.get("realtime"):
        return "Microsoft Defender"
    others = [p.get("name") for p in av.get("products") or [] if p.get("enabled")]
    return others[0] if others else None


def summary(n: Node) -> dict[str, Any]:
    from mmn.services.servers import attention

    inv = n.inventory or {}
    items = checks(n)
    hw = inv.get("hardware") or {}
    osd = inv.get("os") or {}
    upd = n.updates or {}
    enc = next((c for c in items if c["key"] == "encryption"), None)
    online = agent_hub.is_online(n.id)
    return {
        "id": n.id, "name": n.name, "kind": n.kind, "role": n.role or "server", "hostname": n.hostname,
        "customer_id": n.environment_id, "customer": n.environment.name if n.environment else None,
        "online": online, "last_seen_at": n.last_seen_at, "agent_version": n.agent_version,
        "os": osd.get("name") or (n.info or {}).get("os_pretty_name") or n.os,
        "os_build": osd.get("display_version") or osd.get("build"),
        "model": " ".join(x for x in (hw.get("manufacturer"), hw.get("model")) if x) or None,
        "serial": hw.get("serial"), "cpu": hw.get("cpu"), "memory": hw.get("memory"),
        "user": (inv.get("users") or [None])[0] or inv.get("last_user"),
        "users": inv.get("users") or [],
        "antivirus": _defender_line(inv),
        "encryption": enc["status"] if enc else None,
        "updates": len(upd.get("packages") or []),
        "reboot_required": bool(upd.get("reboot_required") or (inv.get("updates") or {}).get("reboot_pending")),
        "last_update_installed": (inv.get("updates") or {}).get("last_installed"),
        "security": status_of(items) if inv else None,
        "issues": [c["code"] for c in items if c["status"] in ("warning", "critical")],
        "attention": attention(n),
        "software_count": len(inv.get("software") or []),
        "inventory_at": n.inventory_at, "inventory_error": n.inventory_error,
        "metrics": {k: (n.metrics or {}).get(k) for k in ("cpu_percent", "mem_used", "mem_total")} if online else None,
    }


def detail(n: Node) -> dict[str, Any]:
    d = summary(n)
    d["inventory"] = n.inventory or {}
    d["checks"] = checks(n)
    d["pending_updates"] = (n.updates or {}).get("packages") or []
    d["disks_live"] = (n.metrics or {}).get("disks") or []
    return d


def _is_ws(n: Node) -> bool:
    return n.kind in KINDS and n.role == "workstation"


async def list_workstations(db: AsyncSession, environment_id: str | None = None) -> dict[str, Any]:
    q = select(Node).where(Node.kind.in_(KINDS), Node.role == "workstation").order_by(Node.name)
    if environment_id:
        q = q.where(Node.environment_id == environment_id)
    rows = [summary(n) for n in (await db.execute(q)).scalars()]
    return {"workstations": rows,
            "totals": {"total": len(rows), "online": sum(1 for r in rows if r["online"]),
                       "windows": sum(1 for r in rows if r["kind"] == "windows"),
                       "linux": sum(1 for r in rows if r["kind"] == "linux"),
                       "critical": sum(1 for r in rows if r["security"] == "critical"),
                       "warning": sum(1 for r in rows if r["security"] == "warning"),
                       "not_encrypted": sum(1 for r in rows if r["encryption"] == "warning"),
                       "updates": sum(r["updates"] for r in rows),
                       "no_inventory": sum(1 for r in rows if not r["inventory_at"])}}


async def get_workstation(db: AsyncSession, ident: str) -> Node:
    n = await nodes.get_node(db, ident)
    if n.kind not in KINDS:
        raise NodeError("this is a Proxmox VE node, not a workstation")
    return n


# ------------------------------------------------------------------ collecting
_locks: dict[str, asyncio.Lock] = {}


async def collect(node_id: str) -> dict[str, Any]:
    """Run the inventory script on the machine and store the result."""
    async with sessionmaker()() as db:
        n = await db.get(Node, node_id)
        if n is None:
            raise NodeError("node not found")
        kind = n.kind
    if kind not in KINDS:
        raise NodeError("inventory is only for Linux and Windows machines")
    if not agent_hub.is_online(node_id):
        raise NodeError("the machine is offline")
    lock = _locks.setdefault(node_id, asyncio.Lock())
    async with lock:
        error = None
        inv = None
        try:
            res = await agent_hub.request(node_id, "exec", {"command": collect_script(kind), "timeout": 240},
                                          timeout=280) or {}
            inv = parse(kind, res)
        except (AgentError, NodeError) as e:
            error = str(e)[:1000]
        async with sessionmaker()() as db:
            n = await db.get(Node, node_id)
            if n is None:
                raise NodeError("node not found")
            if inv is not None:
                n.inventory = inv
                n.inventory_at = datetime.now(UTC)
                n.inventory_error = None
            else:
                n.inventory_error = error
            await db.commit()
            await db.refresh(n)
            if inv is None:
                raise NodeError(f"inventory failed: {error}")
            return detail(n)


async def refresh_stale(max_parallel: int = 4) -> int:
    """Scheduler: refresh the inventory of online workstations that have none or an old one."""
    cutoff = datetime.now(UTC) - STALE_AFTER
    async with sessionmaker()() as db:
        rows = (await db.execute(select(Node).where(Node.kind.in_(KINDS), Node.role == "workstation"))).scalars().all()
        todo = [n.id for n in rows if agent_hub.is_online(n.id) and (n.inventory_at is None or n.inventory_at < cutoff)]
    sem = asyncio.Semaphore(max_parallel)

    async def one(nid: str) -> None:
        async with sem:
            try:
                await collect(nid)
            except Exception as e:  # noqa: BLE001
                log.info("inventory of %s failed: %s", nid, e)

    await asyncio.gather(*(one(i) for i in todo))
    return len(todo)


# ------------------------------------------------------------------ software search
async def find_software(db: AsyncSession, pattern: str, environment_id: str | None = None,
                        include_servers: bool = False) -> list[dict[str, Any]]:
    """Which machines have software installed whose name matches ``pattern`` (case-insensitive
    text or * wildcards), from the stored inventories (also for machines that are offline now)."""
    p = (pattern or "").strip()
    if not p or len(p) > 100:
        raise NodeError("give a name to search for (max 100 characters)")
    rx = re.compile(".*".join(re.escape(x) for x in p.lower().split("*")), re.I)
    q = select(Node).where(Node.kind.in_(KINDS)).order_by(Node.name)
    if not include_servers:
        q = q.where(Node.role == "workstation")
    if environment_id:
        q = q.where(Node.environment_id == environment_id)
    out = []
    for n in (await db.execute(q)).scalars():
        hits = [{"name": s.get("name"), "version": s.get("version"), "publisher": s.get("publisher")}
                for s in (n.inventory or {}).get("software") or [] if rx.search(str(s.get("name") or ""))]
        if hits:
            out.append({"machine": n.name, "node_id": n.id, "customer": n.environment.name if n.environment else None,
                        "online": agent_hub.is_online(n.id), "inventory_at": n.inventory_at, "software": hits[:50]})
    return out


# ------------------------------------------------------------------ remote actions
ACTIONS = ("defender_quick_scan", "defender_full_scan", "defender_update", "gpupdate", "message")
_USER_RE = re.compile(r"^[A-Za-z0-9._ $-]{1,64}$")


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def action_command(kind: str, action: str, *, message: str | None = None) -> tuple[str, int]:
    """The fixed command for an action and its timeout. Only these commands can be sent this way."""
    if action not in ACTIONS:
        raise NodeError(f"unknown action; use one of: {', '.join(ACTIONS)}")
    if action == "message":
        text = " ".join((message or "").split())
        if not text or len(text) > 500:
            raise NodeError("give a message of at most 500 characters")
        if kind == "windows":
            return f"msg * /TIME:600 {_ps_quote(text)}; 'sent'", 60
        return f"wall {_sh_quote(text)} && echo sent", 30
    if kind != "windows":
        raise NodeError("this action is only available on Windows")
    mp = "Join-Path $env:ProgramFiles 'Windows Defender\\MpCmdRun.exe'"
    if action == "defender_quick_scan":
        return f"Start-Process -FilePath ({mp}) -ArgumentList '-Scan','-ScanType','1' -WindowStyle Hidden; 'started'", 60
    if action == "defender_full_scan":
        return f"Start-Process -FilePath ({mp}) -ArgumentList '-Scan','-ScanType','2' -WindowStyle Hidden; 'started'", 60
    if action == "defender_update":
        return ("Update-MpSignature -ErrorAction Stop; $m=Get-MpComputerStatus; "
                "'signatures ' + $m.AntivirusSignatureVersion + ' from ' + $m.AntivirusSignatureLastUpdated"), 600
    return "gpupdate /target:computer /force", 300


def logoff_command(kind: str, user: str) -> str:
    u = (user or "").strip().split("\\")[-1]
    if not _USER_RE.match(u):
        raise NodeError("give a valid user name")
    if kind == "windows":
        return ("$u=" + _ps_quote(u) + ";$n=0;foreach($l in @(quser 2>$null|Select-Object -Skip 1)){"
                "$f=(($l -replace '^>',' ').Trim()) -split '\\s+';if($f[0] -ieq $u){"
                "$id=$f|?{$_ -match '^\\d+$'}|Select-Object -First 1;if($id){logoff $id;$n++}}};"
                "\"logged off $n session(s)\"")
    return f"loginctl terminate-user {_sh_quote(u)} && echo 'logged off'"


async def _run(node_id: str, command: str, timeout: int) -> dict[str, Any]:
    if not agent_hub.is_online(node_id):
        raise NodeError("the machine is offline")
    res = await agent_hub.request(node_id, "exec", {"command": command, "timeout": timeout}, timeout=timeout + 30) or {}
    out = {"ok": res.get("exit_code") == 0 and not res.get("timed_out"), "exit_code": res.get("exit_code"),
           "output": ((res.get("stdout") or "") + (res.get("stderr") or "")).strip()[-4000:]}
    return out


async def run_action(n: Node, action: str, *, message: str | None = None) -> dict[str, Any]:
    if not _is_ws(n) and action != "message":
        # Defender and Group Policy actions are meant for workstations; servers have their own tools.
        if n.kind not in KINDS:
            raise NodeError("not a Linux or Windows machine")
    cmd, timeout = action_command(n.kind, action, message=message)
    res = await _run(n.id, cmd, timeout)
    res["action"] = action
    res["machine"] = n.name
    return res


async def log_off(n: Node, user: str) -> dict[str, Any]:
    if n.kind not in KINDS:
        raise NodeError("not a Linux or Windows machine")
    res = await _run(n.id, logoff_command(n.kind, user), 60)
    res["action"] = "logoff"
    res["machine"] = n.name
    res["user"] = user
    return res


async def diagnose(n: Node, *, refresh: bool = True) -> dict[str, Any]:
    """One call to see what is wrong with a workstation: inventory (refreshed when older than an
    hour and the machine is online), security checks, the agent's health check and findings."""
    online = agent_hub.is_online(n.id)
    notes: list[str] = []
    if refresh and online and (n.inventory_at is None or n.inventory_at < datetime.now(UTC) - timedelta(hours=1)):
        try:
            await collect(n.id)
        except NodeError as e:
            notes.append(str(e))
        async with sessionmaker()() as db:
            n = await db.get(Node, n.id) or n
    health = None
    if online:
        try:
            health = await nodes.node_call(n.id, "diagnose", {})
        except (AgentError, NodeError) as e:
            notes.append(f"health check failed: {e}")
    d = detail(n)
    inv = d.pop("inventory")
    d["inventory"] = {k: v for k, v in inv.items() if k != "software"}
    d["health"] = health
    d["findings"] = [c["text"] for c in d["checks"] if c["status"] in ("warning", "critical")] + \
        list((health or {}).get("findings") or [])
    if not online:
        d["findings"].insert(0, "The workstation is offline or asleep; the data is from when it was last seen.")
    d["notes"] = notes
    d["next_steps"] = ("Use workstation_action for a Defender scan or signature update, install_node_updates "
                       "for pending updates (in the maintenance window), reboot_node for a pending reboot, "
                       "find_software to see which machines have a program.")
    return d
