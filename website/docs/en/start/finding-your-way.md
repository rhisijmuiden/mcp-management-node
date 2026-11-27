# Finding your way

## The top bar

The dark bar at the top is always there:

- **Customer switcher.** Pick a customer and the whole app shows only that customer: every list, page and count. A
  coloured band under the bar reminds you which customer you are working in, with links to its overview and back to
  all customers. Each customer in the list shows its machines, whether UniFi is linked and what needs attention.
- **Search** (++ctrl+k++ or ++cmd+k++). Type part of a page, customer or machine name and jump straight there.
- **Approvals.** The number of requests waiting for you.
- **View as customer** (admins). Opens the customer portal of the picked customer exactly as the customer sees it,
  read-only.
- **Version number.** Opens **Info & updates**.
- **Account menu.** Language, light or dark mode, sign out.

## The left menu

The menu is grouped by what you want to do:

| Group | What's in it |
| --- | --- |
| **Today** | Command center, Morning summary, Approvals, Tickets |
| **Customer** | Customer overview (when a customer is picked), All customers |
| **Environment** | Proxmox, Servers, Workstations, Network (UniFi), Microsoft 365 |
| **Maintenance** | Backups, Updates & software, Monitoring & uptime, Certificates & firewall, Change calendar |
| **Automation** | Runbooks, Software (Repositories, Deployments), Scheduled tasks, Automatic recovery |
| **Knowledge & admin** | Documentation, Password vault, Costs & licences |
| **Integrations** (bottom) | All integrations, Agents & pairing, Connected AI, API tokens, MCP hub, UniFi consoles, Mail server |
| **Settings** (bottom) | Policies & guardrails, Tool catalog, Sessions & audit, Users, App backups, Notifications, Customer portal, Web server & proxy, Audit log, General |
| **Help & guides** | The in-app help |

Numbers next to menu items (badges) show what waits for you: approvals, tickets to answer, checks that are down,
certificates that expire, firewall changes to acknowledge.

## The command center

![Command center](../assets/images/command-center.png)

The start page shows what waits for you, what the AI did in the last 24 hours, the live AI activity, the MCP address
with a copy button, the current guardrails and the health of every customer. The red button **Pause all AI
actions** is the kill switch.

## The in-app help

**Help & guides** has:

- **Connect Claude, ChatGPT or Copilot**: the steps per AI app, with the MCP address of *your* install filled in.
- **What the MCP can do**: every tool group, with the scope it needs.
- **Connect a Proxmox node**: the agent install in a few steps.
- Short explanations of backups, updates, Zoraxy and security.

This site goes deeper and covers every part of the app.
