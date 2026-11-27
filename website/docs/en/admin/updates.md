# Info, updates and channels

![Info and updates](../assets/images/info-updates.png)

Click the **version number** in the top bar to open **Info & updates**.

## What it shows

The version, commit, build date, uptime and the versions of Python, PostgreSQL and the operating system.

## Two channels

| Channel | Follows | Versions |
| --- | --- | --- |
| **Production** | the `main` branch | `vX.Y.Z` |
| **Test** | the `test` branch | `vX.Y.Z-test.N` (prereleases) |

Switch the channel on this page. Use **Test** on a test install to try new features before they reach production.

## Updating, step by step

1. Click **Check now**. Stackpilot asks GitHub for the newest release of your channel and shows its release
   notes.
2. Click **Install** (with the version number).
3. Stackpilot downloads the release, checks its SHA-256, **backs up the database**, migrates, switches to the new
   version and restarts.
4. If the new version doesn't start, it **rolls back** the code and the database by itself.

The page reconnects by itself when the update is done. Running the installer again also upgrades an install.

!!! info "Agents"
    Agents update themselves to the version that comes with Stackpilot, unless you switched that off under
    **Integrations → Agents & pairing**. An agent update briefly interrupts commands that are running on that machine.
