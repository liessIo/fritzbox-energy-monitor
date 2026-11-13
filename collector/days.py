"""Tagestypen: wer war wann zuhause — erkannt aus dem Lastverlauf.

Nur Standardbibliothek und eine SQLite-Verbindung, damit API, Prognose und
report.py dasselbe Modul nutzen können, ohne fritzbox.py zu importieren.

Vier Typen:
  weg         alle weg: nur Grundlast, kein Kochen, kein Abend
  homeoffice  Werktag mit Laptops und Monitor: die Dauerlast tagsüber liegt
              deutlich über der Nacht
  unterwegs   Werktag ohne diese Dauerlast tagsüber
  zuhause     Wochenende oder Feiertag (Hamburg)

Kalibriert an 88 vollen Tagen (Aug.–Nov. 2026), darunter ein ganzer Monat
Abwesenheit. Die Merkmale:
  - Dauerlast tagsüber minus Nacht (10-%-Wert 9–17 Uhr minus 10-%-Wert 1–5 Uhr).
    Homeoffice-Tage liegen bei 66–124 W darüber, die anderen Werktage bei
    höchstens 52 W. Die Lücke dazwischen trägt die Grenze von 60 W; der
    Bereich 38–60 W ist als unsicher markiert, dort liegen halbe Tage.
  - Abend (Median 18–23 Uhr) minus Nacht. Im Urlaub höchstens 64 W, weil nur
    Kühlschränke laufen; bewohnt in jedem Fall über 80 W.
  - Spitzen über 1500 W zwischen 6 und 23 Uhr (Wasserkocher, Herd). Im Urlaub
    keine einzige, am Abreisetag eine.
  - Tagesverbrauch aus Zählerdifferenzen. Im Urlaub 1,7–2,4 kWh, bewohnt nie
    unter 3,0.

Die Leistung dient hier nur als Merkmal für die Einordnung, nie als Energie:
Energie kommt ausschließlich aus dem Zähler.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

TYPEN = ("weg", "homeoffice", "unterwegs", "zuhause")

HOMEOFFICE_W = 60        # Dauerlast tagsüber über der Nacht, ab hier Homeoffice
UNSICHER_AB_W = 38       # darunter sicher unterwegs, darüber bis HOMEOFFICE_W unsicher
WEG_ABEND_W = 70         # Abend über der Nacht, darunter wohnt niemand
WEG_KWH = 2.6            # Tagesverbrauch, darüber war jemand da
SPITZE_W = 1500          # Wasserkocher, Herd, Waschmaschine
NACHT_ERSATZ_W = 55      # wenn die Nacht fehlt: typische Grundlast
MIN_PROBEN = 1000        # ein voller Tag hat 1100–1900 Messwerte


def _feiertage_hamburg(jahr: int) -> set[date]:
    """Gesetzliche Feiertage in Hamburg. Ostern nach der Gaußschen Formel
    (Variante von Meeus/Jones/Butcher)."""
    a, b, c = jahr % 19, jahr // 100, jahr % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = (h + l - 7 * m + 114) % 31 + 1
    ostern = date(jahr, monat, tag)
    return {
        date(jahr, 1, 1), date(jahr, 5, 1), date(jahr, 10, 3), date(jahr, 10, 31),
        date(jahr, 12, 25), date(jahr, 12, 26),
        ostern - timedelta(days=2), ostern + timedelta(days=1),
        ostern + timedelta(days=39), ostern + timedelta(days=50),
    }


def _quantil(werte: list[float], q: float) -> float | None:
    if not werte:
        return None
    werte = sorted(werte)
    return werte[int(q * (len(werte) - 1))]


def _grenze(tag: date) -> int:
    """Lokale Mitternacht als Unix-Sekunden, auch über die Zeitumstellung."""
    return int(datetime.combine(tag, time()).astimezone().timestamp())


def _zaehler_vor(conn: sqlite3.Connection, ts: int) -> tuple[int, float] | None:
    row = conn.execute(
        "SELECT ts, energy_wh FROM counter_readings WHERE ts < ? "
        "ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    return (row[0], row[1]) if row else None


def einordnen(merkmale: dict, tag: date) -> tuple[str, bool, str]:
    """(typ, sicher, begruendung) aus den Merkmalen eines Tages."""
    nacht = merkmale["nacht_w"] if merkmale["nacht_w"] is not None else NACHT_ERSATZ_W
    abend = merkmale["abend_w"]
    tagsueber = merkmale["tag_w"]
    kwh = merkmale["kwh"]
    spitzen = merkmale["spitzen"]
    vollstaendig = merkmale["proben"] >= MIN_PROBEN and kwh is not None

    abend_plus = (abend - nacht) if abend is not None else None
    if (spitzen <= 1 and abend_plus is not None and abend_plus < WEG_ABEND_W
            and (kwh is None or kwh < WEG_KWH)):
        sicher = vollstaendig and abend_plus < WEG_ABEND_W - 15
        return "weg", sicher, (f"nur Grundlast, {spitzen} Spitze{'' if spitzen == 1 else 'n'}, "
                               f"abends {abend_plus:+.0f} W über der Nacht")

    frei = tag.weekday() >= 5 or tag in _feiertage_hamburg(tag.year)
    if frei:
        return "zuhause", vollstaendig, ("Feiertag" if tag.weekday() < 5
                                         else "Wochenende")

    if tagsueber is None:
        return "unterwegs", False, "keine Messwerte tagsüber"
    plus = tagsueber - nacht
    if plus >= HOMEOFFICE_W:
        return "homeoffice", vollstaendig, f"tagsüber {plus:+.0f} W Dauerlast"
    sicher = vollstaendig and plus < UNSICHER_AB_W
    return "unterwegs", sicher, f"tagsüber nur {plus:+.0f} W Dauerlast"


def tage(conn: sqlite3.Connection, n: int, heute: date | None = None,
         korrekturen: dict[str, str] | None = None) -> list[dict]:
    """Die letzten n Tage bis einschließlich heute, ältester zuerst.

    Heute ist nie sicher: bis zum Abend kann noch alles kommen. Vor Mittag
    bleibt der Typ offen (None), außer es sieht nach „weg“ aus.
    Eine Korrektur ersetzt den Typ und gilt als sicher; der erkannte Typ
    bleibt unter "erkannt" erhalten.
    """
    heute = heute or datetime.now().astimezone().date()
    erster = heute - timedelta(days=n - 1)
    von, bis = _grenze(erster), _grenze(heute + timedelta(days=1))

    # Ortszeit rechnet SQLite um, das ist auf dem Pi ein Vielfaches schneller
    # als datetime je Messwert.
    proben: dict[date, list[tuple[int, float]]] = {}
    for tag_text, stunde, power_w in conn.execute(
            "SELECT date(ts,'unixepoch','localtime'), "
            "CAST(strftime('%H',ts,'unixepoch','localtime') AS INTEGER), power_w "
            "FROM power_samples WHERE ts >= ? AND ts < ? ORDER BY ts", (von, bis)):
        proben.setdefault(date.fromisoformat(tag_text), []).append((stunde, power_w))

    ergebnis = []
    for i in range(n):
        tag = erster + timedelta(days=i)
        reihe = proben.get(tag, [])
        if not reihe:
            continue
        start, ende = _grenze(tag), _grenze(tag + timedelta(days=1))

        # Zählerdifferenz wie in /consumption, aber nur, wenn der Basisstand
        # unmittelbar vor Mitternacht liegt — sonst wäre es eine Lücke.
        kwh = None
        basis, letzter = _zaehler_vor(conn, start), _zaehler_vor(conn, ende)
        if basis and letzter and start - basis[0] < 3600 and letzter[0] >= start:
            kwh = round((letzter[1] - basis[1]) / 1000, 3)

        spitzen, vorher = 0, False
        for stunde, p in reihe:
            hoch = p > SPITZE_W and 6 <= stunde < 23
            spitzen += hoch and not vorher
            vorher = hoch

        merkmale = {
            "kwh": kwh,
            "proben": len(reihe),
            "nacht_w": _quantil([p for s, p in reihe if 1 <= s < 5], .1),
            "tag_w": _quantil([p for s, p in reihe if 9 <= s < 17], .1),
            "abend_w": _quantil([p for s, p in reihe if 18 <= s < 23], .5),
            "spitzen": spitzen,
        }
        typ, sicher, grund = einordnen(merkmale, tag)
        if tag == heute:
            sicher = False
            # Vor Mittag sind kaum Stunden des Tagesfensters gemessen.
            if datetime.now().astimezone().hour < 12 and typ != "weg":
                typ, grund = None, "noch zu früh am Tag"
        eintrag = {"tag": tag.isoformat(), "wochentag": tag.weekday(),
                   "typ": typ, "sicher": sicher, "grund": grund,
                   "laufend": tag == heute,
                   # Was die Erkennung allein sagt — bleibt sichtbar, auch
                   # wenn eine Korrektur den Typ überschreibt.
                   "erkannt": typ, "korrigiert": False, **merkmale}
        if korrekturen and tag.isoformat() in korrekturen:
            eintrag.update(typ=korrekturen[tag.isoformat()], sicher=True,
                           korrigiert=True)
        ergebnis.append(eintrag)
    return ergebnis


# --------------------------------------------------------------------------
# Korrekturen — eigene Datei, getrennt von den Messdaten
# --------------------------------------------------------------------------
# Die Messdatenbank bleibt read-only für die API. Korrekturen sind das Einzige,
# was je über die API geschrieben wird, und liegen deshalb in einer eigenen
# Datei: ein Fehler beim Schreiben kann die Messwerte nicht berühren.

KORREKTUR_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_labels (
  tag        TEXT PRIMARY KEY,   -- YYYY-MM-DD, Ortszeit
  typ        TEXT NOT NULL CHECK (typ IN ('weg','homeoffice','unterwegs','zuhause')),
  updated_ts INTEGER NOT NULL
);
"""


def korrekturen_lesen(pfad) -> dict[str, str]:
    """Alle Korrekturen {tag: typ}. Fehlt die Datei, gibt es noch keine."""
    try:
        conn = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return {}
    try:
        return dict(conn.execute("SELECT tag, typ FROM day_labels"))
    except sqlite3.OperationalError:   # Datei da, Tabelle noch nicht
        return {}
    finally:
        conn.close()


def korrektur_setzen(pfad, tag: date, typ: str | None) -> None:
    """Setzt den Typ eines Tages; None nimmt die Korrektur zurück."""
    if typ is not None and typ not in TYPEN:
        raise ValueError(f"unbekannter Typ: {typ}")
    conn = sqlite3.connect(pfad, timeout=10)
    try:
        conn.executescript(KORREKTUR_SCHEMA)
        if typ is None:
            conn.execute("DELETE FROM day_labels WHERE tag = ?", (tag.isoformat(),))
        else:
            conn.execute(
                "INSERT INTO day_labels (tag, typ, updated_ts) VALUES (?, ?, ?) "
                "ON CONFLICT(tag) DO UPDATE SET typ=excluded.typ, "
                "updated_ts=excluded.updated_ts",
                (tag.isoformat(), typ, int(datetime.now().timestamp())))
        conn.commit()
    finally:
        conn.close()
