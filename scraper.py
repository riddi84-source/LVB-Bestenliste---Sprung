"""
Woechentlicher Scraper fuer die Brandenburg-Bestenliste, Sprungdisziplinen.

TECHNISCHER HINTERGRUND
------------------------
Die Brandenburg-Bestenliste laeuft auf der Seite
  https://www.leichtathletikverband-brandenburg.de/wettkaempfe/bestenliste/2020
als eingebetteter iframe, der intern auf
  https://dlvbl.laportal.net/Performances?performanceList=a64ee412-73fe-4f16-bb88-bc39c2d7fcdb
zugreift. Ein DIREKTER Aufruf dieser iframe-Adresse (ohne den Umweg ueber die
Brandenburg-Seite) wurde beim Testen mit "Performance list not found" abgelehnt --
offenbar prueft die Seite, ob die Anfrage wirklich eingebettet erfolgt.

Deshalb geht dieser Scraper den gleichen Weg wie ein echter Browser: er laedt die
Brandenburg-Seite, findet darin den iframe, und bedient darin die Dropdown-Filter
(Disziplin, Altersklasse, Umgebung, Jahr) per sichtbarem Text -- genau wie ein
Mensch es tun wuerde. Das ist robuster als interne Codes zu erraten, aber auch
angewiesen auf die exakten Bezeichnungen/Selektoren der Seite.

WICHTIGER HINWEIS: Ich konnte dieses Skript nicht live gegen die echte Seite
testen (kein Internetzugriff in meiner Entwicklungsumgebung). Die Selektoren
fuer die Dropdowns (siehe SELECT_DISZIPLIN, SELECT_ALTERSKLASSE, etc. unten)
sind nach bestem Wissen geschrieben, muessen aber beim ersten echten Lauf sehr
wahrscheinlich noch nachjustiert werden. Der Log-Output ist bewusst ausfuehrlich,
damit wir bei Fehlern genau sehen, woran es liegt.
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

# dashboard-interner Schluessel -> sichtbarer Text im Altersklasse-Dropdown
# ANNAHME, noch nicht live verifiziert -- siehe Hinweis oben.
AGE_CLASSES = {
    "Männer": "Männer",
    "Frauen": "Frauen",
    "mU20": "Männliche Jugend U20",
    "wU20": "Weibliche Jugend U20",
    "mU18": "Männliche Jugend U18",
    "wU18": "Weibliche Jugend U18",
    "mU16": "Männliche Jugend U16",
    "wU16": "Weibliche Jugend U16",
    "M15": "Jugend M15",
    "W15": "Jugend W15",
    "M14": "Jugend M14",
    "W14": "Jugend W14",
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


async def select_dropdown_by_label(frame, label_substring: str, option_text: str):
    """Sucht ein <select>, dessen sichtbares Label/umgebender Text label_substring
    enthaelt, und waehlt darin die Option mit option_text aus. Mehrere Fallback-
    Strategien, weil die genaue Seitenstruktur nicht live geprueft werden konnte.

    force=True: viele Webseiten blenden das native <select> optisch aus und zeigen
    stattdessen ein eigenes, gestyltes Dropdown an. Das <select> funktioniert dann
    weiterhin technisch, gilt fuer Playwright aber als "nicht sichtbar" -- ohne
    force=True wuerde select_option() dort endlos auf Sichtbarkeit warten und
    nach 30s timeouten (genau das Verhalten aus dem ersten Testlauf)."""
    selects = await frame.query_selector_all("select")
    for sel in selects:
        for attr in ("name", "id", "aria-label"):
            val = await sel.get_attribute(attr)
            if val and label_substring.lower() in val.lower():
                await sel.select_option(label=option_text, force=True)
                return True
        options = await sel.query_selector_all("option")
        option_texts = [await o.inner_text() for o in options]
        if any(option_text.lower() == t.strip().lower() for t in option_texts):
            await sel.select_option(label=option_text, force=True)
            return True
    return False


async def fetch_top15(browser, discipline_label: str, age_label: str, year: int, retries: int = 2):
    last_error = None
    for attempt in range(1, retries + 2):
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(BRANDENBURG_PAGE, wait_until="networkidle", timeout=30000)
            await page.wait_for_selector("iframe", timeout=15000)
            frame = await get_results_frame(page)
            await frame.wait_for_load_state("networkidle", timeout=20000)

            ok_disc = await select_dropdown_by_label(frame, "disziplin", discipline_label)
            ok_age = await select_dropdown_by_label(frame, "altersklasse", age_label)
            # Umgebung/Jahr: best effort, falls Dropdowns existieren
            await select_dropdown_by_label(frame, "umgebung", "Freiluft")
            await select_dropdown_by_label(frame, "jahr", str(year))

            if not ok_disc or not ok_age:
                await dump_diagnostics(frame)
                raise RuntimeError(
                    f"Dropdown fuer Disziplin ({ok_disc}) oder Altersklasse ({ok_age}) "
                    f"nicht gefunden -- Selektoren muessen angepasst werden."
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
            print(f"    Versuch {attempt} fuer '{discipline_label}/{age_label}' fehlgeschlagen: {e}")
            if attempt <= retries:
                await asyncio.sleep(3)
            continue
        finally:
            await context.close()

    print(f"    FEHLER: '{discipline_label}/{age_label}' nach {retries + 1} Versuchen nicht ladbar: {last_error}")
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

        for disc_key, disc_label in DISCIPLINES.items():
            for age_key, age_label in AGE_CLASSES.items():
                print(f"Lade {disc_label} / {age_label} / {year} ...")
                rows = await fetch_top15(browser, disc_label, age_label, year)
                combo_key = f"{disc_key}|{age_key}"
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
