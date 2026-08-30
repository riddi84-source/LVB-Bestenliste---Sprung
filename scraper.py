"""
Woechentlicher Scraper fuer die Brandenburg-Bestenliste, Sprungdisziplinen.

TECHNISCHER HINTERGRUND (nach mehreren Testlaeufen herausgefunden)
---------------------------------------------------------------------
1. Die Brandenburg-Bestenliste laeuft ueber die gemeinsame Plattform
   dlvbl.laportal.net, mit einer festen performanceList-Kennung, die nur
   Brandenburg-Athlet:innen enthaelt.
2. Ein komplett direkter Aufruf (ganz ohne vorherigen Seitenbesuch) wird mit
   "Performance list not found" abgelehnt -- vermutlich eine Session-Pruefung.
   Einmal ueber die eingebettete Brandenburg-Seite aufgerufen, funktionieren
   direkte Adressen mit allen Parametern (eventcode, classcode, year) danach
   aber zuverlaessig -- das wurde durch einen manuellen Test bestaetigt.
3. Die Disziplin-Codes (eventcode) folgen den international ueblichen Kuerzeln:
   HJ=Hochsprung, PV=Stabhochsprung, LJ=Weitsprung (bestaetigt!), TJ=Dreisprung.
4. Die Altersklasse (classcode) nutzt Kuerzel wie "M"/"W" (Maenner/Frauen),
   "MJU18"/"WJU18" usw. -- direkt aus dem Seitenquelltext uebernommen.

Deshalb: zuerst die Brandenburg-Seite besuchen (Session herstellen), danach
direkt die Ziel-URL mit allen Parametern aufrufen -- kein Dropdown-Geklicke
mehr noetig.

WICHTIGER HINWEIS: Die Ergebnistabelle in dieser Ansicht zeigt offenbar nur
Ergebnis+Wind und Name+Verein -- kein Datum/Ort (anders als beim ersten
Stabhochsprung-Tool). Falls sich das beim ersten echten Lauf als falsch
herausstellt, sind date/venue im Ergebnis einfach leer, kein Fehler.
"""

import asyncio
import json
from datetime import datetime, date, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BRANDENBURG_PAGE = "https://www.leichtathletikverband-brandenburg.de/wettkaempfe/bestenliste/2020"
PERFORMANCE_LIST_ID = "a64ee412-73fe-4f16-bb88-bc39c2d7fcdb"
RESULTS_URL = (
    "https://dlvbl.laportal.net/Performances"
    "?performanceList={performance_list}&eventcode={event_code}&classcode={classcode}"
    "&environment=1&year={year}&showForeigners=1"
)

EVENT_CODES = {
    "hochsprung": "HJ",
    "stabhochsprung": "PV",
    "weitsprung": "LJ",   # bestaetigt durch manuellen Test
    "dreisprung": "TJ",
}

# dashboard-interner Schluessel -> "value"-Attribut im Altersklasse-Dropdown
# (direkt aus dem echten Seitenquelltext uebernommen)
AGE_CLASSES = {
    "Männer": "M",
    "Frauen": "W",
    "mU20": "MJU20",
    "wU20": "WJU20",
    "mU18": "MJU18",
    "wU18": "WJU18",
    "mU16": "MJU16",
    "wU16": "WJU16",
    "M15": "M15",
    "W15": "W15",
    "M14": "M14",
    "W14": "W14",
}

TOP_N = 15
DATA_FILE = Path("data.json")
PREVIOUS_FILE = Path("previous_data.json")

_diagnostic_dumped = False


async def dump_diagnostics(page):
    """Schreibt einmalig eine Bestandsaufnahme der Seite ins Log, falls das
    Tabellen-Parsing unerwartet nichts findet."""
    global _diagnostic_dumped
    if _diagnostic_dumped:
        return
    _diagnostic_dumped = True

    print("\n" + "=" * 60)
    print("DIAGNOSE: Seiteninhalt bei fehlgeschlagenem Tabellen-Parsing")
    print("=" * 60)
    print(f"Aktuelle URL: {page.url}")
    body_text = await page.inner_text("body")
    print(f"Sichtbarer Text (erste 1500 Zeichen):\n{body_text[:1500]}")
    tables = await page.query_selector_all("table")
    print(f"\nAnzahl <table>-Elemente: {len(tables)}")
    for i, t in enumerate(tables[:3]):
        html = await t.inner_html()
        print(f"\n  Tabelle #{i} (erste 800 Zeichen HTML):\n{html[:800]}")
    print("=" * 60 + "\n")


async def fetch_top15(browser, discipline_label: str, event_code: str, age_value: str,
                       year: int, retries: int = 2):
    last_error = None
    for attempt in range(1, retries + 2):
        context = await browser.new_context()
        page = await context.new_page()
        try:
            # Schritt 1: Session ueber die eingebettete Brandenburg-Seite herstellen
            await page.goto(BRANDENBURG_PAGE, wait_until="networkidle", timeout=30000)

            # Schritt 2: direkt zur Ziel-URL mit allen Parametern springen
            target_url = RESULTS_URL.format(
                performance_list=PERFORMANCE_LIST_ID,
                event_code=event_code,
                classcode=age_value,
                year=year,
            )
            await page.goto(target_url, wait_until="networkidle", timeout=30000)

            try:
                await page.wait_for_selector("table tbody tr", timeout=15000)
            except Exception:
                await dump_diagnostics(page)
                raise RuntimeError("Keine Ergebnistabelle gefunden (Timeout).")

            rows = await page.query_selector_all("table tbody tr")
            results = []
            for i, row in enumerate(rows[:TOP_N]):
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                cell_texts = [(await c.inner_text()).strip() for c in cells]

                # Erste Zelle: Ergebnis (+ Wind in eigener Zeile), zweite Zelle:
                # Name (+ Verein in eigener Zeile) -- nach Sichtung des manuellen
                # Testaufrufs. Faellt auf einzeiligen Text zurueck, falls anders.
                mark_lines = cell_texts[0].split("\n")
                name_lines = cell_texts[1].split("\n")

                results.append({
                    "rank": i + 1,
                    "mark": mark_lines[0].strip(),
                    "wind": mark_lines[1].strip() if len(mark_lines) > 1 else "",
                    "unit": "m",
                    "name": name_lines[0].strip(),
                    "club": name_lines[1].strip() if len(name_lines) > 1 else "",
                    "birthYear": "",
                    "venue": "",
                    "date": "",
                })

            if not results:
                await dump_diagnostics(page)
                raise RuntimeError("Tabelle gefunden, aber keine auswertbaren Zeilen.")

            return results

        except Exception as e:
            last_error = e
            print(f"    Versuch {attempt} fuer '{discipline_label}/{age_value}' fehlgeschlagen: {e}")
            if attempt <= retries:
                await asyncio.sleep(3)
            continue
        finally:
            await context.close()

    print(f"    FEHLER: '{discipline_label}/{age_value}' nach {retries + 1} Versuchen nicht ladbar: {last_error}")
    return None


async def run(year: int | None = None):
    year = year or date.today().year
    lists = {}
    failures = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        first_combo = True
        for disc_key, event_code in EVENT_CODES.items():
            for age_key, age_value in AGE_CLASSES.items():
                print(f"Lade {disc_key} ({event_code}) / {age_key} ({age_value}) / {year} ...")
                rows = await fetch_top15(browser, disc_key, event_code, age_value, year)
                combo_key = f"{disc_key}|{age_key}"

                if rows is None and first_combo:
                    print("\nABBRUCH: Bereits die allererste Kombination ist fehlgeschlagen -- "
                          "das deutet auf ein grundsaetzliches Problem hin. Breche restliche "
                          "Kombinationen ab, um Zeit zu sparen.")
                    await browser.close()
                    return {}

                first_combo = False
                if rows is None:
                    failures.append(combo_key)
                else:
                    lists[combo_key] = rows
                    print(f"  -> {len(rows)} Eintraege")

        await browser.close()

    previous = {}
    if PREVIOUS_FILE.exists():
        try:
            prev_json = json.loads(PREVIOUS_FILE.read_text(encoding="utf-8"))
            previous = prev_json.get("lists", prev_json)
        except Exception:
            previous = {}

    for combo_key in failures:
        if combo_key in previous:
            print(f"WARNUNG: '{combo_key}' fehlgeschlagen, uebernehme Vorwochen-Stand.")
            lists[combo_key] = previous[combo_key]
        else:
            print(f"WARNUNG: '{combo_key}' fehlgeschlagen UND keine Vorwochen-Daten vorhanden.")

    output = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "lists": lists,
    }
    DATA_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    PREVIOUS_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nFertig: {len(lists)} von {len(EVENT_CODES) * len(AGE_CLASSES)} Kombinationen geladen.")
    if failures:
        print(f"Fehlgeschlagen (siehe Log oben): {', '.join(failures)}")
    return lists


if __name__ == "__main__":
    asyncio.run(run())
