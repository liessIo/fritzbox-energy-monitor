# CLAUDE.md — FRITZ!Box Energy Monitor

Operating rules for Claude Code.
**What was built: `FRITZ-Energy-Monitor-Spec.md`.** This file governs *how*, and records
where the built system deliberately departs from the spec.

## Status
Phases 1–5 are built and deployed; phase 6 was reduced to cleanup (no Settings view).
The collector runs on the Pi as two systemd units; the viewer is installed at
`~/Applications/EnergyMonitor.app` and starts at login via launchd.
Since 2026-09-05 also: the web interface, a **WireGuard VPN** for access from
outside, and **`report.py`** — daily mail report plus alerts, on an hourly timer.

## Project
- `collector/` — Python harvester + FastAPI on a Raspberry Pi 4 (address and user
  in `CLAUDE.local.md`), venv at `~/energiepi/.venv`. Two cadences in one process: live values every **30 s**
  (`getdevicelistinfos`), the full 1-hour stats buffer every **5 min**
  (`getbasicdevicestats`). SQLite in WAL, read-only JSON API.
- `macapp/` — SwiftUI menu bar viewer, SwiftPM package (no Xcode project).
  Reads the Pi API only. **No FRITZ!Box access.**
- `collector/web/` — the web interface: one self-contained HTML file, no third-party
  libraries, charts drawn as inline SVG. Served by the API at `/`.
- `collector/report.py` — daily mail report and alerts. Own systemd unit and
  **hourly** timer (`fritz-report.timer`), stdlib only, outbound SMTP, opens the
  DB read-only. Deliberately does not import `fritzbox.py`: a broken
  `fritzconnection` must not take the heartbeat down with it.
- `scripts/` — weekly database backup from the Pi, launchd plists.

## Verified facts — treat as given, do not re-derive or "correct"
- The meter's AIN is `FRITZBOX_AIN` in `fritzbox.env` (**contains a space**, URL-encode it).
  The powermeter is the `-1` sub-device; the parent carries battery and presence.
- **Single channel. No feed-in.** No sign ambiguity. Do not write feed-in code paths.
- `<voltage/>` is always empty, stats voltage is all zeros → **not stored**.
- **`getbasicdevicestats` power unit is 0.01 W**, not mW. `getdevicelistinfos` live power is mW.
  Getting this wrong is a factor-10 error.
- Stats are **newest-first**; `ts[i] = datatime - i * grid`. Read `count`/`grid`/`datatime` from the
  XML, never hardcode them.
- The meter updates every **~2 minutes**; the box pads 10 s slots by repeating values. Collapse runs
  of identical values and keep the **oldest slot of each run**.
- **The 10 s slot grid drifts.** `datatime mod grid` changes between harvests (measured: 6 → 0 → 7).
  The same physical reading therefore returns with a timestamp shifted by up to `grid-1` seconds,
  so `ts` as primary key does **not** make overlapping harvests idempotent on its own.
- Real readings can be **10 s apart** (the meter jitters), so a time-only dedup window would
  destroy data. Dedup must also compare the value.
- The oldest run in any harvest is **cut off by the buffer edge** — its first slot is not when the
  reading arrived. Drop it; the previous harvest already stored it correctly.
- The box does **not** accumulate daily/monthly kWh. Compute those from **counter deltas**.
- **Stats power values are instantaneous samples, not interval averages.** Integrating them
  overestimates short loads badly: an hour containing a 3426 W kettle integrated to 250 Wh where
  the counter showed 121 Wh. For sustained loads the two agree within a few percent. Never derive
  energy from power — not for reports, not for events, not "just as an estimate".

## Alerting — the one thing that shapes the design
**The Pi cannot report its own power loss.** It hangs off the very supply it
watches, so any "alert when the standing load collapses" logic is silent exactly
when it matters. The resolution needs no third party: **the daily report is the
heartbeat.** If the mail does not arrive, power, Pi or internet is gone — the
*absence* is the signal. Everything else is detectable locally, because the
power is on in those cases.

Thresholds are calibrated against seven days of real operation (2026-08-29..09-05)
and none of them ever tripped in normal use — re-measure before changing them:
house load never fell below 49 W, no 3-hour window stayed above 300 W throughout,
and the fridge had no cycle-free hour in 24. Energy in the report comes from
counter deltas only, and a delta is **refused** when the base reading is more than
an hour older than the window (`MAX_BASE_GAP_S`): when the plug moves to another
appliance its counter keeps running, and the difference across that gap would be
pure fiction — this produced a 2.29 kWh "fridge" reading on the first dry run.

## Non-negotiables
- **Read-only** end to end. Only `getdevicelistinfos` and `getbasicdevicestats`. No switch commands,
  no write/mutate API routes, ever. The API's SQLite connection runs with `PRAGMA query_only`.
- **Local-only.** Pi↔FRITZ!Box, Mac↔Pi. No cloud, telemetry, iCloud, MyFRITZ.
- **Secrets:** FRITZ!Box credentials only on the Pi, in `~/energiepi/fritzbox.env` (chmod 600),
  read by the scripts themselves. Never on the Mac, never committed, never logged.
- **Zero third-party deps in the Mac app** (Swift Charts is a system framework).
  Collector deps limited to `fritzconnection`, `fastapi`, `uvicorn`.
- Units normalized once, in the collector. SQLite stores W and Wh.

## Deliberate departures from the spec
- **Mac credentials are a 0600 file in the app's sandbox container, not the Keychain.**
  The Keychain binds its "always allow" to the app's code signature, which changes with every
  ad-hoc build: it re-prompted after each rebuild and eventually refused to store at all.
  The token grants read-only access to household power data on the LAN.
- **Live poll every 30 s** in addition to the 50-minute stats harvest. Without it the menu bar
  showed a value up to 50 minutes old while claiming to be current. It buys reaction time, not
  resolution — the meter still reports every ~2 min. `/latest` prefers the live value when it is
  fresher than the newest stats sample and flags it with `"live": true`.
- **Counter rows are written only when the value changed**, otherwise the 30 s poll would fill the
  table with copies.
- **DynDNS via desec.io, not MyFRITZ**, although the FRITZ!Box pushes MyFRITZ.
  The "no MyFRITZ" rule stands: desec publishes only a name→IP mapping, while
  MyFRITZ ties the box to a vendor account and opens remote admin by default.
  WireGuard needs *some* dynamic name — a bare IP dies at the nightly reconnect.
  Name in `CLAUDE.local.md`, split tunnel, client config at
  `~/.config/wireguard/energiepi.conf` on the Mac (0600, outside iCloud; `*.conf`
  is gitignored). Works at all only because the line has a public IPv4, no DS-Lite.
- **The Shelly plug moves between appliances.** `SHELLY_DEVICES` in
  `fritzbox.env` is `name=host` and **must be renamed whenever the plug moves**,
  otherwise readings pile up under the previous appliance's name — this already
  happened once and needed a relabel of `device_samples`/`device_counters` by
  timestamp. Past names are listed in `CLAUDE.local.md`.
- **No Settings view** (phase 6 dropped). To change address or token, delete
  `~/Library/Containers/local.energymonitor/Data/Library/Application Support/credentials.json`
  and restart the app.

## Collector conventions (Python)
- Use `fritzconnection` for auth and transport. No raw SID or PBKDF2 code.
- Harvester and API are **separate systemd units** sharing one SQLite file in **WAL** mode. The
  harvester has no web dependency and reuses one authenticated connection, dropping it on error so
  the next cycle re-authenticates.
- Harvests overlap by >90% by design (the buffer holds an hour, we take it every 5 min so
  the chart is never far behind) → **idempotent upsert**: `ON CONFLICT(ts) DO NOTHING` *plus* a
  value-and-tolerance check (`tolerance = grid-1`), because of the slot-phase drift above.
- On any error: log, record in `meta`, skip the cycle, continue (`Restart=always`).
- API: read-only, server-side bucketing via integer division on `ts`, bounded response sizes
  (2000 buckets), optional bearer token, bound to the LAN address not `0.0.0.0`.
- FastAPI runs sync endpoints and sync dependencies on a threadpool and not necessarily on the same
  thread, so per-request SQLite connections need `check_same_thread=False`.

## Mac app conventions (Swift)
macOS 14+, async/await, no force-unwraps on network data. `MenuBarExtra` window style, no dock icon.
Stateless viewer, no disk cache — `URLSessionConfiguration.ephemeral`, so a stale reading can never
be served as current. Small focused files: `APIClient`, `Models`, `AppState`, `CredentialStore`,
`MenuBarContentView`. Menu bar refreshes every 30 s; history/consumption/baseline load only while
the popover is open.

Two things the app must keep doing or it dies silently:
- `applicationShouldTerminateAfterLastWindowClosed` → `false`. The popover is the only window;
  closing it otherwise quits the app.
- `NSSupportsSuddenTermination`/`NSSupportsAutomaticTermination` → `false`, plus the matching
  `ProcessInfo` calls. A menu bar app that sleeps between refreshes looks idle to the system.

## Build and deploy
- Mac: `macapp/build.sh` → builds, ad-hoc signs with sandbox entitlements, installs to
  `~/Applications`. Intermediates go to `~/Library/Caches/local.energymonitor/build`.
  **Never build inside `~/Documents`** — it is iCloud-synced and the file provider corrupts both
  the build directory and the app bundle. If the project moves, delete that cache directory.
- Pi: `scp` the changed file to `~/energiepi/` (host in `CLAUDE.local.md`), then
  `sudo systemctl restart fritz-harvester` / `fritz-api`.
- Backup: `scripts/backup-from-pi.sh` (target in gitignored `scripts/backup.env`), weekly via launchd, `VACUUM INTO` on the Pi for a consistent
  snapshot, `PRAGMA integrity_check` before rotation.

## Workflow — phase gate
One phase at a time, in spec order. After each: **stop, summarize, wait for confirmation.**
Verify against the real system (`sqlite3`, `curl`, the actual box) rather than against assumptions —
every hard bug in this project was found that way and none of them were guessable.
