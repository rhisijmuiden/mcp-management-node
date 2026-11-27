# Gebruikers, beveiliging en webserver

## Gebruikers

**Instellingen → Gebruikers** kent twee rollen:

- **Beheerder**: mag alles, keurt AI-verzoeken goed en deelt de krachtige MCP-scopes uit.
- **Kijker**: kijkt mee, kan niets wijzigen.

Klik op **Gebruiker toevoegen** om er een toe te voegen. Wachtwoorden kunnen per mail gereset worden.

## Auditlog

**Instellingen → Auditlog** legt elke aanmelding, wijziging en actie vast, in de app en via de MCP-server, met de
gebruiker of het token, het doel, het IP-adres en de tijd. Samen met **Sessies & audit** zie je altijd wie, of welke AI,
wat gedaan heeft.

## Webserver en proxy

**Instellingen → Webserver & proxy** kiest tussen de eigen HTTPS van Stackpilot en draaien achter een reverse proxy zoals
Zoraxy: de vertrouwde proxyadressen, of alleen de proxy mag verbinden, een eventueel subpad, en een live weergave van wat
de app van de proxy ontvangt.

## Algemeen

**Instellingen → Algemeen**: de naam van de app, het openbare adres en de standaardtaal.

## Hoe Stackpilot je gegevens beschermt

- Wachtwoorden worden gehasht met **Argon2id**; aanmeldpogingen worden begrensd; sessies gebruiken veilige cookies met
  CSRF-bescherming.
- Elk opgeslagen geheim (inloggegevens voor SMTP, back-ups, UniFi, GitHub en Microsoft 365, de DKIM-sleutel) is
  **versleuteld**. De wachtwoordkluis heeft een eigen sleutel, los van de database.
- Agents gebruiken **mutual TLS** met certificaten van de eigen CA van Stackpilot en een vastgezette vingerafdruk. Bij de
  machines gaan geen poorten open. Een machine verwijderen blokkeert zijn certificaat meteen.
- Elke AI-aanroep gaat langs scopes, klantgrenzen, beleid, goedkeuringen en de noodstop, en wordt vastgelegd.
- Tools van andere MCP-servers worden pas doorgegeven nadat je ze geaccepteerd hebt, en weer ingetrokken als ze
  veranderen.
