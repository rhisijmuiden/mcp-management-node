# Users, security and web server

## Users

**Settings → Users** has two roles:

- **Admin**: can do everything, approve AI requests and hand out the powerful MCP scopes.
- **Viewer**: can look but not change.

Click **Add user** to add one. Passwords can be reset by mail.

## Audit log

**Settings → Audit log** records every sign-in, change and action, in the app and through the MCP server, with the user
or token, the target, the IP address and the time. Together with **Sessions & audit** you can always see who, or which AI,
did what.

## Web server and proxy

**Settings → Web server & proxy** chooses between Stackpilot's own HTTPS and running behind a reverse proxy such as
Zoraxy: the trusted proxy addresses, whether only the proxy may connect, an optional sub-path, and a live view of what
the app receives from the proxy.

## General

**Settings → General**: the app name, the public address and the default language.

## How Stackpilot protects your data

- Passwords are hashed with **Argon2id**; sign-ins are rate-limited; sessions use secure cookies with CSRF protection.
- Every stored secret (SMTP, backup, UniFi, GitHub and Microsoft 365 credentials, DKIM key) is **encrypted**. The
  password vault has its own key, apart from the database.
- Agents use **mutual TLS** with certificates from Stackpilot's own CA and a pinned fingerprint. No ports are opened
  at the machines. Removing a machine blocks its certificate at once.
- Every AI call goes through scopes, customer limits, policies, approvals and the kill switch, and is logged.
- Tools from other MCP servers are only passed on after you accepted them, and are withdrawn again when they change.
