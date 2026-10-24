#!/usr/bin/env bash
# Builds EnergyMonitor.app and installs it to ~/Applications. Usage: ./build.sh [--run]
#
# The bundle is assembled in a temp dir and installed to ~/Applications on
# purpose: a file provider (iCloud Drive et al.) writing extended attributes
# back onto the bundle makes codesign refuse it, and has no business touching
# a running app.
set -euo pipefail
cd "$(dirname "$0")"

STAGE="$(mktemp -d)/EnergyMonitor.app"
DEST="$HOME/Applications/EnergyMonitor.app"
# Zwischenstände außerhalb des synchronisierten Ordners: der Dateianbieter
# fasst sonst dauernd in .build und macht Builds kaputt ("input file was
# modified during the build"). Bleibt bestehen, damit inkrementell gebaut wird.
SCRATCH="$HOME/Library/Caches/local.energymonitor/build"

mkdir -p "$SCRATCH"
swift build -c release --scratch-path "$SCRATCH"

mkdir -p "$STAGE/Contents/MacOS"
cp "$SCRATCH/release/EnergyMonitor" "$STAGE/Contents/MacOS/EnergyMonitor"
cp Resources/Info.plist "$STAGE/Contents/Info.plist"
xattr -cr "$STAGE"

# Ad-hoc reicht: die Signatur trägt nur noch die Sandbox-Entitlements. Eine
# feste Identität war nötig, solange die Zugangsdaten im Schlüsselbund lagen —
# der band seine Erlaubnis an die Signatur und fragte nach jedem Build erneut.
codesign --force --sign - --entitlements EnergyMonitor.entitlements "$STAGE"
codesign --verify --verbose=1 "$STAGE"

pkill -x EnergyMonitor 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
mv "$STAGE" "$DEST"
rm -rf "$(dirname "$STAGE")"

echo "installiert: $DEST"

if [[ "${1:-}" == "--run" ]]; then
    open "$DEST"
    sleep 2
    pgrep -x EnergyMonitor >/dev/null && echo "läuft: PID $(pgrep -x EnergyMonitor)"
fi
