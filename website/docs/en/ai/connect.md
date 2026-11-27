# Connect your AI

Stackpilot has a built-in **MCP server**. MCP (Model Context Protocol) is the standard way AI apps use tools. Once
connected, your AI can use more than 230 Stackpilot tools, always within the [policies](policies-approvals.md) you
set.

**The address** is `https://<your address>/mcp`. You find it, with a copy button, in the command center, under
**Integrations → Connected AI** and in **Help → Connect Claude, ChatGPT or Copilot**.

!!! warning "Claude (web/app) and ChatGPT need a public HTTPS address"
    They connect from their own cloud, so they can't reach a local or plain-HTTP address. Publish Stackpilot through a
    reverse proxy with a domain and certificate (see [Installation](../start/install.md)). Claude Code and VS Code
    run on your own computer and also work on your local network.

## Two ways to sign in

- **OAuth (recommended for AI apps).** The AI app sends you to Stackpilot, you sign in, tick the **permissions**
  (scopes) and click **Allow access**. Nothing to copy. The connection then shows under **Integrations → Connected
  AI**, where you can limit it to some customers or disconnect it.
- **API token (for scripts and tools such as n8n).** Make one under **Integrations → API tokens → New token**: choose
  the scopes, an expiry date and optionally the customers. The token (`mmn_…`) is shown once. Send it as the header
  `Authorization: Bearer mmn_…`.

## Step by step per AI app

=== "Claude (web, desktop, mobile)"

    1. In Claude, open **Settings → Connectors**.
    2. Click **Add custom connector** and give it a name, for example **Stackpilot**.
    3. Enter the **Remote MCP server URL**: `https://<your address>/mcp`.
    4. Click **Add** and then **Connect**. You are sent to Stackpilot: sign in, pick the permissions and click
       **Allow access**.
    5. In a chat, switch the connector on in the **tools menu** and ask, for example: *"Which backups ran this week?"*

    Custom connectors are available on Claude's paid plans; in Team and Enterprise an owner may need to allow them
    first.

=== "Claude Code"

    1. Add the server:
       ```bash
       claude mcp add --transport http stackpilot https://<your address>/mcp
       ```
    2. Start Claude Code, type `/mcp`, choose **stackpilot** and then **Authenticate**.
    3. Your browser opens Stackpilot: sign in and click **Allow access**.

    With a token instead: add `--header "Authorization: Bearer mmn_YOUR_TOKEN"`.

=== "ChatGPT"

    1. Open **Settings → Apps & Connectors → Advanced settings** and switch on **Developer mode**.
    2. Go back to **Apps & Connectors** and click **Create**.
    3. Give it a name and enter the **MCP Server URL**: `https://<your address>/mcp`.
    4. Choose **OAuth**, tick that you trust the application and click **Create**.
    5. Sign in to Stackpilot in the window that opens, pick the permissions and click **Allow access**. In a chat, add
       the connector with **+ → Developer mode**.

    ChatGPT asks for your confirmation before it runs a tool that changes something.

=== "GitHub Copilot (VS Code)"

    1. Create `.vscode/mcp.json` in your project (or run **MCP: Add Server**):
       ```json
       { "servers": { "stackpilot": { "type": "http", "url": "https://<your address>/mcp" } } }
       ```
    2. Click **Start** above the server. VS Code asks you to sign in: allow it and click **Allow access** in
       Stackpilot.
    3. Open Copilot Chat, switch to **Agent** mode and check under the tools icon that Stackpilot is on.
    4. For Copilot Business or Enterprise, an admin must allow *MCP servers in Copilot* in the organisation policy.

=== "Other clients"

    The server follows the MCP standard:

    - **Transport**: Streamable HTTP at `/mcp`.
    - **OAuth 2.1**: discovery at `/.well-known/oauth-protected-resource`, dynamic client registration and PKCE.
    - **Or a token**: header `Authorization: Bearer mmn_…`.

    ```bash
    curl -s https://<your address>/mcp \
      -H "Authorization: Bearer mmn_YOUR_TOKEN" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    ```

## Scopes: what a connection may touch

A scope is a group of tools. A connection only sees the tools of the scopes you ticked; *read* is the default. Only
admins can give out the more powerful scopes.

| Area | Scopes |
| --- | --- |
| Looking | `read` (status, lists, logs of the app), `logs` (logs, services, packages and files on machines) |
| Machines | `nodes`, `node_updates`, `services`, `exec` (commands), `copy`, `workstations` |
| Proxmox | `guests`, `guests_admin`, `templates`, `network`, `cluster`, `tasks` |
| Backups | `backups`, `restore`, `restore_guests` |
| Networks | `unifi`, `unifi_admin`, `doors` |
| Customers' work | `tickets`, `docs`, `vault`, `reports`, `runbooks`, `monitoring`, `netsec`, `changes`, `changes_admin`, `costs`, `costs_admin` |
| Microsoft 365 | `m365`, `m365_admin` |
| Software | `deploy` |
| The app itself | `updates`, `settings` |
| Other MCP servers | `hub` |

The full list, with every tool, is in the [MCP tool reference](../reference/mcp-tools.md).

## Limit a connection to customers

A connection or token can be tied to **only some customers**. It then sees nothing of the others, and tools that work
on the whole app are refused. Set this under **Integrations → Connected AI** (per app), when you make a token, or on
the customer's **AI access** tab.

## What you can ask

- *"What happened last night? What needs my attention today?"*
- *"Check customer Acme: is anything wrong?"*
- *"Which servers still run an nginx version with a known CVE?"*
- *"Install the pending updates on all nodes of Acme during the maintenance window."*
- *"Why can't this laptop get on the WiFi?"*
- *"Open a GitHub repository for the new project and deploy it to a test container."*

The AI first reads the customer's notes, maintenance window and documentation, so it follows your rules.
