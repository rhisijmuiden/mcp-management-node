# MCP-hub en toolcatalogus

## MCP-hub

De **MCP-hub** zet je andere MCP-servers achter hetzelfde Stackpilot-adres. Je AI heeft dan **één koppeling** voor
alles nodig, en elke tool, ook van andere servers, volgt hetzelfde beleid, dezelfde goedkeuringen en hetzelfde auditlog.
Voorbeelden: GitHub, een VoIP-systeem zoals Keystone, Home Assistant.

### Een server toevoegen

1. Open **Koppelingen → MCP hub** en klik op **MCP-server toevoegen**. Voor GitHub is er een kant-en-klare optie.
2. Kies hoe Stackpilot hem bereikt:
    - **Streamable HTTP**, rechtstreeks of **via een agent** in het netwerk van de klant (zodat een server op hun LAN
      geen open poort nodig heeft);
    - het oudere **HTTP + SSE**;
    - een **commando** dat Stackpilot zelf start (bijvoorbeeld `github-mcp-server stdio`).
3. Kies het inloggen: een bearer-token, eigen headers (API-sleutel) of **OAuth** (je logt één keer in; Stackpilot
   bewaart en vernieuwt de tokens). Geheimen worden versleuteld opgeslagen.
4. Geef een **prefix**. Tools verschijnen dan als `<prefix>__<tool>`, bijvoorbeeld `github__create_issue`.
5. Koppel hem eventueel aan een **klant**: alleen koppelingen voor die klant zien hem, en zijn aanroepen volgen het
   beleid van die klant.
6. **Accepteer de tools.** Niets gaat naar je AI voordat je het geaccepteerd hebt.

### Veilig ontworpen

- **Nieuwe of gewijzigde tools worden ingetrokken** tot je ze opnieuw accepteert, zodat een server je AI niet stilletjes
  nieuwe instructies kan geven ("tool poisoning").
- Elke tool krijgt een **risiconiveau**, voorgesteld op basis van de hints van de server zelf, dat je kunt aanpassen.
- Een AI-koppeling heeft de scope **`hub`** nodig om hub-tools te zien; zet die per AI-app aan op de pagina MCP hub.
- De gezondheid per server (status, aantal tools, reactietijd, laatste fout) wordt elke 5 minuten gecontroleerd.

## Toolcatalogus

**Instellingen → Toolcatalogus** toont elke tool die je AI kan krijgen, van Stackpilot en van de hub, met het
risiconiveau. Zet losse tools of een hele hub-server **uit** voor alle koppelingen of voor één token. Een uitgezette tool
wordt niet aan de AI getoond en aanroepen ervan worden geweigerd.
