# FRITZ!Box Energy Monitor — Build Spec (Pi Collector + macOS Viewer)

## Goal
Build a permanent, queryable history of household electricity use from a
**FRITZ!Smart Energy 250**. AVM's apps show live values and no usable history. The FRITZ!Box
retains only a rolling 1-hour power buffer and does **not** accumulate daily/monthly kWh.
A Raspberry Pi harvests that buffer before it rolls over and stores everything in SQLite;
a macOS menu bar app reads the Pi and charts it. Fully local.

## Verified hardware behaviour (measured, not assumed)
Confirmed against the real box on FRITZ!OS **8.25**, `fritzconnection` 1.15.1:

- **One device, one channel.** Parent `<AIN>` (battery, presence) plus a single
  powermeter sub-device `<AIN>-1`. **No feed-in channel** (`-2` does not exist; no PV).
  Note the **space inside the AIN** — URL-encode carefully.
- **`getdevicelistinfos`** → live `power` in **mW**, `energy` in **Wh** (cumulative meter reading),
  `<voltage/>` **always empty**.
- **`getbasicdevicestats`** → returns real history:
  - `<power>`: `count=360, grid=10` → **60 minutes**, newest first, `datatime` = timestamp of the
    newest sample. **Unit is 0.01 W**, not mW. (Verified: stats `23600` × 0.01 = 236 W = live `236000` mW.)
  - The meter reports only every **~2 minutes**; the box pads the 10 s slots by repeating the last
    value 12–13×. Real resolution is ~120 s.
  - `<voltage>`: all zeros. Ignore.
  - `<energy>`: two blocks (`grid=86400` ×31, `grid=2678400` ×12) that contain **only the current
    cumulative counter in slot 0, rest zeros**. The box does not build daily/monthly totals.
    Do not rely on these; compute totals from the counter instead.

**Consequences for the design:**
1. Harvest `getbasicdevicestats` **hourly** rather than polling live values every 60 s. One call
   yields a full hour at 2-minute resolution, and any outage shorter than an hour self-heals from
   the buffer. Poll every **50 minutes** for safety margin against the 60-minute rollover.
2. Also read `getdevicelistinfos` on each cycle to record the **cumulative counter** (the exact
   basis for daily/monthly kWh) plus battery state.
3. Store no voltage.

## Architecture
```
FRITZ!Smart Energy 250 ──DECT──▶ FRITZ!Box 7590 (1 h rolling buffer)
                                      │ AHA-HTTP, harvested every 50 min
                                      ▼
   ┌──────────── Raspberry Pi 4 (LAN, static lease) ───────────────┐
   │  collector/                                                    │
   │   • harvester → stats buffer + counter → SQLite  (systemd)     │
   │   • api       → FastAPI, read-only JSON          (systemd)     │
   │   • data.db   → SQLite (WAL), on microSD                       │
   └───────────────────────────┬────────────────────────────────────┘
                                │ HTTP/JSON over LAN (later: Tailscale)
                                ▼
                    macapp/ — SwiftUI menu bar viewer
```

## Constraints (both components)
- **Read-only** end to end. Only `getdevicelistinfos` and `getbasicdevicestats`. Never a switch command.
- **Local-only.** Pi↔FRITZ!Box, Mac↔Pi. No cloud, telemetry, iCloud, MyFRITZ.
- **Secrets:** FRITZ!Box credentials only on the Pi, via env (`FRITZBOX_HOST/USER/PASS`), systemd
  `EnvironmentFile` perms 600. Never on the Mac, never committed, never logged. A dedicated
  FRITZ!Box user with only the Smart-Home permission is used.
- **Units normalized once**, in the collector: stats power ×0.01 → W; live power ÷1000 → W;
  energy stays Wh. SQLite stores W and Wh.

---

# Part A — Collector (Raspberry Pi)

## Platform
Raspberry Pi 4 (4 GB), Raspberry Pi OS Bookworm 64-bit, Ethernet only (Wi-Fi disabled),
static DHCP lease on the FRITZ!Box.
Python 3.13 in a venv at `~/energiepi/.venv`. Deps: `fritzconnection`, `fastapi`, `uvicorn[standard]`.
SQLite via stdlib.

## Harvest cycle (every 50 minutes)
1. `getbasicdevicestats(ain)` → parse `<power>` stats block.
   - Reconstruct timestamps: value at index `i` (0 = newest) has
     `ts = datatime - i * grid`. Read `count`/`grid`/`datatime` from the XML; never hardcode.
   - Convert each value ×0.01 → W.
   - **Collapse the padding:** consecutive identical values are the same ~2-minute reading repeated.
     Keep the **oldest slot of each run** as the sample timestamp (that is when the reading arrived),
     and drop the duplicates. Store one row per real reading.
2. `getdevicelistinfos()` → cumulative `energy` (Wh), live `power`, `battery`, `batterylow`.
   Write the counter as a `counter_readings` row and update `meta`.
3. Upsert everything; overlapping harvests must not create duplicates.

## Storage (SQLite, WAL)
```sql
CREATE TABLE power_samples (
  ts      INTEGER PRIMARY KEY,   -- unix seconds (UTC), from stats reconstruction
  power_w REAL NOT NULL
);

CREATE TABLE counter_readings (  -- cumulative meter, one per harvest cycle
  ts        INTEGER PRIMARY KEY,
  energy_wh REAL NOT NULL
);

CREATE TABLE meta (              -- battery, batterylow, last_ok, last_error
  key TEXT PRIMARY KEY, value TEXT, updated_ts INTEGER
);
```
- `PRAGMA journal_mode=WAL` so the API reads while the harvester writes.
- `INSERT … ON CONFLICT(ts) DO NOTHING` for idempotent overlapping harvests.
- Volume is tiny: ~260k power rows/year. No pruning needed.
- Because the DB lives on microSD: a weekly `rsync` of `data.db` to the Mac is part of setup.

## Energy accounting
Daily/monthly kWh come from **counter deltas**, not from integrating power:
`consumption(period) = last counter in period − last counter before period`.
This is exact and immune to sampling gaps. Power integration may serve as a sanity cross-check only.

## Harvester service (`fritz-harvester.service`)
- `Restart=always`, runs the 50-minute cycle, no web dependency.
- On any error: log, record it in `meta`, skip the cycle, keep running. Survives box reboots and
  SID expiry (fritzconnection re-authenticates).
- Run one harvest immediately at startup, then on schedule.

## API service (`fritz-api.service`, FastAPI + uvicorn)
Read-only. **No write or control routes exist.** Server-side bucketing so the Mac never pulls raw rows.
- `GET /health` → `{ok, last_harvest_ts, sample_count, battery, batterylow, last_error}`
- `GET /latest` → `{ts, power_w, energy_wh}` (newest sample + newest counter)
- `GET /history?from=<unix>&to=<unix>&bucket=<sec>` → `[{ts, power_avg, power_min, power_max}]`
  - Bucket by integer division `ts/bucket*bucket`.
  - If `bucket` omitted, auto-pick for ≤ ~500 points: 1h→120 s, 24h→5 min, 7d→1 h, 30d→6 h, 1y→1 d.
- `GET /consumption?period=day|month&n=<count>` → kWh per period from counter deltas.
- `GET /baseline?from=<unix>&to=<unix>` → minimum and 5th-percentile power over the window.
  This is the standing-load view used to hunt unexplained consumption.
- Bind to the LAN interface, not WAN. Optional bearer token from env `API_TOKEN`; if unset, no auth.

---

# Part B — macOS Viewer (`macapp/`)

macOS 14+, Xcode 16+, SwiftUI, Swift Charts, `MenuBarExtra` with `.menuBarExtraStyle(.window)`,
no dock icon, **zero third-party dependencies**. No FRITZ!Box access; the Pi API is the only source.

- Menu bar label: current power, e.g. `236 W`; `—` when the Pi is unreachable. Refresh every 5 min
  (the underlying data changes only every ~2 min, so faster polling is pointless).
- Popover: current power + meter reading (kWh); range picker `1h · 24h · 7d · 30d · 1y` → `/history`
  → line chart; energy view → `/consumption` → daily/monthly kWh bars; **baseline row** showing
  standing load for the selected window; status row with last harvest, connection state, battery %.
- Stateless viewer, fetch on open/refresh. Optional SwiftData cache is a stretch goal.
- Settings: Pi base URL and optional token → **Keychain**. "Test connection" hits `/health`.
- App Sandbox + `com.apple.security.network.client`; ATS `NSAllowsLocalNetworking=true`.
  Expect the macOS 15 local-network prompt on first connect.

---

## Implementation Phases
**Collector first — history only exists from the moment it runs.**

1. **Harvest + parse:** stats fetch, timestamp reconstruction, ×0.01 conversion, padding collapse.
   Print the reconstructed samples with human-readable timestamps to the console. Verify the newest
   value matches the live reading before persisting anything.
2. **Persist + deploy:** schema, WAL, idempotent upsert, 50-minute cycle → `fritz-harvester.service`.
   **Deploy here; logging starts.** Verify with `sqlite3` after two cycles that the overlap produced
   no duplicates.
3. **API:** endpoints above → `fritz-api.service`. Verify each with `curl`.
4. **Client + menu bar:** API client (URL/token from Keychain) → `/latest` → live W in menu bar.
5. **Popover + charts:** range picker → `/history`, energy view → `/consumption`, baseline row.
6. **Polish:** Settings, error/empty/unreachable states, weekly DB rsync to the Mac, optional cache.

Each phase compiles, runs, does exactly its scope, then **stop and report**.

## Resolved (do not re-litigate)
Single channel, no feed-in · voltage always empty, not stored · stats power is 0.01 W ·
stats ordering is newest-first · meter reports every ~2 min · box does not accumulate daily/monthly kWh.

## Open Question
The `<energy>` daily/monthly slots are currently zero-filled. Possibly they populate over time on a
recently paired meter. Re-check after a few days. Do not depend on them; counter deltas are the
source of truth either way.
