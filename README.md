<p align="center">
  <img src="branding/logo/rhis-icon.svg" width="88" alt="Stackpilot">
</p>

<h1 align="center">Stackpilot</h1>

<p align="center">
  <strong>The AI gateway for your whole IT stack.</strong><br>
  One place to manage the Proxmox clusters, Linux and Windows servers, workstations and UniFi networks of every
  customer, and one MCP endpoint that lets Claude, ChatGPT or Copilot do the work: scoped, guarded, approved and logged.
</p>

<p align="center">
  <a href="https://github.com/rhisijmuiden/stackpilot/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/rhisijmuiden/stackpilot?label=release&color=14223D"></a>
  <a href="https://github.com/rhisijmuiden/stackpilot/actions/workflows/release.yml"><img alt="Release build" src="https://img.shields.io/github/actions/workflow/status/rhisijmuiden/stackpilot/release.yml?label=build"></a>
  <img alt="Debian 13" src="https://img.shields.io/badge/runs%20on-Debian%2013-FF6B57">
  <img alt="MCP tools" src="https://img.shields.io/badge/MCP%20tools-237-9CC3FF">
  <img alt="English and Dutch" src="https://img.shields.io/badge/languages-EN%20%7C%20NL-F6F7FB">
  <a href="https://rhisijmuiden.github.io/stackpilot/"><img alt="Help site" src="https://img.shields.io/badge/help-rhisijmuiden.github.io%2Fstackpilot-14223D"></a>
</p>

<p align="center">
  <a href="https://rhisijmuiden.github.io/stackpilot/"><strong>Help site</strong></a> ·
  <a href="#what-is-stackpilot">What is it?</a> ·
  <a href="#features">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/features.md">Feature guide</a> ·
  <a href="docs/mcp-tools.md">MCP tool reference</a> ·
  <a href="#roadmap">Roadmap</a> ·
  <a href="README.nl.md">Nederlands</a>
</p>

![Stackpilot command center](docs/images/command-center.png)

## What is Stackpilot?

Stackpilot is **one server from which you look after the IT of all your customers**. You install it once, on a
Debian 13 server or container. A small program, the **agent**, runs on every machine you manage (Proxmox VE nodes,
Linux and Windows servers, PCs and laptops) and reports to it over an encrypted connection that the machine itself
starts, so nothing has to be opened at the customer.

From one web interface you then see how every customer is doing: machines, backups, updates, certificates, networks,
Microsoft 365, tickets and costs. And because Stackpilot is also an **MCP server**, you can let an AI assistant
(Claude, ChatGPT, Copilot) do the work: *"Check customer Acme"*, *"Install the updates tonight"*, *"Why is this laptop
slow?"*. You decide what the AI may do by itself, what it must ask you first and what it may never do.

**Who it's for:** managed service providers (MSPs), freelance system administrators and IT people who look after
several sites, and anyone who wants the same tools for their own lab or home network.

**In short, Stackpilot is:**

1. an **IT management server** for many customers (an RMM-style tool you host yourself),
2. an **AI gateway** that gives your AI one safe connection to all of it, with policies, approvals and a full log,
3. an **MCP hub** that puts your other MCP servers (GitHub, VoIP, Home Assistant) behind that same connection.

> **New here?** The **[help site](https://rhisijmuiden.github.io/stackpilot/)** explains everything step by step, in
> English and Dutch: installing, adding customers and agents, connecting your AI, every integration, approvals and more.

## Why Stackpilot

Letting an AI assistant manage servers usually means a remote session per machine, no overview and no brakes.
Stackpilot turns that around: a small agent on every machine connects **outward** to one server, and that server
gives your AI **one MCP endpoint** for everything. You decide per risk level, per customer and per token what the AI
may do by itself, what it must ask first and what it may never do. Every call lands in the audit log, and one button
pauses all AI actions.

- **Customers first.** Everything hangs under a customer, so an MSP or freelance admin keeps each client apart, down
  to tokens that can only see one customer.
- **No open ports at customer sites.** Agents use outgoing WebSockets with mutual TLS; consoles, file copies and
  UniFi calls are relayed through that same connection.
- **Native and lightweight.** Python 3.13 (FastAPI), PostgreSQL 17 and Nginx on Debian 13, Go agents. No Docker.
- **English and Dutch**, light and dark mode, in the RHIS house style.

## Features

| | |
| --- | --- |
| 🤖 **AI gateway** | Command center, approvals inbox (app + mail), policies per risk level, customer and token, maintenance windows, kill switch, AI sessions and activity log. |
| 🧰 **237 MCP tools** | Read logs, restart services, run commands, patch, manage VMs, network, HA, deployments and UniFi, all through one endpoint with OAuth 2.1 or API tokens. [Full list](docs/mcp-tools.md). |
| 🏢 **Customers** | Customer switcher, customer page with Proxmox, servers, workstations, network and AI access, contacts, notes for the AI, maintenance window, SLA and an optimisation scan. |
| 💻 **Workstations** | PCs and laptops per customer: hardware, OS, logged-in user, installed software, disks and SSD wear, Windows Update history, Defender, BitLocker/LUKS, firewall, TPM, a security status, and remote actions (Defender scan, signature update, Group Policy refresh, message, log off). |
| 🌅 **Nightly checks & morning summary** | Every night every customer is checked: updates, security updates with the CVEs they fix, full disks, certificates that expire, failed or missing backups and the optimisation scan. In the morning one summary in the app and by mail, with what is new and resolved, and proposals you approve with one click; changes wait for the customer's maintenance window. |
| 📒 **Runbooks** | Fixed procedures per customer or for everyone (patch a server, new employee, new project via GitHub, restart a service) that Stackpilot runs step by step with its own tools: approval per step where the policy or the runbook says so, waiting for the maintenance window, manual steps for a person or the AI, and every step in the audit log. Edit them in the app or let the AI start them over MCP. |
| 💓 **Automatic recovery** | Known problems fix themselves within the policy: a stopped service is restarted, a full disk is cleaned up, an offline agent in a VM or container is brought back. Rules per customer link an alert or a nightly finding to a runbook; the problem has to be gone before it counts as fixed. Cooldown, a maximum number of attempts, a rate limit, a circuit breaker and one emergency stop; a Proxmox VE node is never rebooted. Results in the app, by mail, in the morning summary and in the monthly report. |
| 🛟 **Customer backups** | Coverage per customer: which VMs, containers, servers and workstations have a backup, when the last good one ran, size, failures, verify and offsite copy, including Veeam Backup & Replication restore points per VM. Alerts when a backup fails or is older than the customer's SLA, restore of a whole guest or single files (Proxmox Backup Server) with approval, Windows Server Backup status, and managing Proxmox Backup Server datastores, verify, sync and prune jobs. |
| 📄 **Monthly reports** | One report per customer per month (PDF and HTML): work performed with duration and result, availability, incidents, updates, Proxmox backups, workstation security, AI actions and recommendations, mailed to the customer on a set day or made on demand. |
| 🖥️ **Proxmox VE** | Nodes, VMs and containers, browser console (noVNC / xterm.js), snapshots, migration, create from templates, community scripts, ISO or cloud image, CT template library synced to all nodes. |
| 🌐 **Network & cluster** | Bridges and VLANs with a usage map, staged changes with diff and automatic rollback, firewall, SDN designer, HA, replication, backup jobs and storage. |
| 🧑‍💻 **Linux & Windows servers** | Same agent on any Linux server or Windows Server/PC (MSI): services, journal/event log, packages and Windows Update, shell/PowerShell, files, health checks. |
| 🔎 **Troubleshooting** | Log search across many servers, error overviews, software and CVE inventory, data copy between servers. |
| 📶 **UniFi** | UniFi Network, Site Manager and Access: devices, clients, WiFi troubleshooter, guest isolation check, firmware, events, doors and access log. **UniFi Fabrics**: one Site Manager key per Fabric, every console found and linked through the cloud connector, no address, port or agent needed. |
| 📈 **Monitoring** | One sample per node per minute for 30 days, trends, SMART and wearout, alerts by mail. |
| 📚 **Documentation & password vault** | Network plans, procedures, notes and contacts per customer in Markdown, with versions, encrypted attachments and linked machines; the AI reads them before it works on a customer. A password vault per customer (AES-256-GCM, its own key, key rotation, password history, generator): people see secrets after entering their password again, the AI only after an admin approved that one use, or it runs a script with the secret without ever seeing it. Every reveal, copy and use is in the access log. |
| ☁️ **Microsoft 365 & Entra ID** | Connect a customer's tenant with an app registration (client secret or certificate, kept in the password vault). Users with licences, admin roles, MFA and Entra ID risk, licences bought and assigned, security defaults and Conditional Access, and the sign-ins that stand out (risky, failed, from another country, old protocols). Alerts and morning-summary findings for risky users, admins without MFA, inactive or blocked-but-licensed accounts, licence shortages and an app secret that expires. Block, sign out, reset MFA or the password from the app; the AI only after an admin approved it. |
| 🎫 **Customer portal & tickets** | Customers sign in to their own portal, report problems and questions, link them to their computers and servers and follow the status. Stackpilot runs a first diagnosis on those machines right away; staff reply, add internal notes, assign and resolve, with mails through your own SMTP server. The AI reads and answers tickets over MCP. |
| 📡 **Uptime & SLA** | External checks per customer of websites (status, keyword, response time, certificate), TCP ports, ping, DNS records and VPN endpoints, from Stackpilot or from a machine at the customer; outages, alerts and the measured SLA against each customer's target in the monthly report. |
| 🔐 **Certificates, firewall & VPN** | Every certificate of a customer, from its websites and mail servers to the certificate stores on Linux, Proxmox and Windows, with warnings before it expires. Every firewall change (Proxmox, nftables/iptables, Windows Firewall, UniFi) as a diff that someone has to acknowledge, and VPN tunnels (WireGuard, IPsec, OpenVPN, Windows RRAS, UniFi) that alert when they stay down. |
| 🗓️ **Change calendar** | Plan maintenance and changes per customer, checked against the maintenance window, other changes and scheduled tasks, with the next free slot suggested. The customer gets a notice beforehand (3 days and 1 hour by default), when it moves or is cancelled, and afterwards, sees it in the portal and can subscribe to an iCal feed. |
| 🧭 **Navigation & integrations** | A top bar with a customer switcher (the whole app follows the customer you pick), Ctrl+K search and approvals; a customer overview with a tile for everything linked and *what is not set up yet*; an Integrations page with step-by-step guides for GitHub, Microsoft 365, UniFi, MCP servers, AI apps, agents, mail and backups; view the customer portal as the customer sees it. |
| 💶 **Costs & licences** | What every customer costs per month as the basis for invoicing: resources and machines at your price list, Microsoft 365 licences, contracts, licences and hardware, with a PDF/CSV statement per month and warnings before a contract, licence or warranty runs out or must be cancelled. |
| ⏱️ **Scheduled tasks & Proxmox maintenance** | Container and node updates, template sync or your own bash/PowerShell script on a cron schedule, with snapshot, rollback and a mailed report. **Proxmox maintenance** patches clusters node by node: checks before, machines moved away or shut down cleanly, restarts in Proxmox start-up order, checks after, and a timeline report in plain words for each customer. |
| 📦 **Software library & deployments** | Several GitHub tokens (one per owner, with expiry warnings), repositories as product, customer-specific or tooling, linked to customers with rights to deploy and for the AI, a repositories × customers matrix, new repositories created from Stackpilot, and deployments into a container or onto a Linux server (prod/test branch, on push or by polling, snapshot first in containers). |
| 💾 **App backups** | Full and incremental, encrypted (AES-256-GCM), to FTP, FTPS, SFTP or SCP, scheduled, with restore from the remote location. |
| ✉️ **Mail server** | Your own SMTP server with STARTTLS or TLS, minimum TLS 1.2/1.3 and DKIM signing, for alerts, approvals and reports. |
| 🔄 **Updates in the app** | Info & updates shows the version, checks GitHub, switches between production (`main`) and test (`test`) and installs, with database backup and automatic rollback. |

Every feature is explained step by step on the **[help site](https://rhisijmuiden.github.io/stackpilot/)**, and in
technical detail in the **[feature guide](docs/features.md)**.

## A look inside

| Approve what matters | Decide what the AI may do |
| --- | --- |
| ![Approval request](docs/images/approval.png) | ![Policies and guardrails](docs/images/policies.png) |
| **All your customers at a glance** | **Version, channel and updates** |
| ![Customers](docs/images/customers.png) | ![Info and updates](docs/images/info-updates.png) |

<details>
<summary>Dark mode and Dutch</summary>

![Command center in dark mode](docs/images/command-center-dark.png)
![Command center in Dutch](docs/images/command-center-nl.png)

</details>

*Screenshots from a demo install with fictional customers.*

## How it works

```mermaid
flowchart LR
    subgraph AI["AI assistants"]
        C["Claude"]
        G["ChatGPT"]
        V["Copilot / VS Code / n8n"]
    end

    subgraph SP["Stackpilot server (Debian 13)"]
        direction TB
        MCP["MCP endpoint /mcp<br/>OAuth 2.1 or API token"]
        GW["AI gateway<br/>scopes · customers · policies<br/>approvals · kill switch"]
        APP["FastAPI app + React UI"]
        DB[("PostgreSQL 17")]
        HUB["Agent hub :8443<br/>mutual TLS"]
        MCP --> GW --> APP
        APP --- DB
        APP --- HUB
    end

    Admin["You, in the browser"] --> APP

    subgraph Cust["Customer sites (no open ports)"]
        PVE["Proxmox VE nodes"]
        LIN["Linux servers"]
        WIN["Windows servers and PCs"]
        UNI["UniFi consoles"]
    end

    C & G & V --> MCP
    PVE & LIN & WIN -- "outgoing WebSocket" --> HUB
    HUB -. "relayed HTTPS" .-> UNI
    APP -. "mail · backups · GitHub" .-> EXT["SMTP · FTP/FTPS/SFTP/SCP · GitHub"]
```

1. **An AI calls a tool** on the MCP endpoint with its OAuth connection or API token.
2. **The gateway checks** the token's scopes and customers, then the policy for the tool's risk level (read, operate,
   change, destructive), the customer's maintenance window and the kill switch.
3. **It runs, waits or stops.** Allowed calls run right away. Calls that need approval answer `pending_approval`; you
   get a mail and an in-app notification, and after you approve, Stackpilot runs the call itself with the token's own
   rights. Blocked calls are refused.
4. **The agent does the work** on the machine through its outgoing connection, and the result plus every argument is
   written to the AI activity log.

More in [architecture](docs/architecture.md).

## Quick start

On a fresh **Debian 13** server or LXC container:

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/main/deploy/install.sh | sudo bash
```

The installer prints the address and a one-time **setup code**. Open the address, create the first admin, and then:

1. Create a customer under **Customers** (Home is already there).
2. Add a machine from the customer page: **Proxmox VE node**, **server** or **workstation**. Copy the one-line install
   command with its pairing code to the machine.
3. Under **Integrations → Connected AI**, copy the MCP endpoint and connect Claude, ChatGPT or Copilot
   (**Help → Connect Claude, ChatGPT or Copilot** walks you through it).
4. Check **Policies & guardrails**, add a **mail server** and a **backup location**.

Test channel, behind a reverse proxy, sub-paths, agents on Windows and every installer option:
see **[installation](docs/install.md)**.

## Documentation

| Document | What's in it |
| --- | --- |
| **[Help site](https://rhisijmuiden.github.io/stackpilot/)** | How everything works, step by step, in plain language (English and Dutch) |
| [Feature guide](docs/features.md) | Every part of the app, page by page |
| [MCP tool reference](docs/mcp-tools.md) | All 237 tools with risk level, scope and parameters (generated from the code) |
| [Installation](docs/install.md) | Install, upgrade, reverse proxy (Zoraxy), agents, file locations, development |
| [Architecture](docs/architecture.md) | Components, agent protocol, security model |
| [Release history](docs/history.md) | What each phase and release added |
| [House style](branding/huisstijlgids.md) | RHIS logo, colours and typography |

## Roadmap

| Phase | Topic | Status |
| --- | --- | --- |
| 1–6 | Foundation, Proxmox, VMs and containers, network/HA/deployments, Linux and Windows servers, UniFi | ✅ Done |
| 7 | Customers central | ✅ Done (v0.7.0) |
| 8 | AI gateway: command center, approvals, policies, kill switch | ✅ Done (v0.8.0) |
| 9 | **MCP hub**: register other MCP servers (Keystone VoIP, GitHub) and pass their tools through with the same auth, policies and audit; tool catalog with on/off per tool and token | ✅ Done (v0.9.0) |
| 10 | **Workstations and monthly report**: inventory and security status per PC (Defender, BitLocker, patches, disks, software), remote actions, monthly PDF report per customer mailed to the contacts | ✅ Done (v0.10.0) |
| 11 | **Backup management per customer**: coverage, alerts per SLA, restores with approval, Proxmox Backup Server, Windows Server Backup, backups in the monthly report and the optimisation scan | ✅ Done (v0.11.0) |
| 12 | **Nightly checks and morning summary**: updates, CVEs, disks, certificates and backups of every customer each night, one summary in the morning with proposals approved in one click and run in the maintenance window | ✅ Done (v0.12.0) |
| 13 | **Runbooks**: fixed procedures run step by step through Stackpilot's tools, with approval per step, the maintenance window and the audit log, editable in the app and available as MCP tools | ✅ Done (v0.13.0) |
| 14 | **Automatic recovery**: rules that link alerts and nightly findings to repair runbooks, run within the policy with verification, loop protection, a circuit breaker and an emergency stop | ✅ Done (v0.14.0) |
| 15 | **Network recovery and Windows scripts**: a network card or uplink without link is restarted (also by the agent itself when it cannot reach Stackpilot), and scheduled tasks run PowerShell scripts on Windows machines | ✅ Done (v0.15.0) |
| 16 | **Uptime and SLA monitoring**: external checks of websites, VPNs and ports, SLA percentage per customer in the monthly report | ✅ Done (v0.16.0) |
| 17 | **Customer portal with tickets**: customers report problems in their own portal, tickets linked to machines, with a first diagnosis by the AI | ✅ Done (v0.17.0) |
| 18 | **Documentation and password vault per customer**: the AI only uses secrets after approval, every use in the audit log | ✅ Done (v0.18.0) |
| 19 | **Microsoft 365 / Entra management**: users, licences, MFA status and risky sign-ins per customer | ✅ Done (v0.19.0) |
| 20 | **Certificate, firewall and VPN monitoring**: expiry, configuration changes and tunnels that go down | ✅ Done (v0.20.0) |
| 21 | **Costs and licences per customer**: price list, resource use, licences, contracts and hardware with renewal dates, monthly statement with CSV/PDF export | ✅ Done (v0.21.0) |
| 22 | **Change calendar per customer**: planned changes and maintenance checked against the maintenance window, with notices to the customer before and after, the portal and an iCal feed | ✅ Done (v0.22.0) |
| – | **Work performed** in the monthly report, in plain language for the customer | ✅ Done (v0.23.0) |
| – | **Proxmox maintenance** with a timeline report per customer, and the origin of every change | ✅ Done (v0.24.0) |
| – | **New navigation**: top bar with customer switcher and search, customer overview tiles, Integrations page with step-by-step guides, view the portal as a customer | ✅ Done (v0.25.0, v0.26.0) |
| – | **Software library**: several GitHub tokens, repositories with rights per customer, create repositories, deploy onto Linux servers | ✅ Done (v0.27.0) |
| – | **Veeam Backup & Replication** restore points linked to machines | ✅ Done (v0.28.0) |
| – | **UniFi Fabrics**: one Site Manager key per Fabric, consoles linked through the cloud connector | ✅ Done (v0.29.0) |

What every release added: [release history](docs/history.md).

## Releases and channels

- `main` → production releases `vX.Y.Z`
- `test` → test builds `vX.Y.Z-test.N` (prereleases)

Switch channel and update from **Info & updates** (the version number in the top bar). Releases are built by
[`.github/workflows/release.yml`](.github/workflows/release.yml).

## Development

```bash
# Backend (needs PostgreSQL)
cd backend && python3.13 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
export MMN_DATABASE_URL=postgresql+asyncpg://mmn:mmn@127.0.0.1:5432/mmn MMN_DATA_DIR=/tmp/mmn MMN_INSECURE_COOKIES=true
mmn migrate && mmn serve          # API on 127.0.0.1:8740
pytest

# Frontend
cd frontend && npm ci && npm run dev   # http://127.0.0.1:5173, proxies /api and /mcp to the backend

# Agent (Go 1.24)
cd agent && go test ./...

# After adding or changing an MCP tool
python backend/scripts/gen_mcp_reference.py
```

More in [installation → development](docs/install.md#development).
