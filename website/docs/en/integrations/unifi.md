# UniFi

Stackpilot links the UniFi networks of your customers through the **official UniFi APIs**, with API keys that are
stored encrypted and never shown again.

## Kinds of connection

| Kind | What it gives | Where you make the key |
| --- | --- | --- |
| **UniFi Network** | Devices, clients, WiFi, firewall, events of one console | Network → Settings → Control Plane → Integrations |
| **Site Manager** | All consoles and sites of an account (or of one Fabric) | unifi.ui.com → API |
| **UniFi Access** | Doors, lock rules, the access log | Access → Settings → General → Advanced → API Token |

## Add a connection, step by step

1. Open **Integrations → UniFi consoles** (or the customer's **Network** tab) and click **Add connection**.
2. Choose the customer and the kind, and paste the API key.
3. Choose how Stackpilot reaches the console:
    - **directly** (an address Stackpilot can reach);
    - **through an agent** in the customer's network: nothing needs a port forward;
    - for Network, **through the Site Manager cloud connector**: no address at all.
4. For a self-signed certificate, Stackpilot shows its fingerprint and pins it before the key is sent.
5. Optionally mark the connection **read-only**: all actions are then off.

Every 5 minutes Stackpilot reads each connection: devices with status, uplink, firmware and clients, WiFi, WAN state and
doors. Changes become **events** (device offline, WAN change, new firmware) and offline devices raise alerts.

## UniFi Fabrics

A Site Manager key made **inside a UniFi Fabric** only sees the consoles and sites of that Fabric.

1. Open **Network (UniFi) → Fabrics** and click **Add a Fabric key** (or mark an existing Site Manager connection as
   a Fabric key).
2. Stackpilot reads every console of the Fabric and links each one as a Network connection through the cloud
   connector, at the same customer: no address, port or agent needed.
3. **Network (UniFi) → Fabrics** shows per Fabric every console with its apps, sites (ISP, WAN uptime), devices and the
   connection that reaches it.

Consoles that join the Fabric later are linked by themselves (or, with auto-link off, raise an alert and are linked
with one click); consoles that leave give a warning. A console that two keys can see is counted once.

??? info "Errors you may see"
    - **HTTP 403**: the key was not made by the console owner.
    - **Console unreachable**: the cloud connector cannot reach it (offline, not adopted, or UniFi OS older than
      5.0.9).
    - Self-hosted Network Servers and consoles without UniFi Network are listed but not linked.
    - Rolling out configuration through Fabrics (Blueprints) has no public API yet and is not included.

## What you can do

- **Network (UniFi) → Network**: live device details, clients, WiFi on/off, guest authorization, restart a device,
  power-cycle a PoE port, a **WiFi troubleshooter** (*why can't this laptop connect?*) and a **guest isolation check**.
- **Network (UniFi) → Access**: doors and their lock state, remote unlock, temporary lock rules, emergency
  lockdown/evacuation, schedules and the access log (*who opened the server room last night?*).

Door actions need the scope `doors` and are destructive: they ask for approval by default.
