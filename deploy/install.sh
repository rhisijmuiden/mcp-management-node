#!/usr/bin/env bash
# MCP Management Node installer for Debian 13 (trixie). Native install, no Docker.
#
# Quick start (production channel):
#   curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/mcp-management-node/main/deploy/install.sh | sudo bash
#
# Test channel, behind Zoraxy at 10.10.4.2:
#   curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/mcp-management-node/test/deploy/install.sh \
#     | sudo bash -s -- --channel test --proxy 10.10.4.2
#
# Options:
#   --channel production|test   which releases to install and follow (default: production)
#   --version vX.Y.Z[-test.N]   install this exact release
#   --tarball FILE              install from a local release tarball
#   --from-source [BRANCH]      build from the git branch instead of a release (default: main, or test)
#   --proxy IP[,IP...]          run behind a reverse proxy (Zoraxy) at these addresses / CIDRs
#   --restrict-to-proxy         only accept connections from the proxy
#   --direct                    serve HTTPS directly (self-signed certificate until you add one)
#   --server-name NAME          host name for Nginx (default: _ = any)
#   --base-path /PATH           serve under a sub-path, e.g. /mmn (default: /)
#   --github-token TOKEN        token for a private repository (also used for updates). Or set GITHUB_TOKEN.
#   --yes                       don't ask questions
#
# Running it again upgrades or repairs an installation and keeps data, passwords and settings.
set -euo pipefail

APP="mcp-management-node"
REPO="${MMN_GITHUB_REPO:-rhisijmuiden/mcp-management-node}"
INSTALL_DIR="/opt/$APP"
DATA_DIR="/var/lib/$APP"
ETC_DIR="/etc/$APP"
ENV_FILE="$ETC_DIR/mmn.env"
PROXY_FILE="$ETC_DIR/proxy.json"
APP_USER="mmn"

CHANNEL="production" VERSION_TAG="" TARBALL="" FROM_SOURCE=0 SOURCE_REF="" MODE="" PROXIES="" RESTRICT=false
SERVER_NAME="" BASE_PATH="" TOKEN="${GITHUB_TOKEN:-}" ASSUME_YES=0

c_blue=$'\e[38;5;111m' c_coral=$'\e[38;5;209m' c_green=$'\e[32m' c_dim=$'\e[2m' c_bold=$'\e[1m' c_off=$'\e[0m'
[[ -t 1 ]] || { c_blue="" c_coral="" c_green="" c_dim="" c_bold="" c_off=""; }
step() { echo "${c_blue}==>${c_off} ${c_bold}$*${c_off}"; }
info() { echo "    $*"; }
warn() { echo "${c_coral}!!${c_off} $*" >&2; }
die() { echo "${c_coral}error:${c_off} $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="$2"; shift 2 ;;
    --version) VERSION_TAG="$2"; shift 2 ;;
    --tarball) TARBALL="$2"; shift 2 ;;
    --from-source)
      FROM_SOURCE=1
      if [[ $# -gt 1 && "$2" != --* ]]; then SOURCE_REF="$2"; shift; fi
      shift ;;
    --proxy) MODE="proxy"; PROXIES="$2"; shift 2 ;;
    --restrict-to-proxy) RESTRICT=true; shift ;;
    --direct) MODE="direct"; shift ;;
    --server-name) SERVER_NAME="$2"; shift 2 ;;
    --base-path) BASE_PATH="$2"; shift 2 ;;
    --github-token) TOKEN="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,26p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ "$CHANNEL" == "production" || "$CHANNEL" == "test" ]] || die "--channel must be production or test"
[[ -z "$VERSION_TAG" || "$VERSION_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-test\.[0-9]+)?$ ]] || die "invalid --version"
[[ -z "$BASE_PATH" || "$BASE_PATH" =~ ^/[A-Za-z0-9._/-]*$ ]] || die "invalid --base-path"
[[ -n "$SOURCE_REF" ]] || { [[ "$CHANNEL" == "test" ]] && SOURCE_REF="test" || SOURCE_REF="main"; }
[[ "$SOURCE_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid branch name"

# Interactive questions read from the terminal, also when the script is piped into bash.
TTY=""
if [[ "$ASSUME_YES" == 0 ]] && { exec 3</dev/tty; } 2>/dev/null; then TTY=1; fi
ask() { # ask "question" default -> REPLY
  local q="$1" def="${2:-}"
  if [[ -z "$TTY" ]]; then REPLY="$def"; return; fi
  read -r -u 3 -p "    $q ${def:+[$def] }" REPLY || true
  REPLY="${REPLY:-$def}"
}

# ------------------------------------------------------------------ checks
[[ $EUID -eq 0 ]] || die "run as root (sudo bash install.sh)"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  if [[ "${ID:-}" != "debian" || "${VERSION_ID:-}" != "13" ]]; then
    warn "This installer is made for Debian 13; found ${PRETTY_NAME:-unknown}. Continuing anyway."
  fi
fi
command -v systemctl >/dev/null || die "systemd is required"

echo
echo "${c_bold}MCP Management Node${c_off} ${c_dim}installer · channel: $CHANNEL${c_off}"
echo

FRESH=1
[[ -f "$ENV_FILE" ]] && FRESH=0

# ------------------------------------------------------------------ questions (fresh install only)
if [[ -z "$MODE" && ! -f "$PROXY_FILE" ]]; then
  if [[ -n "$TTY" ]]; then
    step "How will people reach the app?"
    info "1) Behind a reverse proxy such as Zoraxy (the proxy does HTTPS)"
    info "2) Directly, with its own HTTPS"
    ask "Choose 1 or 2:" "1"
    if [[ "$REPLY" == "2" ]]; then MODE="direct"; else
      MODE="proxy"
      ask "IP address of Zoraxy (or a range like 10.10.4.0/24):" ""
      PROXIES="$REPLY"
      [[ -n "$PROXIES" ]] || { warn "No proxy address given; switching to direct HTTPS. Change it later in the app."; MODE="direct"; }
    fi
  else
    MODE="direct"
    info "No --proxy given: using direct HTTPS with a self-signed certificate. You can switch to Zoraxy in the app."
  fi
fi

# ------------------------------------------------------------------ packages
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
PKGS=(python3 python3-venv python3-packaging postgresql postgresql-client nginx ssl-cert curl ca-certificates tar openssh-client)
[[ "$FROM_SOURCE" == 1 ]] && PKGS+=(git nodejs npm)
apt-get update -qq
apt-get install -y -qq --no-install-recommends "${PKGS[@]}" >/dev/null
info "done"

# ------------------------------------------------------------------ user and folders
step "Creating user and folders"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR" "$DATA_DIR/run" "$DATA_DIR/files"
install -d -m 0750 -o root -g "$APP_USER" "$ETC_DIR"
install -d -m 0755 -o root -g root "$INSTALL_DIR" "$INSTALL_DIR/releases"

# ------------------------------------------------------------------ database
step "Setting up PostgreSQL"
systemctl enable --now postgresql >/dev/null 2>&1 || true
for _ in $(seq 1 30); do runuser -u postgres -- psql -qAtc 'select 1' >/dev/null 2>&1 && break; sleep 1; done
info "PostgreSQL $(runuser -u postgres -- psql -qAtc 'show server_version' | cut -d' ' -f1)"

if [[ "$FRESH" == 1 ]]; then
  DB_PASS="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  runuser -u postgres -- psql -q -v ON_ERROR_STOP=1 -v pw="$DB_PASS" <<'SQL' >/dev/null
SELECT format('CREATE ROLE mmn LOGIN PASSWORD %L', :'pw') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mmn')
\gexec
SELECT format('ALTER ROLE mmn LOGIN PASSWORD %L', :'pw')
\gexec
SELECT 'CREATE DATABASE mmn OWNER mmn ENCODING ''UTF8'' TEMPLATE template0' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mmn')
\gexec
SQL
  umask 027
  cat > "$ENV_FILE" <<EOF
# MCP Management Node settings. Read by the app (user $APP_USER) and the updater (root).
MMN_DATABASE_URL=postgresql+asyncpg://mmn:${DB_PASS}@127.0.0.1:5432/mmn
MMN_DATA_DIR=$DATA_DIR
MMN_INSTALL_DIR=$INSTALL_DIR
MMN_BASE_PATH=${BASE_PATH:-/}
MMN_GITHUB_REPO=$REPO
MMN_LISTEN_HOST=127.0.0.1
MMN_LISTEN_PORT=8740
EOF
  umask 022
  info "database 'mmn' created"
else
  info "existing installation found, keeping its database and settings"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

set_env() { # set_env KEY VALUE
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
[[ -n "$TOKEN" ]] && set_env MMN_GITHUB_TOKEN "$TOKEN"
[[ -n "$BASE_PATH" ]] && set_env MMN_BASE_PATH "$BASE_PATH"
TOKEN="${TOKEN:-$(sed -n 's/^MMN_GITHUB_TOKEN=//p' "$ENV_FILE")}"

# ------------------------------------------------------------------ get the release
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

gh_api() { # gh_api URL [accept] -> stdout
  local accept="${2:-application/vnd.github+json}"
  local -a hdr=(-H "Accept: $accept" -H "X-GitHub-Api-Version: 2022-11-28" -H "User-Agent: $APP-installer")
  [[ -n "$TOKEN" ]] && hdr+=(-H "Authorization: Bearer $TOKEN")
  if [[ "$accept" == "application/octet-stream" && -n "$TOKEN" ]]; then
    # Follow the storage redirect without sending the token there.
    local loc
    loc="$(curl -fsS -o /dev/null -w '%{redirect_url}' "${hdr[@]}" "$1")"
    if [[ -n "$loc" ]]; then curl -fsSL "$loc"; else curl -fsSL "${hdr[@]}" "$1"; fi
  else
    curl -fsSL "${hdr[@]}" "$1"
  fi
}

build_from_source() {
  step "Building from source (branch $SOURCE_REF)"
  local url="https://github.com/$REPO.git"
  [[ -n "$TOKEN" ]] && url="https://x-access-token:${TOKEN}@github.com/$REPO.git"
  git clone -q --depth 1 --branch "$SOURCE_REF" "$url" "$WORK/src" || die "could not clone $REPO ($SOURCE_REF)"
  local version tag
  version="$(tr -d '[:space:]' < "$WORK/src/VERSION")"
  # Source builds sort just below the first CI build, so the app offers that as an update.
  if [[ "$CHANNEL" == "test" ]]; then tag="v$version-test.0"; else tag="v$version"; fi
  bash "$WORK/src/deploy/build-release.sh" --tag "$tag" --channel "$CHANNEL" --out "$WORK/out" >/dev/null
  TARBALL="$WORK/out/$APP-$tag.tar.gz"
  VERSION_TAG="$tag"
}

if [[ -n "$TARBALL" ]]; then
  [[ -f "$TARBALL" ]] || die "tarball not found: $TARBALL"
  if [[ -z "$VERSION_TAG" ]]; then
    VERSION_TAG="$(basename "$TARBALL" | sed -n "s/^$APP-\(v.*\)\.tar\.gz$/\1/p")"
    [[ -n "$VERSION_TAG" ]] || die "pass --version for this tarball"
  fi
elif [[ "$FROM_SOURCE" == 1 ]]; then
  build_from_source
else
  step "Finding the latest $CHANNEL release"
  if gh_api "https://api.github.com/repos/$REPO/releases?per_page=50" > "$WORK/releases.json" 2>/dev/null; then
    PICK="$(python3 - "$WORK/releases.json" "$CHANNEL" "$VERSION_TAG" <<'PY'
import json, re, sys
from packaging.version import Version
rels, channel, wanted = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
def ver(tag):
    m = re.match(r"^v(\d+\.\d+\.\d+)(?:-test\.(\d+))?$", tag)
    if not m: return None
    return Version(m.group(1) + (f".dev{m.group(2)}" if m.group(2) else ""))
best = None
for r in rels:
    tag = r.get("tag_name", ""); v = ver(tag)
    if not v or r.get("draft"): continue
    if wanted and tag != wanted: continue
    if not wanted and channel == "production" and (r.get("prerelease") or v.is_devrelease): continue
    assets = {a["name"]: a["url"] for a in r.get("assets", [])}
    name = f"mcp-management-node-{tag}.tar.gz"
    if name not in assets or name + ".sha256" not in assets: continue
    if best is None or v > best[0]: best = (v, tag, assets[name], assets[name + ".sha256"])
if best: print(best[1], best[2], best[3])
PY
)"
  else
    PICK=""
    warn "Could not read releases from GitHub (private repository? pass --github-token)."
  fi
  if [[ -n "$PICK" ]]; then
    read -r VERSION_TAG ASSET_URL SUM_URL <<< "$PICK"
    info "release $VERSION_TAG"
    TARBALL="$WORK/$APP-$VERSION_TAG.tar.gz"
    gh_api "$ASSET_URL" application/octet-stream > "$TARBALL" || die "download failed"
    EXPECTED="$(gh_api "$SUM_URL" application/octet-stream | awk '{print $1}')"
    ACTUAL="$(sha256sum "$TARBALL" | awk '{print $1}')"
    [[ -n "$EXPECTED" && "$EXPECTED" == "$ACTUAL" ]] || die "checksum mismatch, download rejected"
    info "checksum verified"
  elif [[ -n "$VERSION_TAG" ]]; then
    die "release $VERSION_TAG not found"
  else
    warn "No $CHANNEL release found yet; building from the $SOURCE_REF branch instead."
    apt-get install -y -qq --no-install-recommends git nodejs npm >/dev/null
    build_from_source
  fi
fi

# ------------------------------------------------------------------ install
step "Installing $VERSION_TAG"
mkdir -p "$WORK/peek"
tar -xzf "$TARBALL" -C "$WORK/peek"
PEEK="$(find "$WORK/peek" -mindepth 1 -maxdepth 1 -type d | head -1)"
[[ -f "$PEEK/backend/mmn/updater_root.py" ]] || die "this is not an MCP Management Node release"
RELEASE="$(cd "$PEEK/backend" && MMN_INSTALL_DIR="$INSTALL_DIR" python3 -m mmn.updater_root install-tarball "$TARBALL" "$VERSION_TAG" | tail -1)"
info "installed in $RELEASE"
CURRENT="$INSTALL_DIR/current"

as_app() { # run a command as the app user with the app's environment
  runuser -u "$APP_USER" -- bash -c 'set -a; . "$0"; set +a; exec "$@"' "$ENV_FILE" "$@"
}

step "Migrating the database"
( cd "$DATA_DIR" && as_app "$CURRENT/venv/bin/mmn" migrate )
( cd "$DATA_DIR" && as_app "$CURRENT/venv/bin/mmn" agent-tls >/dev/null ) || warn "agent certificates not written yet; the app writes them on start"

step "Installing services"
install -m 0644 "$CURRENT"/deploy/systemd/*.service "$CURRENT"/deploy/systemd/*.path /etc/systemd/system/
cat > /usr/local/sbin/mmn-ctl <<EOF
#!/usr/bin/env bash
# Run MCP Management Node commands as the app user, e.g. 'sudo mmn-ctl setup-token'.
set -euo pipefail
[[ \$EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cd "$DATA_DIR"
exec runuser -u "$APP_USER" -- bash -c 'set -a; . "\$0"; set +a; exec "\$@"' "$ENV_FILE" "$CURRENT/venv/bin/mmn" "\$@"
EOF
chmod 0755 /usr/local/sbin/mmn-ctl
systemctl daemon-reload
systemctl enable --now "$APP-updater.path" >/dev/null 2>&1
systemctl enable "$APP.service" >/dev/null 2>&1
systemctl restart "$APP.service"

step "Configuring Nginx"
if [[ -n "$MODE" ]]; then
  python3 - "$WORK/proxy.json" "$MODE" "$PROXIES" "$RESTRICT" "${SERVER_NAME:-_}" "${BASE_PATH:-/}" <<'PY'
import json, sys
out, mode, proxies, restrict, name, base = sys.argv[1:]
json.dump({"mode": mode, "trusted_proxies": [p.strip() for p in proxies.replace(";", ",").split(",") if p.strip()],
           "restrict_to_proxy": restrict == "true", "server_name": name, "base_path": base}, open(out, "w"))
PY
  NGINX_CFG="$WORK/proxy.json"
elif [[ -f "$PROXY_FILE" ]]; then
  NGINX_CFG="$PROXY_FILE"
  if [[ -n "$SERVER_NAME$BASE_PATH" ]]; then
    python3 - "$PROXY_FILE" "$WORK/proxy.json" "$SERVER_NAME" "$BASE_PATH" <<'PY'
import json, sys
src, out, name, base = sys.argv[1:]
cfg = json.load(open(src))
if name: cfg["server_name"] = name
if base: cfg["base_path"] = base
json.dump(cfg, open(out, "w"))
PY
    NGINX_CFG="$WORK/proxy.json"
  fi
else
  echo '{"mode": "direct"}' > "$WORK/proxy.json"; NGINX_CFG="$WORK/proxy.json"
fi
"$CURRENT/venv/bin/python" -m mmn.updater_root apply-nginx "$NGINX_CFG"
chmod 0644 "$PROXY_FILE"
systemctl enable nginx >/dev/null 2>&1 || true
info "Nginx configured ($(python3 -c "import json;print(json.load(open('$PROXY_FILE'))['mode'])") mode)"

step "Starting"
OK=0
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null http://127.0.0.1:8740/api/health; then OK=1; break; fi
  sleep 1
done
if [[ "$OK" != 1 && "$(systemctl show -p ExecMainStatus --value "$APP.service")" == "226" ]]; then
  # 226/NAMESPACE: the container doesn't allow systemd sandboxing (LXC without nesting).
  warn "This container doesn't support systemd sandboxing; relaxing it for $APP."
  mkdir -p "/etc/systemd/system/$APP.service.d"
  cat > "/etc/systemd/system/$APP.service.d/container.conf" <<'EOF'
[Service]
PrivateTmp=no
PrivateDevices=no
ProtectSystem=no
ProtectHome=no
ProtectKernelTunables=no
ProtectKernelModules=no
ProtectKernelLogs=no
ProtectControlGroups=no
ProtectClock=no
ProtectHostname=no
RestrictNamespaces=no
ReadWritePaths=
EOF
  systemctl daemon-reload
  systemctl restart "$APP.service"
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null http://127.0.0.1:8740/api/health; then OK=1; break; fi
    sleep 1
  done
fi
[[ "$OK" == 1 ]] || die "the app did not start. See: journalctl -u $APP -n 50"
STATUS="$(curl -fsS http://127.0.0.1:8740/api/auth/status || echo '{}')"

# ------------------------------------------------------------------ summary
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
MODE_NOW="$(python3 -c "import json;print(json.load(open('$PROXY_FILE'))['mode'])")"
BASE_NOW="$(python3 -c "import json;print(json.load(open('$PROXY_FILE')).get('base_path','/'))")"
if [[ "$MODE_NOW" == "direct" ]]; then URL="https://$IP${BASE_NOW}"; else URL="http://$IP${BASE_NOW} (or your Zoraxy address)"; fi

echo
echo "${c_green}✓${c_off} ${c_bold}MCP Management Node $VERSION_TAG is running${c_off}"
echo "    Open:      $URL"
if [[ "$STATUS" == *'"setup_required":true'* ]]; then
  CODE="$(/usr/local/sbin/mmn-ctl setup-token)"
  echo "    Setup code: ${c_bold}$CODE${c_off}   (create the first admin with this code)"
fi
echo "    Channel:   $CHANNEL — switch and update later under Info & updates in the app"
echo "    Logs:      journalctl -u $APP -f"
echo "    Commands:  sudo mmn-ctl --help"
if [[ "$MODE_NOW" == "direct" ]]; then
  echo "    ${c_dim}The certificate is self-signed for now; your browser will warn once.${c_off}"
fi
echo
