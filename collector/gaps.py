"""Lücken in der Aufzeichnung — Pi ohne Strom, abgestürzt, Box nicht erreichbar.

Der Zählerstand läuft im Lesekopf weiter, die Energie einer Lücke ist also
bekannt: die Differenz der Stände davor und danach. Unbekannt ist nur, wann
sie verbraucht wurde. Sie wird nach dem typischen Stundenprofil verteilt
(Median je Stunde der letzten 8 Wochen, Werktag und Wochenende getrennt):
der Abend mit Kochen bekommt viel, die Nacht wenig. Ausgewiesen als geschätzt;
Summen über die ganze Lücke bleiben exakt.

Ohne diese Verteilung landete alles beim ersten Stand danach: nach einer Nacht
ohne Strom war der Vortag zu niedrig und der Tag danach zu hoch.

Die Leistungskurve einer Lücke ist verloren (die Box puffert nur eine Stunde),
sie wird nicht erfunden. Stdlib, damit API, days.py und report.py es teilen.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from datetime import datetime, timedelta

# Länger ohne Zählerstand ist kein Normalbetrieb: der Zähler meldet etwa alle
# 2 Minuten, und selbst bei 50 W Grundlast ändert sich der Stand laufend.
LUECKE_S = 900
PROFIL_TAGE = 56
_PROFIL: dict = {"zeit": 0.0, "werte": {}}   # je API-Prozess, alle 6 h neu


def _profil(conn: sqlite3.Connection) -> dict[tuple[bool, int], float]:
    """{(wochenende, stunde): typische Wh} — Median über Tage ohne Lücke."""
    if time.time() - _PROFIL["zeit"] < 6 * 3600:
        return _PROFIL["werte"]
    seit = int(time.time()) - PROFIL_TAGE * 86400
    rows = conn.execute(
        "SELECT date(ts,'unixepoch','localtime') d, strftime('%w',ts,'unixepoch','localtime') w, "
        "CAST(strftime('%H',ts,'unixepoch','localtime') AS INTEGER) h, "
        "MAX(energy_wh)-MIN(energy_wh) FROM counter_readings WHERE ts > ? "
        "GROUP BY d, h", (seit,)).fetchall()
    schlecht = {datetime.fromtimestamp(t).strftime("%Y-%m-%d")
                for l in luecken(conn, seit, int(time.time()), profil=False)
                for t in range(l["von"], l["bis"] + 3600, 3600)}
    je: dict[tuple[bool, int], list[float]] = {}
    for d, w, h, wh in rows:
        if d not in schlecht:
            je.setdefault((w in ("0", "6"), h), []).append(wh or 0.0)
    _PROFIL.update(zeit=time.time(),
                   werte={k: statistics.median(v) for k, v in je.items() if len(v) >= 3})
    return _PROFIL["werte"]


def _gewicht(conn: sqlite3.Connection, a: int, b: int) -> float:
    """Typischer Verbrauch zwischen a und b nach Profil (Wh); ohne Profil die
    Dauer — dann wird gleichmäßig verteilt."""
    profil, summe, t = _profil(conn), 0.0, a
    while t < b:
        d = datetime.fromtimestamp(t)
        ende = min(b, t + 3600 - (d.minute * 60 + d.second))
        wh = profil.get((d.weekday() >= 5, d.hour)) if profil else None
        summe += (ende - t) / 3600 * (max(wh, 1.0) if wh is not None else 50.0)
        t = ende
    return summe


def _tagesstuecke(von: int, bis: int) -> list[tuple[int, int]]:
    """[von, bis) an lokalen Mitternächten geteilt."""
    stuecke, t = [], von
    while t < bis:
        # Über das Kalenderdatum: „+ 86400 s“ verfehlt Mitternacht am Tag
        # der Zeitumstellung (25 h) und lief dort endlos.
        morgen = datetime.fromtimestamp(t).date() + timedelta(days=1)
        ende = min(bis, int(datetime.combine(morgen, datetime.min.time()).timestamp()))
        stuecke.append((t, ende))
        t = ende
    return stuecke


def anteil(conn: sqlite3.Connection, luecke: dict, von: int, bis: int) -> float:
    """Anteil der Lückenenergie, der in [von, bis) fällt.

    Reicht die Lücke über Mitternacht, bekommen die Tage danach ihren
    typischen Verbrauch (Aufstehen, Kaffee — jeden Tag fast gleich), und der
    Rest geht an den Tag, an dem die Lücke begann: was vom Üblichen abweicht,
    passiert eher abends (Spülmaschine, Fernseher) als morgens. Innerhalb
    eines Tages verteilt das Profil."""
    a, b = max(luecke["von"], von), min(luecke["bis"], bis)
    if b <= a or luecke["wh"] <= 0:
        return 0.0
    stuecke = _tagesstuecke(luecke["von"], luecke["bis"])
    typisch = [_gewicht(conn, x, y) for x, y in stuecke]
    spaeter = sum(typisch[1:])
    if spaeter >= luecke["wh"]:            # weniger als üblich: anteilig kürzen
        energie = [0.0] + [luecke["wh"] * t / spaeter for t in typisch[1:]]
    else:
        energie = [luecke["wh"] - spaeter] + typisch[1:]
    summe = 0.0
    for (x, y), e, t in zip(stuecke, energie, typisch):
        teil_a, teil_b = max(x, a), min(y, b)
        if teil_b > teil_a and t > 0:
            summe += e * _gewicht(conn, teil_a, teil_b) / t
    return summe / luecke["wh"]


def stand(conn: sqlite3.Connection, ts: int) -> tuple[float, bool] | None:
    """Zählerstand in Wh zum Zeitpunkt ts: (wh, geschätzt). Liegt ts in einer
    Lücke, wird zwischen den Ständen davor und danach linear interpoliert.
    None, wenn es vor ts keinen Stand gibt."""
    vor = conn.execute("SELECT ts, energy_wh FROM counter_readings WHERE ts < ? "
                       "ORDER BY ts DESC LIMIT 1", (ts,)).fetchone()
    if vor is None:
        return None
    nach = conn.execute("SELECT ts, energy_wh FROM counter_readings WHERE ts >= ? "
                        "ORDER BY ts ASC LIMIT 1", (ts,)).fetchone()
    if nach is None or nach[0] - vor[0] <= LUECKE_S:
        return float(vor[1]), False
    l = {"von": vor[0], "bis": nach[0], "wh": nach[1] - vor[1]}
    return vor[1] + anteil(conn, l, vor[0], ts) * (nach[1] - vor[1]), True


def luecken(conn: sqlite3.Connection, von: int, bis: int,
            profil: bool = True) -> list[dict]:
    """Lücken, die [von, bis) berühren: [{von, bis, wh}], älteste zuerst."""
    rows = conn.execute(
        "SELECT a, b, wb - wa FROM (SELECT ts a, energy_wh wa, "
        "LEAD(ts) OVER (ORDER BY ts) b, LEAD(energy_wh) OVER (ORDER BY ts) wb "
        "FROM counter_readings WHERE ts >= (SELECT COALESCE(MAX(ts), 0) FROM "
        "counter_readings WHERE ts < ?) AND ts <= (SELECT COALESCE(MIN(ts), ?) "
        "FROM counter_readings WHERE ts >= ?)) WHERE b - a > ? AND b > ? AND a < ?",
        (von, bis, bis, LUECKE_S, von, bis)).fetchall()
    return [{"von": a, "bis": b, "wh": wh} for a, b, wh in rows]


def ueberlappung_s(luecke: dict, von: int, bis: int) -> int:
    return max(0, min(luecke["bis"], bis) - max(luecke["von"], von))


def kwh(conn: sqlite3.Connection, von: int, bis: int) -> tuple[float | None, float]:
    """Verbrauch in [von, bis) aus Zählerständen: (kWh, davon geschätzt in kWh).
    Geschätzt ist nur der Anteil von Lücken, die über eine Grenze des
    Zeitraums reichen — liegt eine Lücke ganz darin, ist die Summe exakt."""
    a, b = stand(conn, von), stand(conn, bis)
    if a is None or b is None:
        return None, 0.0
    geschaetzt = sum(l["wh"] * anteil(conn, l, von, bis)
                     for l in luecken(conn, von, bis)
                     if l["von"] < von or l["bis"] > bis)
    return round((b[0] - a[0]) / 1000, 3), round(geschaetzt / 1000, 3)
