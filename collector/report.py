#!/usr/bin/env python3
"""Taeglicher Energiebericht und Alarme. Ausgehend, stdlib, read-only.

Laeuft stuendlich. Zwei Aufgaben in einem Lauf:

* **Alarme** — Bedingungen, die auf einen Defekt hindeuten. Jede Bedingung wird
  hoechstens alle ALERT_REPEAT_H Stunden wiederholt, und ihr Ende wird einmal
  als Entwarnung gemeldet.
* **Tagesbericht** — einmal taeglich zur Stunde REPORT_HOUR. Er ist zugleich der
  **Herzschlag**: der Pi haengt an derselben Hausversorgung, die er ueberwacht,
  und kann seinen eigenen Stromausfall darum nicht melden. Bleibt die Mail aus,
  ist Strom, Pi oder Internet weg. Die Abwesenheit ist das Signal.

Energie kommt ausschliesslich aus Zaehlerdifferenzen, nie aus integrierter
Leistung — die Messwerte sind Momentaufnahmen, kein Intervallmittel.
"""

from __future__ import annotations

import json
import os
import smtplib
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "fritzbox.env"
DEFAULT_DB = HERE / "data.db"
STATE_FILE = HERE / "report-state.json"

# Schwellen, kalibriert an sieben Tagen Realbetrieb (2026-08-29..09-05):
# Hauslast fiel nie unter 49 W, kein einziger Zeitraum von 3 h blieb durchgehend
# ueber 300 W, und der Kuehlschrank hatte in 24 h keine einzige taktfreie Stunde.
# Keine der Bedingungen ist im Normalbetrieb je eingetreten.
STALL_MIN = 20            # keine Messung mehr seit ... Minuten -> Collector tot
HOUSE_LOW_W = 20.0        # Hauslast darunter -> Zaehler/Versorgung gestoert
HOUSE_LOW_MIN = 30
HOUSE_HIGH_W = 300.0      # Minimum ueber HOUSE_HIGH_H Stunden -> laeuft dauerhaft
HOUSE_HIGH_H = 3
FRIDGE_CYCLE_W = 15.0     # Kompressorspitze; Zyklen erreichen ~50 W
FRIDGE_SILENT_H = 4
ALERT_REPEAT_H = 12

# Der Basiswert einer Zaehlerdifferenz muss unmittelbar vor dem Zeitraum liegen.
# Zaehlerstaende werden alle 5 Minuten geschrieben; ist der letzte davor Tage
# alt, klafft eine Luecke — bei der Messsteckdose etwa, weil sie zwischenzeitlich
# an einem anderen Geraet hing. Die Differenz waere dann frei erfunden.
MAX_BASE_GAP_S = 3600


# --------------------------------------------------------------------------
# Konfiguration und Zustand
# --------------------------------------------------------------------------

def load_config() -> dict[str, str]:
    """KEY=VALUE aus fritzbox.env. Umgebung gewinnt."""
    config: dict[str, str] = {}
    if CONFIG_FILE.exists():
        if CONFIG_FILE.stat().st_mode & 0o077:
            print(f"warning: {CONFIG_FILE} ist fuer andere lesbar — "
                  f"chmod 600 {CONFIG_FILE}", file=sys.stderr)
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip().strip("'\"")
    config.update({k: v for k, v in os.environ.items()
                   if k.startswith(("SMTP_", "MAIL_", "DB_", "REPORT_", "PRICE_"))})
    return config


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def connect(config: dict) -> sqlite3.Connection:
    path = config.get("DB_PATH") or DEFAULT_DB
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


# --------------------------------------------------------------------------
# Abfragen
# --------------------------------------------------------------------------

def day_start(offset_days: int = 0) -> int:
    """Lokale Mitternacht vor `offset_days` Tagen, als Unix-Sekunde."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((today - timedelta(days=offset_days)).timestamp())


def counter_kwh(conn, start: int, end: int) -> float | None:
    """kWh aus Zaehlerdifferenz. Nie aus Leistung integriert."""
    base = conn.execute(
        "SELECT ts, energy_wh FROM counter_readings WHERE ts < ? "
        "ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
    last = conn.execute(
        "SELECT energy_wh FROM counter_readings WHERE ts >= ? AND ts < ? "
        "ORDER BY ts DESC LIMIT 1", (start, end)).fetchone()
    if base is None or last is None or start - base["ts"] > MAX_BASE_GAP_S:
        return None
    return (last["energy_wh"] - base["energy_wh"]) / 1000.0


def device_kwh(conn, device: str, start: int, end: int) -> float | None:
    base = conn.execute(
        "SELECT ts, energy_wh FROM device_counters WHERE device = ? AND ts < ? "
        "ORDER BY ts DESC LIMIT 1", (device, start)).fetchone()
    last = conn.execute(
        "SELECT energy_wh FROM device_counters WHERE device = ? "
        "AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (device, start, end)).fetchone()
    # Wandert die Steckdose an ein anderes Geraet, laeuft ihr Zaehler waehrend
    # der Abwesenheit weiter: der letzte Stand davor ist dann Tage alt und die
    # Differenz enthielte fremden Verbrauch. Negativ = Zaehler zurueckgesetzt.
    if base is None or last is None or start - base["ts"] > MAX_BASE_GAP_S:
        return None
    delta = (last["energy_wh"] - base["energy_wh"]) / 1000.0
    return delta if delta >= 0 else None


def newest(conn, table: str, where: str = "", args: tuple = ()) -> int | None:
    row = conn.execute(
        f"SELECT MAX(ts) AS ts FROM {table} {where}", args).fetchone()
    return row["ts"] if row and row["ts"] else None


# --------------------------------------------------------------------------
# Alarme
# --------------------------------------------------------------------------

def check_alerts(conn, now: int, device: str) -> dict[str, str]:
    """→ {schluessel: menschenlesbarer Text} fuer jede zutreffende Bedingung."""
    alerts: dict[str, str] = {}

    house_ts = newest(conn, "power_samples")
    if house_ts is None:
        alerts["collector_stalled"] = "Es liegen ueberhaupt keine Messwerte vor."
        return alerts

    age_min = (now - house_ts) // 60
    if age_min > STALL_MIN:
        alerts["collector_stalled"] = (
            f"Seit {age_min} Minuten kein neuer Messwert vom Stromzaehler. "
            f"Der Harvester oder die Verbindung zur FRITZ!Box ist gestoert.")
        # Alle weiteren Aussagen stuetzen sich auf frische Daten. Ohne die
        # waeren sie geraten, also hier Schluss.
        return alerts

    row = conn.execute(
        "SELECT MAX(power_w) AS hoch, COUNT(*) AS n FROM power_samples "
        "WHERE ts > ?", (now - HOUSE_LOW_MIN * 60,)).fetchone()
    if row["n"] >= 3 and row["hoch"] is not None and row["hoch"] < HOUSE_LOW_W:
        alerts["house_low"] = (
            f"Die Hauslast liegt seit {HOUSE_LOW_MIN} Minuten unter "
            f"{HOUSE_LOW_W:.0f} W (hoechstens {row['hoch']:.1f} W). "
            f"Normal sind 50–83 W Grundlast. Versorgung oder Zaehler gestoert.")

    row = conn.execute(
        "SELECT MIN(power_w) AS tief, COUNT(*) AS n FROM power_samples "
        "WHERE ts > ?", (now - HOUSE_HIGH_H * 3600,)).fetchone()
    if row["n"] >= 30 and row["tief"] is not None and row["tief"] > HOUSE_HIGH_W:
        alerts["house_high"] = (
            f"Die Hauslast ist seit {HOUSE_HIGH_H} Stunden nie unter "
            f"{row['tief']:.0f} W gefallen. Da laeuft dauerhaft etwas mit "
            f"mindestens {HOUSE_HIGH_W:.0f} W.")

    dev_ts = newest(conn, "device_samples", "WHERE device = ?", (device,))
    if dev_ts is None:
        return alerts
    dev_age_min = (now - dev_ts) // 60
    if dev_age_min > STALL_MIN:
        alerts["fridge_offline"] = (
            f"Die Messsteckdose am Kuehlschrank meldet sich seit "
            f"{dev_age_min} Minuten nicht mehr (WLAN oder Steckdose ohne Strom).")
        return alerts

    row = conn.execute(
        "SELECT MAX(power_w) AS hoch, COUNT(*) AS n FROM device_samples "
        "WHERE device = ? AND ts > ?",
        (device, now - FRIDGE_SILENT_H * 3600)).fetchone()
    if row["n"] >= 30 and row["hoch"] is not None and row["hoch"] < FRIDGE_CYCLE_W:
        alerts["fridge_silent"] = (
            f"Der Kuehlschrank hat seit {FRIDGE_SILENT_H} Stunden nicht mehr "
            f"getaktet (hoechstens {row['hoch']:.1f} W, normal sind Spitzen um "
            f"50 W). Entweder ist er aus oder der Kompressor laeuft nicht mehr.")

    return alerts


# --------------------------------------------------------------------------
# Bericht
# --------------------------------------------------------------------------

def build_report(conn, now: int, device: str, price: float) -> tuple[str, str]:
    y_start, t_start = day_start(1), day_start(0)
    y_kwh = counter_kwh(conn, y_start, t_start)
    t_kwh = counter_kwh(conn, t_start, now + 1)
    f_kwh = device_kwh(conn, device, y_start, t_start)

    latest = conn.execute(
        "SELECT ts, power_w FROM power_samples ORDER BY ts DESC LIMIT 1").fetchone()
    base = conn.execute(
        "SELECT MIN(power_w) AS w FROM power_samples WHERE ts > ?",
        (now - 86400,)).fetchone()
    peak = conn.execute(
        "SELECT MAX(power_w) AS w FROM power_samples WHERE ts > ?",
        (now - 86400,)).fetchone()
    cycles = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT strftime('%Y%m%d%H', ts, 'unixepoch') h "
        "FROM device_samples WHERE device = ? AND ts > ? AND power_w > ? "
        "GROUP BY h)", (device, now - 86400, FRIDGE_CYCLE_W)).fetchone()

    def kwh(v):
        return f"{v:.2f} kWh" if v is not None else "—"

    def cost(v):
        return f" ({v * price:.2f} €)" if v is not None and price else ""

    yesterday = datetime.fromtimestamp(y_start).strftime("%d.%m.%Y")
    lines = [
        f"Energiebericht für {yesterday}",
        "",
        f"  Gestern gesamt   {kwh(y_kwh)}{cost(y_kwh)}",
        f"  Heute bisher     {kwh(t_kwh)}{cost(t_kwh)}",
        f"  Kühlschrank      {kwh(f_kwh)}{cost(f_kwh)}",
        "",
        f"  Aktuell          {latest['power_w']:.0f} W"
        if latest else "  Aktuell          —",
        f"  Grundlast (24 h) {base['w']:.0f} W" if base and base["w"] is not None
        else "  Grundlast (24 h) —",
        f"  Spitze (24 h)    {peak['w']:.0f} W" if peak and peak["w"] is not None
        else "  Spitze (24 h)    —",
        f"  Kühlschrank taktete in {cycles['n']} der letzten 24 Stunden",
    ]
    subject = f"Energiebericht {yesterday}: {kwh(y_kwh)}"
    return subject, "\n".join(lines)


# --------------------------------------------------------------------------
# Versand
# --------------------------------------------------------------------------

DRY_RUN = False


def send_mail(config: dict, subject: str, body: str) -> None:
    if DRY_RUN:
        print(f"--- WUERDE SENDEN ---\nBetreff: {subject}\n\n{body}\n---")
        return
    host = config.get("SMTP_HOST")
    user = config.get("SMTP_USER")
    password = config.get("SMTP_PASS")
    to = config.get("MAIL_TO") or user
    if not (host and user and password and to):
        sys.exit("SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_TO fehlen in "
                 f"{CONFIG_FILE}")

    message = EmailMessage()
    message["From"] = config.get("MAIL_FROM") or user
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body + "\n\n-- \nEnergyMonitor auf dem Pi\n")

    port = int(config.get("SMTP_PORT") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)


# --------------------------------------------------------------------------

def main() -> None:
    global DRY_RUN
    DRY_RUN = "--dry-run" in sys.argv
    force = "--now" in sys.argv or DRY_RUN
    test = "--test-mail" in sys.argv
    config = load_config()
    device = (config.get("SHELLY_DEVICES") or "kuehlschrank=").partition("=")[0]
    price = float(config.get("PRICE_PER_KWH") or 0)
    report_hour = int(config.get("REPORT_HOUR") or 8)
    now = int(time.time())
    state = load_state()

    if test:
        send_mail(config, "EnergyMonitor: Testmail",
                  "Wenn du das liest, funktioniert der Versand vom Pi.")
        print("Testmail verschickt.")
        return

    conn = connect(config)

    # --- Alarme ---
    alerts = check_alerts(conn, now, device)
    sent = state.setdefault("alerts", {})
    for key, text in alerts.items():
        last = sent.get(key, 0)
        if now - last >= ALERT_REPEAT_H * 3600:
            send_mail(config, f"ALARM: {text.split('.')[0]}.",
                      text + "\n\nDiese Meldung wiederholt sich fruehestens in "
                      f"{ALERT_REPEAT_H} Stunden, solange der Zustand anhaelt.")
            sent[key] = now
            print(f"Alarm verschickt: {key}")
    for key in list(sent):
        if key not in alerts:
            send_mail(config, "Entwarnung: Zustand wieder normal",
                      f"Die Bedingung '{key}' trifft nicht mehr zu.")
            del sent[key]
            print(f"Entwarnung verschickt: {key}")

    # --- Tagesbericht, zugleich Herzschlag ---
    last_report = state.get("last_report", 0)
    due = datetime.now().hour >= report_hour and now - last_report > 20 * 3600
    if force or due:
        subject, body = build_report(conn, now, device, price)
        if alerts:
            body += "\n\nOffene Alarme:\n" + "\n".join(
                f"  - {t}" for t in alerts.values())
        send_mail(config, subject, body)
        state["last_report"] = now
        print("Tagesbericht verschickt.")

    if not DRY_RUN:
        save_state(state)


if __name__ == "__main__":
    main()
