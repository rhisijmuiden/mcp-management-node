# Customer backups

**Maintenance → Backups** answers the question *"is everything backed up?"* for every customer: every VM, container,
server and workstation with its last good backup, its age and its status against the customer's SLA.

!!! note "Two kinds of backup"
    This page is about the backups **of your customers' machines**. Backups **of Stackpilot itself** are under
    [App backups](../admin/backups.md).

## What Stackpilot looks at

- **Proxmox guests**: per VM and container the backup jobs that cover it, the last successful backup (age and size),
  the number of backups, whether the newest one is verified and copied offsite (Proxmox Backup Server) and the last
  failure.
- **Windows Server Backup**: policy, system state and the last result, through the agent.
- **Veeam Backup & Replication** (v12 and v13): on the Veeam server the agent reads the jobs, the last sessions and the
  restore points per VM, read-only. Hyper-V and VMware VMs are matched by name to the customer's machines, so they show
  their last restore point; VMs without an agent appear as their own items. Nothing in Veeam is changed. Needs
  PowerShell 7 (`pwsh`) on the Veeam server.
- **Linux**: backup tools, timers and cron jobs.
- A machine that is a Proxmox guest is covered by the guest's backup.

Every item has a status: **OK**, **too old**, **never backed up**, **no job**, **failed** or **excluded**. The overview is
refreshed every 30 minutes, or now with **Check now**.

## The policy per customer

Per customer you set:

- the **maximum age** in hours (the SLA, default 26);
- alerts for failed and missing backups;
- whether workstations count;
- extra mail recipients, and items to exclude.

Failed or too old backups open an alert and are mailed. They also appear in the morning summary, the monthly report and
the optimisation scan.

## Back up now and restore

- **Back up now** on a guest.
- **Restore a whole guest**: by default as a **new** VM or container next to the original. Overwriting the existing
  one requires typing its id.
- **Restore files or folders** from a Proxmox Backup Server backup to a folder on the node.

Restores are destructive and ask for approval by default, also when the AI asks.

## Proxmox Backup Server

A Linux server with the agent that runs Proxmox Backup Server shows its **datastores** (usage, garbage collection),
**verify**, **sync** and **prune** jobs and **remotes**. Add datastores and remotes and create, change, run or delete
jobs from Stackpilot. Connect a cluster to a datastore under Proxmox → Cluster & HA → Storage.
