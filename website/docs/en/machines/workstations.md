# Workstations

PCs and laptops are managed by the same agent as servers. When you pair one, choose **Workstation** (or **Add
workstation** on the customer's Workstations tab). They appear under **Environment → Workstations**.

## Inventory

Every 6 hours while the PC is on (or now, with **Refresh inventory**) Stackpilot collects, read-only:

- manufacturer, model, serial number, BIOS, CPU and memory;
- operating system, version, install date, last boot and domain;
- who is logged in, and the installed software;
- disks with health, SSD wear and temperature, volumes and free space;
- Windows Update history (apt history on Linux), TPM, Secure Boot and battery.

## Security status

Every PC gets a status: **OK**, **Attention** or **Action needed**, from these checks: antivirus (Microsoft Defender or
another product in Windows Security Center), signature age, threats in the last 30 days, firewall, BitLocker (LUKS on
Linux), pending updates, the last update, pending reboot, disk health, SSD wear and free space. The list shows the
issues, so you see at a glance which PCs need you.

## Find software

**Find software** answers *"which PCs have TeamViewer?"* or `office*`, also for PCs that are switched off right now.

## Actions (admins)

- Microsoft Defender quick or full scan, update virus signatures.
- Refresh Group Policy.
- Send a message to the logged-in users.
- Log off a user (type the PC name to confirm).

Updates, reboots, logs, services and the console are on the machine page under *Logs, services and console*.

## Ask the AI

*"My PC is slow"*: the AI runs a diagnosis on that workstation and tells you what it found. Actions on PCs need the
scope `workstations`; logging off a user is destructive and asks for approval by default.

Workstations appear in the monthly report with their security status. Their availability is not counted (PCs
sleep).
