# Servers

The same agent runs on stand-alone **Linux** (Debian, Ubuntu, RHEL/Rocky/Alma, Fedora, SUSE, Alpine) and **Windows**
servers (2016 and newer). Add one with **Servers → Add server** or **Add machine** on the customer page; see
[Agents and pairing](agents.md).

## Per server

Open **Environment → Servers** and click a server:

- **Health check** in one click: problems found, failed services, recent errors, busiest processes.
- **Services**: systemd units or Windows services, start, stop, restart.
- **Logs**: the journal on Linux, the Event Log on Windows, and log files.
- **Updates**: apt, dnf/yum or Windows Update, install and reboot.
- **Installed software**.
- A **shell** or **PowerShell** in the browser, commands and files.

## Across all servers

- **Log search** (**Servers → Log search**): search many servers at once, or ask for an overview of errors.
- **Data copy** (**Servers → Data copy**): copy files or folders from one server to another. It goes agent to agent
  through Stackpilot, so the servers don't need to reach each other.
- **Software & CVE** (**Maintenance → Updates & software**): which servers run a package, in which version, and are
  they affected by a vulnerability. See [Updates and software](../care/updates-software.md).

## Ask the AI

- *"Why is the web server slow? Look at the logs and restart nginx if needed."*
- *"Which servers of Acme have errors in their event log since yesterday?"*
- *"Copy /srv/data from web01 to web02."*

Commands, restarts and updates follow the [policies](../ai/policies-approvals.md): by default a command asks for
approval, and a stated reason is required.
