# UniFi

Stackpilot koppelt de UniFi-netwerken van je klanten via de **officiële UniFi-API's**, met API-sleutels die versleuteld
worden opgeslagen en nooit meer getoond.

## Soorten koppelingen

| Soort | Wat het geeft | Waar je de sleutel maakt |
| --- | --- | --- |
| **UniFi Network** | Apparaten, clients, wifi, firewall, gebeurtenissen van één console | Network → Settings → Control Plane → Integrations |
| **Site Manager** | Alle consoles en sites van een account (of van één Fabric) | unifi.ui.com → API |
| **UniFi Access** | Deuren, vergrendelregels, het toegangslog | Access → Settings → General → Advanced → API Token |

## Een koppeling toevoegen, stap voor stap

1. Open **Koppelingen → UniFi-consoles** (of het tabblad **Netwerk** van de klant) en klik op **Koppeling toevoegen**.
2. Kies de klant en de soort, en plak de API-sleutel.
3. Kies hoe Stackpilot de console bereikt:
    - **rechtstreeks** (een adres dat Stackpilot kan bereiken);
    - **via een agent** in het netwerk van de klant: niets hoeft open;
    - voor Network, **via de cloudconnector van Site Manager**: helemaal geen adres nodig.
4. Bij een zelfondertekend certificaat toont Stackpilot de vingerafdruk en zet die vast voordat de sleutel verstuurd wordt.
5. Markeer de koppeling eventueel als **alleen-lezen**: alle acties staan dan uit.

Elke 5 minuten leest Stackpilot elke koppeling: apparaten met status, uplink, firmware en clients, wifi, WAN-status en
deuren. Veranderingen worden **gebeurtenissen** (apparaat offline, WAN-wissel, nieuwe firmware) en offline apparaten
geven meldingen.

## UniFi Fabrics

Een Site Manager-sleutel die **binnen een UniFi Fabric** is gemaakt, ziet alleen de consoles en sites van die Fabric.

1. Open **Netwerk (UniFi) → Fabrics** en klik op **Fabric-sleutel toevoegen** (of markeer een bestaande Site
   Manager-koppeling als Fabric-sleutel).
2. Stackpilot leest elke console van de Fabric en koppelt elk als Network-koppeling via de cloudconnector, bij dezelfde
   klant: geen adres, poort of agent nodig.
3. **Netwerk (UniFi) → Fabrics** toont per Fabric elke console met zijn apps, sites (provider, WAN-uptime), apparaten en
   de koppeling die hem bereikt.

Consoles die later bij de Fabric komen, worden vanzelf gekoppeld (of, met automatisch koppelen uit, geven een melding en
koppel je met één klik); consoles die vertrekken geven een waarschuwing. Een console die twee sleutels zien, telt één
keer.

??? info "Foutmeldingen die je kunt zien"
    - **HTTP 403**: de sleutel is niet door de eigenaar van de console gemaakt.
    - **Console onbereikbaar**: de cloudconnector bereikt hem niet (offline, niet geadopteerd, of UniFi OS ouder dan
      5.0.9).
    - Zelf gehoste Network Servers en consoles zonder UniFi Network worden getoond maar niet gekoppeld.
    - Configuratie uitrollen via Fabrics (Blueprints) heeft nog geen openbare API en zit er niet in.

## Wat je kunt doen

- **Netwerk (UniFi) → Network**: live apparaatdetails, clients, wifi aan/uit, gasten toelaten, een apparaat herstarten,
  een PoE-poort uit- en aanzetten, een **wifi-probleemoplosser** (*waarom komt deze laptop er niet op?*) en een
  **controle op gastisolatie**.
- **Netwerk (UniFi) → Access**: deuren en hun vergrendeling, op afstand openen, tijdelijke vergrendelregels,
  noodvergrendeling/evacuatie, schema's en het toegangslog (*wie heeft vannacht de serverruimte geopend?*).

Deuracties vragen de scope `doors` en zijn destructief: ze vragen standaard om goedkeuring.
