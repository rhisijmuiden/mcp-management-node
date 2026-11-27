# Beleid en goedkeuringen

Stackpilot zit tussen je AI en je machines. Elke aanroep gaat langs dezelfde controles, in deze volgorde:

1. **Scope**: heeft de koppeling het recht voor deze tool?
2. **Klanten**: valt het doel binnen de klanten van deze koppeling?
3. **Noodstop**: zijn AI-acties gepauzeerd (voor iedereen, deze klant of dit token)?
4. **Beleid**: voor het risiconiveau van deze tool, bij deze klant, voor dit token: uitvoeren, vragen of blokkeren?
5. **Onderhoudsvenster**: valt een wijziging buiten het venster van de klant?

Daarna wordt de aanroep uitgevoerd, wacht hij op jouw goedkeuring, of wordt hij geweigerd, en hij komt in het
activiteitenlog.

## Risiconiveaus

Elke tool heeft een risiconiveau:

| Risico | Betekenis | Standaard |
| --- | --- | --- |
| **Lezen** | Alleen kijken: status, logs, lijsten | Uitvoeren |
| **Bedienen** | Routine en makkelijk terug te draaien: een service herstarten, een snapshot maken | Uitvoeren |
| **Wijzigen** | Wijzigt instellingen of maakt dingen aan | Uitvoeren |
| **Destructief** | Commando's, updates, herstarts, verwijderen, terugzetten, deuren | Vraag mij |

## Het beleid instellen

![Beleid](../assets/images/policies.png)

Open **Instellingen → Beleid & guardrails**. Kies per risiconiveau, en per losse tool, **Uitvoeren**, **Vraag mij** of
**Blokkeren**:

- **Standaardbeleid**: voor elke klant en elk token.
- **Per klant**: ruimer of strenger. Een aanroep die meerdere klanten raakt, krijgt de strengste uitkomst.
- **Per token**: kan het alleen strenger maken.
- **Buiten het onderhoudsvenster**: wijzigende en destructieve aanroepen vragen standaard om goedkeuring (of worden
  genegeerd of geblokkeerd, jij kiest).

Een aanroep met `dry_run` telt als *lezen*, zodat de AI je altijd eerst kan laten zien wat hij zou wijzigen.

Sommige dingen vragen **altijd** een mens, wat het beleid ook zegt: een geheim uit de wachtwoordkluis gebruiken en
acties op Microsoft 365-gebruikers.

## Goedkeuringen

![Goedkeuringsverzoek](../assets/images/approval.png)

Als een aanroep goedkeuring nodig heeft:

1. Wordt hij **niet uitgevoerd**. De AI krijgt als antwoord *wacht op goedkeuring*, met een id.
2. Krijg je een **mail** en een melding in de app (het getal in de bovenbalk).
3. Open **Vandaag → Goedkeuringen**. Het verzoek toont de tool, wie het vroeg (welke AI-app of welk token, en welke
   gebruiker), de klant, het doel, waarom goedkeuring nodig is en de precieze argumenten.
4. Voeg eventueel een notitie toe (de AI ziet die) en klik op **Goedkeuren en uitvoeren** of **Afwijzen**.
5. Na goedkeuring voert Stackpilot de aanroep zelf uit, met de rechten van die koppeling. De AI leest de uitkomst.

Verzoeken waar niet op tijd over beslist is, **verlopen** en er gebeurt niets. Hoe lang de AI wacht, wanneer verzoeken
verlopen en wie mail krijgt, stel je in onder **Instellingen → Beleid & guardrails → Goedkeuringen & meldingen**.

## De noodstop

**Alle AI-acties pauzeren** (in het commandocentrum) stopt in één keer elke AI-aanroep die iets wijzigt, tot je hervat.
Je kunt ook één klant of één token pauzeren.

## Zien wat er gebeurd is

- **Commandocentrum**: live AI-activiteit van de afgelopen 24 uur.
- **Instellingen → Sessies & audit**: AI-aanroepen gegroepeerd in sessies, met elke aanroep, de argumenten, uitkomst en
  duur, en of hij op goedkeuring wachtte of mislukte.
- **Instellingen → Auditlog**: elke aanmelding, wijziging en actie, van mensen en van AI, met gebruiker of token, doel,
  IP-adres en tijd.
