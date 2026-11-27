# Nachtelijke checks en ochtendsamenvatting

Elke nacht controleert Stackpilot elke klant. 's Ochtends krijg je **één samenvatting**, in de app en per mail, met wat
nieuw is, wat opgelost is en voorstellen die je met één klik goedkeurt.

## Wat er gecontroleerd wordt

| Categorie | Wat |
| --- | --- |
| **Updates** | Openstaande updates en herstarts, oude agents, UniFi-firmware |
| **Beveiliging en CVE's** | Openstaande beveiligingsupdates en de kwetsbaarheden (CVE's) die ze oplossen |
| **Schijven en opslag** | Volle schijven, volle Proxmox-opslag, schijfgezondheid (SMART) |
| **Certificaten en firewall** | Certificaten die verlopen, onbeoordeelde firewallwijzigingen, VPN-tunnels die down zijn |
| **Back-ups** | Mislukte, ontbrekende en te oude back-ups |
| **Microsoft 365** | Riskante gebruikers, beheerders zonder MFA, ongebruikte licenties en meer |
| **Contracten en licenties** | Wat binnenkort afloopt of opgezegd moet worden |
| **Overig** | De rest van de optimalisatiescan: offline machines, gestopte services, netwerk |

Voor Debian en Ubuntu zoekt Stackpilot de CVE's op bij [OSV.dev](https://osv.dev). Alleen pakketnamen en versies worden
verstuurd; je kunt dit uitzetten.

## De samenvatting lezen

Open **Vandaag → Ochtendsamenvatting**. Per klant zie je de bevindingen, op volgorde van belang:

- **Nieuwe** bevindingen sinds vannacht zijn gemarkeerd; **opgeloste** staan er ook bij.
- Een bevinding die in één stap op te lossen is, heeft een **voorstel**: de updates installeren, herstarten (nooit een
  Proxmox-node), de agent bijwerken, schijfruimte vrijmaken, een certificaat vernieuwen, of een riskante Microsoft
  365-gebruiker blokkeren.

## Voorstellen goedkeuren

1. Klik op **Goedkeuren** bij een voorstel, of op **Alles goedkeuren** voor een klant of voor iedereen.
2. Wijzigingen **wachten dan op het onderhoudsvenster van de klant** (te zien als *Gepland*) en starten zodra het opent.
   Klanten zonder venster gaan meteen.
3. Het werk draait met de rechten van de beheerder die goedkeurde en komt in het auditlog.

Voorstellen die je niet goedkeurt, verlopen voor de volgende nacht; dan komen er nieuwe.

## Instellingen

Onder **Instellingen** op dezelfde pagina:

- de tijden van de checks (standaard 02:30) en de samenvatting (standaard 07:00);
- wat er gecontroleerd wordt, het opzoeken van CVE's, voorstellen, wachten op het onderhoudsvenster;
- de melding in de app en de mail: ontvangers, en of er ook gemaild wordt als alles in orde is;
- **Per klant**: checks uitzetten voor een klant en de webadressen opgeven waarvan de certificaten gecontroleerd worden.

**Nu controleren** draait de checks meteen, **Samenvatting versturen** mailt hem opnieuw, **Geschiedenis** bewaart 90
nachten.

## Met de AI

Vraag *"Wat is er vannacht gebeurd?"* of *"Wat heeft vandaag mijn aandacht nodig?"*. De AI leest de samenvatting en kan
een run starten.
