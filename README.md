# Quotenvergleich-Portal (intern)

Automatisches Vergleichsportal für eure Tipps: liest die Tipps aus eurer Google-Tabelle,
holt über die OddsPapi-API die Quoten eurer Buchmacher, **trägt sie direkt in eure Tabelle
neben die Tipps ein** und zeigt alles zusätzlich in einem passwortgeschützten Portal auf
GitHub Pages. Aktualisiert sich alle 30 Minuten selbst.

**Bereiche im Portal**
- **Tipps** – jeder Tipp mit Mindestquote, „erreichbar ja/nein“, beste Quote, Value und
  allen Buchmachern nebeneinander. Zeile anklicken: Quotenverlauf und weitere Buchmacher.
- **Value** – Quoten eurer Buchmacher über der fairen Quote (Marktkonsens ohne Marge).
- **Alle Spiele** – Fußball, Basketball, Tennis der nächsten 24 h; Spiel anklicken: alle Märkte.
- **Status** – Abrufverbrauch, Stand, Protokoll.

Es werden nur **aktive** Quoten verwendet (inaktive, pausierte oder gesperrte Angebote nie).

## Einrichtung (einmalig, ca. 10 Minuten)

### 1. Dateien ins Repository
Diesen Ordnerinhalt ins Repository kopieren (z. B. `reinski260970/soccer`):
```
.github/workflows/odds-portal.yml
portal/fetch.py
portal/config.json
site/index.html
```

### 2. Brücke in eure Google-Tabelle einbauen (Tipps lesen + Quoten eintragen)
1. Eure Tipp-Tabelle öffnen → **Erweiterungen → Apps Script**.
2. Inhalt von `sheet/sheet_bridge.gs` einfügen und oben eintragen:
   - `TOKEN`: ein langes, zufälliges Passwort (mind. 16 Zeichen) – nur für diese Verbindung
   - `BLATT_NAME`: Name eures Tipp-Blatts
   - `AUSGABE_AB_SPALTE`: ab welcher Spalte die Quoten geschrieben werden (Standard `I`)
3. **Bereitstellen → Neue Bereitstellung → Zahnrad → Web-App**
   - Ausführen als: **Ich** · Zugriff: **Jeder**
   - **Bereitstellen** → Zugriff erlauben → **Web-App-URL kopieren**.
   „Jeder“ ist nötig, damit GitHub die Web-App erreicht. Ohne das richtige TOKEN gibt sie
   nichts heraus und schreibt nichts.

Erwartetes Format der Tipp-Tabelle (Spalten in `portal/config.json` änderbar):
| B Anstoß | C Liga | D Spiel | E Tipp | F Einsatz | G Mindestquote |
|---|---|---|---|---|---|
| 17/10/2026 15:00 | League One | Leicester City vs Doncaster Rovers | HOME -0.5 | 6 | 1,75 |

**Was in die Tabelle geschrieben wird (ab Spalte I):** Status · Mindestquote erreichbar
(JA grün / NEIN rot) · Beste Quote · Bester Buchmacher · Value % · Faire Quote · Anzahl
Buchmacher · Spiel (API) · Anstoß (API) · Markt · **eine Spalte je Buchmacher** (beste gelb) ·
Bester sonstiger · Stand. Eure eigenen Spalten werden nie verändert. Fügt ihr Zeilen ein oder
löscht welche, findet das System die Tipps über Spiel + Tipp wieder.

Erkannte Tipps u. a.: `HOME -0.5`, `AWAY +1.5`, `HOME`, `DRAW`, `AWAY`, `OVER 2.5`, `UNDER 2.5`,
`Über 3,5`, `BTTS YES`, `1X`, `X2`, `HOME DNB` – und jeder andere Text, der wie ein
OddsPapi-Markt heißt (z. B. `Correct Score 2-1`, `Total Games Over 22.5`, `1st Half Over 2.5`).

### 3. Secrets anlegen
Repository → **Settings → Secrets and variables → Actions → New repository secret**:
| Name | Inhalt |
|---|---|
| `ODDSPAPI_KEY` | API-Key von oddspapi.io |
| `PORTAL_PASSWORD` | Passwort für das Portal (mind. 8 Zeichen, gerne lang) |
| `SHEET_WEBAPP_URL` | Web-App-URL aus Schritt 2 |
| `SHEET_TOKEN` | dasselbe TOKEN wie im Apps Script |

Alternative ohne Brücke (nur lesen, nichts eintragen): Tabelle über *Datei → Freigeben → Im Web
veröffentlichen* als CSV veröffentlichen und den Link als Secret `TIPS_CSV_URL` anlegen.

### 4. GitHub Pages einschalten
Repository → **Settings → Pages → Source: GitHub Actions**.

### 5. Ersten Lauf starten
**Actions → Quotenvergleich-Portal → Run workflow**. Nach ca. 2–5 Minuten steht unter
Settings → Pages die Adresse des Portals. Passwort eingeben – fertig.

## Datenschutz
GitHub-Pages-Seiten sind technisch öffentlich erreichbar. Deshalb sind **alle Daten
verschlüsselt** (AES-256-GCM, Schlüssel aus eurem Passwort). Ohne Passwort sieht man nur die
Anmeldeseite. API-Key und Brücken-TOKEN liegen ausschließlich in den GitHub Secrets.
Die Tabelle selbst bleibt privat – mit der Brücke muss nichts veröffentlicht werden.
Hinweis: Bei einem **privaten** Repository braucht GitHub Pages einen bezahlten GitHub-Plan,
und Actions-Minuten sind begrenzt (2.000/Monat gratis; dieses Portal braucht grob 1.500–2.500).
Bei einem öffentlichen Repository ist beides kostenlos, die Daten bleiben trotzdem verschlüsselt.

## Abruf-Budget (OddsPapi 5.000 / Monat)
- Das Restkontingent wird automatisch auf die restlichen Tage des Monats verteilt
  (`budget.monthly_reserve` bleibt immer übrig, `budget.daily_max` ist die Obergrenze pro Tag).
- Quoten werden **pro Liga** geholt (bis zu 10 Ligen je Abruf), nicht pro Spiel.
- Tipps mit Anstoß in den nächsten 24 h: bei jedem Lauf (30 min). Weiter entfernte: alle 6 h.
  Wird das Budget knapp, kommen die nächsten Anstöße zuerst dran.
- Tages-Scan aller Spiele: 1× täglich ab 7 Uhr mit max. 40 % des Tagesbudgets.
- Quotenverlauf (`/historical-odds`) und Kontostand (`/account`) kosten nichts.
- Bietet die API einen Abruf mit allen Buchmachern auf einmal nicht an, schaltet das System
  automatisch auf Gruppen à 3 Buchmacher um (steht dann im Status). Das kostet mehr Abrufe.

## Einstellungen (`portal/config.json`)
| Schlüssel | Bedeutung |
|---|---|
| `bookmakers` | eure Buchmacher (Spalten im Portal) |
| `tips.columns` | Spaltenbuchstaben eurer Tipp-Tabelle |
| `tips.near_hours` / `tips.far_refresh_hours` | ab wann alle 30 min / sonst wie oft aktualisiert wird |
| `scan.every_hours` / `scan.share_of_daily_budget` | Tages-Scan aller Spiele |
| `value_min_ev_pct` | ab wie viel % über fairer Quote ein Eintrag in die Value-Liste kommt |
| `leagues_per_call` | Ligen pro Sammelabruf |
