# Certificates, firewall and VPN

**Maintenance → Certificates & firewall** watches three things that usually only break once it is too late.

## Certificates

Stackpilot finds certificates in two ways:

- **Endpoints**: TLS services it connects to, such as a website, a mail server (with STARTTLS) or an RD gateway, from
  Stackpilot or from an agent at the customer. Every HTTPS uptime check becomes an endpoint by itself. Add more with
  **Add address**.
- **Certificate stores**: the agent lists the certificates on every Linux server and Proxmox node (Let's Encrypt,
  `/etc/ssl`, Proxmox, nginx, apache, postfix) and on Windows (the computer store, with IIS bindings and the RDP
  certificate marked as in use).

Per certificate: names, issuer, expiry, whether it is trusted and whether the name matches. A **warning** comes 30 days
before expiry and a **critical** alert 7 days before (adjustable). A certificate you don't care about can be ignored
with a note. Where possible the morning summary proposes to **renew** it (certbot or the Proxmox ACME client).

## Firewall changes

Stackpilot keeps a copy of every firewall configuration: the Proxmox VE firewall, nftables/iptables on Linux, Windows
Firewall and UniFi zones and policies.

1. Every change becomes a new version with a **diff** (lines added and removed).
2. Changes made through Stackpilot are acknowledged automatically, with who made them.
3. Other changes stay an **alert** until an admin clicks **Mark reviewed**, with an optional note (who made it and
   why).

Noise from Docker, fail2ban and CrowdSec is filtered out; you can add your own ignore patterns.

## VPN tunnels

WireGuard, IPsec (strongSwan), OpenVPN, Windows RRAS and UniFi site-to-site tunnels and VPN servers. A tunnel is
**watched** automatically once it has been up (switch it on or off per tunnel). A watched tunnel that stays down longer
than the limit (default 10 minutes) while its machine is online becomes an alert. Tunnels that flap or disappeared are
in the morning summary.
