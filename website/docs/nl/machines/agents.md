# Agents en koppelen

## Wat is de agent?

De agent (`mmn-agent`) is een klein programma dat draait op elke machine die Stackpilot beheert: Proxmox VE-nodes,
Linux-servers, Windows-servers en werkplekken. Hij heeft geen afhankelijkheden en gebruikt weinig geheugen.

- Hij maakt zelf verbinding **naar buiten**, naar Stackpilot op poort **8443**, over een versleutelde verbinding
  (WebSocket met mutual TLS). Bij de klant hoeft niets open: geen port forward, geen VPN.
- Hij voert uit wat Stackpilot vraagt: status en logs lezen, updates installeren, services herstarten, een commando
  draaien. De console in de browser, het kopiëren van bestanden en UniFi-verzoeken gaan ook via deze verbinding.
- Hij **werkt zichzelf bij** naar de versie die bij Stackpilot hoort (uit te zetten onder **Koppelingen → Agents en
  koppelen**).

## Een machine koppelen, stap voor stap

Koppelen betekent: de agent stelt zich één keer voor met een **eenmalige code** en krijgt daarvoor een eigen
certificaat terug.

1. Kies de klant in de bovenbalk en open de klantpagina.
2. Klik op **Machine toevoegen** en kies wat het is: **Proxmox VE-node**, **server** of **werkplek**. (Je kunt ook
   beginnen bij **Proxmox → Nodes → Node toevoegen**, **Servers → Server toevoegen** of **Koppelingen → Agents en
   koppelen**.)
3. Stackpilot maakt een **koppelcode** (24 uur geldig) en toont één installatiecommando voor het juiste
   besturingssysteem.
4. Voer dat commando uit op de machine:

    === "Proxmox VE of Linux (als root)"

        ```bash
        curl -fsSL https://<jouw adres>/api/agent/install.sh | bash -s -- --code XXXX-XXXX-XXXX
        ```

        Werkt op Debian, Ubuntu, RHEL/Rocky/Alma, Fedora, SUSE en Alpine. Op een Proxmox-node kun je de shell in de
        Proxmox-webinterface gebruiken.

    === "Windows (PowerShell als Administrator)"

        Kopieer het `powershell … install.ps1 … -Code XXXX-XXXX-XXXX`-commando uit het venster. Windows Server 2016+
        en Windows 10+ worden ondersteund. De agent draait als Windows-service `mmn-agent`.

    === "Windows, stil (GPO, Intune, RMM)"

        ```powershell
        msiexec /i mmn-agent.msi /qn SERVER=… FINGERPRINT=… CODE=…
        ```

        Het venster toont de precieze waarden.

5. Binnen een paar seconden verschijnt de machine bij de klant, met zijn status. Klaar.

Het commando controleert de download (SHA-256) en de identiteit van je Stackpilot-server (een vastgezette
vingerafdruk), dus een agent praat nooit met een verkeerde server.

## Vóór de eerste machine: het agentadres

Controleer onder **Koppelingen → Agents en koppelen** het **adres voor agents**: de hostnaam waarmee agents poort 8443
bereiken, bijvoorbeeld `stackpilot.example.com`. Achter Zoraxy is daarvoor een **Stream Proxy (TCP)** op 8443 nodig, zie
[Installatie](../start/install.md#stap-3-achter-een-reverse-proxy-optioneel-aangeraden).

## Als een agent offline is

- De machine staat op *offline* en na een paar minuten komt er een melding (ook per mail).
- Controleer of de machine aan staat en internet heeft, en of poort 8443 van Stackpilot vanaf daar bereikbaar is.
- Op Linux: `systemctl status mmn-agent` en `journalctl -u mmn-agent`. Op Windows: de service `mmn-agent` en het log in
  `C:\ProgramData\mmn-agent\agent.log`.
- Voor een VM of container op een Proxmox-node die nog wel online is, kan [automatisch herstel](../automation/recovery.md)
  de guest starten of de agent erin herstarten.
- Kom je er niet uit? **Opnieuw koppelen** op de pagina van de machine maakt een nieuwe code; voer het
  installatiecommando nog eens uit.

!!! info "Een machine verwijderen"
    Verwijder je een machine in Stackpilot, dan wordt zijn certificaat meteen geblokkeerd; de agent kan dan niet meer
    verbinden.
