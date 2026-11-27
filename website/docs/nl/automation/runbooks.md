# Runbooks

Een runbook is een **vaste procedure** die Stackpilot stap voor stap uitvoert met zijn eigen tools, bijvoorbeeld "een
server patchen", "nieuwe medewerker" of "een gestopte service herstarten". Je schrijft het één keer; daarna starten jij,
een collega of de AI het, en elke uitvoering gaat op dezelfde manier.

## Een runbook maken

1. Open **Automatisering → Runbooks** en klik op **Nieuw runbook**. Begin met een sjabloon (server patchen, nieuwe
   medewerker, nieuw project via GitHub, gestopte service herstarten) of vanaf nul.
2. Kies of het voor **één klant** is of voor **alle klanten**.
3. Voeg **parameters** toe: de vragen die bij het starten gesteld worden, bijvoorbeeld *Welke server?*
4. Voeg **stappen** toe:
    - **Tool**: roept een Stackpilot-tool aan of een tool uit de [MCP-hub](../ai/mcp-hub.md). Argumenten kunnen
      plaatshouders gebruiken zoals `{{params.server}}`, `{{customer.name}}` of een resultaat van een eerdere stap
      (`{{steps.<id>.result.<veld>}}`).
        - *Goedkeuring*: volg het beleid, of vraag altijd.
        - *Optioneel*: afwijzen slaat de stap over in plaats van de uitvoering te stoppen.
        - *Wacht op de taak*: voor tools die een taak starten (zoals updates) eindigt de stap als de taak klaar is.
    - **Handmatig**: instructies voor een mens (bevestigd op de pagina van de uitvoering) of voor de AI.
    - **Wachten**: een aantal minuten, bijvoorbeeld tot een server na een herstart terug is.
5. Sla op.

## Een runbook uitvoeren

1. Klik op **Uitvoeren**, vul de parameters in en start.
2. De pagina van de uitvoering toont elke stap live. Een stap die goedkeuring nodig heeft, verschijnt bij
   **Goedkeuringen** (app en mail). Met *Wacht op het onderhoudsvenster* aan wacht een goedgekeurde stap buiten het
   venster tot het opent.
3. Mislukt een stap, dan stopt de uitvoering (tenzij de stap zegt door te gaan). Je kunt de stap **opnieuw proberen**
   of **overslaan**, of de uitvoering **annuleren**; dan worden de openstaande goedkeuringen ingetrokken.

Het getal in het menu laat zien hoeveel uitvoeringen op je wachten.

## Goed om te weten

- Elke toolstap gaat langs dezelfde controles als een AI-aanroep: het beleid van de klant beslist uitvoeren, vragen of
  blokkeren.
- Een uitvoering bewaart een kopie van het runbook, dus een runbook wijzigen verandert nooit een lopende uitvoering.
- Uitvoeringen die in de app gestart zijn, gebruiken de rechten van de beheerder die ze startte; uitvoeringen van de AI
  gebruiken de rechten van zijn koppeling, en hebben de scopes van elke stap nodig.
- Elke stap staat in het auditlog met het id van de uitvoering.
- Runbooks zijn ook de herstelacties van [automatisch herstel](recovery.md).
