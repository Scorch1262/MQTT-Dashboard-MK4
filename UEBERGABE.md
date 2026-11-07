# Übergabe – MQTT Dashboard (MK4)

Technische Übergabe für Weiterentwicklung. Nutzerdoku siehe `README.md`.

## Architektur (seit v2.0.0)

Eine Datei `app.py`, Aufbau von oben nach unten:

| Abschnitt | Inhalt |
|---|---|
| Konstanten | `APP_VERSION`, Zeiträume, Gerätesymbole, Farbpalette, `DEFAULT_CONFIG` |
| Formeln | `compile_formula()` prüft per AST gegen eine Positivliste, `eval_formula()` |
| Validierung | `validate_group/_chart/_widget/_virtual` – einzige Stelle, an der Eingaben geprüft werden |
| `migrate_legacy()` | Umwandlung v1.x-`mqtt_dashboard_config.json` → v2-`config.json` |
| `import_config()` | hochgeladene Konfiguration (auch v1.x) prüfen/übernehmen, liefert Warnungen für übersprungene Einträge |
| `ConfigStore` | Laden/Normalisieren/atomares Speichern (`.tmp` + `os.replace`), defekte Datei wird als `config.json.defekt-<ts>` gesichert |
| `TopicStore`, `_Series` | Werte + Verlauf im Speicher, je Topic drei Zeitstufen: letzte Stunde jeder Punkt (max. 1/s), bis 12 h min/max je 15 s, bis 49 h min/max je 60 s (`_compact_into()`); Verdichtung beim Schreiben alle 10 s und per Hintergrund-Thread jede Minute |
| `VirtualEngine` | berechnet virtuelle Topics bei Änderung einer Quelle (bis Tiefe 3, virtuell auf virtuell möglich) |
| `MqttManager` | paho-Client mit `loop_start`, Auto-Reconnect, Neustart bei Broker-Änderung; 1.6.x und 2.x kompatibel |
| REST-API | siehe unten |
| `INDEX_HTML` | komplettes Frontend (CSS/HTML/JS), wird **nicht** durch Jinja geschickt |
| `main()` | Start, Startmeldung mit LAN-IP |

### REST-API

| Route | Zweck |
|---|---|
| `GET /api/config` | komplette Konfiguration (Passwort entfernt, `password_set`) |
| `GET /api/config/download[?password=1]` | Konfiguration als Datei (Format = `config.json`), Passwort nur mit `password=1` |
| `POST /api/config/upload` | Konfiguration hochladen (`{config, dry_run}`), `import_config()` prüft jeden Eintrag mit den normalen Validatoren, Sicherung `config.json.vor-import-<Zeit>` |
| `PUT /api/settings` | Titel, Port, Standard-Zeitraum |
| `PUT /api/broker` | Broker ändern → sofortiger Reconnect |
| `GET /api/status` | Brokerstatus, Topic-Anzahl, Werte/Fehler der virtuellen Topics |
| `GET /api/topics` | alle empfangenen Topics mit letztem Wert (Auswahllisten) |
| `POST /api/history` | Erstabruf: Verlauf (relativ zur Serverzeit) + Zustände |
| `POST /api/live` | alle 2 s: neue Punkte seit `since` (Serverzeit) + Zustände |
| `POST /api/publish` | senden (merkt sich die Payload 5 s für „eigen“) |
| `POST /api/virtual_topics/test` | Formel mit aktuellen Werten testen |
| `POST/PUT/DELETE /api/<bereich>[/<id>]` | CRUD für `groups`, `charts`, `widgets`, `virtual_topics` |
| `POST /api/<bereich>/reorder` | Reihenfolge (`order`) setzen |

## Entscheidungen

- **Ansicht/Einstellungen wie MK6:** eine Seite, zwei Bereiche
  (`#viewRoot`, `#settingsPanel`), Umschaltung über `enterSettingsMode()`/
  `exitSettingsMode()`. Bearbeiten über Modals, Listen mit ↑/↓, Gruppen-
  Dropdown, Bearbeiten, Kopieren, ×.
- **Kein Flask-SocketIO mehr:** Polling alle 2 s (`/api/live`) mit
  `since` in **Serverzeit** – dadurch keine Probleme mit falsch gehenden
  Uhren alter Tablets, weniger Abhängigkeiten und kein
  `--hidden-import=engineio...` mehr beim PyInstaller-Build. Der
  Zeitstempel eines Messpunkts wird bewusst innerhalb der Sperre von
  `TopicStore.update()` vergeben, damit zwischen zwei Abrufen kein Punkt
  verloren geht.
- **Eigener Canvas-Renderer statt Chart.js/moment/socket.io-CDN:**
  funktioniert ohne Internet; pixelweise min/max-Reduktion
  (`pathIndices()`) hält Spitzen sichtbar.
- **ES5 + Flexbox ohne `gap` in der Ansicht:** v1.9 war ausdrücklich
  iOS-9-tauglich (Wandtablets) – das bleibt so. Der Einstellungsbereich
  nutzt dieselbe Technik.
- **Formel-Sicherheit:** v1.9 nutzte `eval` mit leeren Builtins
  (aushebelbar über Attributzugriffe). Jetzt AST-Positivliste, keine
  Attribute/Indizes, Exponent ≤ 100.
- **Port 5001** wie v1.9 beibehalten, damit vorhandene Lesezeichen
  weiter funktionieren; jetzt in den Einstellungen änderbar.

## Versionshistorie

### v2.0.5
- Einstellungen: Konfiguration **hochladen** (Bereich heißt jetzt
  „Konfiguration sichern & wiederherstellen“). Ablauf: Datei im Browser
  lesen → `POST /api/config/upload` mit `dry_run` → Vorschau zum Bestätigen
  → Übernahme. Jeder Eintrag läuft durch die normalen Validatoren,
  fehlerhafte werden mit Hinweis übersprungen; ungültige IDs werden neu
  vergeben. v1.x-Dateien werden umgewandelt (Port/Titel/Zeitraum bleiben).
  Vorherige Konfiguration wird als `config.json.vor-import-<Zeit>` gesichert
  (letzte 5). Fehlendes Broker-Passwort → gespeichertes bleibt (gleicher
  Broker/Benutzer). Broker wird nur bei Änderung neu verbunden. Uploads max. 5 MB.
- Meldungen im Browser mit echten Umlauten (vorher „ue“, „ae“ …).

### v2.0.4
- Einstellungen: neuer Bereich „Konfiguration sichern“ – lädt die komplette
  Konfiguration als `mqtt-dashboard-mk4-config_<Datum>.json` herunter
  (Format = `config.json`, direkt als Ersatz verwendbar). Broker-Passwort
  nur auf Wunsch (Häkchen, Route `GET /api/config/download?password=1`).
- Optik: Häkchen-Zeilen in den Einstellungen nicht mehr in Großbuchstaben.

### v2.0.3
- Fehler behoben: In der 24h-/48h-Ansicht endete die Linie mancher Diagramme
  mitten in der Anzeige. Ursache: max. 20 000 gespeicherte Punkte je Topic -
  bei einem Wert pro Sekunde reichte das nur ~5,5 h (bei 2 s ~11 h usw.).
  Jetzt zeitlich gestaffelter Speicher (`_Series`), der immer volle 49 h
  abdeckt und dabei sogar weniger Speicher braucht (max. ~13 300 Punkte je
  1-Hz-Topic statt 20 000).
- Browser: `trimData()` hat ältere Punkte bei jedem Abruf erneut halbiert
  (bei lange offener Seite wurden ältere Abschnitte immer lückenhafter).
  Ersetzt durch stabile zeitbasierte min/max-Verdichtung (`compactSeries()`).

### v2.0.2
- Projektname korrigiert: **MQTT-Dashboard-MK4** (vorher fälschlich MK3).
  Ordner `/root/MQTT-Dashboard-MK4`, Dienst `mqtt-dashboard-mk4`,
  Firewall-Regel `MQTT-Dashboard-MK4`, Zip `MQTT-Dashboard-MK4_v<Version>.zip`.
- `install.sh` übernimmt eine Installation unter dem alten Namen
  (`/root/MQTT-Dashboard-MK3`, Dienst `mqtt-dashboard-mk3`): Konfiguration
  kopieren, alten Dienst und alte Firewall-Regel entfernen.

### v2.0.1
- Installer `install.sh`/`uninstall.sh` für GL.iNet Brume 2 (OpenWrt):
  Python per opkg nur falls nötig, Kopie nach `/root/MQTT-Dashboard-MK4`,
  Übernahme einer v1.x-Konfiguration, Abschalten einer alten v1.x-Instanz
  auf Port 5001, procd-Dienst `mqtt-dashboard-mk4`, Firewall-Regel,
  Funktionstest. Anleitung: `INSTALL-BRUME2.md`.
- `vendor/`: Flask 3.1, Werkzeug, Jinja2, MarkupSafe (ohne C-Erweiterung),
  itsdangerous, click 8.1 (letzte Version für Python 3.9), blinker,
  importlib-metadata/zipp, paho-mqtt 2.1 - alles reine Python-Pakete,
  kompatibel ab Python 3.9 (OpenWrt 21.02 auf dem Brume 2).
- `app.py` nimmt `vendor/` automatisch in den Suchpfad auf (nicht in der exe).

### v2.0.0
- Komplett überarbeitet im Design des Drucker Dashboards MK6.
- Ansicht und Einstellungen getrennt; alle Diagramme, Elemente, Gruppen
  und virtuellen Topics nachträglich bearbeitbar, kopierbar, sortierbar.
- Diagramme: Linienfarben, Bezeichnungen, Einheit, feste Y-Achse,
  Höhe, Stufen-Darstellung, Fläche, eigener Zeitraum; Legende mit
  aktuellen Werten, Tooltip.
- Schalter/Slider mit optionalem Befehls-Topic und Retain; Anzeige mit
  Nachkommastellen und „aktiv“-Wert; zusätzliche Symbole.
- Broker mit Port, Benutzername, Passwort, Client-ID; jederzeit änderbar.
- `config.json` statt `mqtt_dashboard_config.json` (automatische Übernahme).
- GitHub Actions: Windows-exe + macOS-arm64 als Release, Version aus `APP_VERSION`.

### v1.9
- Flask-SocketIO-Dashboard, Elemente nur hinzufügen/löschen (nicht bearbeiten).
