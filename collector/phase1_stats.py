#!/usr/bin/env python3
"""Phase 1 — harvest + parse, print only. No persistence, no API.

Kept as a diagnostic: prints the reconstructed buffer for comparison against
the live reading. Parsing lives in fritzbox.py and is shared with the harvester.

Usage:
    python phase1_stats.py [--raw]

    --raw  additionally dump the raw XML responses (no credentials in there)
"""

from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timezone

import fritzbox
from fritzbox import AIN, LIVE_POWER_TO_W, STATS_POWER_TO_W


def local(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z")


def main() -> int:
    dump_raw = "--raw" in sys.argv[1:]
    fc = fritzbox.connect()

    stats_xml = fritzbox.aha_get(fc, "getbasicdevicestats", AIN)
    devices_xml = fritzbox.aha_get(fc, "getdevicelistinfos")
    fetched_at = int(time.time())

    if dump_raw:
        print("--- getbasicdevicestats ---")
        print(stats_xml)
        print("--- getdevicelistinfos ---")
        print(devices_xml)
        print("---")

    samples, attrs = fritzbox.parse_power_stats(stats_xml)
    collapsed = fritzbox.collapse_runs(samples)
    device = fritzbox.parse_device_list(devices_xml)

    if attrs["blocks"] > 1:
        print(f"note: <power> has {attrs['blocks']} stats blocks; "
              f"using the first (finest) one.")

    print(f"AIN                 {AIN}"
          + (f"  ({device['name']})" if device.get("name") else ""))
    print(f"stats block         count={attrs['count']} grid={attrs['grid']}s "
          f"datatime={attrs['datatime']}  ({local(attrs['datatime'])})")
    print(f"                    values in XML: {attrs['values_in_xml']}, "
          f"invalid/missing: {attrs['invalid']}, usable: {len(samples)}")
    print(f"buffer span         {local(min(t for t, _ in samples))} → "
          f"{local(max(t for t, _ in samples))} "
          f"({attrs['count'] * attrs['grid'] / 60:.0f} min nominal)")
    print(f"fetched at          {local(fetched_at)}")
    print()

    print(f"{'#':>4}  {'local time':<26} {'watts':>9}  {'slots':>5}  {'raw':>7}")
    for i, (ts, raw, run) in enumerate(collapsed):
        marker = "  ← newest" if i == len(collapsed) - 1 else ""
        print(f"{i:>4}  {local(ts):<26} {raw * STATS_POWER_TO_W:>9.2f}  "
              f"{run:>5}  {raw:>7}{marker}")
    print()

    watts = [raw * STATS_POWER_TO_W for _, raw, _ in collapsed]
    newest_ts, newest_raw, _ = collapsed[-1]
    newest_w = newest_raw * STATS_POWER_TO_W
    live_mw = device.get("power_mw")
    live_w = live_mw * LIVE_POWER_TO_W if live_mw is not None else None

    print(f"SUMMARY  samples={len(collapsed)} (from {len(samples)} slots)  "
          f"min={min(watts):.2f} W  max={max(watts):.2f} W  "
          f"median={statistics.median(watts):.2f} W  "
          f"newest={newest_w:.2f} W @ {local(newest_ts)}  "
          + (f"live={live_w:.2f} W" if live_w is not None else "live=n/a"))

    if live_w is not None:
        print(f"         delta newest−live = {newest_w - live_w:+.2f} W "
              f"(live raw {live_mw} mW, stats raw {newest_raw} ×0.01 W)")
    print(f"         counter = "
          + (f"{device['energy_wh']} Wh ({device['energy_wh'] / 1000:.3f} kWh)"
             if device.get("energy_wh") is not None else "n/a")
          + f"   battery = {device.get('battery', 'n/a')}%"
          f"   batterylow = {device.get('batterylow', 'n/a')}"
          f"   present = {device.get('present', 'n/a')}")
    print(f"         newest sample is {fetched_at - newest_ts} s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
