# Runbooks

A runbook is a **fixed procedure** that Stackpilot carries out step by step with its own tools, for example "patch a
server", "new employee" or "restart a failed service". You write it once; after that you, a colleague or the AI start
it, and every run goes the same way.

## Make a runbook

1. Open **Automation → Runbooks** and click **New runbook**. Start from a template (patch a server, new employee, new
   project via GitHub, restart a failed service) or from scratch.
2. Choose whether it is for **one customer** or for **every customer**.
3. Add **parameters**: the questions asked when it starts, for example *Which server?*
4. Add **steps**:
    - **Tool**: calls a Stackpilot tool or a tool from the [MCP hub](../ai/mcp-hub.md). Arguments can use
      placeholders such as `{{params.server}}`, `{{customer.name}}` or a result of an earlier step
      (`{{steps.<id>.result.<field>}}`).
        - *Approval*: follow the policy, or always ask.
        - *Optional*: rejecting the step skips it instead of stopping the run.
        - *Wait for the job*: for tools that start a job (such as updates), the step ends when the job is done.
    - **Manual**: instructions for a person (confirmed on the run page) or for the AI.
    - **Wait**: a number of minutes, for example for a server to come back after a reboot.
5. Save.

## Run a runbook

1. Click **Run**, fill in the parameters and start.
2. The run page shows every step live. A step that needs approval appears in **Approvals** (app and mail). With
   *Wait for the maintenance window* on, an approved step outside the window waits until it opens.
3. When a step fails the run stops (unless the step says to go on). You can **retry** or **skip** the step, or
   **cancel** the run, which withdraws its open approvals.

The menu badge shows how many runs wait for you.

## Good to know

- Every tool step goes through the same checks as an AI call: the customer's policy decides run, ask or block.
- A run keeps a copy of the runbook, so editing a runbook never changes a run in progress.
- Runs started in the app use the rights of the admin who started them; runs started by the AI use the rights of its
  connection, and need the scopes of every step.
- Every step is in the audit log with the run's id.
- Runbooks are also the repair actions of [automatic recovery](recovery.md).
