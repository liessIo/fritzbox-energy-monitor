"""Strompreise mit Gültigkeitsdatum. Stdlib, damit API und report.py sie teilen.

Zwei Arten:
* ``kwh``   — Arbeitspreis in € je kWh, bezahlt den Verbrauch.
* ``jahr``  — Grundpreis in € je Jahr, fällig unabhängig vom Verbrauch.

Jeder Eintrag gilt ab seinem Tag (Ortszeit, 0 Uhr) bis zum nächsten Eintrag
derselben Art. Vergangene Zeiträume behalten so ihren damaligen Preis, ein neuer
Tarif wird vorab mit seinem Startdatum eingetragen. Die Preise liegen in
labels.db neben den Tageskorrekturen — nie in data.db, die bleibt unberührt.

Ohne Eintrag gilt für ``kwh`` PRICE_PER_KWH aus fritzbox.env, von Anfang an;
für ``jahr`` gibt es keine Vorgabe, dann wird kein Grundpreis ausgewiesen.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, time as dtime, timedelta

ANFANG = "2000-01-01"        # Schlüssel für „von Anfang an“
ARTEN = ("kwh", "jahr")
# Obergrenzen gegen Tippfehler (Komma vergessen: 3412 statt 34,12).
HOECHST = {"kwh": 5.0, "jahr": 2000.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
  art        TEXT NOT NULL CHECK (art IN ('kwh','jahr')),
  ab         TEXT NOT NULL,      -- YYYY-MM-DD, Ortszeit
  wert       REAL NOT NULL CHECK (wert >= 0),
  updated_ts INTEGER NOT NULL,
  PRIMARY KEY (art, ab)
);
"""


def lesen(pfad, art: str, vorgabe: float = 0.0) -> list[dict]:
    """Alle Preise einer Art, älteste zuerst: [{ab, wert, quelle, schluessel}].
    Der erste gilt immer von Anfang an — fehlt ein eigener, springt `vorgabe`
    ein. `schluessel` ist das gespeicherte Datum (zum Ändern/Löschen), None
    bei der Vorgabe aus fritzbox.env."""
    eintraege: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
        try:
            eintraege = [{"ab": ab, "wert": w, "quelle": "eingetragen", "schluessel": ab}
                         for ab, w in conn.execute(
                             "SELECT ab, wert FROM prices WHERE art = ? ORDER BY ab",
                             (art,))]
        finally:
            conn.close()
    except sqlite3.OperationalError:   # Datei oder Tabelle noch nicht da
        pass
    if not eintraege or eintraege[0]["ab"] != ANFANG:
        if vorgabe > 0:
            eintraege.insert(0, {"ab": ANFANG, "wert": vorgabe, "quelle": "fritzbox.env",
                                 "schluessel": None})
        elif eintraege:
            # Keine Vorgabe: der früheste Eintrag gilt auch rückwirkend,
            # sonst hätten ältere Zeiträume gar keinen Preis.
            eintraege[0] = {**eintraege[0], "ab": ANFANG}
    return eintraege


def preis_am(preise: list[dict], tag: str | date) -> float | None:
    """Der am Tag gültige Preis — der letzte Eintrag, der nicht später beginnt."""
    tag = tag.isoformat() if isinstance(tag, date) else tag
    gilt = None
    for e in preise:
        if e["ab"] <= tag:
            gilt = e["wert"]
    return gilt


def wechsel_ts(preise: list[dict]) -> list[int]:
    """Unix-Zeiten der Preiswechsel um 0 Uhr Ortszeit, ohne „Anfang“."""
    return [int(datetime.combine(date.fromisoformat(e["ab"]), dtime()).astimezone()
                .timestamp())
            for e in preise if e["ab"] != ANFANG]


def grundpreis_jahr(preise: list[dict], jahr: int) -> float | None:
    """Grundpreis für ein Kalenderjahr. Wechselt er im Jahr, zählt jeder Tag
    mit seinem Preis anteilig (so rechnen Versorger ab)."""
    if not preise:
        return None
    erster = date(jahr, 1, 1)
    tage = 366 if calendar.isleap(jahr) else 365
    summe = sum((preis_am(preise, erster + timedelta(days=i)) or 0.0)
                for i in range(tage))
    return round(summe / tage, 2)


def _schema(conn: sqlite3.Connection) -> None:
    """Legt die Tabelle an und hebt ältere Fassungen an: (ab, euro_kwh) ohne
    Art, und die kurzlebige mit Grundpreis je Monat (×12 → je Jahr)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='prices'").fetchone()
    if row and "'jahr'" not in row[0]:
        conn.execute("ALTER TABLE prices RENAME TO prices_alt")
        conn.executescript(SCHEMA)
        if "euro_kwh" in row[0]:
            conn.execute("INSERT INTO prices SELECT 'kwh', ab, euro_kwh, updated_ts "
                         "FROM prices_alt")
        else:
            conn.execute("INSERT INTO prices SELECT CASE art WHEN 'monat' THEN 'jahr' "
                         "ELSE art END, ab, CASE art WHEN 'monat' THEN wert * 12 "
                         "ELSE wert END, updated_ts FROM prices_alt")
        conn.execute("DROP TABLE prices_alt")
    conn.executescript(SCHEMA)


def setzen(pfad, art: str, ab: str, wert: float | None) -> None:
    """Setzt einen Preis ab einem Tag (ANFANG = von Anfang an); None löscht ihn."""
    if art not in ARTEN:
        raise ValueError(f"art muss einer von {', '.join(ARTEN)} sein")
    if ab != ANFANG:
        try:
            date.fromisoformat(ab)
        except ValueError:
            raise ValueError("Datum muss JJJJ-MM-TT sein") from None
    if wert is not None:
        untergrenze_ok = wert > 0 if art == "kwh" else wert >= 0
        if not (untergrenze_ok and wert < HOECHST[art]):
            raise ValueError(f"Preis muss zwischen 0 und {HOECHST[art]:g} € liegen")
    conn = sqlite3.connect(pfad, timeout=10)
    try:
        _schema(conn)
        if wert is None:
            conn.execute("DELETE FROM prices WHERE art = ? AND ab = ?", (art, ab))
        else:
            conn.execute(
                "INSERT INTO prices (art, ab, wert, updated_ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(art, ab) DO UPDATE SET wert=excluded.wert, "
                "updated_ts=excluded.updated_ts",
                (art, ab, round(wert, 5), int(datetime.now().timestamp())))
        conn.commit()
    finally:
        conn.close()
