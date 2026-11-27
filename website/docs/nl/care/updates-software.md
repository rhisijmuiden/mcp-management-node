# Updates en software

## Pakketupdates

**Onderhoud → Updates & software → Updates** toont de openstaande updates van elke Proxmox-node. Installeer ze met een
live log en herstart waar nodig. Servers en werkplekken krijgen hun updates (apt, dnf/yum of Windows Update) op hun
eigen pagina.

Wil je volgens een schema patchen, gebruik dan een [geplande taak](../automation/maintenance.md); voor een heel
Proxmox-cluster [Proxmox-onderhoud](../automation/maintenance.md#proxmox-onderhoud). De
[ochtendsamenvatting](../automation/morning-summary.md) stelt elke dag updates voor, die je met één klik goedkeurt.

## Software & CVE

**Updates & software → Software & CVE** beantwoordt: *welke machines draaien deze software, in welke versie, en zijn
ze kwetsbaar?*

1. Vul een naam of jokerteken in: `nginx*`, `openssl`, `*7-zip*`, of op Windows een hotfix zoals `KB5*`.
2. Vul eventueel de **eerste versie met de oplossing** uit het beveiligingsadvies in.
3. Stackpilot toont elke machine met het pakket en markeert oudere versies als **kwetsbaar**, met of een openstaande
   update het oplost.

Vraag de AI: *"Welke servers zijn nog kwetsbaar voor CVE-2026-1234 in openssl?"*
