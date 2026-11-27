# Klantback-ups

**Onderhoud → Back-ups** beantwoordt voor elke klant de vraag *"is alles geback-upt?"*: elke VM, container, server en
werkplek met zijn laatste goede back-up, de leeftijd en de status tegenover de SLA van de klant.

!!! note "Twee soorten back-ups"
    Deze pagina gaat over de back-ups **van de machines van je klanten**. Back-ups **van Stackpilot zelf** staan onder
    [App-back-ups](../admin/backups.md).

## Waar Stackpilot naar kijkt

- **Proxmox-guests**: per VM en container de back-uptaken die hem dekken, de laatste geslaagde back-up (leeftijd en
  grootte), het aantal back-ups, of de nieuwste geverifieerd en extern gekopieerd is (Proxmox Backup Server) en de
  laatste fout.
- **Windows Server Backup**: beleid, systeemstatus en het laatste resultaat, via de agent.
- **Veeam Backup & Replication** (v12 en v13): op de Veeam-server leest de agent de taken, de laatste sessies en de
  herstelpunten per VM, alleen-lezen. Hyper-V- en VMware-VM's worden op naam gekoppeld aan de machines van de klant,
  zodat ze hun laatste herstelpunt tonen; VM's zonder agent verschijnen als eigen items. Er wordt niets in Veeam
  gewijzigd. Hiervoor is PowerShell 7 (`pwsh`) op de Veeam-server nodig.
- **Linux**: back-uptools, timers en cronjobs.
- Een machine die een Proxmox-guest is, valt onder de back-up van de guest.

Elk item heeft een status: **OK**, **te oud**, **nooit geback-upt**, **geen taak**, **mislukt** of **uitgesloten**. Het
overzicht wordt elke 30 minuten vernieuwd, of nu met **Nu controleren**.

## Het beleid per klant

Per klant stel je in:

- de **maximale leeftijd** in uren (de SLA, standaard 26);
- meldingen bij mislukte en ontbrekende back-ups;
- of werkplekken meetellen;
- extra mailontvangers, en items om uit te sluiten.

Mislukte of te oude back-ups geven een melding en worden gemaild. Ze staan ook in de ochtendsamenvatting, het
maandrapport en de optimalisatiescan.

## Nu back-uppen en terugzetten

- **Nu back-uppen** op een guest.
- **Een hele guest terugzetten**: standaard als **nieuwe** VM of container naast het origineel. De bestaande
  overschrijven vraagt om het id in te typen.
- **Bestanden of mappen terugzetten** uit een back-up op een Proxmox Backup Server, naar een map op de node.

Terugzetten is destructief en vraagt standaard om goedkeuring, ook als de AI het vraagt.

## Proxmox Backup Server

Een Linux-server met de agent waarop Proxmox Backup Server draait, toont zijn **datastores** (gebruik, garbage
collection), **verify**-, **sync**- en **prune**-taken en **remotes**. Voeg datastores en remotes toe en maak, wijzig,
start of verwijder taken vanuit Stackpilot. Koppel een cluster aan een datastore onder Proxmox → Cluster & HA → Opslag.
