# Installation

[← Back to the README](../README.md) · Step-by-step guide: [help site](https://rhisijmuiden.github.io/stackpilot/start/install/)

- [Requirements](#requirements)
- [Install](#install)
- [First steps](#first-steps)
- [Behind a reverse proxy (Zoraxy)](#behind-a-reverse-proxy-zoraxy)
- [Agents](#agents)
- [Updates](#updates)
- [File locations](#file-locations)
- [Internal names](#internal-names)
- [Development](#development)

## Requirements

- **Debian 13** (trixie), on a VM, an LXC container or bare metal.
- Outbound HTTPS to GitHub (releases and updates).
- Agents must be able to reach the server on port **8443** (TCP, mutual TLS). Nothing has to be opened at the
  machines themselves.

Stackpilot installs natively, no Docker: Python 3.13 (FastAPI, systemd), PostgreSQL 17 and Nginx serving the built
React app, all from the Debian repositories.

## Install

```bash
# Production channel
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/main/deploy/install.sh | sudo bash

# Test channel, behind a reverse proxy at 192.168.1.2
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/test/deploy/install.sh \
  | sudo bash -s -- --channel test --proxy 192.168.1.2
```

The installer prints the address and a one-time **setup code**; open the address and create the first admin.
Lost the code? `sudo mmn-ctl setup-token`.

Options (`--help`): `--channel production|test`, `--version vX.Y.Z`, `--tarball FILE`, `--from-source [BRANCH]`,
`--proxy IP[,IP]`, `--restrict-to-proxy`, `--direct`, `--server-name`, `--base-path /mmn`,
`--github-token` (only for a private repository). Running it again upgrades or repairs and keeps all data.

## First steps

1. Open the address and create the first admin with the setup code.
2. **Settings → General**: set the public address (for example `https://stackpilot.example.com`) and the
   default language (English or Dutch).
3. **Integrations → Mail server**: your SMTP server for alerts, approvals and reports.
4. **Settings → App backups**: add a backup location (FTP, FTPS, SFTP or SCP).
5. **Customers**: create your customers and add machines from each customer page.
6. **Integrations → Connected AI**: connect Claude, ChatGPT or Copilot to the MCP endpoint.

## Behind a reverse proxy (Zoraxy)

1. Install with `--proxy <Zoraxy IP>` (or change it later under **Settings → Web server & proxy**).
2. In Zoraxy add an HTTP proxy rule for your host name to this server on port 80, with TLS on and WebSockets allowed.
3. Set the public address (e.g. `https://stackpilot.example.com`) under **Settings → General**.

Nginx trusts `X-Forwarded-For` / `X-Forwarded-Proto` only from the configured proxy addresses, and can serve the app
under a sub-path (`--base-path /mmn`). The Web server page shows what the app receives from the proxy.

Zoraxy only has to pass the agent port (default **8443**) through as a **Stream Proxy (TCP)**: agents use mutual
TLS with this server, so Zoraxy must not terminate TLS on that port. See **Integrations → Agents & pairing**.

## Agents

Every managed machine runs `mmn-agent` (Go, no dependencies). It opens one outgoing WebSocket to
`<address>:8443` with a client certificate from the app's private CA, so nodes need no open ports.

1. **Proxmox → Nodes → Add node** creates a one-time pairing code (valid 24 hours) and shows the install command:
   `curl -fsSL https://<address>/api/agent/install.sh | bash -s -- --code XXXX-XXXX-XXXX`
2. The script installs the `.deb` (systemd unit `mmn-agent`), pins the CA fingerprint and enrolls.
3. The node appears with its status, CPU, RAM, disk, guests and available updates. Agents update themselves to
   the version bundled with the app (can be turned off).

Built for Linux amd64/arm64 (`.deb` and plain binary) and Windows amd64 (`.exe` and `.msi`, see `agent/build.sh`).
Source in [`agent/`](agent/).

### Stand-alone servers (Linux and Windows)

**Servers → Add server** creates the same kind of pairing code and shows the command for the chosen OS:

- **Linux** (Debian, Ubuntu, RHEL/Rocky/Alma, Fedora, SUSE, Alpine), as root:
  `curl -fsSL https://<address>/api/agent/install.sh | bash -s -- --code XXXX-XXXX-XXXX`
  Uses the `.deb` where dpkg exists, otherwise `/usr/local/bin/mmn-agent` with a systemd unit.
- **Windows** (Server 2016+ / Windows 10+), in PowerShell as Administrator:
  the `powershell … install.ps1 … -Code XXXX-XXXX-XXXX` one-liner shown in the dialog
  or silently with `msiexec /i mmn-agent.msi /qn SERVER=… FINGERPRINT=… CODE=…` (GPO, Intune, RMM).
  The agent runs as the Windows service `mmn-agent`; logs in `C:\ProgramData\mmn-agent\agent.log`.

Per server: services (systemd or Windows services), journal or event log, health check, installed software,
updates (apt, dnf/yum or Windows Update), a shell or PowerShell in the browser, commands and files.
Across servers: **Log search**, **Software & CVE** and **Data copy** (see the [feature guide](features.md#linux-and-windows-servers)).

## Updates

- `main` → production releases `vX.Y.Z` (published once per `VERSION`).
- `test` → test builds `vX.Y.Z-test.N` (prereleases).

**Info & updates** (the version number in the top bar) shows the version, checks for updates, switches channel and installs an
update. The privileged updater downloads the release again, verifies its SHA-256, backs up the database, migrates,
switches the `current` link and restarts; when the health check fails it rolls back the code and database.
Running the installer again also upgrades or repairs and keeps all data.

## File locations

| What | Where |
| --- | --- |
| App (venv, built frontend) | `/opt/mcp-management-node/current` → `releases/<tag>-<time>` |
| Settings (database URL, …) | `/etc/mcp-management-node/mmn.env` (root:mmn 0640) |
| Web server settings | `/etc/mcp-management-node/proxy.json`, rendered to Nginx |
| Data (encryption key, files, status) | `/var/lib/mcp-management-node` |
| Services | `mcp-management-node.service`, `mcp-management-node-updater.path` |
| Logs | `journalctl -u mcp-management-node -f` |

## Internal names

The product was called *MCP Management Node* (and before that *Proxmox Management*) up to 0.6.2 and is now
**Stackpilot**; the repository moved to `rhisijmuiden/stackpilot` in 0.6.4 (GitHub redirects the old URLs). The
internal names stay the same, so existing installs and agents keep working without any action: the services
`mcp-management-node`, `mcp-management-node-updater` and `mmn-agent`, the paths `/opt/mcp-management-node`,
`/etc/mcp-management-node`, `/etc/mmn-agent`, the commands `mmn-ctl` and `mmn-agent`, the `.deb`/MSI package names,
the release file names, the Windows install folder and registry key, and the production/test update channels.

## Development

```bash
# Backend (needs PostgreSQL)
cd backend && python3.13 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
export MMN_DATABASE_URL=postgresql+asyncpg://mmn:mmn@127.0.0.1:5432/mmn MMN_DATA_DIR=/tmp/mmn MMN_INSECURE_COOKIES=true
mmn migrate && mmn serve          # API on 127.0.0.1:8740
pytest                            # tests (see tests/conftest.py for the test database)

# Frontend
cd frontend && npm ci && npm run dev   # http://127.0.0.1:5173, proxies /api and /mcp to the backend
node scripts/check-i18n.mjs            # English and Dutch have the same keys
```

Release tarballs are built by `deploy/build-release.sh` (used by the Release workflow); it also builds the agent
with `agent/build.sh`, so Go 1.24 is needed. Agent tests: `cd agent && go test ./...`.

The MCP tool reference in [`docs/mcp-tools.md`](mcp-tools.md) is generated from the code. After adding or changing a
tool, run (with the backend installed):

```bash
python backend/scripts/gen_mcp_reference.py
```
