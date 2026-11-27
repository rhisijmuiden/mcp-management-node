# Installation

Stackpilot is installed once, on its own server. You then reach it in the browser and every managed machine connects
to it.

## What you need

- A server, VM or LXC container with **Debian 13** (trixie). 2 CPU cores, 4 GB of memory and 20 GB of disk are
  plenty to start with.
- **Outbound internet** to GitHub (for releases and updates).
- Port **8443** (TCP) on the Stackpilot server must be reachable for your agents. Nothing has to be opened at the
  machines you manage.
- For Claude (web/app) and ChatGPT: a **public HTTPS address**, for example `https://stackpilot.example.com`, usually
  through a reverse proxy such as Zoraxy. Claude Code and VS Code also work on your local network.

Stackpilot runs natively, without Docker: Python 3.13, PostgreSQL 17 and Nginx, all from the Debian repositories.

## Step 1: run the installer

Log in on the server as root (or with sudo) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/main/deploy/install.sh | sudo bash
```

Want the **test channel** instead, and does a reverse proxy at `192.168.1.2` sit in front?

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/test/deploy/install.sh \
  | sudo bash -s -- --channel test --proxy 192.168.1.2
```

The installer takes a few minutes. At the end it prints the **address** of the app and a one-time **setup code**.

??? info "All installer options"
    `--channel production|test`, `--version vX.Y.Z`, `--tarball FILE`, `--from-source [BRANCH]`,
    `--proxy IP[,IP]`, `--restrict-to-proxy`, `--direct`, `--server-name`, `--base-path /mmn`,
    `--github-token` (only for a private fork). Run it with `--help` to see them all.
    Running the installer again upgrades or repairs an install and keeps all data.

## Step 2: create the first admin

1. Open the address in your browser.
2. Enter the **setup code** and choose your name, email address and password.
3. You are now signed in as the first admin.

!!! tip "Lost the setup code?"
    Run `sudo mmn-ctl setup-token` on the server for a new one.

## Step 3: behind a reverse proxy (optional, recommended)

To reach Stackpilot from the internet with a real certificate, put it behind a reverse proxy. With **Zoraxy**:

1. Install with `--proxy <Zoraxy IP>`, or set it later under **Settings → Web server & proxy**.
2. In Zoraxy, add an **HTTP proxy rule** for your host name (for example `stackpilot.example.com`) to the Stackpilot
   server on port **80**, with TLS on and **WebSockets allowed**.
3. For the agents, add a **Stream Proxy (TCP)** for port **8443** to the Stackpilot server. Zoraxy must *not*
   terminate TLS on this port: the agents use their own certificates (mutual TLS). Switch on *Auto start* for the
   stream so it survives a restart of Zoraxy.
4. In Stackpilot, set the **public address** under **Settings → General**.

The page **Settings → Web server & proxy** shows live what the app receives from the proxy, so you can see straight
away whether the host name and HTTPS come through.

## Next

Continue with the [first steps](first-steps.md).

??? note "Where things are on the server"
    | What | Where |
    | --- | --- |
    | App | `/opt/mcp-management-node/current` |
    | Settings | `/etc/mcp-management-node/mmn.env` |
    | Data (keys, files) | `/var/lib/mcp-management-node` |
    | Service | `mcp-management-node.service` |
    | Logs | `journalctl -u mcp-management-node -f` |

    The product used to be called *MCP Management Node*; the internal names stayed the same so existing installs
    keep working.
