# Automatisch herstel

Bekende problemen die Stackpilot **zelf** oplost, binnen je beleid: een gestopte service wordt herstart, een volle schijf
opgeruimd, een offline agent in een VM of container teruggehaald, een netwerkkaart zonder link herstart.

## Hoe het werkt

Een **regel** koppelt een probleem aan een herstel-[runbook](runbooks.md):

- **Het probleem** is een open melding (agent offline, service gestopt, schijf of opslag vol, geheugen, CPU, SMART,
  UniFi, back-ups, uptime-controle down) of een bevinding van de nachtelijke checks (een volle schijf, een oude agent,
  openstaande updates, een certificaat dat verloopt).
- **Welke machines**: alle van een klant, of beperkt op naam (`web*`) en soort.
- **Het herstel**: het runbook, met de parameters ingevuld vanuit het probleem (`{{machine.name}}`,
  `{{alert.detail.service}}`, …).
- **Modus**: *automatisch* (binnen het beleid), *met goedkeuring* (een beheerder keurt elke stap goed die iets wijzigt)
  of *alleen melden* (wijzigt niets, zegt alleen wat het zou doen).

Na het herstel moet het probleem binnen een paar minuten echt **weg** zijn; anders telt de poging als mislukt.

## Begin met de aanbevolen regels

Open **Automatisering → Automatisch herstel** en klik op **Aanbevolen regels**. Elke regel komt met zijn runbook:

- een gestopte service herstarten (je noemt de services die bewaakt worden);
- schijfruimte vrijmaken;
- een offline agent terughalen (de VM of container starten, of de agent erin herstarten);
- een netwerkadapter zonder link herstarten;
- verouderde agents bijwerken.

!!! tip "Probeer eerst *alleen melden*"
    Zet een nieuwe regel een week op *alleen melden*. Het tabblad Activiteit laat dan zien wat hij gedaan zou hebben.

## Netwerkadapters zonder link

Een netwerkregel bewaakt de bekabelde adapters van zijn machines (alle, of degene die je noemt: `Ethernet 2`, `eno1`,
`*X710*`). Een adapter die up was en zijn link kwijt is, wordt herstart, en Stackpilot controleert of de link terug is.

Met de **lokale terugval** doet de agent dit **zelf** als hij Stackpilot niet kan bereiken (vaak juist door die link), en
meldt het zodra hij terug is. Hiervoor is agent 0.15.0 of nieuwer nodig.

## Guardrails

- Elke stap gaat langs de AI-gateway: destructief vraagt goedkeuring, buiten het onderhoudsvenster vraagt goedkeuring.
- Destructieve stappen vragen altijd een beheerder. Een Proxmox VE-node wordt **nooit** herstart. Automatisch herstel
  verwijdert, herstelt uit back-up, draait terug of wijzigt de netwerkconfiguratie nooit.
- **Lusbescherming**: een wachttijd voor het ingrijpen, een afkoeltijd tussen pogingen en een maximum per 24 uur (daarna
  stopt het en krijg je mail); voor de hele app een maximum per uur en een stroomonderbreker na te veel mislukkingen.
- **Noodknop**: één knop stopt al het automatisch herstel en annuleert wat bezig is.

Elke poging staat op het tabblad **Activiteit** en in het auditlog. Je krijgt mail bij elke uitkomst, alleen bij
mislukkingen, of nooit; de ochtendsamenvatting en het maandrapport laten zien wat er opgelost is.
