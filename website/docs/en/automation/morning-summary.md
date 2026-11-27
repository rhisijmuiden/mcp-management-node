# Nightly checks and morning summary

Every night Stackpilot checks every customer. In the morning you get **one summary**, in the app and by mail, with
what is new, what was resolved and proposals you approve with one click.

## What is checked

| Category | What |
| --- | --- |
| **Updates** | Pending updates and reboots, old agents, UniFi firmware |
| **Security and CVEs** | Pending security updates and the vulnerabilities (CVEs) they fix |
| **Disks and storage** | Full disks, full Proxmox storage, disk health (SMART) |
| **Certificates and firewall** | Certificates that expire, firewall changes not acknowledged, VPN tunnels down |
| **Backups** | Failed, missing and too old backups |
| **Microsoft 365** | Risky users, admins without MFA, unused licences and more |
| **Contracts and licences** | What runs out or must be cancelled soon |
| **Other** | The rest of the optimisation scan: offline machines, failed services, network |

For Debian and Ubuntu, Stackpilot looks up the CVEs at [OSV.dev](https://osv.dev). Only package names and versions are
sent; you can switch this off.

## Reading the summary

Open **Today → Morning summary**. Per customer you see the findings, sorted by importance:

- **New** findings since last night are marked; **resolved** ones are listed too.
- A finding that can be fixed in one step has a **proposal**: install the updates, reboot (never a Proxmox node),
  update the agent, free disk space, renew a certificate, or block a risky Microsoft 365 user.

## Approving proposals

1. Click **Approve** on a proposal, or **Approve all** for a customer or for everyone.
2. Changes then **wait for the customer's maintenance window** (shown as *Scheduled*) and start when it opens.
   Customers without a window run right away.
3. The work runs with the rights of the admin who approved it, and lands in the audit log.

Proposals you don't approve expire before the next night, which makes fresh ones.

## Settings

Under **Settings** on the same page:

- the times of the checks (default 02:30) and the summary (default 07:00);
- what is checked, the CVE lookup, proposals, waiting for the maintenance window;
- the notification in the app and the mail: recipients, and whether to mail when everything is fine;
- **Per customer**: switch checks off for a customer and list the web addresses whose certificates to check.

**Run checks now** runs them straight away, **Send summary** mails it again, **History** keeps 90 nights.

## With the AI

Ask *"What happened last night?"* or *"What needs my attention today?"*. The AI reads the summary and can start a run.
