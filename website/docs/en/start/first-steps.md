# First steps

You have just installed Stackpilot and created the first admin. Go through this list once; it takes about half an
hour. The customer overview later shows a tile **What is not set up yet?** that reminds you of anything you skipped.

- [ ] **Public address and language.** Under **Settings → General**, set the public address (for example
      `https://stackpilot.example.com`) and the default language (English or Dutch). Every user can choose their own
      language later in the account menu at the top right.
- [ ] **Mail server.** Under **Integrations → Mail server**, enter your SMTP server and send a test mail. Without it
      you get no alerts, approvals or reports. See [Mail server](../admin/mail.md).
- [ ] **Backup of Stackpilot itself.** Under **Settings → App backups**, add a backup location (FTP, FTPS, SFTP or
      SCP), set a **passphrase** and a schedule. See [App backups](../admin/backups.md).
- [ ] **Customers.** Under **Customer → All customers**, create your customers. *Home* already exists for your own
      environment. Fill in contacts, the maintenance window and the SLA. See [Customers](../customers/customers.md).
- [ ] **Machines.** On a customer page click **Add machine** and install the agent on a Proxmox VE node, a server or
      a workstation. See [Agents and pairing](../machines/agents.md).
- [ ] **Your AI.** Connect Claude, ChatGPT or Copilot. See [Connect your AI](../ai/connect.md).
- [ ] **Policies.** Check under **Settings → Policies & guardrails** what the AI may do by itself. The defaults are safe:
      destructive actions and anything outside a maintenance window ask you first. See
      [Policies and approvals](../ai/policies-approvals.md).
- [ ] **Users.** Add colleagues under **Settings → Users** as *admin* or *viewer* (looks, cannot change).

## Then, when you are ready

- Link **Microsoft 365**, **UniFi** and **GitHub** under **Integrations**: every integration has a step-by-step guide.
  See [Integrations](../integrations/index.md).
- Look at the first [morning summary](../automation/morning-summary.md) the day after: it shows what needs attention
  at each customer.
- Switch on the [customer portal](../customers/portal-tickets.md) under **Settings → Customer portal** if customers
  should report problems themselves.
- Fill in your prices under **Costs & licences** if you want a monthly statement per customer.
