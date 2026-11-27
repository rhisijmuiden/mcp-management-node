# Documentatie en wachtwoordkluis

## Documentatie

**Kennis & administratie → Documentatie** bewaart de kennis over elke klant op één plek, voor je team en voor de AI.

1. Klik op **Nieuwe pagina**, kies de klant en een categorie (netwerk, procedure, notities, contacten, handleiding,
   hardware, software, beveiliging, overig). Begin eventueel met een sjabloon: netwerkplan, procedure, contacten,
   hardwarelijst.
2. Schrijf in Markdown in de editor; het voorbeeld ernaast toont het resultaat.
3. Koppel de machines waar de pagina over gaat, voeg tags toe en zet belangrijke pagina's bovenaan vast.
4. Voeg bijlagen toe (tekeningen, pdf's, exports tot 25 MB). Die worden versleuteld opgeslagen.

Elke keer opslaan bewaart de vorige versie (de laatste 100), zodat je altijd kunt terugkijken en een versie kunt
terugzetten. Slaan twee mensen tegelijk dezelfde pagina op, dan krijgt de tweede een waarschuwing in plaats van dat hij
het werk van de ander overschrijft.

**Voor de AI:** voordat hij bij een klant aan de slag gaat, ziet de AI de lijst met pagina's en kan hij ze lezen. Zet
*De AI mag deze pagina lezen* uit voor pagina's die de AI nooit mag zien. Met de scope `docs` kan de AI ook pagina's
schrijven, bijvoorbeeld een regel met datum toevoegen aan een logboekpagina.

## Wachtwoordkluis

**Kennis & administratie → Wachtwoordkluis** bewaart de inloggegevens van elke klant: logins, servers,
netwerkapparatuur, databases, API-sleutels, wifi, certificaten, licenties en beveiligde notities.

### Een item toevoegen

1. Klik op **Nieuwe inlog**, kies de klant en de soort.
2. Vul gebruikersnaam, wachtwoord (of gebruik de **generator**), URL en notities in. Extra velden kunnen geheim zijn of
   niet.
3. Koppel de machines en, als je een herinnering wilt, een wisselperiode. De lijst laat zien welke items aan de beurt
   zijn.

Bij het wijzigen van een wachtwoord blijven de vorige bewaard (standaard 10).

### Wie ziet wat

- **Mensen**: iedereen ziet namen en gebruikersnamen. Beheerders zien en kopiëren geheimen nadat ze hun wachtwoord
  opnieuw hebben ingetypt (15 minuten ontgrendeld).
- **De AI**: ziet de kluis *zonder* geheimen. Een geheim gebruiken vraagt altijd goedkeuring van een beheerder, wat het
  beleid ook zegt, en een item kan voor de AI op *Nooit* staan.
    - De voorkeur heeft **uitvoeren met inloggegevens**: na goedkeuring draait een script op de machine met de
      gebruikersnaam en het wachtwoord in zijn omgeving. Het geheim gaat nooit langs de AI en wordt uit de uitvoer
      gewist.
    - **Tonen** vraagt eerst goedkeuring; daarna mag de AI het geheim één keer lezen, binnen 15 minuten.

Elke keer tonen, kopiëren, gebruiken en wijzigen staat in het **toegangslog**, ook mislukte pogingen en wie wat
goedkeurde. Openstaande AI-toestemmingen kun je intrekken.

### Hoe het beveiligd is

Elk item heeft een eigen willekeurige sleutel en wordt versleuteld met AES-256-GCM. Die sleutels worden beschermd door
de kluissleutel, een bestand buiten de database (`/var/lib/mcp-management-node/vault.key`), dus een databasedump alleen
verraadt niets. De kluissleutel gaat mee in de [app-back-ups](../admin/backups.md).

!!! warning "Stel een wachtwoordzin voor back-ups in"
    Zonder app-back-up met wachtwoordzin ben je bij verlies van de server ook de kluis kwijt. De kluis waarschuwt
    als een back-uplocatie geen wachtwoordzin heeft.
