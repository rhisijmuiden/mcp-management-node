# Certificaten, firewall en VPN

**Onderhoud → Certificaten & firewall** bewaakt drie dingen die meestal pas stukgaan als het te laat is.

## Certificaten

Stackpilot vindt certificaten op twee manieren:

- **Adressen**: TLS-diensten waar het verbinding mee maakt, zoals een website, een mailserver (met STARTTLS) of een
  RD-gateway, vanaf Stackpilot of vanaf een agent bij de klant. Elke HTTPS-uptime-controle wordt vanzelf een adres.
  Voeg er meer toe met **Adres toevoegen**.
- **Certificaatarchieven**: de agent zet de certificaten op een rij op elke Linux-server en Proxmox-node (Let's
  Encrypt, `/etc/ssl`, Proxmox, nginx, apache, postfix) en op Windows (het computerarchief, met IIS-bindingen en het
  RDP-certificaat gemarkeerd als in gebruik).

Per certificaat: namen, uitgever, vervaldatum, of het vertrouwd is en of de naam klopt. Een **waarschuwing** komt 30
dagen voor het verlopen en een **kritieke** melding 7 dagen ervoor (instelbaar). Een certificaat waar je niet om geeft,
kun je met een notitie negeren. Waar het kan, stelt de ochtendsamenvatting voor om het te **vernieuwen** (certbot of de
ACME-client van Proxmox).

## Firewallwijzigingen

Stackpilot bewaart een kopie van elke firewallconfiguratie: de Proxmox VE-firewall, nftables/iptables op Linux, Windows
Firewall en UniFi-zones en -beleid.

1. Elke wijziging wordt een nieuwe versie met een **diff** (toegevoegde en verwijderde regels).
2. Wijzigingen via Stackpilot worden automatisch beoordeeld, met wie ze maakte.
3. Andere wijzigingen blijven een **melding** tot een beheerder op **Als beoordeeld markeren** klikt, met eventueel een
   notitie (wie het maakte en waarom).

Ruis van Docker, fail2ban en CrowdSec wordt eruit gefilterd; je kunt eigen negeerpatronen toevoegen.

## VPN-tunnels

WireGuard, IPsec (strongSwan), OpenVPN, Windows RRAS en UniFi site-to-site-tunnels en VPN-servers. Een tunnel wordt
vanzelf **bewaakt** zodra hij een keer up is geweest (per tunnel aan of uit te zetten). Een bewaakte tunnel die langer
dan de grens (standaard 10 minuten) down blijft terwijl zijn machine online is, wordt een melding. Tunnels die
klapperen of verdwenen zijn, staan in de ochtendsamenvatting.
