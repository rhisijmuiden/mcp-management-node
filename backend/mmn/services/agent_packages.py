"""Agent builds shipped with this release (``<install dir>/current/agent``) and the install script."""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mmn.config import get_settings

NAME_RE = re.compile(r"^mmn-agent[A-Za-z0-9_.-]*$")


def dist_dir() -> Path:
    return get_settings().agent_dist_dir


@lru_cache(maxsize=4)
def _manifest(path: str, mtime: float) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def manifest() -> dict[str, Any]:
    f = dist_dir() / "manifest.json"
    try:
        return _manifest(str(f), f.stat().st_mtime)
    except OSError:
        return {}


def bundled_version() -> str | None:
    return manifest().get("version")


def file_path(name: str) -> Path | None:
    if not NAME_RE.match(name) or name not in manifest().get("files", {}):
        return None
    p = dist_dir() / name
    return p if p.is_file() else None


def package_for(os_name: str | None, arch: str | None, install_kind: str | None) -> tuple[str, dict] | None:
    """The file an agent on this platform should update to."""
    m = manifest()
    files = m.get("files", {})
    version = m.get("version")
    arch = arch or "amd64"
    if os_name == "windows":
        # Windows (MSI or manual install) swaps the executable itself.
        name = f"mmn-agent-windows-{arch}.exe"
    elif install_kind == "deb":
        name = f"mmn-agent_{version}_{arch}.deb"
    else:
        name = f"mmn-agent-{os_name or 'linux'}-{arch}"
    return (name, files[name]) if name in files else None


def lan_ip(towards: list[str] | None = None) -> str | None:
    """This server's own address on the network that reaches ``towards`` (the reverse proxy), else the
    default route. A UDP connect only picks the route; no packet is sent."""
    candidates: list[str] = []
    for entry in towards or []:
        try:
            net = ipaddress.ip_network(str(entry).strip(), strict=False)
        except ValueError:
            continue
        candidates.append(str(net.network_address if net.num_addresses == 1 else next(net.hosts(), net.network_address)))
    candidates += ["192.0.2.1", "2001:db8::1"]
    for target in candidates:
        fam = socket.AF_INET6 if ":" in target else socket.AF_INET
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.connect((target, 9))
                ip = s.getsockname()[0]
        except OSError:
            continue
        if ip and not ipaddress.ip_address(ip).is_loopback and not ipaddress.ip_address(ip).is_unspecified:
            return ip
    return None


def _hostport(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def agent_endpoint_info(agents: dict[str, Any], general: dict[str, Any], request_host: str | None = None,
                        proxy: dict[str, Any] | None = None) -> tuple[str, str]:
    """(host:port that agents connect to, where it came from: address | lan_ip | base_url | request).

    Agents speak mTLS with a pinned CA, so they must reach this server's agent port directly or through a
    TCP stream proxy. Behind a reverse proxy the public name in base_url usually resolves to the router
    or gateway, which answers on that port with its own certificate, so then the LAN address is used.
    """
    port = int(agents.get("port") or 8443)
    addr = (agents.get("address") or "").strip()
    if addr:
        addr = re.sub(r"^[a-z]+://", "", addr).split("/")[0]
        if addr.startswith("["):
            return (addr if "]:" in addr else f"{addr}:{port}"), "address"
        return (addr if ":" in addr else f"{addr}:{port}"), "address"
    if (proxy or {}).get("mode") == "proxy":
        ip = lan_ip((proxy or {}).get("trusted_proxies") or [])
        if ip:
            return _hostport(ip, port), "lan_ip"
    host = urlparse(general.get("base_url") or "").hostname
    if host:
        return _hostport(host, port), "base_url"
    try:
        req = urlparse("//" + (request_host or "")).hostname
    except ValueError:
        req = None
    return _hostport(req or "localhost", port), "request"


def agent_endpoint(agents: dict[str, Any], general: dict[str, Any], request_host: str | None = None,
                   proxy: dict[str, Any] | None = None) -> str:
    """host:port that agents connect to."""
    return agent_endpoint_info(agents, general, request_host, proxy)[0]


INSTALL_SCRIPT = r"""#!/usr/bin/env bash
# Stackpilot: install the agent on this Proxmox VE node or Linux server and pair it.
#   curl -fsSL __APP__/api/agent/install.sh | bash -s -- --code ABCD-EFGH-JKLM
# Debian, Ubuntu and Proxmox VE get the .deb package; other distributions with systemd (RHEL, Rocky,
# Alma, Fedora, SUSE, Arch, ...) get the binary in /usr/local/bin plus a systemd unit.
set -euo pipefail
APP_URL=__APP_Q__
SERVER=__SERVER_Q__
FINGERPRINT=__FP_Q__
VERSION=__VERSION_Q__
declare -A SHA=(__SHAS__)
CODE="" NAME="" FORCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code) CODE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --force) FORCE="--force"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -n "$CODE" ]] || { echo "usage: install.sh --code <pairing code from the app>" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum (coreutils) is required" >&2; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fetch() { # fetch NAME: download and verify one file from the app
  [[ -n "${SHA[$1]:-}" ]] || { echo "no agent build $1 in this release" >&2; exit 1; }
  echo "==> downloading $1"
  # --insecure: the web address may use a self-signed certificate; the SHA-256 below (from this script)
  # is what makes the download trustworthy.
  curl -fsSL --insecure -o "$TMP/$1" "$APP_URL/api/agent/download/$1"
  echo "${SHA[$1]}  $TMP/$1" | sha256sum -c --quiet - || { echo "checksum mismatch, aborting" >&2; exit 1; }
}
if command -v dpkg >/dev/null && command -v apt-get >/dev/null; then
  ARCH="$(dpkg --print-architecture)"
  PKG="mmn-agent_${VERSION}_${ARCH}.deb"
  fetch "$PKG"
  echo "==> installing the package"
  DEBIAN_FRONTEND=noninteractive dpkg -i "$TMP/$PKG" >/dev/null
  BIN=/usr/bin/mmn-agent
else
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || { echo "this installer needs systemd" >&2; exit 1; }
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
  esac
  FILE="mmn-agent-linux-$ARCH"
  fetch "$FILE"
  echo "==> installing /usr/local/bin/mmn-agent"
  install -m 0755 "$TMP/$FILE" /usr/local/bin/mmn-agent
  install -d -m 0700 /etc/mmn-agent /var/lib/mmn-agent
  cat > /etc/systemd/system/mmn-agent.service <<'UNIT'
[Unit]
Description=Stackpilot agent
Documentation=https://github.com/rhisijmuiden/mcp-management-node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mmn-agent run
Restart=always
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
  # SELinux (RHEL family): give the binary the label of system binaries.
  command -v restorecon >/dev/null && restorecon /usr/local/bin/mmn-agent 2>/dev/null || true
  systemctl daemon-reload
  BIN=/usr/local/bin/mmn-agent
fi
echo "==> pairing with $SERVER"
ARGS=(--server "$SERVER" --fingerprint "$FINGERPRINT" --code "$CODE")
[[ -n "$NAME" ]] && ARGS+=(--name "$NAME")
[[ -n "$FORCE" ]] && ARGS+=("$FORCE")
"$BIN" enroll "${ARGS[@]}"
systemctl enable --now mmn-agent.service >/dev/null
systemctl restart mmn-agent.service
echo "==> done: the server appears in Stackpilot within a few seconds"
"""

INSTALL_PS1 = r"""# Stackpilot: install the agent on this Windows server and pair it (run as Administrator).
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm '__APP__/api/agent/install.ps1'))) -Code ABCD-EFGH-JKLM"
param([Parameter(Mandatory = $true)][string]$Code, [string]$Name = '', [string]$Server = __SERVER_PS__)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$AppUrl = __APP_PS__
$Fingerprint = __FP_PS__
$File = 'mmn-agent-windows-amd64.msi'
$Sha = __SHA_PS__
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in PowerShell as Administrator.' }
if (-not $Sha) { throw 'This build of Stackpilot has no Windows installer.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tmp = Join-Path $env:TEMP $File
Write-Host "==> downloading $File"
Invoke-WebRequest -UseBasicParsing -Uri "$AppUrl/api/agent/download/$File" -OutFile $tmp
if ((Get-FileHash -Algorithm SHA256 $tmp).Hash.ToLower() -ne $Sha) { Remove-Item $tmp; throw 'checksum mismatch, aborting' }
Write-Host '==> installing the service and pairing'
$msiArgs = @('/i', "`"$tmp`"", '/qn', '/norestart', "SERVER=$Server", "FINGERPRINT=$Fingerprint", "CODE=$Code")
if ($Name) { $msiArgs += "NAME=$Name" }
$p = Start-Process msiexec.exe -ArgumentList $msiArgs -Wait -PassThru
Remove-Item $tmp -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec failed with exit code $($p.ExitCode)" }
Write-Host '==> done: the server appears in Stackpilot within a few seconds'
"""


def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install_ps1(app_url: str, server: str, fingerprint: str) -> str:
    sha = manifest().get("files", {}).get("mmn-agent-windows-amd64.msi", {}).get("sha256", "")
    return (INSTALL_PS1.replace("__APP__", app_url).replace("__APP_PS__", _ps(app_url))
            .replace("__SERVER_PS__", _ps(server)).replace("__FP_PS__", _ps(fingerprint)).replace("__SHA_PS__", _ps(sha)))


def install_script(app_url: str, server: str, fingerprint: str) -> str:
    m = manifest()
    shas = " ".join(f"[{shlex.quote(n)}]={shlex.quote(v['sha256'])}" for n, v in m.get("files", {}).items()
                    if n.endswith(".deb") or re.fullmatch(r"mmn-agent-linux-[a-z0-9]+", n))
    return (INSTALL_SCRIPT.replace("__APP__", app_url)
            .replace("__APP_Q__", shlex.quote(app_url)).replace("__SERVER_Q__", shlex.quote(server))
            .replace("__FP_Q__", shlex.quote(fingerprint)).replace("__VERSION_Q__", shlex.quote(m.get("version", "")))
            .replace("__SHAS__", shas))
