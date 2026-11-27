# Customers and their settings

Customers are the top layer of Stackpilot. Every Proxmox node, server, workstation, UniFi network, Microsoft 365
tenant, backup, ticket and cost line belongs to one customer. Your own environment is the customer **Home**.

![Customers](../assets/images/customers.png)

## Create a customer

1. Open **Customer → All customers** and click **New customer**.
2. Enter the name and, if you like, a short code and the main contact.
3. Save. The customer page opens.

## The customer page

Pick the customer in the top bar (or click it in the list). The customer page has these tabs:

| Tab | What you do there |
| --- | --- |
| **Overview** | Tiles for everything linked to the customer, plus **What is not set up yet?** |
| **Proxmox**, **Servers**, **Workstations** | The customer's machines. **Add machine** installs a new one. |
| **Network** | The UniFi connections of this customer. |
| **AI access** | Which AI apps and tokens may work for this customer. |
| **Contacts & notes** | Contacts, SLA, maintenance window, notes for the AI. |
| **Reports** | Monthly reports and maintenance report recipients. |

### Overview tiles

The overview starts with a tile for Proxmox, servers, workstations, UniFi, Microsoft 365, MCP servers, software,
backups, open tickets, AI access, documentation and vault, the portal and uptime checks. A **dashed tile** is not set
up yet: click it and the step-by-step guide opens.

**What is not set up yet?** lists the gaps, each with a link to fix it: machines offline, backup problems, no
maintenance window, no SLA, contact or notes, no uptime checks, no portal accounts, no app backup location.

## Contacts, maintenance window and SLA

On **Contacts & notes**:

1. **Contacts**: names, email and phone. The main contact gets the monthly report unless you choose other recipients.
2. **Maintenance window**: when changes and restarts are allowed, for example `Sat 22:00-02:00`. Stackpilot uses it
   everywhere: the AI asks for approval outside it, approved proposals wait for it, and the change calendar plans
   inside it.
3. **SLA**: for example "99.9% uptime, backups at most 26 hours old". The uptime percentage and backup age are read
   from it.
4. **Notes for the AI**: things the AI must know, such as *"Never restart the SQL server during office hours"*. The
   AI reads these before it works on this customer.

!!! tip
    Tell the AI a fact about a customer ("the NAS is in the meter cupboard") and it can save it as a note itself.

## Optimisation scan

The **AI optimisation scan** on the customer page answers *"what can be improved here?"* in one go: memory and balance of the
Proxmox cluster, storage and disk health, full disks, pending updates and reboots, old agents, failed services,
stopped guests, UniFi devices offline, firmware and whether the guest WiFi is separated from the LAN. Every finding
comes with a suggestion. The AI can run the same scan.

## AI access per customer

Under **AI access** you tie AI connections and API tokens to customers. A token set to *Only these customers* sees
nothing of other customers, enforced for every tool. Tools that work on the whole app (app updates, mail server,
audit log) are then refused. Click **New customer token** to make one straight from here.

## Moving a machine

A machine that ended up at the wrong customer can be moved on its own page under **Manage**.
