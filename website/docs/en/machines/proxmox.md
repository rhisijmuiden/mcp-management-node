# Proxmox VE

Every Proxmox VE node runs the agent. Stackpilot then manages the node, its VMs and containers, its network and the
cluster, without the Proxmox API being reachable from outside.

## Nodes

**Environment → Proxmox → Nodes** shows every node with status, CPU, memory, disk, guests and available updates.
Per node:

- **Package updates**: refresh, install and reboot, with a live log.
- **CPU power mode** (governor), for example `powersave` or `performance`.
- **Notification mail**: the customer's SMTP settings are pushed to every node, with a test notification.
- **Logs, services, packages, files, commands** and a **shell** in the browser.
- The agent version, with self-update.

## VMs and containers

**Proxmox → VMs & containers** lists every guest of every node.

- **Power**: start, shut down, stop, reboot.
- **Snapshots**: take, roll back, delete.
- **Migrate** within the cluster, change CPU, memory and tags, delete.
- **Console** in the browser: VMs through noVNC (with Ctrl+Alt+Del), containers through a terminal. It is relayed
  through the agent, so the node needs no open ports.

### Create a guest

Click **Create** and choose:

1. A **container** from the template library.
2. A **community script** from community-scripts.org (for example an app such as Home Assistant). Stackpilot shows the
   script first; it runs as root on the chosen node.
3. A **VM from an ISO** (existing, or downloaded for you).
4. A **VM from a cloud image** with cloud-init.

The node defaults to the least loaded one.

## Templates

**Proxmox → Templates** is one list of official and your own container templates, showing per node whether each one
is current, outdated or missing. **Push to all nodes** (and a nightly task) puts them on every node.

## Network, firewall and SDN

- **Proxmox → Network** shows every bridge with the guests on it, grouped by VLAN, plus SDN networks and bridges that
  guests point to but that don't exist.
- **Changes are staged first** and shown as a diff. Applying needs the node name typed in. The node keeps a backup and
  **restores the old configuration by itself** if Stackpilot can't reach it again within 90 seconds.
- **Firewall**: cluster, node and guest rules.
- **SDN designer**: build a zone in five steps with a live preview. Rolling out keeps a backup and restores it when the
  reload fails. Every backup can be restored under **Network → Config backups**.

## Cluster and HA

**Proxmox → Cluster & HA**: HA resources and rules, storage replication, the cluster's own backup jobs and storage
(directory, NFS, SMB, Proxmox Backup Server).

## Maintenance of a whole cluster

Patching nodes one by one, with guests moved away and back, is a scheduled task: see
[Proxmox maintenance](../automation/maintenance.md#proxmox-maintenance).
