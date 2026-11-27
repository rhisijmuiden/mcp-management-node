# Mailserver

Stackpilot verstuurt alle mail via **je eigen SMTP-server**: meldingen, goedkeuringen, de ochtendsamenvatting, rapporten,
ticketmails, berichten over wijzigingen, uitnodigingen en wachtwoordresets.

## Instellen

1. Open **Koppelingen → Mailserver**.
2. Vul in:
    - **Host** en **poort** (meestal 587 voor STARTTLS, 465 voor TLS);
    - **Beveiliging**: geen, STARTTLS of TLS, en de **minimale TLS-versie** (1.2 of 1.3);
    - **Inloggen**: gebruikersnaam en wachtwoord (versleuteld opgeslagen);
    - **Afzender**- en **antwoord**-adres.
3. Zet eventueel **DKIM-ondertekening** aan: vul het domein, de selector en je privésleutel in, en publiceer de bijbehorende
   publieke sleutel als DNS-TXT-record op `<selector>._domainkey.<jouw domein>`.
4. Klik op **Testmail versturen**. Die gebruikt de waarden in het formulier, ook voordat je ze opslaat.

!!! tip "Goed aankomen"
    Gebruik een afzenderadres op een domein met SPF, DKIM en DMARC, en verstuur via een server die voor dat domein mag
    mailen (bijvoorbeeld Microsoft 365, je hostingpartij of een relay zoals Postmark of Mailgun).

## Wie krijgt wat

- **Instellingen → Meldingen**: wie monitoringmeldingen en andere meldingen krijgt.
- **Instellingen → Beleid & guardrails → Goedkeuringen & meldingen**: wie goedkeuringsverzoeken krijgt.
- De **ochtendsamenvatting**, **rapporten**, **tickets** en **wijzigingsberichten** hebben elk hun eigen ontvangers in
  hun eigen instellingen.

Proxmox-nodes krijgen hun eigen meldingsmail-instellingen van de klant (Proxmox → Nodes), doorgezet door de agent.
