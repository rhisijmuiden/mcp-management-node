# GitHub, softwarebibliotheek en deployments

## Een GitHub-token toevoegen

Stackpilot werkt met GitHub via een token. Je kunt er meerdere toevoegen: één per eigenaar, bijvoorbeeld je eigen account
en de organisatie van een klant.

1. Open **Koppelingen → Alle koppelingen → GitHub → Stap voor stap instellen**.
2. Ga op GitHub naar **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new
   token**. Kies de eigenaar en **All repositories**.
3. Geef **Read and write** op: Administration, Contents, Pull requests, Issues, Actions, Workflows, Webhooks, Secrets,
   Variables, Environments, Deployments, Pages en Commit statuses. Daarmee kan Stackpilot bijna alles, ook repositories
   aanmaken. (Een klassiek token werkt ook; de uitleg heeft een vooraf ingevulde link.)
4. Plak het token in de laatste stap van de uitleg.

Elk token toont zijn login, soort, vervaldatum en laatste controle. Een token dat binnen 14 dagen verloopt of niet meer
werkt, wordt hier en op de pagina Koppelingen gemarkeerd.

## De softwarebibliotheek

**Automatisering → Software → Repositories** is de bibliotheek van je repositories. Die staat boven de klanten: één
repository kan aan meerdere klanten gekoppeld zijn.

- **Soorten**: *Product* (je eigen software, voor meerdere klanten), *Klantspecifiek* (alleen voor die ene klant) en
  *Tooling* (je eigen scripts en tools).
- **Rechten per klant**: *Mag uitrollen* (mag op de containers en servers van die klant) en *AI mag lezen*. Stel ze per
  repository in, of in de **matrix** van repositories × klanten, waar je met één klik koppelt of weghaalt.
- **Toevoegen vanaf GitHub** importeert repositories die je tokens kunnen zien.
- **Nieuwe repository** maakt er een aan op GitHub (je account of een organisatie, privé of openbaar, met eventueel een
  `.gitignore` en een `test`-branch) en zet hem in één keer in de bibliotheek.

Met een klant gekozen in de bovenbalk zie je alleen de repositories van die klant. Een repository uit de bibliotheek
halen verandert niets op GitHub.

## Deployments

**Automatisering → Software → Deployments** zet een repository op een machine en houdt hem bij.

1. Klik op **Nieuwe deployment** en kies een repository uit de bibliotheek.
2. Kies een **productie**- en een **test**-branch.
3. Kies het doel: een **container** op Proxmox VE of een **Linux-server** met de agent.
4. Kies een voorinstelling (een installatiescript in de repo, Python, Node.js, een statische site) of schrijf je eigen
   **setupcommando** (draait één keer) en **deploycommando** (draait elke keer). Voeg omgevingsvariabelen toe en de
   systemd-service die herstart moet worden.
5. Kies wanneer hij uitrolt: met de hand, **bij elke push** (GitHub-webhook) of door elke 5 minuten op nieuwe commits te
   controleren.

In een container maakt Stackpilot **eerst een snapshot** en zet die terug als de deployment mislukt. Een server heeft
geen snapshots, dus test eerst op de testbranch. **Aanmaken → GitHub** maakt in één keer een nieuwe container en rolt
erin uit.

Een deployment wordt alleen opgeslagen als de repository bij die klant uitgerold mag worden. Het token komt alleen
tijdens het uitrollen op de machine.

## Samen met de AI

Met de GitHub-MCP-server in de [MCP-hub](../ai/mcp-hub.md) kan de AI een project van A tot Z doen: de repository, issues
en pull requests, releases en Actions aanmaken, en het daarna uitrollen. Het runbooksjabloon *Nieuw project via GitHub*
doet dit stap voor stap.
