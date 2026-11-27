# Change calendar

**Maintenance → Change calendar** holds the planned maintenance and changes of every customer, and keeps the customer
informed with notices before and after.

## Plan a change

1. Click **Plan a change** and choose the customer.
2. Fill in the title, kind (maintenance, updates, change, migration, other), expected impact (none, short
   interruptions, slower, unavailable), the start and the length.
3. Pick the machines it touches.
4. Write a **message for the customer** (used in the notices, the portal and the calendar) and, if needed, internal
   notes for your team.
5. Optionally link the **scheduled task** that does the work and a **runbook** that starts by itself at the start.
6. Save as **draft** (only visible for staff, sends nothing) or **plan** it. Every change gets a number, for example
   `CHG-1001`.

### Checks while you plan

Stackpilot checks the time against the customer's **maintenance window**, other changes of the same customer
(warning when they share machines), scheduled tasks in that period, and whether the customer still gets the usual
notice. It **suggests the next free slot** inside the window; take it over with one click.

## Notices to the customer

Through your own mail server, to the customer's contacts and portal users, in Dutch or English:

- by default **3 days** and **1 hour** before the start (other times per customer or per change);
- a **new time** mail when an announced change moves;
- a **cancellation** when it is called off;
- a **notice afterwards** with the outcome.

Every notice is logged on the change.

## Status

*Planned* → *in progress* at the start → *completed* at the end, by the clock. With a linked runbook the run decides
whether it completed or failed. You can also finish by hand, with a note for the customer.

## Where a change came from

Every change shows its **origin**: manual, a ticket, an alert or finding, a schedule, the AI or automatic recovery.
Work Stackpilot does itself is recorded automatically: every Proxmox maintenance run (with its timeline report),
scheduled tasks that change machines, and automatic recovery (the last two can be switched off).

## Calendar and iCal

The month view shows every change, the maintenance windows and, optionally, scheduled task runs. Subscribe to it in
Outlook, Google or Apple Calendar with a secret **iCal link**: one per customer (also shown in their portal) and one for
staff with every customer. Drafts and internal notes never appear in it.
