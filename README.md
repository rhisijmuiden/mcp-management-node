# MCP Management Node

One modern management interface for Proxmox VE environments and stand-alone servers, with a built-in
MCP server so Claude, ChatGPT or Copilot can answer questions and carry out tasks, with every action logged.

> Phase 1 (this release) is the foundation: sign-in, users, mail server, backups with restore, updates
> from the interface, web server / Zoraxy settings, the MCP server with OAuth, and the audit log.
> Proxmox environments and agent-managed servers follow in the next phases.

The interface is in English and Dutch, in the RHIS house style (see [`branding/`](branding/)).

## Install (Debian 13)

Native install, no Docker: Python 3.13 (FastAPI, systemd), PostgreSQL 17 and Nginx serving the built React app.

```bash
# Production channel
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/mcp-management-node/main/deploy/install.sh | sudo bash

# Test channel, behind Zoraxy at 10.10.4.2
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/mcp-management-node/test/deploy/install.sh \
  | sudo bash -s -- --channel test --proxy 10.10.4.2
```

The installer prints the address and a one-time **setup code**; open the address and create the first admin.
Lost the code? `sudo mmn-ctl setup-token`.

Options (`--help`): `--channel production|test`, `--version vX.Y.Z`, `--tarball FILE`, `--from-source [BRANCH]`,
`--proxy IP[,IP]`, `--restrict-to-proxy`, `--direct`, `--server-name`, `--base-path /mmn`,
`--github-token` (only for a private repository). Running it again upgrades or repairs and keeps all data.

| What | Where |
| --- | --- |
| App (venv, built frontend) | `/opt/mcp-management-node/current` → `releases/<tag>-<time>` |
| Settings (database URL, …) | `/etc/mcp-management-node/mmn.env` (root:mmn 0640) |
| Web server settings | `/etc/mcp-management-node/proxy.json`, rendered to Nginx |
| Data (encryption key, files, status) | `/var/lib/mcp-management-node` |
| Services | `mcp-management-node.service`, `mcp-management-node-updater.path` |
| Logs | `journalctl -u mcp-management-node -f` |

## Behind Zoraxy

1. Install with `--proxy <Zoraxy IP>` (or change it later under **Administration → Web server & proxy**).
2. In Zoraxy add an HTTP proxy rule for your host name to this server on port 80, with TLS on and WebSockets allowed.
3. Set the public address (e.g. `https://mcp.example.com`) under **Administration → General**.

Nginx trusts `X-Forwarded-For` / `X-Forwarded-Proto` only from the configured proxy addresses, and can serve the app
under a sub-path (`--base-path /mmn`). The Web server page shows what the app receives from the proxy.

## Updates, production and test

- `main` → production releases `vX.Y.Z` (published once per `VERSION`).
- `test` → test builds `vX.Y.Z-test.N` (prereleases).

**Info & updates** (bottom of the menu) shows the version, checks for updates, switches channel and installs an
update. The privileged updater downloads the release again, verifies its SHA-256, backs up the database, migrates,
switches the `current` link and restarts; when the health check fails it rolls back the code and database.

## MCP server

The MCP endpoint is `https://<your address>/mcp` (Streamable HTTP). Claude, ChatGPT and VS Code connect with
OAuth 2.1 (dynamic client registration + PKCE); scripts can use a personal API token. Each connection only gets the
permissions you tick (read, backups, restore, updates, settings), destructive tools require confirmation of the exact
target, and every call is recorded in the **AI activity log**. Step-by-step instructions are in the app under
**Help → Connect Claude, ChatGPT or Copilot**.

## Backups

Full and incremental backups (database per table + data files, SHA-256 manifest, optional AES-256-GCM encryption)
to FTP, FTPS (explicit/implicit), SFTP or SCP, on a schedule or on demand, with restore from the remote location.

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

Release tarballs are built by `deploy/build-release.sh` (used by the Release workflow).
