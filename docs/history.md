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
| 10 | v0.10.0 | Workstations (inventory, security status, actions) and monthly customer reports |
| 11 | v0.11.0 | Backup management per customer |
| 12 | v0.12.0 | Nightly checks and the morning summary |
| 13 | v0.13.0 | Runbooks |
| 14 | v0.14.0 | Automatic recovery (self-healing) |
| 15 | v0.15.0 | Network recovery and PowerShell scripts on Windows |
| 16 | v0.16.0 | Uptime and SLA monitoring |
| 17 | v0.17.0 | Customer portal with tickets |
| 18 | v0.18.0 | Documentation and password vault per customer |
| 19 | v0.19.0 | Microsoft 365 and Entra ID per customer |

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

**Phase 10** makes **workstations** first-class: an inventory per PC or laptop (hardware, OS, users, software,
disks and SSD wear, Windows Update history, Defender, BitLocker/LUKS, firewall, TPM, Secure Boot) collected through
the existing agent with a read-only script, a security status with the issues per PC, software search across PCs and
remote actions (Defender scan and signature update, Group Policy refresh, message to the user, log off). The
**monthly report** per customer (PDF and HTML in the house style) covers availability, incidents, updates, Proxmox
backups, workstation security, AI actions and recommendations, and is mailed to the customer's contacts on a set day
or made on demand. New MCP tools: `list_workstations`, `get_workstation`, `diagnose_workstation`, `find_software`,
`refresh_workstation_inventory`, `workstation_action`, `log_off_workstation_user`, `get_customer_report`,
`send_customer_report`; new scopes `workstations` and `reports`.

**Phase 11** adds **backup management per customer** under Operations → Backups. The coverage overview shows, per
customer, every VM and container (which backup job covers it, last successful backup, age, size, number of backups,
verify state, offsite copy, last failure) and every Linux and Windows server and workstation (Windows Server Backup
with system state, Veeam, other backup agents, or "backed up as a Proxmox guest"). Each customer has a backup policy:
the maximum age in hours (SLA), whether failed and missing backups raise an alert, whether workstations count, extra
mail recipients and excluded items. Alerts appear in Monitoring and are mailed. From the app you can back up a guest
now, restore a whole guest (as a new guest, or over the existing one after typing its VMID) and restore single files
from a Proxmox Backup Server backup, and manage Proxmox Backup Server itself: datastores, verify, sync and prune jobs
and remotes for offsite copies. The monthly report and the optimisation scan gained a backups section. New MCP tools:
`list_backup_coverage`, `set_backup_policy`, `list_guest_backups`, `backup_guest_now`, `restore_guest`,
`list_backup_files`, `restore_files`, `get_pbs`, `save_pbs_job`, `run_pbs_job`, `delete_pbs_job`,
`add_pbs_datastore`, `add_pbs_remote`; new scope `restore_guests` (given automatically to tokens with `guests_admin`).
Restores are destructive and need approval by default.

**Phase 12** adds the **nightly checks and the morning summary**. Every night every customer is checked for pending
updates and reboots, security updates with the CVEs they fix (looked up at OSV.dev), full disks and storage, SMART
health, certificates that expire (on the machines through the agent and on TLS addresses listed per customer), failed
or missing backups and the rest of the optimisation scan. Findings are compared with the night before (new,
resolved). Findings with a one-step fix become proposals: approval requests that are approved with one click (one at a
time, per customer or all) and that wait for the customer's maintenance window before they run, with the rights of the
admin who approved them. In the morning one summary goes out in the app and by mail. New page AI gateway → Morning
summary with settings per customer and the history of earlier nights; approved requests can now be scheduled for the
maintenance window. New MCP tools: `get_morning_summary`, `run_nightly_checks`. The optimisation scan now counts
security updates from a security suite.

**Phase 13** adds **runbooks**: fixed procedures (patch a server, new employee, new project via GitHub, restart a
service) that Stackpilot runs step by step. A step calls a Stackpilot or MCP hub tool, asks a person or the AI to do
something, or waits. Arguments take placeholders for parameters, the customer and results of earlier steps. Every tool
step goes through the AI gateway: the customer's policy decides allow, approve or block, a step can always ask approval,
and approved steps outside the maintenance window wait for it. Runs started in the app use the rights of the admin
(approvals: of the approving admin); runs started by the AI use the token's rights. Failed runs can retry or skip a
step, runs can be cancelled (open approvals are withdrawn), and every step is in the audit log with the run's id. New
page Operations → Runbooks with an editor, templates and run history; new scope `runbooks`; new MCP tools
`list_runbooks`, `get_runbook`, `run_runbook`, `get_runbook_run`, `list_runbook_runs`, `complete_runbook_step`,
`cancel_runbook_run`, `save_runbook`, `delete_runbook`. Runbooks with category "repair action" are the building blocks
for automatic recovery (phase 14).

**Phase 14** added automatic recovery: known problems that Stackpilot fixes by itself, within the policy. Rules (per
customer or for everyone) link a problem to a repair runbook: an open monitoring alert (agent offline, a stopped
service that the rule watches, full disk or storage, memory, SMART, UniFi, backups) or a finding of the nightly checks
(a full disk, an outdated agent, ...). The rule fills in the runbook's parameters (`{{machine.name}}`,
`{{alert.detail.service}}`) and runs in mode automatic, with approval or report only. Every step goes through the AI
gateway; on top of that destructive steps always ask an admin, a Proxmox VE node is never rebooted and nothing is
deleted or restored. After a run the alert has to be gone within a few minutes, otherwise the attempt counts as
failed. Loop protection per problem (wait first, cooldown, maximum attempts per 24 hours), a limit per hour for the
whole app, a circuit breaker that stops everything after too many failures, and an emergency stop that cancels the
recoveries that are busy. New page Operations → Automatic recovery with activity, rules, recommended rules (restart a
stopped service, free disk space, bring an offline agent back, update outdated agents) and settings; notifications in
the app and by mail; results in the morning summary and the monthly report. New runbook templates "Free disk space",
"Bring an offline agent back" and "Update the agent"; new MCP tools `get_remediation`, `save_remediation_rule`,
`delete_remediation_rule`, `pause_remediation`, `cleanup_disk` and `recover_agent`.

**Phase 15** added network recovery and scripts on Windows. A new alert kind "network adapter without link": on
machines that a network rule watches, Stackpilot reads the physical wired adapters every few minutes
(`Get-NetAdapter` on Windows, `/sys/class/net` on Linux) and alerts when one that was up before has no link (an
adapter that is switched off is left alone). The new recommended rule and runbook template "Restart a network
adapter" restart it (`Restart-NetAdapter`, or `ip link` down/up) and check that the link is back. With the **local
fallback** the agent (v0.15.0) also does this itself when it cannot reach Stackpilot, which is often because of that
very link: after the rule's wait, with its cooldown and maximum per 24 hours; it reports what it did when it is back,
as a recovery attempt "repaired by the agent itself". This came from a Hyper-V host whose X710 ports did not come
back after a switch reboot. Scheduled tasks with a custom command now also run on Windows servers and workstations:
a PowerShell script next to the bash script, each machine runs the one of its OS (agent v0.15.0 runs it as a .ps1
file under LocalSystem). The Windows agent now limits `%ProgramData%\mmn-agent` to SYSTEM and administrators. New
MCP tools `list_network_adapters` and `restart_network_adapter`.

**Phase 16** added uptime and SLA monitoring: external checks per customer of websites (status, keyword, response
time, certificate expiry), TCP ports, ping, DNS records and VPN endpoints (an SSL VPN portal, or a host behind a
tunnel), run by Stackpilot every 30 seconds to an hour or by the agent of a machine at the customer. A check is down
after a number of failed checks in a row; outages are recorded from the first failure to the first good check.
Down, slow and expiring checks become monitoring alerts (kind "uptime", also mailed to the customer's uptime
recipients) and can trigger automatic recovery. Each customer gets an uptime SLA target (or the percentage in its
SLA text); the measured availability per month, without downtime inside the maintenance window, is on the new page
Operations → Uptime & SLA and in the monthly report, with an optional alert when the month falls below the target.
New MCP tools `list_uptime_checks`, `get_uptime_check`, `run_uptime_check`, `get_sla_report`, `save_uptime_check`,
`delete_uptime_check` and `set_uptime_sla`, and a new token scope `monitoring`. New dependency: dnspython.

**Phase 17** added the customer portal with tickets. People at a customer get a portal account (an invitation
by mail, a member sees their own tickets, a manager every ticket of the organisation) and sign in at `/portal`,
separate from the staff accounts and always limited to their own customer. They report a problem, question or
request, pick the computers and servers it is about and follow the status and the replies. When a ticket comes
in, Stackpilot runs a first diagnosis (the checks of `diagnose_server`, `diagnose_workstation` and `find_errors`,
plus open alerts and uptime checks that are down) and shows it to staff only. Staff get the new page Operations →
Tickets: the list per customer, status, priority, assignment, public replies and internal notes, portal accounts and
portal settings, with mails through the app's SMTP server. Resolved tickets close by themselves after a number of
days. New MCP tools `list_tickets`, `get_ticket`, `diagnose_ticket`, `reply_ticket`, `update_ticket` and
`create_ticket`, and a new token scope `tickets`.

**Phase 18** added documentation and a password vault per customer. Operations → **Documentation**: Markdown pages
(network plans, procedures, notes, contacts, hardware, ...) with templates, a side-by-side editor, tables and
checklists, linked machines, tags, every earlier version (view and restore) and attachments that are stored
encrypted; a page can be hidden from the AI. Operations → **Password vault**: credentials per customer (logins,
servers, network devices, databases, API keys, WiFi, certificates, licences) with extra fields, a password generator,
password history and a rotation reminder, encrypted with AES-256-GCM under a data key per item that is wrapped with
a separate vault key (`vault.key`, part of the app backups, rotatable from the interface). Admins see or copy a
secret after entering their password again; every reveal and copy is in the vault's access log. The AI only gets a
secret after an admin approved that one use through the approvals inbox, whatever the policies say:
`reveal_credential` returns it once after approval, `run_with_credential` hands it to a script on the machine in
environment variables and wipes it from the output. New MCP tools `list_documents`, `get_document`,
`save_document`, `list_credentials`, `reveal_credential`, `run_with_credential` and `save_credential`, new token
scopes `docs` and `vault`, and `get_customer_context` lists the customer's documentation.

**Phase 19** added Microsoft 365 and Entra ID per customer. Infrastructure → **Microsoft 365**: connect a tenant with
an app registration in the customer's Entra ID (application permissions with admin consent; the page lists them and
walks through the setup). The client secret, or a certificate that Stackpilot can make itself, is kept in the
customer's password vault and every use is in the vault's access log. Every 15 minutes Stackpilot reads the users
with licences, last sign-in, admin roles, MFA registration (from the registration report, or per user on tenants
without Entra ID P1) and Entra ID Protection risk, the licences, risk detections, security defaults and Conditional
Access, the expiry of the app's own secret or certificate, and the new sign-ins; only the ones that stand out are
kept (risky, failed, from a country outside the tenant's list, legacy authentication). Parts a tenant lacks (P1/P2)
or the app may not read are shown as such. Monitoring alerts (kind `m365`) and a new morning-summary category
"Microsoft 365" cover risky users (with a proposal to block or sign out), admins and users without MFA, no security
defaults or Conditional Access, inactive licensed accounts, blocked accounts that still have a licence, licence
shortages and unused licences, expired subscriptions, the app secret expiring, many failed sign-ins and sign-ins from
abroad. Admins block or unblock users, sign them out everywhere, reset MFA or the password (the temporary password
goes into the vault) and confirm or dismiss risk. New MCP tools `list_m365_tenants`, `list_m365_users`,
`get_m365_user`, `list_m365_licences`, `list_m365_signins`, `refresh_m365` and `m365_user_action` (always needs an
admin's approval), new token scopes `m365` and `m365_admin`, and `get_customer_context` includes the tenant summary.

**Phase 20** added certificate, firewall and VPN monitoring per customer (Operations → **Certificates &
firewall**). Certificates come from TLS endpoints (manual, every HTTPS and SSL VPN uptime check, and the nightly
endpoints, checked from Stackpilot or a machine at the customer, with STARTTLS) and from the certificate stores on
Linux servers, Proxmox nodes and Windows (cert store, IIS and RDP bindings), with warnings before expiry, trust and
name checks and renewal tracking. Firewall configurations (Proxmox VE, nftables/iptables, Windows Firewall, UniFi
zones and policies) are versioned with diffs; changes made outside Stackpilot are an alert until acknowledged. VPN
tunnels (WireGuard, IPsec, OpenVPN, Windows RRAS and UniFi) are watched and alert when they stay down. Monitoring
alerts of kinds `cert`, `firewall` and `vpn`, a new morning-summary category, the MCP tools `list_certificates`,
`list_firewall_changes`, `get_firewall_change`, `list_vpn_tunnels`, `save_cert_endpoint`, `delete_cert_endpoint`,
`set_certificate_ignored`, `acknowledge_firewall_change`, `set_vpn_watch` and `refresh_netsec`, and the token scope
`netsec`. No agent update is needed: everything runs through the existing command channel.

Bug fix releases in between (v0.2.1, v0.6.1, v0.6.2, v0.7.1) are listed on the releases page.
