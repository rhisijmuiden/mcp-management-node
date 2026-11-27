# Mail server

Stackpilot sends every mail through **your own SMTP server**: alerts, approvals, the morning summary, reports, ticket
mails, change notices, invitations and password resets.

## Set it up

1. Open **Integrations → Mail server**.
2. Fill in:
    - **Host** and **port** (usually 587 for STARTTLS, 465 for TLS);
    - **Security**: none, STARTTLS or TLS, and the **minimum TLS version** (1.2 or 1.3);
    - **Sign-in**: username and password (stored encrypted);
    - **From** and **reply-to** address.
3. Optionally switch on **DKIM signing**: enter the domain, the selector and your private key, and publish the matching
   public key as a DNS TXT record at `<selector>._domainkey.<your domain>`.
4. Click **Send test email**. It uses the values in the form, also before you save them.

!!! tip "Good delivery"
    Use a from address on a domain with SPF, DKIM and DMARC set up, and send through a server that is allowed to send
    for it (for example Microsoft 365, your hosting provider or a relay such as Postmark or Mailgun).

## Who gets what

- **Settings → Notifications**: who gets monitoring alerts and other notifications.
- **Settings → Policies & guardrails → Approvals & notifications**: who gets approval requests.
- The **morning summary**, **reports**, **tickets** and **change notices** each have their own recipients in their own
  settings.

Proxmox nodes get their own notification mail settings from the customer (Proxmox → Nodes), pushed by the agent.
