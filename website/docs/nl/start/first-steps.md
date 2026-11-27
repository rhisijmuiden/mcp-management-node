# Eerste stappen

Je hebt Stackpilot net geïnstalleerd en de eerste beheerder aangemaakt. Loop deze lijst één keer door; dat kost ongeveer
een half uur. Het klantoverzicht heeft later een tegel **Wat is nog niet ingericht?** die je herinnert aan wat je hebt
overgeslagen.

- [ ] **Openbaar adres en taal.** Stel onder **Instellingen → Algemeen** het openbare adres in (bijvoorbeeld
      `https://stackpilot.example.com`) en de standaardtaal (Engels of Nederlands). Elke gebruiker kiest later zelf zijn
      taal in het accountmenu rechtsboven.
- [ ] **Mailserver.** Vul onder **Koppelingen → Mailserver** je SMTP-server in en verstuur een testmail. Zonder
      mailserver krijg je geen meldingen, goedkeuringen of rapporten. Zie [Mailserver](../admin/mail.md).
- [ ] **Back-up van Stackpilot zelf.** Voeg onder **Instellingen → App-back-ups** een back-uplocatie toe (FTP, FTPS,
      SFTP of SCP), met een **wachtwoordzin** en een schema. Zie [App-back-ups](../admin/backups.md).
- [ ] **Klanten.** Maak onder **Klant → Alle klanten** je klanten aan. *Home* bestaat al, voor je eigen omgeving. Vul
      contactpersonen, het onderhoudsvenster en de SLA in. Zie [Klanten](../customers/customers.md).
- [ ] **Machines.** Klik op een klantpagina op **Machine toevoegen** en installeer de agent op een Proxmox VE-node, een
      server of een werkplek. Zie [Agents en koppelen](../machines/agents.md).
- [ ] **Je AI.** Koppel Claude, ChatGPT of Copilot. Zie [Je AI koppelen](../ai/connect.md).
- [ ] **Beleid.** Kijk onder **Instellingen → Beleid & guardrails** wat de AI zelf mag doen. De standaard is veilig:
      destructieve acties en alles buiten een onderhoudsvenster worden eerst aan jou gevraagd. Zie
      [Beleid en goedkeuringen](../ai/policies-approvals.md).
- [ ] **Gebruikers.** Voeg collega's toe onder **Instellingen → Gebruikers** als *beheerder* of *kijker* (kijkt mee,
      kan niets wijzigen).

## Daarna, als je zover bent

- Koppel **Microsoft 365**, **UniFi** en **GitHub** onder **Koppelingen**: elke koppeling heeft een stap-voor-stap-uitleg.
  Zie [Koppelingen](../integrations/index.md).
- Bekijk de dag erna de eerste [ochtendsamenvatting](../automation/morning-summary.md): die laat per klant zien wat
  aandacht nodig heeft.
- Zet het [klantportaal](../customers/portal-tickets.md) aan onder **Instellingen → Klantportaal** als klanten zelf
  problemen moeten kunnen melden.
- Vul je prijzen in onder **Kosten & licenties** als je per klant een maandoverzicht wilt.
