# Stackpilot

One modern management interface for the IT of all your customers (Proxmox VE nodes, stand-alone servers,
workstations and UniFi networks), with a built-in MCP server so Claude, ChatGPT or Copilot can answer questions and
carry out tasks, with every action logged.

> Phase 1 laid the foundation: sign-in, users, mail server, backups with restore, updates from the interface,
> web server / Zoraxy settings, the MCP server with OAuth, and the audit log.
> Phase 2 added environments (Home plus one per customer), the agent on every Proxmox node, the
> overview dashboard, package updates from the interface, notification mail per environment, the CPU governor per
> node, and server administration through the agent (logs, services, packages, files, commands) for people and AI.
> Phase 3 added VMs and containers: power actions, snapshots, migration, the console in the browser
> (noVNC for VMs, a terminal for containers and node shells), creating guests from the template library,
> community-scripts.org, an ISO or a cloud image, the CT template library with nightly sync to all nodes, and
> scheduled tasks (updates in containers, node updates, template sync, custom commands) with snapshot, rollback and
> a mailed report.
> Phase 4 added network management (bridges and VLANs with a graphical overview of which guests use
> them, the firewall, SDN with a step-by-step designer), cluster features (HA, replication, Proxmox backup jobs,
> storage), monitoring of all nodes with trends, SMART and mailed alerts, and deployments from GitHub repositories
> into containers.
> Phase 5 added stand-alone **Linux and Windows servers** in their own Servers section, managed by the
> same agent (Windows service with an MSI installer, event log, PowerShell, Windows Update), plus troubleshooting
> across servers for people and AI: log search on many servers at once, error overviews, health checks, a software
> and CVE inventory, and data copies between servers.

> Phase 6 added **UniFi**: UniFi Network (local Integration API), the UniFi Site Manager cloud API and
> UniFi Access, per environment/customer, in a UniFi section of the menu, with MCP tools so an assistant can answer
> "why can't this laptop get on WiFi?", "which APs need firmware?", "is guest isolated from the LAN?" and "who opened
> the server room last night?".

> Phase 7 (this release) makes **customers** the top layer. The former environments are now customers ("Home" is
> one too) with contact details, contacts, notes, a maintenance window and an SLA. A customer switcher at the top of
> the menu limits the whole interface to one customer; every customer has a page with Overview, Proxmox, Servers,
> Workstations, Network, AI access, Contacts & notes and Reports. Linux and Windows machines can be marked as
> workstation (PC or laptop). API tokens and AI apps connected through OAuth can be tied to one or more customers: they
> then only see those customers' machines, UniFi connections, jobs and alerts, enforced in the database layer for every
> tool. New MCP tools: `list_customers`, `get_customer_overview`, `optimisation_scan` ("what can be optimised at
> customer X"), `get_customer_context`, `create_customer` and `update_customer`; most tools take `customer` instead of
> `environment`; the customer notes are the MCP resources `stackpilot://customers` and `stackpilot://customers/<id>`.
> Existing installs migrate automatically: every node, server, UniFi connection and agent stays where it was.

> **Name.** The product was called *MCP Management Node* (and before that *Proxmox Management*) up to 0.6.2 and is
> now **Stackpilot**; the repository moved to `rhisijmuiden/stackpilot` in 0.6.4 (GitHub redirects the old URLs).
> The internal names stay the same, so existing installs and agents keep working without any action: the services
> `mcp-management-node`, `mcp-management-node-updater` and `mmn-agent`, the paths `/opt/mcp-management-node`,
> `/etc/mcp-management-node`, `/etc/mmn-agent`, the commands `mmn-ctl` and `mmn-agent`, the `.deb`/MSI package names,
> the release file names, the Windows install folder and registry key, and the production/test update channels.
> The updater rewrites an old repository name in `mmn.env` by itself, and a saved app name or mail sender name that
> still holds the old default shows as Stackpilot.

The interface is in English and Dutch, in the RHIS house style (see [`branding/`](branding/)).

## Install (Debian 13)

Native install, no Docker: Python 3.13 (FastAPI, systemd), PostgreSQL 17 and Nginx serving the built React app.

```bash
# Production channel
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/main/deploy/install.sh | sudo bash

# Test channel, behind Zoraxy at 10.10.4.2
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/test/deploy/install.sh \
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

## Customers

Everything hangs under a customer: create them under **Customers**, then add machines from the customer page
(**Add machine**: Proxmox VE node, server or workstation) or move existing ones under **Manage** on the machine's page.
The switcher at the top of the menu limits every page to one customer. Notes on the **Contacts & notes** tab
("never restart the SQL server during office hours") and the maintenance window are given to the AI before it works
on that customer. Under **AI access**, or when you create a token or connect an AI app, choose *Only these customers*
to keep an assistant inside one customer; tools that act on the whole app (app updates and backups, mail server, audit
log, template library, scheduled tasks) are then refused.

## Behind Zoraxy

1. Install with `--proxy <Zoraxy IP>` (or change it later under **Administration → Web server & proxy**).
2. In Zoraxy add an HTTP proxy rule for your host name to this server on port 80, with TLS on and WebSockets allowed.
3. Set the public address (e.g. `https://mcp.example.com`) under **Administration → General**.

Nginx trusts `X-Forwarded-For` / `X-Forwarded-Proto` only from the configured proxy addresses, and can serve the app
under a sub-path (`--base-path /mmn`). The Web server page shows what the app receives from the proxy.

Zoraxy only has to pass the agent port (default **8443**) through as a **Stream Proxy (TCP)**: agents use mutual
TLS with this server, so Zoraxy must not terminate TLS on that port. See **Administration → Agents & pairing**.

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
Across servers: **Log search**, **Software & CVE** and **Data copy** (agent to agent through this app, so the servers
don't need to reach each other; the size limit is set under Agents & pairing).

## VMs and containers

- **Proxmox → VMs & containers** lists every guest of every node (reported by the agents every 20 seconds). Per guest:
  start, shut down, stop, reboot, snapshots (take, roll back, delete), migrate within the cluster, CPU/memory/tags,
  delete, and the console, inline or in its own tab (VMs via noVNC with Ctrl+Alt+Del, containers via xterm.js).
- **Create** offers a container from the template library, a community script (the script is shown first and runs
  as root on the chosen node; default or interactive mode), a VM from an ISO (existing or downloaded) and a VM from
  a cloud image with cloud-init. The node defaults to the least loaded one (60 % memory, 40 % CPU).
- **CT templates** keeps one list of official (pveam) and custom templates (uploaded or downloaded, stored in the app
  and its backups) and shows per node whether each one is current, outdated or missing. "Push to all nodes" and the
  nightly task place them on every node.
- **Scheduled tasks** run community-script updates or `apt-get update && full-upgrade` in containers, node updates,
  the template sync or your own command, on a cron schedule (time zone configurable), with a snapshot first,
  rollback on failure and a report by mail. Every run is kept with its output per target.

The console is relayed by the app: the browser connects to the app, the app to the agent's outgoing connection, so
nodes still need no open ports and Zoraxy only has to pass WebSockets.

## Network, SDN and cluster

- **Proxmox → Network** shows every bridge with the VMs and containers on it, grouped by VLAN tag, plus SDN VNets
  and bridges guests point to that don't exist. Bridge changes are staged first and shown as a diff of
  `/etc/network/interfaces`. Applying needs the node name typed in; the node backs up the file, reloads, and restores
  the old file by itself when the app can't reach it again within 90 seconds. The firewall tab covers cluster, node
  and guest rules and options.
- **SDN designer** builds a zone in five steps (zone, VNets, subnets, nodes, review) with a live preview that marks
  what is new or changed compared with the cluster. The review lists every API change; rolling out needs the zone
  name, backs up `/etc/pve/sdn/*.cfg` first and restores it automatically when the reload fails. Every backup can be
  restored with one click under **Network → Config backups**.
- **Proxmox → Cluster & HA** manages HA resources and rules (or groups on older versions), storage replication,
  the cluster's own backup jobs (vzdump) and storage (directory, NFS, SMB/CIFS, Proxmox Backup Server).

## Monitoring

**Servers → Monitoring** keeps one sample per node per minute for 30 days and shows CPU, memory and disk with
trends over 1 hour to 30 days, storage usage and SMART/wearout of physical disks (collected every 10 minutes).
Alerts for offline nodes, high CPU, memory, disk or storage use and failing disks are mailed when they open and close.

## Deployments from GitHub

Add a fine-grained GitHub token (read access to Contents and Metadata) under **Proxmox → Deployments**, then pick a
repository, a production and a test branch, and a target container. The app clones the repository into the container,
runs your setup command once and your deploy command every time, writes environment variables to
`/etc/mmn-deploy/<name>.env`, restarts the systemd service you name, and takes a snapshot first that is restored
when the deployment fails. Presets cover an install script in the repo, Python, Node.js and static sites; everything
runs natively on Debian, no Docker. Deploy by hand, on every push (GitHub webhook to
`/api/hooks/github/<id>`, signed with a secret) or by checking for new commits every 5 minutes. **Create → GitHub**
makes a new container and deploys into it in one go.

## UniFi

**UniFi → Connections** links consoles per environment (customer). Only the official APIs are used, with API keys
that are stored encrypted (Fernet, like every other secret) and never shown again:

| Kind | API | Key |
| --- | --- | --- |
| UniFi Network | `https://<console>/proxy/network/integration/v1` (Network 9.0+) | Network → Settings → Control Plane → Integrations |
| Site Manager | `https://api.ui.com/v1` (all consoles and sites of an account) | unifi.ui.com → API |
| UniFi Access | `https://<console>:12445/api/v1/developer` | Access → Settings → General → Advanced → API Token |

A connection reaches its console **directly**, **through an agent** in the customer's network (the agent makes the
HTTPS call, so nothing needs a port forward; needs agent 0.6.0, which agents install themselves) or, for Network,
**through the Site Manager cloud connector**. Local consoles use self-signed certificates: pin the certificate's
SHA-256 fingerprint (**Fetch** reads it); the pin is checked during the TLS handshake, before the key is sent. Mark a
connection **read-only** to switch off all actions; use a key of a View Only admin for that.

Every 5 minutes the app polls each connection: devices with status, uplink, firmware and clients, WiFi with VLAN,
WAN state, doors. Differences become **events** (device offline/online, WAN change, uplink change, new firmware) and
offline devices or unreachable connections raise **monitoring alerts** (mailed like the others, can be switched off).
**UniFi → Network** shows all of it, with live device details, the client list, WiFi on/off, guest authorization,
restart and PoE power-cycle, a WiFi troubleshooter and a guest isolation check (from the zone-based firewall).
**UniFi → Access** shows doors and lock state, remote unlock, temporary lock rules, emergency lockdown/evacuation,
schedules and the access log. Every action is in the audit log.

## Updates, production and test

- `main` → production releases `vX.Y.Z` (published once per `VERSION`).
- `test` → test builds `vX.Y.Z-test.N` (prereleases).

**Info & updates** (bottom of the menu) shows the version, checks for updates, switches channel and installs an
update. The privileged updater downloads the release again, verifies its SHA-256, backs up the database, migrates,
switches the `current` link and restarts; when the health check fails it rolls back the code and database.

## MCP server

The MCP endpoint is `https://<your address>/mcp` (Streamable HTTP). Claude, ChatGPT and VS Code connect with
OAuth 2.1 (dynamic client registration + PKCE); scripts can use a personal API token. Each connection only gets the
permissions you tick (read, backups, restore, updates, settings, nodes, node updates, logs, services, exec, guests,
create and remove guests, templates, scheduled tasks, network, cluster, deployments, UniFi read, UniFi actions, doors),
destructive tools require confirmation of the exact target, commands and service actions need a stated reason, and
every call is recorded in the **AI activity log**. Through the agents an assistant can read logs and the journal,
check and restart services, find which servers have a package (e.g. nginx for a CVE), read files, run commands,
install updates and reboot nodes, and manage VMs, containers, snapshots, templates and scheduled tasks, read the network and change it with a dry run first, design and roll out SDN, manage HA, replication, backup jobs and storage, read monitoring and SMART data, start deployments, and look into UniFi networks and doors (devices, clients, WiFi problems, firmware, guest isolation, events, access log) and act on them when allowed. **Help → What the MCP can do** lists everything. Step-by-step instructions are in the app under
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

Release tarballs are built by `deploy/build-release.sh` (used by the Release workflow); it also builds the agent
with `agent/build.sh`, so Go 1.24 is needed. Agent tests: `cd agent && go test ./...`.
