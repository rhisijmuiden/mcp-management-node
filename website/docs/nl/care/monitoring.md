# Monitoring, uptime en SLA

## Monitoring

**Onderhoud → Monitoring & uptime → Monitoring** bewaart 30 dagen lang één meting per machine per minuut: CPU, geheugen
en schijf, met trends van 1 uur tot 30 dagen, het gebruik van opslag en de gezondheid en slijtage van fysieke schijven.

**Meldingen** gaan open bij offline machines, hoog CPU-, geheugen-, schijf- of opslaggebruik, falende schijven,
UniFi-problemen, back-ups, certificaten, Microsoft 365 en meer. Ze worden gemaild als ze opengaan en als ze sluiten. Wie
welke mail krijgt, stel je in onder **Instellingen → Meldingen**.

## Uptime en SLA

**Monitoring & uptime → Uptime & SLA** controleert de diensten van je klanten van buitenaf, en telt ze op tot de SLA die
je echt geleverd hebt.

### Soorten controles

| Controle | Wat er getest wordt |
| --- | --- |
| **Website** | HTTP(S)-status, een woord dat (niet) op de pagina moet staan, reactietijd, het certificaat en de vervaldatum |
| **TCP-poort** | Een poort antwoordt, bijvoorbeeld een mailserver, RDP-gateway of database |
| **Ping** | De host antwoordt |
| **DNS** | Een record (A, AAAA, CNAME, MX, TXT, …) bestaat, eventueel met de verwachte waarde |
| **VPN** | Het portaal van een SSL-VPN antwoordt, of een host achter een tunnel antwoordt op ping |

### Een controle toevoegen

1. Klik op **Controle toevoegen**, kies de klant en de soort.
2. Vul het adres in en wat als goed telt.
3. Kies **vanaf waar**: vanaf Stackpilot (de blik van buiten) of vanaf de agent van een machine bij de klant (voor
   interne adressen en tunnels).
4. Stel het interval in (30 seconden tot een uur), *down na* N mislukkingen op rij, en eventueel *traag boven*.
5. **Test** voert hem één keer uit zonder op te slaan. Sla daarna op.

Een controle die down of traag is of een verlopend certificaat heeft, wordt een melding en wordt gemaild. Het menu laat
zien hoeveel controles down zijn. Koppel de controle aan de machine waar de dienst op draait, en
[automatisch herstel](../automation/recovery.md) kan de service herstarten.

### De SLA

Het tabblad **SLA** toont per maand en per klant de gemeten beschikbaarheid, het doel, of het gehaald is, en de
uitvaltijd tegenover de toegestane uitvaltijd. Het doel komt uit de SLA-instelling van de klant. Uitval **binnen het
onderhoudsvenster telt niet mee** (tenzij je dat uitzet). Het maandrapport heeft er een hoofdstuk over.
