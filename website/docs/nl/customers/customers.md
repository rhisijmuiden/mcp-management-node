# Klanten en hun instellingen

Klanten zijn de bovenste laag van Stackpilot. Elke Proxmox-node, server, werkplek, elk UniFi-netwerk, elke Microsoft
365-tenant, back-up, elk ticket en elke kostenregel hoort bij één klant. Je eigen omgeving is de klant **Home**.

![Klanten](../assets/images/customers.png)

## Een klant aanmaken

1. Open **Klant → Alle klanten** en klik op **Nieuwe klant**.
2. Vul de naam in en, als je wilt, een korte code en de hoofdcontactpersoon.
3. Sla op. De klantpagina gaat open.

## De klantpagina

Kies de klant in de bovenbalk (of klik erop in de lijst). De klantpagina heeft deze tabbladen:

| Tabblad | Wat je daar doet |
| --- | --- |
| **Overzicht** | Tegels voor alles wat aan de klant gekoppeld is, plus **Wat is er nog niet ingericht?** |
| **Proxmox**, **Servers**, **Werkplekken** | De machines van de klant. **Machine toevoegen** installeert een nieuwe. |
| **Netwerk** | De UniFi-koppelingen van deze klant. |
| **AI-toegang** | Welke AI-apps en tokens voor deze klant mogen werken. |
| **Contacten & notities** | Contactpersonen, SLA, onderhoudsvenster, notities voor de AI. |
| **Rapporten** | Maandrapporten en de ontvangers van onderhoudsrapporten. |

### Tegels in het overzicht

Het overzicht begint met een tegel voor Proxmox, servers, werkplekken, UniFi, Microsoft 365, MCP-servers, software,
back-ups, open tickets, AI-toegang, documentatie en kluis, het portaal en uptime-controles. Een **gestippelde tegel** is
nog niet ingericht: klik erop en de stap-voor-stap-uitleg gaat open.

**Wat is er nog niet ingericht?** zet de gaten op een rij, elk met een link om het op te lossen: machines offline,
back-upproblemen, geen onderhoudsvenster, geen SLA, contactpersoon of notities, geen uptime-controles, geen
portaalaccounts, geen back-uplocatie voor de app.

## Contactpersonen, onderhoudsvenster en SLA

Op **Contacten & notities**:

1. **Contactpersonen**: namen, e-mail en telefoon. De hoofdcontactpersoon krijgt het maandrapport, tenzij je andere
   ontvangers kiest.
2. **Onderhoudsvenster**: wanneer wijzigingen en herstarts mogen, bijvoorbeeld `za 22:00-02:00`. Stackpilot gebruikt
   het overal: de AI vraagt erbuiten om goedkeuring, goedgekeurde voorstellen wachten erop en de wijzigingskalender
   plant erbinnen.
3. **SLA**: bijvoorbeeld "99,9% uptime, back-ups maximaal 26 uur oud". Het uptimepercentage en de back-upleeftijd
   worden eruit gelezen.
4. **Notities voor de AI**: wat de AI moet weten, zoals *"Herstart de SQL-server nooit onder kantoortijd"*. De AI leest
   deze voordat hij bij deze klant aan de slag gaat.

!!! tip
    Vertel de AI iets over een klant ("de NAS staat in de meterkast") en hij kan het zelf als notitie bewaren.

## Optimalisatiescan

De **AI-optimalisatiescan** op de klantpagina beantwoordt in één keer *"wat kan hier beter?"*: geheugen en balans van
het Proxmox-cluster, opslag en schijfgezondheid, volle schijven, openstaande updates en herstarts, oude agents,
gestopte services, gestopte guests, UniFi-apparaten offline, firmware en of het gastnetwerk gescheiden is van het LAN.
Bij elke bevinding hoort een voorstel. De AI kan dezelfde scan draaien.

## AI-toegang per klant

Onder **AI-toegang** koppel je AI-verbindingen en API-tokens aan klanten. Een token met *Alleen deze klanten* ziet
niets van andere klanten, en dat geldt voor elke tool. Tools die op de hele app werken (app-updates, mailserver,
auditlog) worden dan geweigerd. Met **Nieuw klanttoken** maak je er hier direct een.

## Een machine verplaatsen

Staat een machine bij de verkeerde klant? Verplaats hem op zijn eigen pagina onder **Beheren**.
