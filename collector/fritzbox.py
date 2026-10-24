"""FRITZ!Box access and stats parsing. Read-only.

Only getbasicdevicestats and getdevicelistinfos are ever called.
The parsing here is the version verified in Phase 1 against the live reading.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from fritzconnection import FritzConnection

# getbasicdevicestats power values are in 0.01 W; live power is in mW.
STATS_POWER_TO_W = 0.01
LIVE_POWER_TO_W = 0.001

CONFIG_FILE = Path(__file__).resolve().parent / "fritzbox.env"


# --------------------------------------------------------------------------
# config + transport (fritzconnection handles auth/SID; no raw SID or PBKDF2)
# --------------------------------------------------------------------------

def load_config() -> dict[str, str]:
    """Read KEY=VALUE lines from fritzbox.env. Environment wins if set."""
    config: dict[str, str] = {}
    if CONFIG_FILE.exists():
        if CONFIG_FILE.stat().st_mode & 0o077:
            print(f"warning: {CONFIG_FILE} is readable by other users — "
                  f"run: chmod 600 {CONFIG_FILE}", file=sys.stderr)
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip().strip("'\"")
    config.update({k: v for k, v in os.environ.items()
                   if k.startswith(("FRITZBOX_", "DB_", "API_"))})
    return config


# The meter's AIN comes from fritzbox.env (FRITZBOX_AIN), e.g. "12345 6789012-1".
# It usually contains a space. The powermeter is a sub-device ("-1") of a parent
# that carries battery and presence.
AIN = (load_config().get("FRITZBOX_AIN") or "").strip()
PARENT_AIN = AIN.rsplit("-", 1)[0]


def connect() -> FritzConnection:
    config = load_config()
    if not AIN:
        sys.exit(f"FRITZBOX_AIN missing. Put the meter's AIN in {CONFIG_FILE}")
    host = config.get("FRITZBOX_HOST") or "fritz.box"
    user = config.get("FRITZBOX_USER")
    password = config.get("FRITZBOX_PASS")
    if not user or not password:
        sys.exit(f"FRITZBOX_USER / FRITZBOX_PASS missing. Put them in {CONFIG_FILE}")
    fc = FritzConnection(address=host, user=user, password=password)
    if not hasattr(fc, "call_http"):
        sys.exit("fritzconnection is too old: call_http() is required (>= 1.9).")
    return fc


def aha_get(fc: FritzConnection, command: str, identifier: str | None = None) -> str:
    """Call one read-only AHA-HTTP command and return the response body."""
    response = fc.call_http(command, identifier)
    content = response["content"] if isinstance(response, dict) else response
    if not content or not content.lstrip().startswith("<"):
        raise RuntimeError(f"{command}: unexpected response: {content[:120]!r}")
    return content


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_power_stats(xml: str) -> tuple[list[tuple[int, int]], dict]:
    """Return ([(ts, raw_value), ...] newest first, block attributes).

    Timestamps are reconstructed as ts[i] = datatime - i * grid, with count,
    grid and datatime read from the XML — never hardcoded.
    """
    root = ET.fromstring(xml)
    power = root.find("power")
    if power is None:
        raise RuntimeError("no <power> block in getbasicdevicestats response")

    blocks = power.findall("stats")
    if not blocks:
        raise RuntimeError("<power> block contains no <stats> element")
    block = blocks[0]

    count = int(block.get("count"))
    grid = int(block.get("grid"))
    datatime = int(block.get("datatime"))

    raw_values = (block.text or "").strip().split(",")
    samples: list[tuple[int, int]] = []
    invalid = 0
    for i, token in enumerate(raw_values):
        token = token.strip()
        if not token or token == "-":  # AVM marks missing slots with '-'
            invalid += 1
            continue
        samples.append((datatime - i * grid, int(token)))

    attrs = {
        "count": count,
        "grid": grid,
        "datatime": datatime,
        "blocks": len(blocks),
        "values_in_xml": len(raw_values),
        "invalid": invalid,
    }
    return samples, attrs


def collapse_runs(samples: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Collapse padded repeats. Input is newest-first, output oldest-first.

    The meter reports every ~2 min; the box repeats the value across the 10 s
    slots in between. A run of identical values is one reading, and it arrived
    at the *oldest* slot of the run — that slot is the sample timestamp.
    Keeping the oldest slot is also what makes overlapping harvests idempotent:
    the newest run is still growing, but its first slot never moves.
    Returns [(ts, raw_value, slots_in_run), ...].
    """
    chronological = sorted(samples)  # oldest first
    collapsed: list[tuple[int, int, int]] = []
    for ts, value in chronological:
        if collapsed and collapsed[-1][1] == value:
            prev_ts, prev_value, run = collapsed[-1]
            collapsed[-1] = (prev_ts, prev_value, run + 1)  # keep the oldest ts
        else:
            collapsed.append((ts, value, 1))
    return collapsed


def parse_device_list(xml: str) -> dict:
    """Live power (mW), cumulative energy (Wh) and battery from the device list."""
    root = ET.fromstring(xml)
    info: dict = {}

    def normalized(value: str | None) -> str:
        return (value or "").replace(" ", "")

    for device in root.iter("device"):
        ident = normalized(device.get("identifier"))
        if ident == normalized(AIN):
            meter = device.find("powermeter")
            if meter is not None:
                info["power_mw"] = text_int(meter, "power")
                info["energy_wh"] = text_int(meter, "energy")
            info["name"] = (device.findtext("name") or "").strip()
            info["present"] = text_int(device, "present")
        if ident in (normalized(AIN), normalized(PARENT_AIN)):
            battery = text_int(device, "battery")
            if battery is not None:
                info["battery"] = battery
            batterylow = text_int(device, "batterylow")
            if batterylow is not None:
                info["batterylow"] = batterylow
    return info


def text_int(element: ET.Element, tag: str) -> int | None:
    value = (element.findtext(tag) or "").strip()
    try:
        return int(value)
    except ValueError:
        return None
