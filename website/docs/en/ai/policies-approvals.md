# Policies and approvals

Stackpilot sits between your AI and your machines. Every call passes the same checks, in this order:

1. **Scope**: does the connection have the permission for this tool?
2. **Customers**: is the target inside the customers of this connection?
3. **Kill switch**: are AI actions paused (for everyone, this customer or this token)?
4. **Policy**: for this tool's risk level, at this customer, for this token: run, ask, or block?
5. **Maintenance window**: is a change outside the customer's window?

Then the call runs, waits for your approval, or is refused, and it is written to the activity log.

## Risk levels

Every tool has a risk level:

| Risk | Meaning | Default |
| --- | --- | --- |
| **Read** | Look only: status, logs, lists | Run |
| **Operate** | Routine and easy to undo: restart a service, take a snapshot | Run |
| **Change** | Changes settings or creates things | Run |
| **Destructive** | Commands, updates, reboots, deletes, restores, doors | Ask me |

## Setting the policies

![Policies](../assets/images/policies.png)

Open **Settings → Policies & guardrails**. For every risk level, and for single tools, choose **Run**, **Ask me** or **Block**:

- **Default policy**: for every customer and token.
- **Per customer**: looser or stricter. A call that touches several customers gets the strictest outcome.
- **Per token**: can only make it stricter.
- **Outside the maintenance window**: change and destructive calls ask for approval by default (or are ignored or
  blocked, your choice).

A call with `dry_run` counts as *read*, so the AI can always show you first what it would change.

Some things **always** need a person, whatever the policy says: using a secret from the password vault and actions
on Microsoft 365 users.

## Approvals

![Approval request](../assets/images/approval.png)

When a call needs approval:

1. It is **not run**. The AI gets the answer *pending approval* with an id.
2. You get a **mail** and a notification in the app (the badge in the top bar).
3. Open **Today → Approvals**. The request shows the tool, who asked (which AI app or token, and which user), the
   customer, the target, why it needs approval and the exact arguments.
4. Add a note if you like (the AI sees it) and click **Approve and run** or **Reject**.
5. After approval Stackpilot runs the call itself, with the rights of that connection. The AI reads the outcome.

Requests that are not decided in time **expire** and nothing runs. How long the AI waits, when requests expire and who
gets mail is set under **Settings → Policies & guardrails → Approvals & notifications**.

## The kill switch

**Pause all AI actions** (in the command center) stops every AI call that changes something, at once, until you
resume. You can also pause one customer or one token.

## Seeing what happened

- **Command center**: live AI activity of the last 24 hours.
- **Settings → Sessions & audit**: AI calls grouped into sessions, with every call, its arguments, outcome and duration,
  and whether it waited for approval or failed.
- **Settings → Audit log**: every sign-in, change and action, by people and by AI, with user or token, target, IP and
  time.
