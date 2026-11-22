#!/usr/bin/env bash
# Builds the agent for every supported platform and the Debian packages.
#
#   agent/build.sh --version 0.2.0 --out dist/agent
#
# Output: mmn-agent-linux-amd64, mmn-agent-linux-arm64, mmn-agent-windows-amd64.exe,
#         mmn-agent_<version>_amd64.deb, mmn-agent_<version>_arm64.deb,
#         mmn-agent-windows-amd64.msi (needs wixl from msitools) and manifest.json (sha256 of each).
set -euo pipefail

VERSION="" OUT="dist/agent"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -n "$VERSION" ]] || VERSION="$(tr -d '[:space:]' < "$HERE/../VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.a-z0-9]*)?$ ]] || { echo "invalid version: $VERSION" >&2; exit 2; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
command -v go >/dev/null || { echo "Go is required to build the agent" >&2; exit 1; }

build() { # build GOOS GOARCH output
  echo "==> agent $1/$2"
  ( cd "$HERE" && CGO_ENABLED=0 GOOS="$1" GOARCH="$2" go build -trimpath -ldflags "-s -w -X main.version=$VERSION" \
      -o "$OUT/$3" ./cmd/mmn-agent )
}
build linux amd64 mmn-agent-linux-amd64
build linux arm64 mmn-agent-linux-arm64
build windows amd64 mmn-agent-windows-amd64.exe

deb() { # deb ARCH
  local arch="$1" stage
  stage="$(mktemp -d)"
  install -D -m 0755 "$OUT/mmn-agent-linux-$arch" "$stage/usr/bin/mmn-agent"
  install -D -m 0644 "$HERE/packaging/mmn-agent.service" "$stage/lib/systemd/system/mmn-agent.service"
  install -d "$stage/DEBIAN"
  install -m 0755 "$HERE/packaging/debian/postinst" "$HERE/packaging/debian/prerm" "$HERE/packaging/debian/postrm" "$stage/DEBIAN/"
  local size
  size="$(du -sk "$stage/usr" "$stage/lib" | awk '{s+=$1} END {print s}')"
  cat > "$stage/DEBIAN/control" <<CTRL
Package: mmn-agent
Version: $VERSION
Architecture: $arch
Maintainer: RHIS <noreply@rhis.nl>
Installed-Size: $size
Depends: systemd
Section: admin
Priority: optional
Homepage: https://github.com/rhisijmuiden/stackpilot
Description: Agent for Stackpilot
 Connects this Proxmox VE node or Linux server to Stackpilot over an
 outgoing mutual-TLS connection: status, metrics, logs, services, package
 updates, commands and data copies, managed from one interface and through
 its MCP server.
CTRL
  dpkg-deb --root-owner-group --build "$stage" "$OUT/mmn-agent_${VERSION}_${arch}.deb" >/dev/null
  rm -rf "$stage"
  echo "==> mmn-agent_${VERSION}_${arch}.deb"
}
if command -v dpkg-deb >/dev/null; then
  deb amd64
  deb arm64
else
  echo "dpkg-deb not found: skipping .deb packages" >&2
fi

# Windows installer (service + pairing), built on Linux with wixl.
if command -v wixl >/dev/null; then
  msi_version="${VERSION%%[-+]*}"
  stage="$(mktemp -d)"
  cp "$OUT/mmn-agent-windows-amd64.exe" "$stage/mmn-agent.exe"
  sed -e "s|@MSI_VERSION@|$msi_version|g" -e "s|@VERSION@|$VERSION|g" -e "s|@EXE@|$stage/mmn-agent.exe|g" \
    "$HERE/packaging/windows/mmn-agent.wxs" > "$stage/mmn-agent.wxs"
  wixl -a x64 -o "$OUT/mmn-agent-windows-amd64.msi" "$stage/mmn-agent.wxs"
  rm -rf "$stage"
  echo "==> mmn-agent-windows-amd64.msi"
else
  echo "wixl not found: skipping the Windows installer (.msi)" >&2
fi

( cd "$OUT" && python3 - "$VERSION" <<'PY'
import hashlib, json, os, sys
files = {}
for name in sorted(os.listdir(".")):
    if name.startswith("mmn-agent") and os.path.isfile(name):
        files[name] = {"sha256": hashlib.sha256(open(name, "rb").read()).hexdigest(), "size": os.path.getsize(name)}
json.dump({"version": sys.argv[1], "files": files}, open("manifest.json", "w"), indent=1)
PY
)
echo "$OUT"
