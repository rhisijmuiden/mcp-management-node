# Wijzigingskalender

**Onderhoud → Wijzigingskalender** bevat het geplande onderhoud en de wijzigingen van elke klant, en houdt de klant op
de hoogte met berichten vooraf en achteraf.

## Een wijziging plannen

1. Klik op **Wijziging plannen** en kies de klant.
2. Vul de titel in, de soort (onderhoud, updates, wijziging, migratie, overig), de verwachte impact (geen, korte
   onderbrekingen, trager, niet beschikbaar), de start en de duur.
3. Kies de machines die erbij betrokken zijn.
4. Schrijf een **bericht voor de klant** (gebruikt in de berichten, het portaal en de agenda) en, als dat nodig is,
   interne notities voor je team.
5. Koppel eventueel de **geplande taak** die het werk doet en een **runbook** dat vanzelf start bij het begin.
6. Sla op als **concept** (alleen zichtbaar voor je team, verstuurt niets) of **plan** hem. Elke wijziging krijgt een
   nummer, bijvoorbeeld `CHG-1001`.

### Controles tijdens het plannen

Stackpilot controleert de tijd tegen het **onderhoudsvenster** van de klant, andere wijzigingen van dezelfde klant
(waarschuwing als ze machines delen), geplande taken in die periode, en of de klant nog op tijd het gebruikelijke
bericht krijgt. Het **stelt het eerstvolgende vrije moment voor** binnen het venster; neem het met één klik over.

## Berichten aan de klant

Via je eigen mailserver, aan de contactpersonen en portaalgebruikers van de klant, in het Nederlands of Engels:

- standaard **3 dagen** en **1 uur** voor de start (andere tijden per klant of per wijziging);
- een mail met de **nieuwe tijd** als een aangekondigde wijziging verschuift;
- een **afzegging** als hij niet doorgaat;
- een **bericht achteraf** met de uitkomst.

Elk bericht wordt bij de wijziging vastgelegd.

## Status

*Gepland* → *bezig* bij de start → *afgerond* aan het eind, op de klok. Met een gekoppeld runbook bepaalt de uitvoering of
hij afgerond of mislukt is. Je kunt ook met de hand afronden, met een notitie voor de klant.

## Waar een wijziging vandaan kwam

Elke wijziging toont zijn **herkomst**: handmatig, een ticket, een melding of bevinding, een schema, de AI of automatisch
herstel. Werk dat Stackpilot zelf doet, wordt automatisch vastgelegd: elke uitvoering van Proxmox-onderhoud (met het
tijdlijnrapport), geplande taken die machines wijzigen, en automatisch herstel (de laatste twee kun je uitzetten).

## Kalender en iCal

De maandweergave toont elke wijziging, de onderhoudsvensters en, als je wilt, de uitvoeringen van geplande taken.
Abonneer je erop in Outlook, Google of Apple Agenda met een geheime **iCal-link**: één per klant (ook te zien in hun
portaal) en één voor je team met alle klanten. Concepten en interne notities verschijnen er nooit in.
