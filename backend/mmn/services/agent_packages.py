"""Agent builds shipped with this release (``<install dir>/current/agent``) and the install script."""

from __future__ import annotations

import json
import re
import shlex
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
        name = f"mmn-agent-windows-{arch}.exe"
    elif install_kind == "deb":
        name = f"mmn-agent_{version}_{arch}.deb"
    else:
        name = f"mmn-agent-{os_name or 'linux'}-{arch}"
    return (name, files[name]) if name in files else None


def agent_endpoint(agents: dict[str, Any], general: dict[str, Any], request_host: str | None = None) -> str:
    """host:port that agents connect to."""
    port = int(agents.get("port") or 8443)
    addr = (agents.get("address") or "").strip()
    if addr:
        addr = re.sub(r"^[a-z]+://", "", addr).split("/")[0]
        if addr.startswith("["):
            return addr if "]:" in addr else f"{addr}:{port}"
        return addr if ":" in addr else f"{addr}:{port}"
    host = urlparse(general.get("base_url") or "").hostname or (request_host or "").split(":")[0] or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{port}"


INSTALL_SCRIPT = r"""#!/usr/bin/env bash
# MCP Management Node: install the agent on this Proxmox VE node / Debian server and pair it.
#   curl -fsSL __APP__/api/agent/install.sh | bash -s -- --code ABCD-EFGH-JKLM
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
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$CODE" ]] || { echo "usage: install.sh --code <pairing code from the app>" >&2; exit 2; }
command -v dpkg >/dev/null || { echo "this installer needs a Debian based system (Proxmox VE, Debian, Ubuntu)" >&2; exit 1; }
ARCH="$(dpkg --print-architecture)"
PKG="mmn-agent_${VERSION}_${ARCH}.deb"
[[ -n "${SHA[$PKG]:-}" ]] || { echo "no agent package for architecture $ARCH" >&2; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo "==> downloading $PKG"
# --insecure: the web address may use a self-signed certificate; the SHA-256 below (from this script)
# is what makes the download trustworthy.
curl -fsSL --insecure -o "$TMP/$PKG" "$APP_URL/api/agent/download/$PKG"
echo "${SHA[$PKG]}  $TMP/$PKG" | sha256sum -c --quiet - || { echo "checksum mismatch, aborting" >&2; exit 1; }
echo "==> installing"
DEBIAN_FRONTEND=noninteractive dpkg -i "$TMP/$PKG" >/dev/null
echo "==> pairing with $SERVER"
ARGS=(--server "$SERVER" --fingerprint "$FINGERPRINT" --code "$CODE")
[[ -n "$NAME" ]] && ARGS+=(--name "$NAME")
[[ -n "$FORCE" ]] && ARGS+=("$FORCE")
/usr/bin/mmn-agent enroll "${ARGS[@]}"
systemctl enable --now mmn-agent.service >/dev/null
systemctl restart mmn-agent.service
echo "==> done: the node appears in MCP Management Node within a few seconds"
"""


def install_script(app_url: str, server: str, fingerprint: str) -> str:
    m = manifest()
    shas = " ".join(f"[{shlex.quote(n)}]={shlex.quote(v['sha256'])}" for n, v in m.get("files", {}).items()
                    if n.endswith(".deb"))
    return (INSTALL_SCRIPT.replace("__APP__", app_url)
            .replace("__APP_Q__", shlex.quote(app_url)).replace("__SERVER_Q__", shlex.quote(server))
            .replace("__FP_Q__", shlex.quote(fingerprint)).replace("__VERSION_Q__", shlex.quote(m.get("version", "")))
            .replace("__SHAS__", shas))
