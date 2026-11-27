# Installatie

Stackpilot installeer je één keer, op een eigen server. Daarna open je het in de browser en maakt elke beheerde machine
er verbinding mee.

## Wat je nodig hebt

- Een server, VM of LXC-container met **Debian 13** (trixie). 2 CPU-cores, 4 GB geheugen en 20 GB schijf is ruim
  genoeg om mee te beginnen.
- **Uitgaand internet** naar GitHub (voor releases en updates).
- Poort **8443** (TCP) van de Stackpilot-server moet bereikbaar zijn voor je agents. Bij de machines die je beheert hoeft
  niets open.
- Voor Claude (web/app) en ChatGPT: een **openbaar HTTPS-adres**, bijvoorbeeld `https://stackpilot.example.com`,
  meestal via een reverse proxy zoals Zoraxy. Claude Code en VS Code werken ook op je lokale netwerk.

Stackpilot draait native, zonder Docker: Python 3.13, PostgreSQL 17 en Nginx, allemaal uit de Debian-repositories.

## Stap 1: de installer draaien

Log op de server in als root (of met sudo) en voer uit:

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/main/deploy/install.sh | sudo bash
```

Liever het **testkanaal**, met een reverse proxy op `192.168.1.2` ervoor?

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/stackpilot/test/deploy/install.sh \
  | sudo bash -s -- --channel test --proxy 192.168.1.2
```

De installatie duurt een paar minuten. Aan het eind toont de installer het **adres** van de app en een eenmalige
**setupcode**.

??? info "Alle opties van de installer"
    `--channel production|test`, `--version vX.Y.Z`, `--tarball BESTAND`, `--from-source [BRANCH]`,
    `--proxy IP[,IP]`, `--restrict-to-proxy`, `--direct`, `--server-name`, `--base-path /mmn`,
    `--github-token` (alleen voor een privé-fork). Met `--help` zie je ze allemaal.
    De installer opnieuw draaien werkt een installatie bij of repareert hem, en houdt alle gegevens.

## Stap 2: de eerste beheerder aanmaken

1. Open het adres in je browser.
2. Vul de **setupcode** in en kies je naam, e-mailadres en wachtwoord.
3. Je bent nu ingelogd als eerste beheerder.

!!! tip "Setupcode kwijt?"
    Voer `sudo mmn-ctl setup-token` uit op de server voor een nieuwe.

## Stap 3: achter een reverse proxy (optioneel, aangeraden)

Wil je Stackpilot vanaf internet bereiken met een echt certificaat, zet het dan achter een reverse proxy. Met
**Zoraxy**:

1. Installeer met `--proxy <IP van Zoraxy>`, of stel het later in onder **Instellingen → Webserver & proxy**.
2. Maak in Zoraxy een **HTTP-proxyregel** voor je hostnaam (bijvoorbeeld `stackpilot.example.com`) naar de
   Stackpilot-server op poort **80**, met TLS aan en **WebSockets toegestaan**.
3. Maak voor de agents een **Stream Proxy (TCP)** voor poort **8443** naar de Stackpilot-server. Zoraxy mag op deze
   poort TLS *niet* afhandelen: de agents gebruiken hun eigen certificaten (mutual TLS). Zet *Auto start* aan voor de
   stream, zodat hij een herstart van Zoraxy overleeft.
4. Stel in Stackpilot het **openbare adres** in onder **Instellingen → Algemeen**.

De pagina **Instellingen → Webserver & proxy** laat live zien wat de app van de proxy ontvangt, zodat je meteen ziet of
de hostnaam en HTTPS goed doorkomen.

## Verder

Ga door met de [eerste stappen](first-steps.md).

??? note "Waar staat wat op de server"
    | Wat | Waar |
    | --- | --- |
    | App | `/opt/mcp-management-node/current` |
    | Instellingen | `/etc/mcp-management-node/mmn.env` |
    | Gegevens (sleutels, bestanden) | `/var/lib/mcp-management-node` |
    | Service | `mcp-management-node.service` |
    | Logs | `journalctl -u mcp-management-node -f` |

    Het product heette eerder *MCP Management Node*; de interne namen zijn gebleven, zodat bestaande installaties
    blijven werken.
