#!/usr/bin/env bash
# Holt eine konsistente Kopie der Pi-Datenbank auf den Mac.
#
# Nicht einfach die Datei kopieren: der Harvester schreibt währenddessen, und
# die WAL-Datei gehört zum Stand dazu. "VACUUM INTO" erzeugt auf dem Pi einen
# in sich geschlossenen Schnappschuss, der ohne WAL vollständig ist.
#
# Aufruf: ./backup-from-pi.sh          (per launchd wöchentlich, siehe README)
set -euo pipefail

# Ziel als user@host, aus der Umgebung oder aus backup.env neben diesem
# Skript (nicht eingecheckt), z. B.: PI=pi@raspberrypi.local
ENV_FILE="$(dirname "$0")/backup.env"
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"
PI="${PI:?PI nicht gesetzt - user@host des Pi in $ENV_FILE eintragen}"
DEST="${DEST:-$HOME/Backups/EnergyMonitor}"
KEEP="${KEEP:-8}"          # Anzahl aufbewahrter Sicherungen

STAMP="$(date +%Y-%m-%d)"
SNAPSHOT="/tmp/energiepi-snapshot-$$.db"
TARGET="$DEST/data-$STAMP.db"

mkdir -p "$DEST"

ssh -o BatchMode=yes "$PI" \
    "rm -f '$SNAPSHOT' && sqlite3 ~/energiepi/data.db \"VACUUM INTO '$SNAPSHOT'\""
scp -q "$PI:$SNAPSHOT" "$TARGET"
ssh -o BatchMode=yes "$PI" "rm -f '$SNAPSHOT'"

# Nur melden, was wirklich lesbar ist — eine stumme kaputte Sicherung ist
# schlimmer als gar keine.
CHECK="$(sqlite3 "$TARGET" 'PRAGMA integrity_check;' 2>&1 | head -1)"
if [[ "$CHECK" != "ok" ]]; then
    echo "FEHLER: Sicherung ist beschädigt: $CHECK" >&2
    exit 1
fi

SAMPLES="$(sqlite3 "$TARGET" 'SELECT COUNT(*) FROM power_samples;')"
NEWEST="$(sqlite3 "$TARGET" "SELECT datetime(MAX(ts),'unixepoch','localtime') FROM power_samples;")"

# Rotation: die ältesten über KEEP hinaus entfernen.
ls -1t "$DEST"/data-*.db 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -f "$old"
    echo "entfernt: $(basename "$old")"
done

echo "gesichert: $TARGET"
echo "  $(du -h "$TARGET" | cut -f1), $SAMPLES Messwerte, neuester $NEWEST"
