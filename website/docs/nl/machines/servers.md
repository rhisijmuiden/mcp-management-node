# Servers

Dezelfde agent draait op losse **Linux**-servers (Debian, Ubuntu, RHEL/Rocky/Alma, Fedora, SUSE, Alpine) en
**Windows**-servers (2016 en nieuwer). Voeg er een toe met **Servers → Server toevoegen** of **Machine toevoegen** op de
klantpagina; zie [Agents en koppelen](agents.md).

## Per server

Open **Omgeving → Servers** en klik op een server:

- **Gezondheidscheck** met één klik: gevonden problemen, gestopte services, recente fouten, drukste processen.
- **Services**: systemd-units of Windows-services, starten, stoppen, herstarten.
- **Logs**: het journal op Linux, het Event Log op Windows, en logbestanden.
- **Updates**: apt, dnf/yum of Windows Update, installeren en herstarten.
- **Geïnstalleerde software**.
- Een **shell** of **PowerShell** in de browser, commando's en bestanden.

## Over alle servers heen

- **Logs zoeken** (**Servers → Logs zoeken**): zoek op veel servers tegelijk, of vraag een overzicht van fouten.
- **Data kopiëren** (**Servers → Data kopiëren**): kopieer bestanden of mappen van de ene server naar de andere. Dat gaat
  van agent naar agent via Stackpilot, dus de servers hoeven elkaar niet te kunnen bereiken.
- **Software & CVE** (**Onderhoud → Updates & software**): welke servers draaien een pakket, in welke versie, en zijn ze
  kwetsbaar? Zie [Updates en software](../care/updates-software.md).

## Vraag het de AI

- *"Waarom is de webserver traag? Kijk in de logs en herstart nginx als dat nodig is."*
- *"Welke servers van Acme hebben sinds gisteren fouten in hun event log?"*
- *"Kopieer /srv/data van web01 naar web02."*

Commando's, herstarts en updates volgen het [beleid](../ai/policies-approvals.md): standaard vraagt een commando om
goedkeuring, en een reden is verplicht.
