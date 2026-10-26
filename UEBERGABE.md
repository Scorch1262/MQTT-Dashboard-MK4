# Übergabe – MQTT Dashboard (MK3)

Technische Übergabe für Weiterentwicklung. Nutzerdoku siehe `README.md`.

## Architektur (seit v2.0.0)

Eine Datei `app.py`, Aufbau von oben nach unten:

| Abschnitt | Inhalt |
|---|---|
| Konstanten | `APP_VERSION`, Zeiträume, Gerätesymbole, Farbpalette, `DEFAULT_CONFIG` |
| Formeln | `compile_formula()` prüft per AST gegen eine Positivliste, `eval_formula()` |
| Validierung | `validate_group/_chart/_widget/_virtual` – einzige Stelle, an der Eingaben geprüft werden |
| `migrate_legacy()` | Umwandlung v1.x-`mqtt_dashboard_config.json` → v2-`config.json` |
| `ConfigStore` | Laden/Normalisieren/atomares Speichern (`.tmp` + `os.replace`), defekte Datei wird als `config.json.defekt-<ts>` gesichert |
| `TopicStore` | Werte + Verlauf im Speicher (Deque je Topic, 1 Punkt/s, 49 h, 20 000 Punkte), min/max-Ausdünnung |
| `VirtualEngine` | berechnet virtuelle Topics bei Änderung einer Quelle (bis Tiefe 3, virtuell auf virtuell möglich) |
| `MqttManager` | paho-Client mit `loop_start`, Auto-Reconnect, Neustart bei Broker-Änderung; 1.6.x und 2.x kompatibel |
| REST-API | siehe unten |
| `INDEX_HTML` | komplettes Frontend (CSS/HTML/JS), wird **nicht** durch Jinja geschickt |
| `main()` | Start, Startmeldung mit LAN-IP |

### REST-API

| Route | Zweck |
|---|---|
| `GET /api/config` | komplette Konfiguration (Passwort entfernt, `password_set`) |
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

### v2.0.1
- Installer `install.sh`/`uninstall.sh` für GL.iNet Brume 2 (OpenWrt):
  Python per opkg nur falls nötig, Kopie nach `/root/MQTT-Dashboard-MK3`,
  Übernahme einer v1.x-Konfiguration, Abschalten einer alten v1.x-Instanz
  auf Port 5001, procd-Dienst `mqtt-dashboard-mk3`, Firewall-Regel,
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
