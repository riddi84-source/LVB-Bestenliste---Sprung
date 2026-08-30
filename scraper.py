"""
Woechentlicher Scraper fuer die Brandenburg-Bestenliste, Sprungdisziplinen.

TECHNISCHER HINTERGRUND
------------------------
Die Brandenburg-Bestenliste laeuft auf der Seite
  https://www.leichtathletikverband-brandenburg.de/wettkaempfe/bestenliste/2020
als eingebetteter iframe, der intern auf
  https://dlvbl.laportal.net/Performances?performanceList=a64ee412-73fe-4f16-bb88-bc39c2d7fcdb
zugreift. Ein DIREKTER Aufruf dieser iframe-Adresse (ohne den Umweg ueber die
Brandenburg-Seite) wird mit "Performance list not found" abgelehnt -- die Seite
prueft offenbar, ob die Anfrage wirklich eingebettet erfolgt. Deshalb laedt dieser
Scraper immer die Brandenburg-Seite und arbeitet dann im eingebetteten iframe.

Die Filter-Felder haben feste IDs im HTML (per Quelltext-Analyse bestaetigt):
  #classcode    Altersklasse (z.B. "MJU18" = maennliche Jugend U18)
  #eventcode    Disziplin -- WICHTIG: dieses <select> ist beim Laden der Seite
                LEER (<select id="eventcode"></select>) und wird erst per
                JavaScript nachtraeglich befuellt. Der Scraper muss deshalb
                aktiv warten, bis Optionen erscheinen, bevor er waehlen kann.
  #environment  Umgebung (Freiluft/Halle), bereits beim Laden befuellt
  #year         Jahr, bereits beim Laden befuellt

Die genauen "value"-Codes fuer #eventcode (Disziplin) kenne ich nicht (das
<select> war ja leer im Quelltext) -- deshalb waehlt der Scraper weiterhin
per sichtbarem Options-TEXT ("Hochsprung", "Stabhochsprung", ...), nicht per
Code. Das classcode-Feld dagegen ist bereits beim Laden befuellt und bekannt
(siehe AGE_CLASSES unten, aus dem echten Quelltext uebernommen).
"""

import asyncio
import json
import re
from datetime import datetime, date, timezone
from pathlib import Path

from playwright.async_api import async_playwright

BRANDENBURG_PAGE = "https://www.leichtathletikverband-brandenburg.de/wettkaempfe/bestenliste/2020"

DISCIPLINES = {
    "hochsprung": "Hochsprung",
    "stabhochsprung": "Stabhochsprung",
    "weitsprung": "Weitsprung",
    "dreisprung": "Dreisprung",
}

# dashboard-interner Schluessel -> "value"-Attribut im Altersklasse-Dropdown
# (direkt aus dem echten Seitenquelltext uebernommen, nicht mehr geraten)
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


async def get_results_frame(page):
    """Findet den eingebetteten iframe mit der eigentlichen Bestenliste."""
    for frame in page.frames:
        if "laportal" in frame.url or "dlvbl" in frame.url:
            return frame
    # Fallback: erster iframe auf der Seite
    frame_element = await page.query_selector("iframe")
    if frame_element:
        return await frame_element.content_frame()
    raise RuntimeError("Kein iframe mit der Bestenliste gefunden -- Seitenstruktur hat sich evtl. geaendert.")


_diagnostic_dumped = False


async def dump_diagnostics(frame):
    """Schreibt einmalig eine Bestandsaufnahme aller <select>-Elemente und ihrer
    Optionen ins Log, damit wir die tatsaechliche Seitenstruktur sehen koennen,
    ohne dass jemand manuell den Quelltext durchsuchen muss."""
    global _diagnostic_dumped
    if _diagnostic_dumped:
        return
    _diagnostic_dumped = True

    print("\n" + "=" * 60)
    print("DIAGNOSE: gefundene <select>-Elemente auf der Seite")
    print("=" * 60)
    selects = await frame.query_selector_all("select")
    print(f"Anzahl <select>-Elemente: {len(selects)}")
    for i, sel in enumerate(selects):
        name = await sel.get_attribute("name")
        sel_id = await sel.get_attribute("id")
        aria = await sel.get_attribute("aria-label")
        options = await sel.query_selector_all("option")
        option_texts = [(await o.inner_text()).strip() for o in options]
        print(f"\n  Select #{i}: name={name!r} id={sel_id!r} aria-label={aria!r}")
        print(f"    Optionen ({len(option_texts)}): {option_texts[:20]}")

    # Falls es KEIN natives <select> fuer die Disziplin gibt, ist es vermutlich
    # ein custom Dropdown -- suche nach typischen Mustern (button/div mit
    # "disziplin" im Attribut, oder anklickbare Listen)
    custom_candidates = await frame.query_selector_all(
        "[class*='disziplin' i], [id*='disziplin' i], [class*='event' i], [id*='event' i]"
    )
    print(f"\n  Moegliche custom-Dropdown-Elemente (Klasse/ID enthaelt 'disziplin' oder 'event'): "
          f"{len(custom_candidates)}")
    for i, el in enumerate(custom_candidates[:10]):
        tag = await el.evaluate("el => el.tagName")
        cls = await el.get_attribute("class")
        el_id = await el.get_attribute("id")
        text = (await el.inner_text())[:80] if await el.inner_text() else ""
        print(f"    [{i}] <{tag} class={cls!r} id={el_id!r}> Text: {text!r}")
    print("=" * 60 + "\n")


async def select_by_id(frame, element_id: str, value: str = None, label: str = None):
    """Waehlt eine Option in einem <select id="..."> per value oder sichtbarem
    Options-Text. force=True, weil das native <select> auf dieser Seite optisch
    versteckt und durch ein eigenes Erscheinungsbild ersetzt wird -- Playwright
    wuerde sonst auf "Sichtbarkeit" warten und nach 30s timeouten."""
    sel = await frame.query_selector(f"#{element_id}")
    if not sel:
        return False
    try:
        if value is not None:
            await sel.select_option(value=value, force=True)
        else:
            await sel.select_option(label=label, force=True)
        return True
    except Exception:
        return False


PERFORMANCE_LIST_ID = "a64ee412-73fe-4f16-bb88-bc39c2d7fcdb"

# Bekannt aus dem urspruenglichen Seitenquelltext (siehe Screenshot vom Nutzer);
# das <select id="year"> wird von der Seiten-eigenen JS-Komponente offenbar
# geleert und nie neu befuellt -- deshalb hier fest hinterlegt statt verlassen
# auf das, was die Seite selbst anbietet.
KNOWN_YEARS = [2026, 2025, 2024, 2023, 2022, 2021, 2020]


async def populate_eventcode_directly(frame, classcode_value: str, environment_value: str = "1"):
    """Die Disziplin-Liste (#eventcode) wird normalerweise per AJAX-Aufruf an
    GetEventsForClass befuellt -- das per Netzwerk-Mitschnitt bestaetigt wurde.
    Dieser Aufruf feuert aber nur EINMAL automatisch beim Laden der Seite, mit
    cls=null&env=null (liefert dadurch nichts Brauchbares), und wird durch
    unsere spaetere Altersklassen-Auswahl NICHT erneut ausgeloest. Deshalb rufen
    wir die Adresse hier selbst mit den richtigen Werten auf und tragen das
    Ergebnis direkt in das <select> ein, statt auf einen Auto-Trigger zu hoffen."""
    url = (f"https://dlvbl.laportal.net/Performances/GetEventsForClass"
           f"?cls={classcode_value}&performanceList={PERFORMANCE_LIST_ID}&env={environment_value}")
    html_fragment = await frame.evaluate(
        """async (url) => {
            const res = await fetch(url, {credentials: 'include'});
            return await res.text();
        }""",
        url,
    )
    if not html_fragment or "<option" not in html_fragment.lower():
        raise RuntimeError(f"GetEventsForClass lieferte keine Optionen zurueck (Antwortlaenge: {len(html_fragment or '')}).")
    await frame.evaluate(
        """([html]) => { document.getElementById('eventcode').innerHTML = html; }""",
        [html_fragment],
    )


async def populate_year_directly(frame):
    """#year wird ebenfalls von der Seiten-JS geleert -- feste Liste aus dem
    urspruenglichen Quelltext direkt eintragen, statt auf die Seite zu vertrauen."""
    options_html = "".join(f'<option value="{y}">{y}</option>' for y in KNOWN_YEARS)
    await frame.evaluate(
        """([html]) => { document.getElementById('year').innerHTML = html; }""",
        [options_html],
    )


async def fetch_top15(browser, discipline_label: str, age_value: str, year: int, retries: int = 2):
    global _diagnostic_dumped
    last_error = None
    for attempt in range(1, retries + 2):
        context = await browser.new_context()
        page = await context.new_page()

        # Nur beim allerersten Versuch: Netzwerk-Anfragen und Konsolen-Meldungen
        # mitschneiden, um zu sehen, ob/welche Anfrage die Disziplin-/Jahr-Liste
        # eigentlich befuellen sollte, und ob dabei ein JS-Fehler auftritt.
        captured_requests = []
        captured_console = []
        if not _diagnostic_dumped:
            page.on("request", lambda req: captured_requests.append(f"{req.method} {req.url}"))
            page.on("console", lambda msg: captured_console.append(f"[{msg.type}] {msg.text}"))
            page.on("pageerror", lambda exc: captured_console.append(f"[pageerror] {exc}"))

        try:
            await page.goto(BRANDENBURG_PAGE, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("iframe", timeout=15000)
            frame = await get_results_frame(page)
            await frame.wait_for_load_state("networkidle", timeout=20000)

            ok_age = await select_by_id(frame, "classcode", value=age_value)

            try:
                await populate_eventcode_directly(frame, age_value, environment_value="1")
                await populate_year_directly(frame)
            except Exception as e:
                if not _diagnostic_dumped:
                    print("\n--- Mitgeschnittene Netzwerk-Anfragen waehrend des Ladens ---")
                    relevant = [r for r in captured_requests if "laportal" in r.lower() or "api" in r.lower()
                                or "event" in r.lower() or "json" in r.lower()]
                    for r in (relevant or captured_requests)[:40]:
                        print(f"  {r}")
                    print(f"  (gesamt {len(captured_requests)} Anfragen, davon {len(relevant)} als relevant gefiltert)")
                    print("--- Konsolen-Meldungen / JS-Fehler ---")
                    if captured_console:
                        for c in captured_console[:30]:
                            print(f"  {c}")
                    else:
                        print("  (keine)")
                    await dump_diagnostics(frame)
                raise RuntimeError(f"Direktes Befuellen von Disziplin/Jahr fehlgeschlagen: {e}")

            ok_disc = await select_by_id(frame, "eventcode", label=discipline_label)
            await select_by_id(frame, "environment", value="1")  # 1 = Freiluft
            await select_by_id(frame, "year", value=str(year))

            if not ok_disc or not ok_age:
                await dump_diagnostics(frame)
                raise RuntimeError(
                    f"Auswahl Disziplin ({ok_disc}) oder Altersklasse ({ok_age}) fehlgeschlagen."
                )

            # Suche-Button klicken (mehrere moegliche Beschriftungen probieren)
            clicked = False
            for btn_text in ["Suche Starten", "Suche starten", "Suchen", "Suche"]:
                btn = await frame.query_selector(f"text={btn_text}")
                if btn:
                    await btn.click()
                    clicked = True
                    break
            if not clicked:
                raise RuntimeError("Such-Button nicht gefunden.")

            await frame.wait_for_selector("table tbody tr", timeout=20000)
            rows = await frame.query_selector_all("table tbody tr")

            results = []
            for row in rows[:TOP_N]:
                cells = await row.query_selector_all("td")
                if len(cells) < 5:
                    continue
                texts = [(await c.inner_text()).strip() for c in cells]
                # ANNAHME zur Spaltenreihenfolge, evtl. anzupassen nach erstem Testlauf:
                # Platz | Leistung | Name | Jahrgang | Verein | Ort | Datum
                try:
                    rank = int(re.sub(r"\D", "", texts[0]) or "0")
                except ValueError:
                    rank = 0
                mark_match = re.search(r"[\d.,]+", texts[1])
                mark = mark_match.group(0).replace(",", ".") if mark_match else texts[1]

                results.append({
                    "rank": rank,
                    "mark": mark,
                    "unit": "m",
                    "name": texts[2] if len(texts) > 2 else "",
                    "birthYear": texts[3] if len(texts) > 3 else "",
                    "club": texts[4] if len(texts) > 4 else "",
                    "venue": texts[5] if len(texts) > 5 else "",
                    "date": normalize_date(texts[6]) if len(texts) > 6 else "",
                })
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
    return None  # explizit None = "fehlgeschlagen", anders als [] = "geladen, aber leer"


def normalize_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


async def run(year: int | None = None):
    year = year or date.today().year
    lists = {}
    failures = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        first_combo = True
        for disc_key, disc_label in DISCIPLINES.items():
            for age_key, age_value in AGE_CLASSES.items():
                print(f"Lade {disc_label} / {age_key} ({age_value}) / {year} ...")
                rows = await fetch_top15(browser, disc_label, age_value, year)
                combo_key = f"{disc_key}|{age_key}"

                if rows is None and first_combo:
                    # Fail-fast: wenn schon die allererste Kombination scheitert,
                    # ist das Problem grundsaetzlich (nicht kombinationsspezifisch).
                    # Abbrechen statt 47 weitere Kombinationen sinnlos durchzuprobieren
                    # und wertvolle Laufzeit zu verschwenden.
                    print("\nABBRUCH: Bereits die allererste Kombination ist fehlgeschlagen -- "
                          "das deutet auf ein grundsaetzliches Problem hin, nicht auf einen "
                          "Einzelfall. Breche restliche 47 Kombinationen ab, um Zeit zu sparen.")
                    await browser.close()
                    return {}

                first_combo = False
                if rows is None:
                    failures.append(combo_key)
                else:
                    lists[combo_key] = rows
                    print(f"  -> {len(rows)} Eintraege")

        await browser.close()

    # Sicherheitsnetz: fehlgeschlagene Kombinationen mit Vorwochen-Daten auffuellen,
    # statt sie leer zu lassen (verhindert Datenverlust bei technischen Aussetzern)
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

    print(f"\nFertig: {len(lists)} von {len(DISCIPLINES) * len(AGE_CLASSES)} Kombinationen geladen.")
    if failures:
        print(f"Fehlgeschlagen (siehe Log oben): {', '.join(failures)}")
    return lists


if __name__ == "__main__":
    asyncio.run(run())
