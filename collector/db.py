"""SQLite storage for the collector. WAL so the API can read while we write."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS power_samples (
  ts      INTEGER PRIMARY KEY,   -- unix seconds (UTC), from stats reconstruction
  power_w REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counter_readings (
  ts        INTEGER PRIMARY KEY,
  energy_wh REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT, updated_ts INTEGER
);

-- Einzelne Messsteckdosen, bewusst getrennt von power_samples: die messen eine
-- *Teilmenge* dessen, was der Hauszähler misst. Die beiden Reihen dürfen nie
-- addiert werden, und ein Schema, das sie trennt, macht den Fehler unmöglich.
CREATE TABLE IF NOT EXISTS device_samples (
  device  TEXT    NOT NULL,
  ts      INTEGER NOT NULL,
  power_w REAL    NOT NULL,
  PRIMARY KEY (device, ts)
);

CREATE TABLE IF NOT EXISTS device_counters (
  device    TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  energy_wh REAL    NOT NULL,
  PRIMARY KEY (device, ts)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_samples(conn: sqlite3.Connection, rows: list[tuple[int, float]],
                   tolerance: int = 0) -> int:
    """Idempotent insert. Overlapping harvests must not create duplicates.

    The box's 10 s slot grid is anchored to `datatime`, which drifts between
    harvests (measured: phase 6 → 0 → 7). The same physical reading therefore
    comes back with a timestamp shifted by up to grid-1 seconds, so the ts
    primary key alone does not catch it. `tolerance` (set to grid-1) treats a
    sample as already known when an existing row has the same power within
    that window — safe, because two *different* readings can never be that
    close with an identical value: adjacent readings differ by construction
    (identical neighbours were collapsed into one run), so equal values are at
    least two grid steps apart.
    """
    before = conn.total_changes
    for ts, power_w in rows:
        if tolerance and conn.execute(
                "SELECT 1 FROM power_samples WHERE ts BETWEEN ? AND ? "
                "AND ABS(power_w - ?) < 0.005 LIMIT 1",
                (ts - tolerance, ts + tolerance, power_w)).fetchone():
            continue
        conn.execute(
            "INSERT INTO power_samples (ts, power_w) VALUES (?, ?) "
            "ON CONFLICT(ts) DO NOTHING", (ts, power_w))
    conn.commit()
    return conn.total_changes - before


def insert_counter(conn: sqlite3.Connection, ts: int, energy_wh: float) -> int:
    before = conn.total_changes
    conn.execute(
        "INSERT INTO counter_readings (ts, energy_wh) VALUES (?, ?) "
        "ON CONFLICT(ts) DO NOTHING", (ts, energy_wh))
    conn.commit()
    return conn.total_changes - before


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_ts=excluded.updated_ts",
        (key, "" if value is None else str(value), int(time.time())))
    conn.commit()


def insert_device_sample(conn: sqlite3.Connection, device: str, ts: int,
                         power_w: float) -> None:
    """Kein Entdoppeln nötig: der Shelly liefert einen Momentanwert je Abfrage,
    keine wiederholten Puffer-Slots wie die FRITZ!Box."""
    conn.execute(
        "INSERT INTO device_samples (device, ts, power_w) VALUES (?, ?, ?) "
        "ON CONFLICT(device, ts) DO NOTHING", (device, ts, power_w))
    conn.commit()


def insert_device_counter(conn: sqlite3.Connection, device: str, ts: int,
                          energy_wh: float) -> None:
    conn.execute(
        "INSERT INTO device_counters (device, ts, energy_wh) VALUES (?, ?, ?) "
        "ON CONFLICT(device, ts) DO NOTHING", (device, ts, energy_wh))
    conn.commit()


def last_counter(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT energy_wh FROM counter_readings ORDER BY ts DESC LIMIT 1").fetchone()
    return row[0] if row else None


def sample_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM power_samples").fetchone()[0]
