# Updates and software

## Package updates

**Maintenance → Updates & software → Updates** shows the pending updates of every Proxmox node. Install them with a live
log, and reboot where needed. Servers and workstations get their updates (apt, dnf/yum or Windows Update) on their own
page.

To patch on a schedule, use a [scheduled task](../automation/maintenance.md); for a whole Proxmox cluster, use
[Proxmox maintenance](../automation/maintenance.md#proxmox-maintenance). The [morning summary](../automation/morning-summary.md)
proposes updates every day, which you approve in one click.

## Software & CVE

**Updates & software → Software & CVE** answers: *which machines run this software, in which version, and are they
vulnerable?*

1. Enter a name or wildcard: `nginx*`, `openssl`, `*7-zip*`, or on Windows a hotfix such as `KB5*`.
2. Optionally enter the **first fixed version** from the security advisory.
3. Stackpilot lists every machine with the package and marks older versions as **affected**, with whether a pending
   update fixes them.

Ask the AI: *"Which servers are still vulnerable to CVE-2026-1234 in openssl?"*
