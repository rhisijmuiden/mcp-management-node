# Automatic recovery

Known problems that Stackpilot fixes **by itself**, within your policies: a stopped service is restarted, a full disk
is cleaned up, an offline agent in a VM or container is brought back, a network card without link is restarted.

## How it works

A **rule** links a problem to a repair [runbook](runbooks.md):

- **The problem** is an open alert (agent offline, service stopped, disk or storage full, memory, CPU, SMART, UniFi,
  backups, uptime check down) or a finding of the nightly checks (a full disk, an old agent, pending updates, a
  certificate that expires).
- **Which machines**: all of a customer, or narrowed by name (`web*`) and type.
- **The repair**: the runbook, with its parameters filled in from the problem (`{{machine.name}}`,
  `{{alert.detail.service}}`, …).
- **Mode**: *automatic* (within the policy), *with approval* (an admin approves every step that changes something) or
  *report only* (changes nothing, only says what it would do).

After the repair the problem must really be **gone** within a few minutes; otherwise the attempt counts as failed.

## Start with the recommended rules

Open **Automation → Automatic recovery** and click **Recommended rules**. Each one comes with its runbook:

- restart a stopped service (you name the services to watch);
- free disk space;
- bring an offline agent back (start the VM or container, or restart the agent inside it);
- restart a network adapter without link;
- update outdated agents.

!!! tip "Try *report only* first"
    Set a new rule to *report only* for a week. The Activity tab then shows what it would have done.

## Network adapters without link

A network rule watches the wired adapters of its machines (all, or the ones you name: `Ethernet 2`, `eno1`,
`*X710*`). An adapter that was up and lost its link is restarted, and Stackpilot checks that the link is back.

With the **local fallback** the agent does this **itself** when it can't reach Stackpilot (often because of that very
link), and reports it when it is back. Needs agent 0.15.0 or newer.

## Guardrails

- Every step goes through the AI gateway: destructive needs approval, outside the maintenance window needs approval.
- Destructive steps always ask an admin. A Proxmox VE node is **never** rebooted. Automatic recovery never deletes,
  restores, rolls back or changes the network configuration.
- **Loop protection**: a wait before acting, a cooldown between attempts and a maximum number per 24 hours (then it
  stops and mails you); for the whole app a maximum per hour and a circuit breaker after too many failures.
- **Emergency stop**: one button stops all automatic recovery and cancels what is busy.

Every attempt is on the **Activity** tab and in the audit log. You get mail for every outcome, only failures, or never;
the morning summary and the monthly report show what was fixed.
