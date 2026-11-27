# Info, updates en kanalen

![Info en updates](../assets/images/info-updates.png)

Klik op het **versienummer** in de bovenbalk om **Info & updates** te openen.

## Wat het laat zien

De versie, commit, builddatum, uptime en de versies van Python, PostgreSQL en het besturingssysteem.

## Twee kanalen

| Kanaal | Volgt | Versies |
| --- | --- | --- |
| **Productie** | de `main`-branch | `vX.Y.Z` |
| **Test** | de `test`-branch | `vX.Y.Z-test.N` (prereleases) |

Wissel van kanaal op deze pagina. Gebruik **Test** op een testinstallatie om nieuwe functies te proberen voordat ze in
productie komen.

## Updaten, stap voor stap

1. Klik op **Nu controleren**. Stackpilot vraagt GitHub naar de nieuwste release van je kanaal en toont de
   releaseopmerkingen.
2. Klik op **Installeren** (met het versienummer).
3. Stackpilot downloadt de release, controleert de SHA-256, **maakt een back-up van de database**, migreert, schakelt
   over naar de nieuwe versie en herstart.
4. Start de nieuwe versie niet, dan **draait** het de code en de database **zelf terug**.

De pagina maakt vanzelf weer verbinding als de update klaar is. De installer opnieuw draaien werkt een installatie ook
bij.

!!! info "Agents"
    Agents werken zichzelf bij naar de versie die bij Stackpilot hoort, tenzij je dat uitzette onder **Koppelingen →
    Agents en koppelen**. Een agent-update onderbreekt kort commando's die op dat moment op die machine draaien.
