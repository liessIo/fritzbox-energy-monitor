# FRITZ!Box Energy Monitor

*Reads a FRITZ!Smart Energy 250 meter reader through the FRITZ!Box, stores the
data on a Raspberry Pi and shows it in a web interface and a macOS menu bar app.
Read-only, LAN-only, no cloud. Documentation is in German.*

Der Stromzähler wird über den **FRITZ!Smart Energy 250** (optischer Lesekopf, DECT)
an der FRITZ!Box ausgelesen. Ein Raspberry Pi holt die Werte regelmäßig ab,
speichert sie in SQLite und stellt sie im Heimnetz bereit:

- **Weboberfläche**: Leistung, Verlauf, Verbrauch pro Tag/Monat, Grundlast, Prognose
- **Mac-App** in der Menüleiste: aktuelle Leistung, Diagramme
- **Täglicher Bericht per Mail** plus Alarme, z. B. wenn die Grundlast einbricht
  (Gefrierschrank aus?) oder stundenlang etwas Großes läuft
- **Optional Shelly-Messsteckdosen** (Plug PM Gen3) für einzelne Geräte

Alles bleibt im eigenen Netz: keine Cloud, kein MyFRITZ, keine Telemetrie.
Der Monitor liest nur; an der FRITZ!Box wird nichts geschaltet oder verändert.

## Was man braucht

- FRITZ!Box mit DECT und einem **FRITZ!Smart Energy 250** am Zähler
  (entwickelt und getestet mit FRITZ!Box 7590, FRITZ!OS 8.25)
- **Raspberry Pi** (getestet: Pi 4, Raspberry Pi OS Bookworm, Python 3.11+)
- für die Mac-App: macOS 14+ und die Xcode-Command-Line-Tools
- optional: Shelly Plug PM Gen3, ein SMTP-Konto für den Tagesbericht

## Aufbau

```
FRITZ!Smart Energy 250 ──DECT──▶ FRITZ!Box ──AHA-HTTP──▶ Raspberry Pi
                                                          ├─ harvester  (systemd)  → SQLite
                                                          ├─ api + web  (systemd)  → :8000
                                                          └─ report     (Timer, stündlich) → Mail
                                         Mac-App / Browser ──HTTP──▶ api
```

| Verzeichnis | Inhalt |
|---|---|
| `collector/` | Python: Harvester, FastAPI (nur lesend), Mail-Bericht, systemd-Units |
| `collector/web/` | Weboberfläche, eine einzelne HTML-Datei ohne Fremdbibliotheken |
| `macapp/` | SwiftUI-Menüleisten-App, Swift Package ohne Xcode-Projekt |
| `scripts/` | wöchentliches Datenbank-Backup vom Pi auf den Mac, launchd-Vorlagen |

## Einrichtung

### 1. FRITZ!Box

- Eigenen Benutzer für den Monitor anlegen (System → FRITZ!Box-Benutzer),
  Recht **Smart Home** genügt.
- AIN des Lesekopfs notieren: Smart Home → Gerät → Kennung. Verwendet wird das
  Untergerät mit der Endung `-1`. Die AIN enthält ein Leerzeichen, das gehört dazu.

### 2. Raspberry Pi

```bash
mkdir -p ~/energiepi && cd ~/energiepi
# Inhalt von collector/ hierher kopieren, dann:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp fritzbox.env.example fritzbox.env && chmod 600 fritzbox.env
nano fritzbox.env          # FRITZBOX_*, FRITZBOX_AIN, API_HOST eintragen
```

Erster Test, ob die Box antwortet:

```bash
.venv/bin/python phase1_stats.py      # zeigt die aktuellen Messwerte
.venv/bin/python harvester.py --once  # ein Abruf in die Datenbank
```

Die systemd-Units gehen von Benutzer `pi` und `/home/pi/energiepi` aus.
Bei anderem Benutzer vorher `User=`, `Group=` und die Pfade anpassen:

```bash
sudo cp fritz-harvester.service fritz-api.service \
        fritz-report.service fritz-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fritz-harvester fritz-api
```

Die Weboberfläche ist danach unter `http://<API_HOST>:8000/` erreichbar.

### 3. Tagesbericht und Alarme (optional)

SMTP-Daten in `fritzbox.env` eintragen (siehe `fritzbox.env.example`), dann:

```bash
.venv/bin/python report.py --test-mail   # Testmail
.venv/bin/python report.py --dry-run     # Bericht anzeigen statt senden
sudo systemctl enable --now fritz-report.timer
```

Der Bericht ist zugleich das **Lebenszeichen**: Der Pi hängt am selben Stromnetz,
das er überwacht, und kann seinen eigenen Stromausfall nicht melden. Kommt die
Mail nicht, ist Strom, Pi oder Internet weg.

### 4. Mac-App (optional)

```bash
cd macapp && ./build.sh --run
```

Baut die App, signiert sie ad hoc und installiert sie nach `~/Applications`.
Beim ersten Start fragt sie nach der Adresse des Pi (z. B. `raspberrypi.local:8000`)
und, falls gesetzt, nach dem `API_TOKEN`. Achtung: nicht in einem
iCloud-synchronisierten Ordner bauen, die Dateisynchronisation beschädigt den Build.
`build.sh` legt seine Zwischenstände deshalb unter `~/Library/Caches` ab.

Autostart: `scripts/local.energymonitor.plist` nach `~/Library/LaunchAgents`
kopieren, `PFAD_ZUR_APP` ersetzen, `launchctl load` darauf.

### 5. Backup (optional)

`scripts/backup-from-pi.sh` holt per SSH einen konsistenten Schnappschuss
(`VACUUM INTO`) auf den Mac und prüft ihn. Ziel in `scripts/backup.env`
eintragen (`PI=pi@raspberrypi.local`), wöchentlich per
`scripts/local.energymonitor.backup.plist`.

## Was man über den Lesekopf wissen sollte

Alles am echten Gerät gemessen; ausführlich in
[`FRITZ-Energy-Monitor-Spec.md`](FRITZ-Energy-Monitor-Spec.md) und
[`CLAUDE.md`](CLAUDE.md).

- **Einheiten:** `getbasicdevicestats` liefert Leistung in **0,01 W**,
  `getdevicelistinfos` in **mW**. Verwechselt man das, liegt man um Faktor 10 daneben.
- Die FRITZ!Box puffert **eine Stunde** in 10-s-Schritten, der Zähler meldet
  aber nur etwa **alle 2 Minuten**. Die Wiederholungen werden zusammengefasst.
- Das 10-s-Raster **wandert** zwischen zwei Abrufen. Derselbe Messwert kommt mit
  verschobenem Zeitstempel wieder; doppelte Werte erkennt man nur über Zeit
  *und* Wert.
- Die Box summiert **keine** Tages- oder Monatswerte. Verbrauch kommt aus
  **Zählerstandsdifferenzen**, nie aus aufsummierter Leistung: die Leistungswerte
  sind Momentaufnahmen und überschätzen kurze Lasten (Wasserkocher) massiv.

## Sicherheit

- Die API ist nur lesend, die Datenbankverbindung läuft mit `PRAGMA query_only`.
- Sie lauscht nur auf `API_HOST` (LAN-Adresse), nie auf `0.0.0.0`.
  Ohne `API_TOKEN` ist sie im Heimnetz ohne Anmeldung lesbar, also nicht ins
  Internet freigeben. Für den Zugriff von unterwegs ein VPN nutzen
  (z. B. WireGuard auf der FRITZ!Box).
- Zugangsdaten liegen nur in `fritzbox.env` auf dem Pi (chmod 600) und werden
  nie eingecheckt.

## Lizenz

MIT, siehe [`LICENSE`](LICENSE).
