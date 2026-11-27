# MCP hub and tool catalog

## MCP hub

The **MCP hub** puts your other MCP servers behind the same Stackpilot address. Your AI then needs **one
connection** for everything, and every tool, also from other servers, follows the same policies, approvals and audit
log. Examples: GitHub, a VoIP system such as Keystone, Home Assistant.

### Add a server

1. Open **Integrations → MCP hub** and click **Add MCP server**. For GitHub there is a ready-made option.
2. Choose how Stackpilot reaches it:
    - **Streamable HTTP**, directly or **through an agent** in the customer's network (so a server on their LAN needs
      no open port);
    - the older **HTTP + SSE**;
    - a **command** that Stackpilot starts itself (for example `github-mcp-server stdio`).
3. Choose the sign-in: a bearer token, your own headers (API key) or **OAuth** (you sign in once; Stackpilot keeps and
   renews the tokens). Secrets are stored encrypted.
4. Give it a **prefix**. Tools then appear as `<prefix>__<tool>`, for example `github__create_issue`.
5. Optionally tie it to a **customer**: only connections for that customer see it, and its calls follow that
   customer's policies.
6. **Accept the tools.** Nothing is passed to your AI until you accepted it.

### Safe by design

- **New or changed tools are withdrawn** until you accept them again, so a server can't quietly slip new
  instructions to your AI ("tool poisoning").
- Every tool gets a **risk level**, suggested from the server's own hints, which you can change.
- An AI connection needs the scope **`hub`** to see hub tools; switch it on per AI app on the MCP hub page.
- Health per server (status, number of tools, response time, last error) is checked every 5 minutes.

## Tool catalog

**Settings → Tool catalog** lists every tool your AI can get, from Stackpilot and from the hub, with its risk level.
Switch single tools or a whole hub server **off** for every connection or for one token. A switched-off tool is not
shown to the AI and calls to it are refused.
