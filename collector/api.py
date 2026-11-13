#!/usr/bin/env python3
"""Phase 3 — read-only JSON API over the collected data.

The measurement database is never written: its connection runs with PRAGMA
query_only, so even a mistake cannot mutate it. The one exception to "read-only"
are PUT /days/{tag} and PUT /prices/{ab} — the user's corrections of the
detected day type and the electricity price with its start date. Both go to a
separate file (labels.db), need their own key (LABEL_TOKEN), and cannot touch
the measurements. Bucketing happens server-side so the Mac never pulls
raw rows.

Usage:
    python api.py            # serves on API_HOST:API_PORT from fritzbox.env
"""

from __future__ import annotations

import calendar
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

import days
import fritzbox
import prices

DEFAULT_DB = Path(__file__).resolve().parent / "data.db"
WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_POINTS = 2000     # hard ceiling on /history response size
STALE_AFTER_S = 600   # five missed live cycles → not ok
FORECAST_DAYS = 60    # Vorbildtage der Prognose; ältere wiegen ohnehin < 15 %
FORECAST_HALF_LIFE_DAYS = 21

CONFIG = fritzbox.load_config()
DB_PATH = Path(CONFIG.get("DB_PATH") or DEFAULT_DB)
API_TOKEN = CONFIG.get("API_TOKEN") or ""
# Korrekturen der Tagestypen: eigene Datei, eigener Schlüssel. Ohne Schlüssel
# ist das Schreiben abgeschaltet.
LABELS_PATH = Path(CONFIG.get("LABELS_DB_PATH") or DB_PATH.with_name("labels.db"))
LABEL_TOKEN = CONFIG.get("LABEL_TOKEN") or ""
# Gilt von Anfang an, solange auf der Webseite kein eigener Preis eingetragen ist.
GRUNDPREIS = float(CONFIG.get("PRICE_PER_KWH") or 0.30)

app = FastAPI(title="FRITZ!Box Energy Monitor", version="3", docs_url=None,
              redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync dependencies and sync endpoints
    # on its threadpool, and not necessarily on the same worker thread — which
    # made requests fail intermittently with a ProgrammingError. Each request
    # still gets its own connection, so no connection is ever shared.
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")  # the API can never write
    try:
        yield conn
    finally:
        conn.close()


def require_token(request: Request) -> None:
    """Bearer auth, only if API_TOKEN is configured."""
    if not API_TOKEN:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token != API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing token")


auth = [Depends(require_token)]


def require_label_token(request: Request) -> None:
    """Schreiben nur mit LABEL_TOKEN — anders als beim Lesen auch im Heimnetz."""
    if not LABEL_TOKEN:
        raise HTTPException(status_code=403, detail="Korrekturen sind abgeschaltet "
                                                    "(LABEL_TOKEN nicht gesetzt)")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, LABEL_TOKEN):
        raise HTTPException(status_code=401, detail="falscher oder fehlender Schlüssel")


def meta_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def preise_lesen() -> list[dict]:
    return prices.lesen(LABELS_PATH, GRUNDPREIS)


def kosten(conn: sqlite3.Connection, start: int, end: int, base_wh: float | None,
           preise: list[dict]) -> float | None:
    """Euro für den Verbrauch ab base_wh bis end. Der Zeitraum wird an jedem
    Preiswechsel geteilt, jedes Stück aus Zählerdifferenzen mit seinem Preis —
    ein Monat mit Tarifwechsel am 15. kostet so genau, was er gekostet hat."""
    if base_wh is None:
        return None
    punkte = [start] + [ts for ts, _ in prices.wechsel_ts(preise)
                        if start < ts < end] + [end]
    euro, vorher = 0.0, base_wh
    for a, b in zip(punkte, punkte[1:]):
        row = conn.execute(
            "SELECT energy_wh FROM counter_readings WHERE ts < ? "
            "ORDER BY ts DESC LIMIT 1", (b,)).fetchone()
        stand = max(row["energy_wh"], vorher) if row else vorher
        preis = prices.preis_am(preise, datetime.fromtimestamp(a).astimezone().date())
        if preis is None:
            return None
        euro += (stand - vorher) / 1000 * preis
        vorher = stand
    return round(euro, 2)


def auto_bucket(span: int) -> int:
    """Pick a bucket that keeps the response at a few hundred points."""
    for limit, bucket in ((3600, 120), (86400, 300), (7 * 86400, 3600),
                          (30 * 86400, 21600)):
        if span <= limit:
            return bucket
    return 86400


def period_starts(period: str, n: int) -> list[int]:
    """Local-time start of each of the last n days/months, oldest first."""
    # Über Kalenderdaten gehen, nicht über datetime-Arithmetik: die behielt
    # den UTC-Versatz von heute bei, und jenseits der Zeitumstellung begann
    # jeder Tag und Monat um 1 Uhr statt um Mitternacht.
    today = datetime.now().astimezone().date()
    if period == "day":
        tage = [today - timedelta(days=i) for i in range(n)]
    else:
        tage, jahr, monat = [], today.year, today.month
        for _ in range(n):
            tage.append(date(jahr, monat, 1))
            jahr, monat = (jahr, monat - 1) if monat > 1 else (jahr - 1, 12)
    return sorted(int(datetime.combine(t, datetime.min.time()).astimezone().timestamp())
                  for t in tage)


# --------------------------------------------------------------------------
# routes — read-only, except PUT /days and /prices (labels.db, own key)
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Die Weboberfläche. Eine einzelne Datei, keine Fremdbibliotheken —
    die Seite muss auch ohne Internetzugang funktionieren."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health", dependencies=auth)
def health(conn: sqlite3.Connection = Depends(db)) -> dict:
    last_harvest = meta_value(conn, "last_harvest_ts")
    last_harvest_ts = int(last_harvest) if last_harvest else None
    last_live = meta_value(conn, "last_live_ts")
    last_live_ts = int(last_live) if last_live else None
    battery = meta_value(conn, "battery")
    batterylow = meta_value(conn, "batterylow")
    return {
        # Health follows the 2-minute cycle: a stalled harvester shows up in
        # minutes, not after the next 50-minute stats run would have been due.
        "ok": last_live_ts is not None
              and time.time() - last_live_ts < STALE_AFTER_S,
        "last_harvest_ts": last_harvest_ts,
        "last_live_ts": last_live_ts,
        "sample_count": conn.execute(
            "SELECT COUNT(*) AS n FROM power_samples").fetchone()["n"],
        # Oldest sample: how far back the web interface may scroll.
        "first_ts": conn.execute(
            "SELECT MIN(ts) AS t FROM power_samples").fetchone()["t"],
        "battery": int(battery) if battery else None,
        "batterylow": int(batterylow) if batterylow else None,
        "last_error": meta_value(conn, "last_error") or None,
    }


@app.get("/latest", dependencies=auth)
def latest(conn: sqlite3.Connection = Depends(db)) -> dict:
    """Newest power reading plus the newest counter.

    The stats buffer is only harvested every 50 minutes, so its newest sample
    can be that old. The 2-minute live poll is preferred whenever it is fresher
    — otherwise the menu bar would claim a stale number is current.
    """
    sample = conn.execute(
        "SELECT ts, power_w FROM power_samples ORDER BY ts DESC LIMIT 1").fetchone()
    counter = conn.execute(
        "SELECT ts, energy_wh FROM counter_readings "
        "ORDER BY ts DESC LIMIT 1").fetchone()

    live_ts = meta_value(conn, "live_ts")
    live_power = meta_value(conn, "live_power_w")
    ts = sample["ts"] if sample else None
    power_w = sample["power_w"] if sample else None
    is_live = False
    if live_ts and live_power and (ts is None or int(live_ts) > ts):
        ts, power_w, is_live = int(live_ts), float(live_power), True

    if ts is None:
        raise HTTPException(status_code=503, detail="no readings yet")
    return {
        "ts": ts,
        "power_w": power_w,
        "live": is_live,
        "energy_wh": counter["energy_wh"] if counter else None,
        "energy_ts": counter["ts"] if counter else None,
    }


@app.get("/history", dependencies=auth)
def history(
    from_: int = Query(..., alias="from", description="unix seconds, inclusive"),
    to: int = Query(..., description="unix seconds, inclusive"),
    bucket: int | None = Query(None, ge=1, description="seconds; auto if omitted"),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    if to < from_:
        raise HTTPException(status_code=400, detail="to must be >= from")
    size = bucket or auto_bucket(to - from_)
    points = (to - from_) // size + 1
    if points > MAX_POINTS:
        raise HTTPException(
            status_code=400,
            detail=f"{points} buckets exceeds the limit of {MAX_POINTS}; "
                   f"use bucket >= {(to - from_) // MAX_POINTS + 1}")
    rows = conn.execute(
        "SELECT ts / ? * ? AS bucket_ts, AVG(power_w) AS power_avg, "
        "MIN(power_w) AS power_min, MAX(power_w) AS power_max, "
        "COUNT(*) AS samples FROM power_samples "
        "WHERE ts BETWEEN ? AND ? GROUP BY bucket_ts ORDER BY bucket_ts",
        (size, size, from_, to)).fetchall()
    return [{"ts": r["bucket_ts"], "power_avg": round(r["power_avg"], 2),
             "power_min": r["power_min"], "power_max": r["power_max"],
             "samples": r["samples"]} for r in rows]


@app.get("/devices", dependencies=auth)
def devices(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    """Which plugs exist, and what each one reads right now."""
    rows = conn.execute(
        # Zuletzt gemessenes Geraet zuerst: wandert die Steckdose an ein anderes
        # Geraet, zeigt die App das neue statt der eingefrorenen alten Kurve.
        "SELECT device, COUNT(*) AS samples, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM device_samples GROUP BY device ORDER BY last_ts DESC").fetchall()
    result = []
    for row in rows:
        latest = conn.execute(
            "SELECT power_w FROM device_samples WHERE device = ? "
            "ORDER BY ts DESC LIMIT 1", (row["device"],)).fetchone()
        counter = conn.execute(
            "SELECT ts, energy_wh FROM device_counters WHERE device = ? "
            "ORDER BY ts DESC LIMIT 1", (row["device"],)).fetchone()
        error = meta_value(conn, f"device_error_{row['device']}")
        result.append({
            "device": row["device"],
            "samples": row["samples"],
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "power_w": latest["power_w"] if latest else None,
            "energy_wh": counter["energy_wh"] if counter else None,
            "energy_ts": counter["ts"] if counter else None,
            "last_error": error or None,
        })
    return result


@app.get("/device/history", dependencies=auth)
def device_history(
    device: str = Query(..., min_length=1, max_length=64),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    bucket: int | None = Query(None, ge=1),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """Same shape as /history, for one plug.

    Deliberately a separate route rather than a parameter on /history: a plug
    measures a subset of the house total, and the two series must never end up
    added together by accident.
    """
    if to < from_:
        raise HTTPException(status_code=400, detail="to must be >= from")
    size = bucket or auto_bucket(to - from_)
    points = (to - from_) // size + 1
    if points > MAX_POINTS:
        raise HTTPException(
            status_code=400,
            detail=f"{points} buckets exceeds the limit of {MAX_POINTS}; "
                   f"use bucket >= {(to - from_) // MAX_POINTS + 1}")
    rows = conn.execute(
        "SELECT ts / ? * ? AS bucket_ts, AVG(power_w) AS power_avg, "
        "MIN(power_w) AS power_min, MAX(power_w) AS power_max, "
        "COUNT(*) AS samples FROM device_samples "
        "WHERE device = ? AND ts BETWEEN ? AND ? "
        "GROUP BY bucket_ts ORDER BY bucket_ts",
        (size, size, device, from_, to)).fetchall()
    return [{"ts": r["bucket_ts"], "power_avg": round(r["power_avg"], 2),
             "power_min": r["power_min"], "power_max": r["power_max"],
             "samples": r["samples"]} for r in rows]


@app.get("/consumption", dependencies=auth)
def consumption(
    period: str = Query("day", pattern="^(day|month)$"),
    n: int = Query(30, ge=1, le=366),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """kWh per period from counter deltas — exact, immune to sampling gaps."""
    starts = period_starts(period, n)
    bounds = starts + [int(time.time()) + 1]
    preise = preise_lesen()
    result = []
    for i, start in enumerate(starts):
        end = bounds[i + 1]
        base = conn.execute(
            "SELECT ts, energy_wh FROM counter_readings WHERE ts < ? "
            "ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
        last = conn.execute(
            "SELECT ts, energy_wh FROM counter_readings WHERE ts >= ? AND ts < ? "
            "ORDER BY ts DESC LIMIT 1", (start, end)).fetchone()

        # Kein Stand vor dem Zeitraum: die Aufzeichnung begann mittendrin. Statt
        # den Zeitraum ganz zu verschweigen, ab dem ersten vorhandenen Stand
        # rechnen — echte Zaehlerdifferenz, nur ueber eine kuerzere Strecke.
        # Das MUSS gekennzeichnet werden, sonst liest sich ein Rumpfmonat wie
        # ein voller und der Vergleich mit den Nachbarmonaten wird falsch.
        partial_from = None
        if base is None and last is not None:
            first = conn.execute(
                "SELECT ts, energy_wh FROM counter_readings "
                "WHERE ts >= ? AND ts < ? ORDER BY ts ASC LIMIT 1",
                (start, end)).fetchone()
            if first is not None and last["ts"] > first["ts"]:
                base, partial_from = first, first["ts"]

        kwh = None
        if base is not None and last is not None:
            kwh = round((last["energy_wh"] - base["energy_wh"]) / 1000.0, 3)
        result.append({
            "period": period,
            "start_ts": start,
            "start": datetime.fromtimestamp(start, tz=timezone.utc)
                     .astimezone().strftime("%Y-%m-%d"),
            "kwh": kwh,
            # Mit dem Preis, der damals galt — nicht mit dem heutigen.
            "euro": kosten(conn, base["ts"] if partial_from else start, end,
                           base["energy_wh"], preise) if kwh is not None else None,
            "counter_wh": last["energy_wh"] if last else None,
            # None = der Zeitraum ist vollstaendig abgedeckt.
            "partial_from_ts": partial_from,
        })
    return result


@app.get("/forecast", dependencies=auth)
def forecast(conn: sqlite3.Connection = Depends(db)) -> dict:
    """Hochrechnung für Tag, Woche und Monat — ausschließlich aus Zählerdifferenzen.

    Die Tagesform ist nicht gleichmäßig: morgens und abends wird gekocht, nachts
    läuft nur die Grundlast. Deshalb wird nicht linear hochgerechnet ("bis jetzt
    3 kWh in 6 Stunden, also 12 am Tag"), sondern mit dem gemittelten Stundenprofil
    der bisherigen vollen Tage. Der Rest des Tages wird mit dem Tagesmittel
    gewichtet nach dem noch ausstehenden Anteil angesetzt — das ist früh am Tag
    stabil und läuft gegen Mitternacht auf den Istwert zu.
    """
    now = datetime.now().astimezone()
    preise = preise_lesen()
    price = prices.preis_am(preise, now.date()) or 0.0

    rows = conn.execute(
        "SELECT date(ts,'unixepoch','localtime') AS tag, "
        "CAST(strftime('%H',ts,'unixepoch','localtime') AS INTEGER) AS stunde, "
        "MIN(ts) AS t0, MAX(ts) AS t1, MAX(energy_wh)-MIN(energy_wh) AS wh "
        "FROM counter_readings GROUP BY tag, stunde ORDER BY tag, stunde").fetchall()

    per_day: dict[str, float] = {}
    spans: dict[str, tuple[int, int]] = {}
    per_hour: dict[str, dict[int, float]] = {}
    for row in rows:
        tag = row["tag"]
        per_day[tag] = per_day.get(tag, 0.0) + (row["wh"] or 0.0)
        t0, t1 = spans.get(tag, (row["t0"], row["t1"]))
        spans[tag] = (min(t0, row["t0"]), max(t1, row["t1"]))
        per_hour.setdefault(tag, {})[row["stunde"]] = row["wh"] or 0.0

    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    full_days = sorted(d for d, (t0, t1) in spans.items()
                       if d != today and t1 - t0 > 23.5 * 3600)

    # Welche Tage taugen als Vorbild? Rückgerechnet an 32 bewohnten Tagen
    # (Okt./Nov. 2026): Abwesenheitstage mitzumitteln drückte die Prognose um
    # 1,7 kWh je Tag nach unten; ohne sie bleibt 0,3 kWh, und mit stärker
    # gewichteten neueren Tagen (Halbwertszeit 21 Tage) verschwindet auch das —
    # der Verbrauch steigt zum Winter hin. Wochentag und Tagestyp brachten für
    # die Tagessumme nichts; die übrigen ~1,2 kWh Streuung sind Zufall.
    typen = {t["tag"]: t["typ"] for t in days.tage(
        conn, FORECAST_DAYS, korrekturen=days.korrekturen_lesen(LABELS_PATH))}
    bewohnt = [d for d in full_days if d in typen and typen[d] != "weg"]
    weg = [d for d in full_days if typen.get(d) == "weg"]
    # Gestern und heute weg: dann geht es vermutlich so weiter (Urlaub).
    abwesend = typen.get(today) == "weg" and typen.get(yesterday) == "weg" and bool(weg)
    vorbild = (weg if abwesend else bewohnt) or full_days

    def gewicht(d: str) -> float:
        alter = (now.date() - date.fromisoformat(d)).days
        return 0.5 ** (alter / FORECAST_HALF_LIFE_DAYS)

    gewichte = {d: gewicht(d) for d in vorbild}
    summe = sum(gewichte.values())

    # Stundenprofil: Anteil jeder Stunde am Tagesverbrauch, über die Vorbildtage.
    if len(vorbild) >= 2:
        profile = [0.0] * 24
        for d in vorbild:
            gesamt = per_day[d] or 1.0
            for hour in range(24):
                profile[hour] += gewichte[d] * per_hour[d].get(hour, 0.0) / gesamt
        profile = [p / summe for p in profile]
    else:
        profile = [1 / 24] * 24  # ohne Historie: gleichmäßig verteilt

    elapsed = sum(profile[:now.hour]) + profile[now.hour] * (now.minute / 60)
    elapsed = min(max(elapsed, 0.0), 1.0)

    mean = sum(gewichte[d] * per_day[d] for d in vorbild) / summe if vorbild else 0.0
    sigma = ((sum(gewichte[d] * (per_day[d] - mean) ** 2 for d in vorbild) / summe) ** 0.5
             if len(vorbild) > 1 else 0.0)

    def consumed_since(start: datetime) -> float | None:
        start_ts = int(start.timestamp())
        before = conn.execute(
            "SELECT energy_wh FROM counter_readings WHERE ts < ? "
            "ORDER BY ts DESC LIMIT 1", (start_ts,)).fetchone()
        latest = conn.execute(
            "SELECT energy_wh FROM counter_readings ORDER BY ts DESC LIMIT 1").fetchone()
        if before is None or latest is None:
            return None
        return max(0.0, latest["energy_wh"] - before["energy_wh"])

    # Über Kalenderdaten, damit eine Zeitumstellung in der Woche oder im Monat
    # den Beginn nicht um eine Stunde verschiebt.
    def lokal(d: date) -> datetime:
        return datetime.combine(d, datetime.min.time()).astimezone()

    midnight = lokal(now.date())
    week_start = lokal(now.date() - timedelta(days=now.weekday()))
    month_start = lokal(now.date().replace(day=1))

    day_so_far = consumed_since(midnight)
    week_so_far = consumed_since(week_start)
    month_so_far = consumed_since(month_start)

    rest_today = mean * (1 - elapsed)
    days_left_week = 6 - now.weekday()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left_month = days_in_month - now.day

    def euro(start: datetime, days_left: int) -> float | None:
        """Bisheriges zum damaligen Preis, den Rest zum Preis seines Tages —
        ein zum Monatsersten angekündigter Tarif zählt so schon mit."""
        start_ts = int(start.timestamp())
        before = conn.execute(
            "SELECT energy_wh FROM counter_readings WHERE ts < ? "
            "ORDER BY ts DESC LIMIT 1", (start_ts,)).fetchone()
        bisher = kosten(conn, start_ts, int(time.time()) + 1,
                        before["energy_wh"] if before else None, preise)
        if bisher is None:
            return None
        rest = rest_today / 1000 * price + sum(
            mean / 1000 * (prices.preis_am(preise, now.date() + timedelta(days=k)) or 0)
            for k in range(1, days_left + 1))
        return round(bisher + rest, 2)

    def block(so_far: float | None, days_left: int, label: str,
              start: datetime) -> dict:
        if so_far is None:
            return {"zeitraum": label, "ist_kwh": None, "prognose_kwh": None}
        projected = so_far + rest_today + mean * days_left
        # Unsicherheit wächst mit der Zahl der noch offenen Tage, nicht linear.
        spread = sigma * ((days_left + (1 - elapsed)) ** 0.5)
        return {
            "zeitraum": label,
            "ist_kwh": round(so_far / 1000, 2),
            "prognose_kwh": round(projected / 1000, 2),
            "unten_kwh": round(max(so_far, projected - spread) / 1000, 2),
            "oben_kwh": round((projected + spread) / 1000, 2),
            "prognose_euro": euro(start, days_left),
            "anteil_erledigt": round(
                (so_far / projected) if projected > 0 else 0.0, 3),
        }

    return {
        "tag": block(day_so_far, 0, "Tag", midnight),
        "woche": block(week_so_far, days_left_week, "Woche", week_start),
        "monat": block(month_so_far, days_left_month, "Monat", month_start),
        "basis": {
            "volle_tage": len(full_days),
            "vorbild_tage": len(vorbild),
            "weg_ausgelassen": 0 if abwesend else len(weg),
            "abwesend": abwesend,
            "halbwertszeit_tage": FORECAST_HALF_LIFE_DAYS,
            "mittel_kwh": round(mean / 1000, 2),
            "streuung_kwh": round(sigma / 1000, 2),
            "tagesanteil_verstrichen": round(elapsed, 3),
            "preis_pro_kwh": price,
        },
    }


@app.get("/days", dependencies=auth)
def days_route(
    n: int = Query(42, ge=1, le=366),
    conn: sqlite3.Connection = Depends(db),
) -> list[dict]:
    """Tagestyp je Tag (weg, homeoffice, unterwegs, zuhause): erkannt aus dem
    Lastverlauf (siehe days.py), überschrieben von deinen Korrekturen."""
    return days.tage(conn, n, korrekturen=days.korrekturen_lesen(LABELS_PATH))


@app.put("/days/{tag}", dependencies=[Depends(require_label_token)])
def day_correct(tag: date, typ: str | None = Body(..., embed=True)) -> dict:
    """Die einzige schreibende Route. Setzt den Typ eines Tages, null nimmt die
    Korrektur zurück. Schreibt nur labels.db, nie die Messdaten."""
    if tag > datetime.now().astimezone().date():
        raise HTTPException(status_code=400, detail="Tag liegt in der Zukunft")
    if typ is not None and typ not in days.TYPEN:
        raise HTTPException(status_code=400,
                            detail=f"typ muss einer von {', '.join(days.TYPEN)} sein")
    days.korrektur_setzen(LABELS_PATH, tag, typ)
    return {"tag": tag.isoformat(), "typ": typ}


@app.get("/prices", dependencies=auth)
def prices_route() -> dict:
    """Alle Preise mit Startdatum, dazu der heute gültige."""
    preise = preise_lesen()
    return {"preise": preise, "anfang": prices.ANFANG,
            "heute": prices.preis_am(preise, datetime.now().astimezone().date())}


@app.put("/prices/{ab}", dependencies=[Depends(require_label_token)])
def price_set(ab: str, euro_kwh: float | None = Body(..., embed=True)) -> dict:
    """Preis ab einem Tag setzen (auch künftig), null löscht ihn. ab = ANFANG
    ändert den Preis, der von Beginn der Aufzeichnung an gilt. Schreibt nur
    labels.db."""
    try:
        prices.setzen(LABELS_PATH, ab, euro_kwh)
    except ValueError as fehler:
        raise HTTPException(status_code=400, detail=str(fehler)) from None
    return {"ab": ab, "euro_kwh": euro_kwh}


def load_stats(conn: sqlite3.Connection, table: str, where: str,
               params: tuple) -> dict:
    """Minimum, 5th and 99th percentile and mean power over a selection."""
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN(power_w) AS min_w, AVG(power_w) AS avg_w "
        f"FROM {table} WHERE {where}", params).fetchone()

    def percentile(share: float) -> float | None:
        if not row["n"]:
            return None
        return conn.execute(
            f"SELECT power_w FROM {table} WHERE {where} "
            f"ORDER BY power_w LIMIT 1 OFFSET ?",
            params + (int(share * (row["n"] - 1)),)).fetchone()["power_w"]

    return {"samples": row["n"], "min_w": row["min_w"],
            "p05_w": percentile(0.05),
            # Upper end of normal operation: the web interface pins its axis
            # here while scrolling, so rare spikes do not flatten every day.
            "p99_w": percentile(0.99),
            "avg_w": round(row["avg_w"], 2) if row["avg_w"] is not None else None}


@app.get("/baseline", dependencies=auth)
def baseline(
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Standing load: minimum and 5th percentile power over the window."""
    if to < from_:
        raise HTTPException(status_code=400, detail="to must be >= from")
    return {"from": from_, "to": to,
            **load_stats(conn, "power_samples", "ts BETWEEN ? AND ?", (from_, to))}


@app.get("/device/baseline", dependencies=auth)
def device_baseline(
    device: str = Query(..., min_length=1, max_length=64),
    from_: int = Query(..., alias="from"),
    to: int = Query(...),
    conn: sqlite3.Connection = Depends(db),
) -> dict:
    """Same shape as /baseline, for one plug."""
    if to < from_:
        raise HTTPException(status_code=400, detail="to must be >= from")
    return {"device": device, "from": from_, "to": to,
            **load_stats(conn, "device_samples",
                         "device = ? AND ts BETWEEN ? AND ?", (device, from_, to))}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG.get("API_HOST") or "127.0.0.1",
                port=int(CONFIG.get("API_PORT") or 8000), access_log=False)
