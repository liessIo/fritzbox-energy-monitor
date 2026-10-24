"""Local, read-only access to Shelly Gen3 plugs. No cloud, stdlib only.

The Plug PM Gen3 exposes a pure meter component (`pm1:0`) and has no relay at
all, so there is nothing here that could switch anything even by accident.
"""

from __future__ import annotations

import json
import urllib.request

TIMEOUT_S = 5.0


def parse_devices(spec: str) -> list[tuple[str, str]]:
    """"fridge=shelly-fridge.local, freezer=..." → [(name, host), ...]"""
    devices: list[tuple[str, str]] = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, host = entry.partition("=")
        if host.strip():
            devices.append((name.strip(), host.strip()))
    return devices


def read_meter(host: str) -> dict:
    """Current power in W and the plug's own cumulative counter in Wh."""
    url = f"http://{host}/rpc/PM1.GetStatus?id=0"
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        data = json.load(response)
    return {
        "power_w": float(data["apower"]),
        # Shelly documents aenergy.total in Wh. Cross-checked against the
        # integral of apower after deployment.
        "energy_wh": float(data["aenergy"]["total"]),
        "voltage": float(data.get("voltage", 0.0)),
    }
