"""Strompreis je kWh mit Gültigkeitsdatum. Stdlib, damit API und report.py ihn
teilen.

Jeder Eintrag gilt ab seinem Tag (Ortszeit, 0 Uhr) bis zum nächsten Eintrag.
Vergangene Zeiträume behalten so ihren damaligen Preis, ein neuer Tarif wird
vorab mit seinem Startdatum eingetragen. Die Preise liegen in labels.db neben
den Tageskorrekturen — nie in data.db, die bleibt unberührt.

Ohne Eintrag gilt PRICE_PER_KWH aus fritzbox.env, und zwar von Anfang an.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as dtime

ANFANG = "2000-01-01"        # Schlüssel für „von Anfang an“
HOECHSTPREIS = 5.0           # € je kWh; alles darüber ist ein Tippfehler

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
  ab         TEXT PRIMARY KEY,   -- YYYY-MM-DD, Ortszeit
  euro_kwh   REAL NOT NULL CHECK (euro_kwh > 0 AND euro_kwh < 5),
  updated_ts INTEGER NOT NULL
);
"""


def lesen(pfad, grundpreis: float = 0.0) -> list[dict]:
    """Alle Preise, älteste zuerst: [{ab, euro_kwh, quelle}]. Der erste gilt
    immer von Anfang an — fehlt ein eigener, springt PRICE_PER_KWH ein."""
    eintraege: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
        try:
            eintraege = [{"ab": ab, "euro_kwh": p, "quelle": "eingetragen"}
                         for ab, p in conn.execute(
                             "SELECT ab, euro_kwh FROM prices ORDER BY ab")]
        finally:
            conn.close()
    except sqlite3.OperationalError:   # Datei oder Tabelle noch nicht da
        pass
    if not eintraege or eintraege[0]["ab"] != ANFANG:
        if grundpreis > 0:
            eintraege.insert(0, {"ab": ANFANG, "euro_kwh": grundpreis,
                                 "quelle": "fritzbox.env"})
        elif eintraege:
            # Kein Grundpreis: der früheste Eintrag gilt auch rückwirkend,
            # sonst hätten ältere Zeiträume gar keinen Preis.
            eintraege[0] = {**eintraege[0], "ab": ANFANG}
    return eintraege


def preis_am(preise: list[dict], tag: str | date) -> float | None:
    """Der am Tag gültige Preis — der letzte Eintrag, der nicht später beginnt."""
    tag = tag.isoformat() if isinstance(tag, date) else tag
    gilt = None
    for e in preise:
        if e["ab"] <= tag:
            gilt = e["euro_kwh"]
    return gilt


def wechsel_ts(preise: list[dict]) -> list[tuple[int, float]]:
    """(Unix-Zeit des Wechsels um 0 Uhr Ortszeit, neuer Preis), ohne „Anfang“."""
    return [(int(datetime.combine(date.fromisoformat(e["ab"]), dtime()).astimezone()
                 .timestamp()), e["euro_kwh"])
            for e in preise if e["ab"] != ANFANG]


def setzen(pfad, ab: str, euro_kwh: float | None) -> None:
    """Setzt den Preis ab einem Tag (ANFANG = von Anfang an); None löscht ihn."""
    if ab != ANFANG:
        date.fromisoformat(ab)   # wirft bei Unsinn
    if euro_kwh is not None and not 0 < euro_kwh < HOECHSTPREIS:
        raise ValueError(f"Preis muss zwischen 0 und {HOECHSTPREIS:g} € liegen")
    conn = sqlite3.connect(pfad, timeout=10)
    try:
        conn.executescript(SCHEMA)
        if euro_kwh is None:
            conn.execute("DELETE FROM prices WHERE ab = ?", (ab,))
        else:
            conn.execute(
                "INSERT INTO prices (ab, euro_kwh, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(ab) DO UPDATE SET euro_kwh=excluded.euro_kwh, "
                "updated_ts=excluded.updated_ts",
                (ab, round(euro_kwh, 5), int(datetime.now().timestamp())))
        conn.commit()
    finally:
        conn.close()
