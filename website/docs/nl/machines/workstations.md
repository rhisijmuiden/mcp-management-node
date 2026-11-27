# Werkplekken

Pc's en laptops worden door dezelfde agent beheerd als servers. Kies bij het koppelen **Werkplek** (of **Werkplek
toevoegen** op het tabblad Werkplekken van de klant). Ze verschijnen onder **Omgeving → Werkplekken**.

## Inventaris

Elke 6 uur dat de pc aan staat (of nu, met **Inventaris vernieuwen**) verzamelt Stackpilot, alleen-lezen:

- fabrikant, model, serienummer, BIOS, CPU en geheugen;
- besturingssysteem, versie, installatiedatum, laatste opstart en domein;
- wie er is ingelogd, en de geïnstalleerde software;
- schijven met gezondheid, SSD-slijtage en temperatuur, volumes en vrije ruimte;
- de geschiedenis van Windows Update (apt-geschiedenis op Linux), TPM, Secure Boot en accu.

## Beveiligingsstatus

Elke pc krijgt een status: **OK**, **Aandacht** of **Actie nodig**, op basis van deze checks: antivirus (Microsoft
Defender of een ander product in Windows-beveiliging), leeftijd van de virusdefinities, dreigingen in de afgelopen 30
dagen, firewall, BitLocker (LUKS op Linux), openstaande updates, de laatste update, wachtende herstart,
schijfgezondheid, SSD-slijtage en vrije ruimte. De lijst toont de problemen, zodat je in één oogopslag ziet welke pc's
je nodig hebben.

## Software zoeken

**Software zoeken** beantwoordt *"welke pc's hebben TeamViewer?"* of `office*`, ook voor pc's die nu uit staan.

## Acties (beheerders)

- Microsoft Defender snelle of volledige scan, virusdefinities bijwerken.
- Groepsbeleid vernieuwen.
- Een bericht sturen aan de ingelogde gebruikers.
- Een gebruiker afmelden (typ de pc-naam om te bevestigen).

Updates, herstarts, logs, services en de console staan op de pagina van de machine onder *Logs, services en console*.

## Vraag het de AI

*"Mijn pc is traag"*: de AI draait een diagnose op die werkplek en vertelt wat hij vond. Acties op pc's vragen de scope
`workstations`; een gebruiker afmelden is destructief en vraagt standaard om goedkeuring.

Werkplekken staan in het maandrapport met hun beveiligingsstatus. Hun beschikbaarheid telt niet mee (pc's gaan slapen).
