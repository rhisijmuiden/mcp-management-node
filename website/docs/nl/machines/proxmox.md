# Proxmox VE

Op elke Proxmox VE-node draait de agent. Stackpilot beheert dan de node, zijn VM's en containers, het netwerk en het
cluster, zonder dat de Proxmox-API van buitenaf bereikbaar hoeft te zijn.

## Nodes

**Omgeving → Proxmox → Nodes** toont elke node met status, CPU, geheugen, schijf, guests en beschikbare updates. Per
node:

- **Pakketupdates**: vernieuwen, installeren en herstarten, met een live log.
- **CPU-energiestand** (governor), bijvoorbeeld `powersave` of `performance`.
- **Meldingsmail**: de SMTP-instellingen van de klant worden naar elke node gestuurd, met een testmelding.
- **Logs, services, pakketten, bestanden, commando's** en een **shell** in de browser.
- De versie van de agent, met zelf-update.

## VM's en containers

**Proxmox → VM's & containers** toont elke guest van elke node.

- **Aan/uit**: starten, afsluiten, stoppen, herstarten.
- **Snapshots**: maken, terugdraaien, verwijderen.
- **Migreren** binnen het cluster, CPU, geheugen en tags wijzigen, verwijderen.
- **Console** in de browser: VM's via noVNC (met Ctrl+Alt+Del), containers via een terminal. Dat loopt via de agent, dus
  de node heeft geen open poorten nodig.

### Een guest aanmaken

Klik op **Aanmaken** en kies:

1. Een **container** uit de templatebibliotheek.
2. Een **community script** van community-scripts.org (bijvoorbeeld een app zoals Home Assistant). Stackpilot toont
   eerst het script; het draait als root op de gekozen node.
3. Een **VM vanaf een ISO** (bestaand, of voor je gedownload).
4. Een **VM vanaf een cloud image** met cloud-init.

Standaard wordt de minst belaste node gekozen.

## Templates

**Proxmox → CT-templates** is één lijst van officiële en eigen containertemplates, met per node of elke template
actueel, verouderd of afwezig is. **Naar alle nodes** (en een nachtelijke taak) zet ze op elke node.

## Netwerk, firewall en SDN

- **Proxmox → Netwerk** toont elke bridge met de guests erop, gegroepeerd per VLAN, plus SDN-netwerken en bridges waar
  guests naar verwijzen maar die niet bestaan.
- **Wijzigingen worden eerst klaargezet** en als verschil (diff) getoond. Toepassen vraagt om de nodenaam. De node
  bewaart een back-up en **zet de oude configuratie zelf terug** als Stackpilot hem niet binnen 90 seconden weer bereikt.
- **Firewall**: regels voor cluster, node en guest.
- **SDN-ontwerper**: bouw een zone in vijf stappen met een live voorbeeld. Uitrollen bewaart een back-up en zet die
  terug als het herladen mislukt. Elke back-up kun je terugzetten onder **Netwerk → Configuratieback-ups**.

## Cluster en HA

**Proxmox → Cluster & HA**: HA-resources en -regels, replicatie van opslag, de eigen back-uptaken van het cluster en
opslag (directory, NFS, SMB, Proxmox Backup Server).

## Onderhoud van een heel cluster

Nodes één voor één patchen, met guests weg en weer terug verplaatst, is een geplande taak: zie
[Proxmox-onderhoud](../automation/maintenance.md#proxmox-onderhoud).
