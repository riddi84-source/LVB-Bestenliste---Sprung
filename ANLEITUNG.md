# Anleitung: Brandenburg Sprung-Bestenliste einrichten

Die Einrichtung läuft **genauso ab wie beim Stabhochsprung-Dashboard** — falls du
das schon eingerichtet hast, ist dir der Ablauf bekannt. Kurzfassung hier, bei
Fragen zu einem einzelnen Schritt gilt die ausführliche ANLEITUNG.md aus dem
ersten Paket.

1. **Neues Repository** auf GitHub anlegen (z. B. `brandenburg-sprung-bestenliste`), public
2. **Dateien hochladen**: `dashboard.html` und `scraper.py` per "Upload files";
   für `.github/workflows/weekly-update.yml` den Weg über "Add file" → "Create new file"
   nehmen und den Pfad `.github/workflows/weekly-update.yml` eintippen (siehe
   Stabhochsprung-Anleitung, Schritt 3, für Details)
3. **GitHub Pages aktivieren**: Settings → Pages → Source: `main` Branch, `/ (root)`
4. **Workflow-Rechte setzen**: Settings → Actions → General → Workflow permissions
   → "Read and write permissions" → Save
5. **Testen**: Actions → "Wöchentliches Brandenburg-Sprung-Update" → "Run workflow"

## Ein wichtiger Unterschied zum ersten Tool

Dieser Scraper bedient die Filter-Dropdowns auf der Brandenburg-Seite so, wie du
es manuell im Browser tust (Disziplin auswählen, Altersklasse auswählen, Suche
starten) — statt über eine feste Web-Adresse pro Kategorie. Das ist technisch
angewiesener auf die genaue Beschriftung der Dropdown-Menüs. Ich konnte das
**nicht live testen**, deshalb ist beim ersten echten Lauf eine Fehlermeldung
wahrscheinlicher als beim Stabhochsprung-Tool.

**Falls der erste Lauf fehlschlägt:** Schau im Actions-Log nach der Zeile, die
mit "Dropdown fuer Disziplin ... oder Altersklasse ... nicht gefunden" oder
einer aehnlichen Meldung beginnt, und schick sie mir. Am hilfreichsten ist dann
zusätzlich ein Screenshot der Brandenburg-Bestenliste-Seite mit geöffnetem
Altersklasse-Dropdown (damit ich die exakte Schreibweise der Optionen sehe,
z. B. ob es "Männliche Jugend U18" oder "Jugend männlich U18" oder eine andere
Formulierung heißt).

## Umfang

Aktuell abgedeckt: Hochsprung, Stabhochsprung, Weitsprung, Dreisprung, jeweils
für M14/W14 bis Männer/Frauen (12 Altersklassen) — macht 48 Kombinationen pro
Lauf. Rechne mit spürbar längerer Laufzeit als beim Stabhochsprung-Tool (dort
liefen nur ~2 Kategorien).
