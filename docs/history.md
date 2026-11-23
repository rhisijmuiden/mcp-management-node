# Release history

[← Back to the README](../README.md)

Stackpilot is built in phases. Each phase is one pull request and one minor release; see the
[releases](https://github.com/rhisijmuiden/stackpilot/releases) for every version.

| Phase | Release | Topic |
| --- | --- | --- |
| 1 | v0.1.0 | Foundation and MCP server |
| 2 | v0.2.0 | Proxmox nodes through the agent |
| 3 | v0.3.0 | VMs, containers, templates and scheduled tasks |
| 4 | v0.4.0 | Network, SDN, HA, monitoring and GitHub deployments |
| 5 | v0.5.0 | Linux and Windows servers, troubleshooting |
| 6 | v0.6.0 | UniFi Network and Access |
| – | v0.6.3–0.6.4 | Renamed to Stackpilot, repository moved to `rhisijmuiden/stackpilot` |
| 7 | v0.7.0 | Customers central |
| 8 | v0.8.0 | AI gateway: command center, approvals, policies, kill switch |
| 9 | v0.9.0 | MCP hub and tool catalog |
| 10 | – | Workstations and monthly report (planned) |

## What each phase added

**Phase 1** laid the foundation: sign-in, users, mail server, backups with restore, updates from the interface,
web server / Zoraxy settings, the MCP server with OAuth, and the audit log.

**Phase 2** added environments (Home plus one per customer), the agent on every Proxmox node, the
overview dashboard, package updates from the interface, notification mail per environment, the CPU governor per
node, and server administration through the agent (logs, services, packages, files, commands) for people and AI.

**Phase 3** added VMs and containers: power actions, snapshots, migration, the console in the browser
(noVNC for VMs, a terminal for containers and node shells), creating guests from the template library,
community-scripts.org, an ISO or a cloud image, the CT template library with nightly sync to all nodes, and
scheduled tasks (updates in containers, node updates, template sync, custom commands) with snapshot, rollback and
a mailed report.

**Phase 4** added network management (bridges and VLANs with a graphical overview of which guests use
them, the firewall, SDN with a step-by-step designer), cluster features (HA, replication, Proxmox backup jobs,
storage), monitoring of all nodes with trends, SMART and mailed alerts, and deployments from GitHub repositories
into containers.

**Phase 5** added stand-alone **Linux and Windows servers** in their own Servers section, managed by the
same agent (Windows service with an MSI installer, event log, PowerShell, Windows Update), plus troubleshooting
across servers for people and AI: log search on many servers at once, error overviews, health checks, a software
and CVE inventory, and data copies between servers.

**Phase 6** added **UniFi**: UniFi Network (local Integration API), the UniFi Site Manager cloud API and
UniFi Access, per environment/customer, in a UniFi section of the menu, with MCP tools so an assistant can answer
"why can't this laptop get on WiFi?", "which APs need firmware?", "is guest isolated from the LAN?" and "who opened
the server room last night?".

**Phase 7** made **customers** the top layer. The former environments are now customers ("Home" is
one too) with contact details, contacts, notes, a maintenance window and an SLA. A customer switcher at the top of
the menu limits the whole interface to one customer; every customer has a page with Overview, Proxmox, Servers,
Workstations, Network, AI access, Contacts & notes and Reports. Linux and Windows machines can be marked as
workstation (PC or laptop). API tokens and AI apps connected through OAuth can be tied to one or more customers: they
then only see those customers' machines, UniFi connections, jobs and alerts, enforced in the database layer for every
tool. New MCP tools: `list_customers`, `get_customer_overview`, `optimisation_scan` ("what can be optimised at
customer X"), `get_customer_context`, `create_customer` and `update_customer`; most tools take `customer` instead of
`environment`; the customer notes are the MCP resources `stackpilot://customers` and `stackpilot://customers/<id>`.
Existing installs migrate automatically: every node, server, UniFi connection and agent stays where it was.

**Phase 8** turns Stackpilot into an **AI gateway**. The app opens on the **Command center** (pending
approvals, live AI activity, AI sessions, connected AI tools, guardrails and the health of every customer), and the
menu is now AI Gateway / Infrastructure / Operations / Administration. Every MCP tool has a risk level (read,
operate, change, destructive); **Policies & guardrails** decide per risk level and per tool whether the AI may run
it, must ask first or may never do it, as defaults, per customer (looser or stricter) and per token (only stricter),
plus "outside the customer's maintenance window needs approval". A call that needs approval is stored in
**Approvals** and answers `pending_approval`; you get a mail and an in-app notification, and after you approve the
app runs the call with the token's own rights (the AI reads the outcome with `get_approval`). **Pause all AI
actions** is the kill switch (also per customer or token). New MCP tools: `get_approval`, `get_guardrails`.

**Phase 9** adds the **MCP hub**: register other MCP servers (Streamable HTTP directly or through an agent on the
customer's LAN, HTTP + SSE, or a command over stdio) with a bearer token, headers or OAuth, all stored encrypted.
Their tools appear on Stackpilot's endpoint as `<prefix>__<tool>`, only after an admin accepted them (a changed tool
is withdrawn until accepted again), and go through the same scopes (`hub`), risk levels, policies, approvals and
audit log, per customer. Ready-made setups for GitHub (projects from A to Z) and Keystone VoIP. The **Tool catalog**
switches tools or whole servers on or off for all tokens or one token. New MCP tools: `list_mcp_servers`,
`refresh_mcp_server`.
Default policy after the update: read, operate and change run, destructive tools (commands, updates, reboots,
deletes, doors) ask for approval first; change it under AI Gateway → Policies & guardrails.

Bug fix releases in between (v0.2.1, v0.6.1, v0.6.2, v0.7.1) are listed on the releases page.
