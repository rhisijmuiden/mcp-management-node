# Vragen en begrippen

## Vragen

??? question "Moet ik poorten openzetten bij mijn klanten?"
    Nee. Agents maken zelf verbinding naar buiten, naar Stackpilot. Alleen de Stackpilot-server moet poort 8443
    bereikbaar hebben voor de agents (en HTTPS voor jou en je AI).

??? question "Kan de AI iets kapotmaken?"
    Alleen binnen wat jij toestaat. Standaard vragen destructieve acties (commando's, updates, herstarts, verwijderen,
    terugzetten) en wijzigingen buiten een onderhoudsvenster eerst aan jou. Je kunt tools helemaal blokkeren, een
    koppeling tot bepaalde klanten beperken en met één knop alle AI-acties pauzeren. Elke aanroep wordt vastgelegd.

??? question "Welke AI-apps werken?"
    Elke MCP-client: Claude (web, desktop, mobiel en Claude Code), ChatGPT (ontwikkelaarsmodus), GitHub Copilot in VS
    Code, n8n en je eigen scripts. Zie [Je AI koppelen](../ai/connect.md).

??? question "Heb ik een AI nodig om Stackpilot te gebruiken?"
    Nee. Alles wat de AI kan, kun je ook in de interface. De AI is een extra manier van werken.

??? question "Draait het in Docker?"
    Nee. Stackpilot installeert native op Debian 13 (Python, PostgreSQL, Nginx). Dat houdt het licht en makkelijk bij te
    werken vanuit de app.

??? question "Wat gebeurt er met mijn gegevens als de server kapotgaat?"
    Zet een [app-back-up](../admin/backups.md) terug op een nieuwe server. Stel op dag één een back-uplocatie en een
    wachtwoordzin in.

??? question "Welke talen?"
    Engels en Nederlands, overal: de app, het portaal, mails en rapporten. Elke gebruiker kiest zijn eigen taal; elke
    klant krijgt mails en rapporten in zijn eigen taal.

??? question "Hoe probeer ik veilig een nieuwe versie?"
    Installeer een tweede Stackpilot op het **test**kanaal, of zet een testinstallatie daarop onder
    [Info & updates](../admin/updates.md).

## Begrippen

Agent
:   Het kleine programma op elke beheerde machine dat verbinding maakt met Stackpilot en daar het werk doet.

Goedkeuring
:   Een verzoek dat op een mens wacht voordat een actie wordt uitgevoerd. Komt per mail en in de app.

Klant
:   De bovenste laag: een organisatie waarvan je de IT beheert. Je eigen omgeving is de klant *Home*.

Noodstop
:   *Alle AI-acties pauzeren*: stopt elke AI-aanroep die iets wijzigt, tot je hervat.

Onderhoudsvenster
:   De tijd waarin wijzigingen en herstarts bij een klant mogen, bijvoorbeeld `za 22:00-02:00`.

MCP
:   Model Context Protocol, de standaard waarmee AI-apps tools gebruiken. Stackpilot is een MCP-server.

MCP-hub
:   Andere MCP-servers achter het adres van Stackpilot, met dezelfde regels.

Koppelcode
:   Een eenmalige code (24 uur geldig) om een nieuwe agent te koppelen.

Beleid
:   De regel die bepaalt of een tool wordt uitgevoerd, om goedkeuring vraagt of geblokkeerd wordt.

Risiconiveau
:   Lezen, bedienen, wijzigen of destructief: hoeveel schade een tool kan doen.

Runbook
:   Een vaste procedure die Stackpilot stap voor stap uitvoert.

Scope
:   Een groep tools die een koppeling mag gebruiken, zoals `logs` of `exec`.

SLA
:   De afgesproken serviceniveaus met een klant, bijvoorbeeld 99,9% uptime en back-ups maximaal 26 uur oud.
