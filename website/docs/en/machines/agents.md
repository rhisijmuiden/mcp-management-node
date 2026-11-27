# Agents and pairing

## What is the agent?

The agent (`mmn-agent`) is a small program that runs on every machine Stackpilot manages: Proxmox VE nodes, Linux
servers, Windows servers and workstations. It has no dependencies and uses little memory.

- It connects **outward** to Stackpilot on port **8443**, over an encrypted connection (WebSocket with mutual TLS).
  Nothing has to be opened at the customer: no port forward, no VPN.
- It carries out what Stackpilot asks: read status and logs, install updates, restart services, run a command. The
  browser console, file copies and UniFi calls are also relayed through this connection.
- It **updates itself** to the version that comes with Stackpilot (can be switched off under **Integrations →
  Agents & pairing**).

## Pairing a machine, step by step

Pairing means: the agent introduces itself once with a **one-time code**, and gets its own certificate in return.

1. Pick the customer in the top bar and open the customer page.
2. Click **Add machine** and choose what it is: **Proxmox VE node**, **server** or **workstation**. (You can also start
   from **Proxmox → Nodes → Add node**, **Servers → Add server** or **Integrations → Agents & pairing**.)
3. Stackpilot makes a **pairing code** (valid 24 hours) and shows one install command for the right operating system.
4. Run that command on the machine:

    === "Proxmox VE or Linux (as root)"

        ```bash
        curl -fsSL https://<your address>/api/agent/install.sh | bash -s -- --code XXXX-XXXX-XXXX
        ```

        Works on Debian, Ubuntu, RHEL/Rocky/Alma, Fedora, SUSE and Alpine. On a Proxmox node you can use the shell
        in the Proxmox web interface.

    === "Windows (PowerShell as Administrator)"

        Copy the one-line `powershell … install.ps1 … -Code XXXX-XXXX-XXXX` command from the dialog. Windows Server
        2016+ and Windows 10+ are supported. The agent runs as the Windows service `mmn-agent`.

    === "Windows, silent (GPO, Intune, RMM)"

        ```powershell
        msiexec /i mmn-agent.msi /qn SERVER=… FINGERPRINT=… CODE=…
        ```

        The dialog shows the exact values.

5. Within seconds the machine appears at the customer with its status. Done.

The command checks the download (SHA-256) and the identity of your Stackpilot server (a pinned fingerprint), so an
agent never talks to a wrong server.

## Before the first machine: the agent address

Under **Integrations → Agents & pairing** check the **address for agents**: the host name agents use to reach port
8443, for example `stackpilot.example.com`. Behind Zoraxy this needs a **Stream Proxy (TCP)** on 8443, see
[Installation](../start/install.md#step-3-behind-a-reverse-proxy-optional-recommended).

## When an agent is offline

- The machine shows *offline* and, after a few minutes, an alert opens (and is mailed).
- Check that the machine is on and has internet, and that port 8443 of Stackpilot is reachable from there.
- On Linux: `systemctl status mmn-agent` and `journalctl -u mmn-agent`. On Windows: the service `mmn-agent` and the
  log in `C:\ProgramData\mmn-agent\agent.log`.
- For a VM or container on a Proxmox node that is still online, [automatic recovery](../automation/recovery.md) can
  start the guest or restart the agent inside it.
- Still stuck? **Pair again** on the machine's page makes a new code; run the install command again.

!!! info "Removing a machine"
    Removing a machine in Stackpilot blocks its certificate immediately; the agent can no longer connect.
