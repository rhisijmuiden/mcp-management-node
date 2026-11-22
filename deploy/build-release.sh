#!/usr/bin/env bash
# Builds a release tarball of MCP Management Node.
#
#   deploy/build-release.sh --tag v0.1.0 --channel production --out dist [--wheels] [--commit SHA]
#
# Layout of mcp-management-node-<tag>.tar.gz:
#   mcp-management-node-<tag>/
#     VERSION
#     backend/      Python package (mmn) with VERSION and BUILD.json inside the package
#     frontend/     built single-page app (served by Nginx)
#     deploy/       installer and systemd units
#     agent/        agent binaries, .deb packages and manifest.json (needs Go)
#     wheels/       (optional) all Python dependencies as wheels for an offline install
set -euo pipefail

TAG="" CHANNEL="production" OUT="dist" WHEELS=0 COMMIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --wheels) WHEELS=1; shift ;;
    --commit) COMMIT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
[[ -n "$TAG" ]] || TAG="v$VERSION"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-test\.[0-9]+)?$ ]] || { echo "invalid tag: $TAG" >&2; exit 2; }
[[ -n "$COMMIT" ]] || COMMIT="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"

NAME="mcp-management-node-$TAG"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/$NAME"
mkdir -p "$DEST"

echo "==> building frontend"
( cd "$ROOT/frontend" && npm ci --no-audit --no-fund && npm run build )

echo "==> staging $NAME"
cp "$ROOT/VERSION" "$DEST/VERSION"
mkdir -p "$DEST/backend"
cp "$ROOT/backend/pyproject.toml" "$DEST/backend/"
[[ -f "$ROOT/backend/README.md" ]] && cp "$ROOT/backend/README.md" "$DEST/backend/"
cp -r "$ROOT/backend/mmn" "$DEST/backend/mmn"
find "$DEST/backend" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$ROOT/VERSION" "$DEST/backend/mmn/VERSION"
cat > "$DEST/backend/mmn/BUILD.json" <<JSON
{"version": "$VERSION", "tag": "$TAG", "channel": "$CHANNEL", "commit": "$COMMIT", "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON
cp -r "$ROOT/frontend/dist" "$DEST/frontend"
mkdir -p "$DEST/deploy"
cp -r "$ROOT/deploy/systemd" "$DEST/deploy/systemd"
cp "$ROOT/deploy/install.sh" "$ROOT/deploy/build-release.sh" "$DEST/deploy/"

echo "==> building agent"
bash "$ROOT/agent/build.sh" --version "${TAG#v}" --out "$DEST/agent"

if [[ "$WHEELS" == 1 ]]; then
  echo "==> building wheels"
  python3 -m venv "$STAGE/.wheelenv"
  WPIP=("$STAGE/.wheelenv/bin/pip" --no-cache-dir --disable-pip-version-check -q)
  "${WPIP[@]}" install -U pip setuptools wheel
  "${WPIP[@]}" wheel -w "$DEST/wheels" "$DEST/backend"
  # The installer builds the app from backend/ offline, so it needs the build backend too.
  "${WPIP[@]}" wheel -w "$DEST/wheels" setuptools wheel
fi

TARBALL="$OUT/$NAME.tar.gz"
tar -C "$STAGE" --owner=0 --group=0 --numeric-owner -czf "$TARBALL" "$NAME"
( cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
echo "$TARBALL"
