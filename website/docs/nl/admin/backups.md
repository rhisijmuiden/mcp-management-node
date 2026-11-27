# App-back-ups en herstel

Dit zijn back-ups **van Stackpilot zelf**: de database en gegevensbestanden, inclusief de sleutels van de
wachtwoordkluis en de certificaatautoriteit van de agents. (Back-ups van de machines van je klanten staan onder
[Klantback-ups](../care/backups.md).)

## Volledig en incrementeel

- Een **volledige** back-up bewaart alles.
- Een **incrementele** back-up bewaart alleen wat er sinds de vorige back-up veranderd is.
- **Terugzetten** neemt de volledige back-up plus de incrementele tot het moment dat jij kiest.

Elke back-up heeft een SHA-256-manifest en kan versleuteld worden met **AES-256-GCM**, met jouw wachtwoordzin.

## Een back-uplocatie instellen, stap voor stap

1. Open **Instellingen → App-back-ups** en klik op **Backuplocatie toevoegen**.
2. Kies het protocol: **FTP**, **FTPS** (expliciet of impliciet), **SFTP** of **SCP**, en vul server, poort, map en
   inloggegevens in.
3. Stel een **wachtwoordzin** in. Bewaar die op een veilige plek buiten Stackpilot: zonder kan een back-up niet worden
   teruggezet.
4. Stel het schema in, bijvoorbeeld **elke zondag een volledige back-up** en **elke nacht een incrementele**, en hoeveel
   volledige reeksen je bewaart.
5. Klik op **Testen** en daarna op **Nu back-uppen** om de eerste te zien slagen.

!!! warning "Zonder back-up geen kluis"
    De sleutel van de wachtwoordkluis staat op de server. Raak je de server kwijt en is er geen app-back-up met
    wachtwoordzin, dan kan de kluis niet meer open.

## Terugzetten

1. Open **Instellingen → App-back-ups** en klik bij de locatie op **Terugzetten**.
2. Stackpilot toont de back-ups die daar staan.
3. Kies het moment waarnaar je terug wilt. Stackpilot neemt de volledige back-up en de incrementele tot dat moment.
4. Bevestig. Dit vervangt alle huidige gegevens; Stackpilot zet terug en herstart.

Op een nieuwe server: installeer Stackpilot, voeg dezelfde locatie toe en zet vanaf daar terug. Vanaf de opdrachtregel:
`sudo mmn restore`.
