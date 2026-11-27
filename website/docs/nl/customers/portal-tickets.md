# Klantportaal en tickets

Klanten kunnen problemen en vragen melden in hun eigen portaal. Stackpilot koppelt elk ticket aan de juiste machines en
doet meteen een eerste diagnose, zodat je al weet waar je moet kijken voordat je het ticket opent.

## Het portaal aanzetten

1. Open **Instellingen → Klantportaal**.
2. Zet **Klantportaal aan**, kies wie een mail krijgt bij nieuwe tickets (de behandelaar, een vast adres of alle
   beheerders) en schrijf een welkomsttekst.
3. Het portaal staat op `https://<jouw adres>/portal`.

## Klanten een account geven

1. Open **Tickets → Portaal-accounts** (of de pagina van de klant).
2. Klik op **Account toevoegen**, kies een contactpersoon en de rol:
    - **Lid**: ziet de tickets die hij of zij zelf meldde.
    - **Beheerder**: ziet alle tickets van de organisatie, en kan over elk ticket een mail krijgen.
3. De contactpersoon krijgt een uitnodiging om een wachtwoord te kiezen (7 dagen geldig). Je kunt die opnieuw sturen,
   een wachtwoordlink sturen, een account uitzetten of verwijderen.

Portaalaccounts staan los van de accounts van je team: ze kunnen alleen in het portaal, en zien altijd alleen hun eigen
organisatie.

!!! tip "Zie wat de klant ziet"
    **Bekijk als klant** in de bovenbalk opent het portaal van de gekozen klant zoals de beheerders van die klant het
    zien, alleen-lezen.

## Wat de klant in het portaal doet

- Een probleem, vraag of verzoek melden met een prioriteit, en de computers of servers kiezen waar het over gaat.
- De status volgen, reageren en een ticket sluiten.
- Gepland en recent onderhoud zien onder **Onderhoud**, en zich erop abonneren als agenda.

Het portaal is er in het Engels en het Nederlands, licht en donker, en werkt op een telefoon.

## Tickets afhandelen

Open **Vandaag → Tickets**. Het getal in het menu laat zien hoeveel tickets op een antwoord wachten.

1. Open een ticket. De **eerste diagnose** staat er al: de gekoppelde machines zijn gecontroleerd (gevonden problemen,
   gestopte services, recente fouten, drukste processen), met open meldingen, uptime-controles die down zijn en
   voorgestelde vervolgstappen. Alleen je team ziet dit. **Opnieuw uitvoeren** doet het nog eens.
2. Zet de status (nieuw, open, wacht op klant, in de wacht, opgelost, gesloten), prioriteit, soort en behandelaar.
3. **Reageer** aan de klant, of voeg een **interne notitie** toe. Een reactie kan in één keer de status wijzigen
   ("vragen en wachten", "oplossen").
4. Belde een klant? Klik op **Nieuw ticket** en maak er een voor ze aan.

Mails gaan via je eigen mailserver, in de taal van de klant. Opgeloste tickets sluiten vanzelf na een aantal dagen
zonder reactie.

## Met de AI

De AI kan tickets opvragen en lezen, de diagnose draaien en (scope `tickets`) reageren, notities toevoegen, tickets
wijzigen en aanmaken. Bijvoorbeeld: *"Kijk naar de nieuwe tickets van Acme en zeg welke je kunt oplossen."*
