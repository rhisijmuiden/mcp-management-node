# Kosten en licenties

**Kennis & administratie → Kosten & licenties** laat zien wat elke klant je per maand kost, als basis voor je factuur,
en bewaart alles met een prijs of een einddatum op één plek.

## Stap 1: de prijslijst invullen

Open het tabblad **Prijslijst** en vul maandprijzen in voor:

- elke vCPU, GB geheugen en GB schijf die de VM's en containers van de klant krijgen;
- elke beheerde machine (server, werkplek, Proxmox VE-node);
- elke gelicentieerde Microsoft 365-gebruiker en elk soort Microsoft 365-licentie.

Een klant kan voor elke regel een eigen prijs hebben, die voorgaat op de standaard. Vul een inkoopprijs in om je marge te
zien. Microsoft 365-licenties die een tenant heeft maar die nog geen prijs hebben, worden genoemd, zodat je niets
vergeet.

## Stap 2: licenties, contracten en hardware toevoegen

Voeg op **Licenties & contracten** softwarelicenties, contracten (support, internet, hosting, lease), hardware met
garantie, diensten en eenmalige kosten toe. Per item: leverancier, referentie, serienummer, de machine waar het bij
hoort, aantal, verkoop- en inkoopprijs, en hoe het gefactureerd wordt (per maand, kwartaal, jaar, eenmalig, of alleen
bijhouden).

Geef contracten een **einddatum**, een **opzegtermijn** en of ze **automatisch verlengen**. Stackpilot waarschuwt voor de
einddatum en voor de laatste dag om op te zeggen (standaard 60 dagen vooraf), en schuift de einddatum op als een
contract vanzelf verlengt. **Hardware importeren** maakt een item voor elke machine met een model of serienummer.

## Stap 3: het maandoverzicht

Elk uur legt Stackpilot vast wat elke klant gebruikt. Het **Maandoverzicht** per klant toont de regels van de maand
(resources, machines, Microsoft 365, daarna de items die die maand gefactureerd worden), korting, btw, totalen en marge.

1. Controleer de maand. Die wordt live berekend.
2. Klik op **Definitief maken**: de regels worden vastgezet, zodat de factuur niet verandert als prijzen later wijzigen.
   Je kunt hem weer openen.
3. Exporteer per klant als **pdf** of **csv**, of alle klanten van een maand in één csv voor je boekhouding.

Per klant stel je in of hij überhaupt gefactureerd wordt (je eigen omgeving niet), het debiteurnummer, een korting,
welke onderdelen gefactureerd worden en een opmerking op het overzicht.

## Waarschuwingen

Contracten, licenties en garanties die aflopen geven een melding en staan in de ochtendsamenvatting, samen met regels
zonder prijs. Het maandrapport voor de klant kan het kostenoverzicht bevatten (staat standaard uit).
