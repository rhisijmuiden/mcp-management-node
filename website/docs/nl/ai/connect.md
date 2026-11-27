# Je AI koppelen

Stackpilot heeft een ingebouwde **MCP-server**. MCP (Model Context Protocol) is de standaard waarmee AI-apps tools
gebruiken. Na het koppelen kan je AI meer dan 230 Stackpilot-tools gebruiken, altijd binnen het
[beleid](policies-approvals.md) dat jij instelt.

**Het adres** is `https://<jouw adres>/mcp`. Je vindt het, met een kopieerknop, in het commandocentrum, onder
**Koppelingen → Gekoppelde AI** en in **Help → Claude, ChatGPT of Copilot koppelen**.

!!! warning "Claude (web/app) en ChatGPT hebben een openbaar HTTPS-adres nodig"
    Ze maken verbinding vanuit hun eigen cloud, dus een lokaal of gewoon HTTP-adres bereiken ze niet. Publiceer
    Stackpilot via een reverse proxy met een domein en certificaat (zie [Installatie](../start/install.md)). Claude Code
    en VS Code draaien op je eigen computer en werken ook op je lokale netwerk.

## Twee manieren van inloggen

- **OAuth (aangeraden voor AI-apps).** De AI-app stuurt je naar Stackpilot, je logt in, vinkt de **rechten** (scopes)
  aan en klikt op **Toegang toestaan**. Niets te kopiëren. De koppeling staat daarna onder **Koppelingen → Gekoppelde
  AI**, waar je hem tot een paar klanten beperkt of loskoppelt.
- **API-token (voor scripts en tools zoals n8n).** Maak er een onder **Koppelingen → API-tokens → Nieuwe token**: kies de
  scopes, een vervaldatum en eventueel de klanten. Het token (`mmn_…`) wordt één keer getoond. Stuur het mee als header
  `Authorization: Bearer mmn_…`.

## Stap voor stap per AI-app

=== "Claude (web, desktop, mobiel)"

    1. Open in Claude **Instellingen → Connectors**.
    2. Klik op **Aangepaste connector toevoegen** en geef hem een naam, bijvoorbeeld **Stackpilot**.
    3. Vul de **URL van de externe MCP-server** in: `https://<jouw adres>/mcp`.
    4. Klik op **Toevoegen** en daarna op **Verbinden**. Je gaat naar Stackpilot: log in, kies de rechten en klik op
       **Toegang toestaan**.
    5. Zet in een chat de connector aan via het **toolsmenu** en vraag bijvoorbeeld: *"Welke back-ups zijn deze week
       gedraaid?"*

    Aangepaste connectors zijn er in de betaalde abonnementen van Claude; in Team en Enterprise moet een eigenaar ze
    misschien eerst toestaan.

=== "Claude Code"

    1. Voeg de server toe:
       ```bash
       claude mcp add --transport http stackpilot https://<jouw adres>/mcp
       ```
    2. Start Claude Code, typ `/mcp`, kies **stackpilot** en dan **Authenticate**.
    3. Je browser opent Stackpilot: log in en klik op **Toegang toestaan**.

    Met een token in plaats daarvan: voeg `--header "Authorization: Bearer mmn_JOUW_TOKEN"` toe.

=== "ChatGPT"

    1. Open **Instellingen → Apps & Connectors → Geavanceerde instellingen** en zet **Ontwikkelaarsmodus** aan.
    2. Ga terug naar **Apps & Connectors** en klik op **Maken**.
    3. Geef een naam en vul de **MCP-server-URL** in: `https://<jouw adres>/mcp`.
    4. Kies **OAuth**, vink aan dat je de applicatie vertrouwt en klik op **Maken**.
    5. Log in het venster dat opent in bij Stackpilot, kies de rechten en klik op **Toegang toestaan**. Voeg in een chat
       de connector toe via **+ → Ontwikkelaarsmodus**.

    ChatGPT vraagt je om bevestiging voordat het een tool uitvoert die iets wijzigt.

=== "GitHub Copilot (VS Code)"

    1. Maak `.vscode/mcp.json` in je project (of kies **MCP: Add Server**):
       ```json
       { "servers": { "stackpilot": { "type": "http", "url": "https://<jouw adres>/mcp" } } }
       ```
    2. Klik op **Start** boven de server. VS Code vraagt je in te loggen: sta het toe en klik in Stackpilot op
       **Toegang toestaan**.
    3. Open Copilot Chat, kies de **Agent**-modus en controleer onder het toolsicoon dat Stackpilot aan staat.
    4. Bij Copilot Business of Enterprise moet een beheerder *MCP servers in Copilot* toestaan in het beleid van de
       organisatie.

=== "Andere clients"

    De server volgt de MCP-standaard:

    - **Transport**: Streamable HTTP op `/mcp`.
    - **OAuth 2.1**: discovery op `/.well-known/oauth-protected-resource`, dynamische clientregistratie en PKCE.
    - **Of een token**: header `Authorization: Bearer mmn_…`.

    ```bash
    curl -s https://<jouw adres>/mcp \
      -H "Authorization: Bearer mmn_JOUW_TOKEN" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    ```

## Scopes: waar een koppeling bij mag

Een scope is een groep tools. Een koppeling ziet alleen de tools van de scopes die je aanvinkte; *read* is de standaard.
Alleen beheerders kunnen de krachtigere scopes uitdelen.

| Gebied | Scopes |
| --- | --- |
| Kijken | `read` (status, lijsten, logs van de app), `logs` (logs, services, pakketten en bestanden op machines) |
| Machines | `nodes`, `node_updates`, `services`, `exec` (commando's), `copy`, `workstations` |
| Proxmox | `guests`, `guests_admin`, `templates`, `network`, `cluster`, `tasks` |
| Back-ups | `backups`, `restore`, `restore_guests` |
| Netwerken | `unifi`, `unifi_admin`, `doors` |
| Werk voor klanten | `tickets`, `docs`, `vault`, `reports`, `runbooks`, `monitoring`, `netsec`, `changes`, `changes_admin`, `costs`, `costs_admin` |
| Microsoft 365 | `m365`, `m365_admin` |
| Software | `deploy` |
| De app zelf | `updates`, `settings` |
| Andere MCP-servers | `hub` |

De volledige lijst, met elke tool, staat in de [MCP-toolnaslag](../reference/mcp-tools.md) (Engels).

## Een koppeling tot klanten beperken

Een koppeling of token kan aan **alleen bepaalde klanten** gekoppeld worden. Hij ziet dan niets van de andere, en tools
die op de hele app werken worden geweigerd. Stel dit in onder **Koppelingen → Gekoppelde AI** (per app), bij het maken
van een token, of op het tabblad **AI-toegang** van de klant.

## Wat je kunt vragen

- *"Wat is er vannacht gebeurd? Wat heeft vandaag mijn aandacht nodig?"*
- *"Controleer klant Acme: is er iets mis?"*
- *"Welke servers draaien nog een nginx-versie met een bekende CVE?"*
- *"Installeer in het onderhoudsvenster de openstaande updates op alle nodes van Acme."*
- *"Waarom komt deze laptop niet op de wifi?"*
- *"Maak een GitHub-repository voor het nieuwe project en zet het op een testcontainer."*

De AI leest eerst de notities, het onderhoudsvenster en de documentatie van de klant, en volgt dus jouw regels.
