# Questions and glossary

## Questions

??? question "Do I have to open ports at my customers?"
    No. Agents connect outward to Stackpilot. Only the Stackpilot server needs port 8443 reachable for the agents
    (and HTTPS for you and your AI).

??? question "Can the AI break something?"
    Only within what you allow. By default destructive actions (commands, updates, reboots, deletes, restores) and
    changes outside a maintenance window ask you first. You can block tools completely, limit a connection to some
    customers, and pause all AI actions with one button. Every call is logged.

??? question "Which AI apps work?"
    Every MCP client: Claude (web, desktop, mobile and Claude Code), ChatGPT (developer mode), GitHub Copilot in VS
    Code, n8n and your own scripts. See [Connect your AI](../ai/connect.md).

??? question "Do I need an AI to use Stackpilot?"
    No. Everything the AI can do, you can also do in the interface. The AI is an extra way of working.

??? question "Does it run in Docker?"
    No. Stackpilot installs natively on Debian 13 (Python, PostgreSQL, Nginx). That keeps it light and easy to update
    from the app.

??? question "What happens to my data if the server breaks?"
    Restore an [app backup](../admin/backups.md) on a new server. Set a backup location and a passphrase on day one.

??? question "Which languages?"
    English and Dutch, everywhere: the app, the portal, mails and reports. Each user chooses their own language; each
    customer gets mails and reports in its own language.

??? question "How do I try a new version safely?"
    Install a second Stackpilot on the **test** channel, or switch a test install to it under
    [Info & updates](../admin/updates.md).

## Glossary

Agent
:   The small program on every managed machine that connects to Stackpilot and does the work there.

Approval
:   A request that waits for a person before an action runs. Comes by mail and in the app.

Customer
:   The top layer: an organisation whose IT you manage. Your own environment is the customer *Home*.

Kill switch
:   *Pause all AI actions*: stops every AI call that changes something, until you resume.

Maintenance window
:   The time in which changes and restarts are allowed at a customer, for example `Sat 22:00-02:00`.

MCP
:   Model Context Protocol, the standard way for AI apps to use tools. Stackpilot is an MCP server.

MCP hub
:   Other MCP servers behind Stackpilot's address, with the same rules.

Pairing code
:   A one-time code (valid 24 hours) to connect a new agent.

Policy
:   The rule that decides whether a tool runs, asks for approval or is blocked.

Risk level
:   Read, operate, change or destructive: how much harm a tool can do.

Runbook
:   A fixed procedure that Stackpilot carries out step by step.

Scope
:   A group of tools a connection may use, such as `logs` or `exec`.

SLA
:   The service level agreed with a customer, for example 99.9% uptime and backups at most 26 hours old.
