#!/usr/bin/env python3
"""Harvest the FRITZ!Box — and any Shelly plugs — into SQLite.

Three cadences, one process:
  * every 30 s  — getdevicelistinfos: live power, counter, battery. Plus each
    configured Shelly plug over its local HTTP API.
  * every 5 min — getbasicdevicestats: the full 1-hour power buffer. This is
    the history; one call yields an hour at 2-minute resolution.

House meter and plugs are stored in separate tables on purpose: a plug measures
a *subset* of what the house meter measures, so the two must never be summed.

Read-only throughout. No web dependency; the API is a separate unit. Any error
is logged, recorded in `meta`, and the cycle is skipped — the loop keeps running
and reconnects on the next pass. A plug that drops off the WiFi cannot stall
the FRITZ!Box side.

Usage:
    python harvester.py           # run forever (this is what systemd starts)
    python harvester.py --once    # one stats harvest + one live poll
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

import db
import fritzbox
import shelly

DEFAULT_LIVE_INTERVAL_S = 30    # override with LIVE_INTERVAL in fritzbox.env
# Der Puffer fasst 60 Minuten, gegen Datenverlust würden also 50 reichen. Aber
# die Historie ist erst nach einem Harvest sichtbar, und ein Diagramm, das bis
# zu 50 Minuten alt ist, sieht kaputt aus. Häufiger heißt nur mehr Überlappung,
# und die Entdopplung fängt sie ohnehin ab.
DEFAULT_STATS_INTERVAL_S = 300  # override with STATS_INTERVAL in fritzbox.env
DEFAULT_DB = Path(__file__).resolve().parent / "data.db"

log = logging.getLogger("harvester")
stop = threading.Event()

_connection = None
_last_counter_bucket: dict[str, int] = {}


def get_connection():
    """Reuse one authenticated connection; rebuilt after any failure."""
    global _connection
    if _connection is None:
        _connection = fritzbox.connect()
    return _connection


def drop_connection() -> None:
    """Force a fresh login next cycle — survives box reboots and SID expiry."""
    global _connection
    _connection = None


def poll_live(conn, fc) -> None:
    """Cheap cycle: live power, cumulative counter, battery."""
    device = fritzbox.parse_device_list(
        fritzbox.aha_get(fc, "getdevicelistinfos"))
    now = int(time.time())

    power_mw = device.get("power_mw")
    if power_mw is not None:
        db.set_meta(conn, "live_power_w", power_mw * fritzbox.LIVE_POWER_TO_W)
        db.set_meta(conn, "live_ts", now)

    # The meter only reports every ~2 min, so polling faster returns the same
    # counter repeatedly. Store a row only when it actually moved — otherwise
    # the table grows with copies that carry no information.
    energy_wh = device.get("energy_wh")
    stored = False
    if energy_wh is not None and float(energy_wh) != db.last_counter(conn):
        db.insert_counter(conn, now, float(energy_wh))
        stored = True

    for key in ("battery", "batterylow"):
        if key in device:
            db.set_meta(conn, key, device[key])
    db.set_meta(conn, "last_live_ts", now)

    log.info("live: %.2f W, counter %s Wh%s, battery %s%%",
             (power_mw or 0) * fritzbox.LIVE_POWER_TO_W, energy_wh,
             "" if stored else " (unverändert)", device.get("battery"))


def harvest_stats(conn, fc) -> None:
    """Expensive cycle: the 1-hour power buffer becomes history."""
    samples, attrs = fritzbox.parse_power_stats(
        fritzbox.aha_get(fc, "getbasicdevicestats", fritzbox.AIN))
    now = int(time.time())
    collapsed = fritzbox.collapse_runs(samples)

    # The oldest run is cut off by the buffer edge, so its first slot is not
    # where the reading actually arrived — the true start already rolled out.
    # A previous harvest recorded that same reading with its correct, earlier
    # timestamp; keeping this one would store the reading twice. Harvests
    # overlap by 10 minutes, so dropping it loses nothing but the very first
    # ~2 minutes of history ever.
    dropped = None
    if len(collapsed) > 1:
        dropped, collapsed = collapsed[0], collapsed[1:]

    rows = [(ts, raw * fritzbox.STATS_POWER_TO_W) for ts, raw, _ in collapsed]
    inserted = db.insert_samples(conn, rows, tolerance=attrs["grid"] - 1)

    db.set_meta(conn, "last_harvest_ts", now)

    newest_ts, newest_raw, _ = collapsed[-1]
    log.info(
        "stats: %d samples from %d slots, %d new, %d already known%s; "
        "newest %.2f W @ %d (%d s old); datatime=%d grid=%d phase=%d",
        len(rows), len(samples), inserted, len(rows) - inserted,
        f", dropped truncated oldest @ {dropped[0]}" if dropped else "",
        newest_raw * fritzbox.STATS_POWER_TO_W, newest_ts, now - newest_ts,
        attrs["datatime"], attrs["grid"], attrs["datatime"] % attrs["grid"])


def poll_devices(conn, devices: list[tuple[str, str]]) -> None:
    """Messsteckdosen abfragen. Jede für sich — ein Gerät im WLAN-Nirwana darf
    weder die anderen noch den FRITZ-Teil aufhalten."""
    for name, host in devices:
        try:
            reading = shelly.read_meter(host)
        except Exception as error:
            log.warning("shelly %s (%s) nicht erreichbar: %s: %s",
                        name, host, type(error).__name__, error)
            db.set_meta(conn, f"device_error_{name}",
                        f"{int(time.time())} {type(error).__name__}: {error}")
            continue

        now = int(time.time())
        db.insert_device_sample(conn, name, now, reading["power_w"])
        # Zählerstand nur alle 5 Minuten: er wächst mit jeder Abfrage, für
        # Tages- und Zeitraumdifferenzen reicht dieser Abstand vollkommen.
        if now // 300 != _last_counter_bucket.get(name):
            db.insert_device_counter(conn, name, now, reading["energy_wh"])
            _last_counter_bucket[name] = now // 300
        db.set_meta(conn, f"device_power_w_{name}", reading["power_w"])
        db.set_meta(conn, f"device_ts_{name}", now)
        db.set_meta(conn, f"device_error_{name}", "")


def cycle(conn, stats_due: bool) -> None:
    fc = get_connection()
    if stats_due:
        harvest_stats(conn, fc)
    poll_live(conn, fc)
    db.set_meta(conn, "last_ok", int(time.time()))
    db.set_meta(conn, "last_error", "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    config = fritzbox.load_config()
    db_path = Path(config.get("DB_PATH") or DEFAULT_DB)
    live_interval = max(10, int(config.get("LIVE_INTERVAL") or DEFAULT_LIVE_INTERVAL_S))
    stats_interval = max(60, int(config.get("STATS_INTERVAL") or DEFAULT_STATS_INTERVAL_S))
    devices = shelly.parse_devices(config.get("SHELLY_DEVICES") or "")
    conn = db.connect(db_path)
    log.info("harvester started, db=%s, live=%ds, stats=%ds, %d samples on record; "
             "devices: %s", db_path, live_interval, stats_interval,
             db.sample_count(conn),
             ", ".join(f"{n}@{h}" for n, h in devices) or "keine")

    once = "--once" in sys.argv[1:]
    next_stats = 0.0  # run the stats harvest immediately on startup
    while not stop.is_set():
        started = time.monotonic()
        stats_due = started >= next_stats
        try:
            cycle(conn, stats_due)
            if stats_due:
                next_stats = started + stats_interval
        except Exception as error:  # log, record, skip the cycle, keep running
            log.error("cycle failed: %s: %s", type(error).__name__, error)
            drop_connection()
            try:
                db.set_meta(conn, "last_error",
                            f"{int(time.time())} {type(error).__name__}: {error}")
            except Exception as meta_error:
                log.error("could not record error in meta: %s", meta_error)
        if devices:
            poll_devices(conn, devices)
        if once:
            break
        stop.wait(max(1.0, live_interval - (time.monotonic() - started)))

    conn.close()
    log.info("harvester stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
