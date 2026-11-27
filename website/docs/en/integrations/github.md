# GitHub, software library and deployments

## Add a GitHub token

Stackpilot works with GitHub through a token. You can add several: one per owner, for example your own account and a
customer's organisation.

1. Open **Integrations → All integrations → GitHub → Set up step by step**.
2. On GitHub go to **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new
   token**. Choose the owner and **All repositories**.
3. Give **Read and write** on: Administration, Contents, Pull requests, Issues, Actions, Workflows, Webhooks, Secrets,
   Variables, Environments, Deployments, Pages and Commit statuses. With these Stackpilot can do almost everything,
   including creating repositories. (A classic token also works; the guide has a pre-filled link.)
4. Paste the token in the last step of the guide.

Each token shows its login, kind, expiry date and last check. A token that expires within 14 days, or stops working,
is flagged here and on the Integrations page.

## The software library

**Automation → Software → Repositories** is the library of your repositories. It sits above the customers: one
repository can be linked to several customers.

- **Kinds**: *Product* (your own software, for several customers), *Customer-specific* (only for that one customer)
  and *Tooling* (your scripts and tools).
- **Rights per customer**: *May deploy* (may go onto that customer's containers and servers) and *AI may read*. Set
  them per repository, or in the **matrix** of repositories × customers where one click links or removes.
- **Add from GitHub** imports repositories your tokens can see.
- **New repository** creates one on GitHub (your account or an organisation, private or public, with an optional
  `.gitignore` and a `test` branch) and adds it to the library in one go.

With a customer picked in the top bar, only that customer's repositories show. Removing a repository from the library
changes nothing on GitHub.

## Deployments

**Automation → Software → Deployments** puts a repository onto a machine and keeps it up to date.

1. Click **New deployment** and pick a repository from the library.
2. Choose a **production** and a **test** branch.
3. Choose the target: a **container** on Proxmox VE or a **Linux server** with the agent.
4. Pick a preset (an install script in the repo, Python, Node.js, a static site) or write your own **setup command**
   (runs once) and **deploy command** (runs every time). Add environment variables and the systemd service to restart.
5. Choose when it deploys: by hand, **on every push** (GitHub webhook) or by checking for new commits every 5 minutes.

In a container Stackpilot takes a **snapshot first** and restores it when the deployment fails. A server has no
snapshots, so test on the test branch first. **Create → GitHub** makes a new container and deploys into it in one go.

A deployment only saves when the repository may be deployed at that customer. The token reaches the machine only for
the duration of the deploy.

## Together with the AI

With the GitHub MCP server in the [MCP hub](../ai/mcp-hub.md), the AI can run a project from A to Z: create the
repository, issues and pull requests, releases and Actions, and then deploy it. The runbook template *New project via
GitHub* does this step by step.
