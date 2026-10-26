# Installation auf dem GL.iNet Brume 2

Kurzfassung: Zip per `scp` auf den Brume 2 kopieren, entpacken,
**`sh install.sh`** ausführen – fertig. Der Installer richtet alles ein,
inklusive Autostart. Die Python-Bibliotheken (Flask, paho-mqtt) liegen
der Zip bei, dafür ist **kein Internet** nötig.

| | |
|---|---|
| Installationsordner | `/root/MQTT-Dashboard-MK3` |
| Konfiguration | `/root/MQTT-Dashboard-MK3/config.json` |
| Dienst (Autostart) | `/etc/init.d/mqtt-dashboard-mk3` |
| Webseite | `http://<IP-des-Brume>:5001` |

---

## Voraussetzungen

- Brume 2 mit GL.iNet-Firmware 4.x (OpenWrt), per SSH erreichbar.
  Benutzer `root`, Passwort = Admin-Passwort der GL.iNet-Oberfläche.
  Standard-IP ab Werk: `192.168.8.1`.
- **Python 3**: Ist es noch nicht installiert, holt der Installer es
  selbst per `opkg install python3` – nur dafür braucht der Brume 2
  **einmalig Internet**. Ist Python schon vorhanden (z. B. durch andere
  Dashboards), läuft die Installation komplett offline.

## Schritt 1 – Zip auf den Brume 2 kopieren

Auf dem PC (Windows-PowerShell, macOS- oder Linux-Terminal) im Ordner
mit der Zip-Datei:

```bash
scp -O MQTT-Dashboard-MK3_v2.0.1.zip root@192.168.8.1:/tmp/
```

> `-O` ist wichtig: Neuere `scp`-Versionen nutzen sonst SFTP, das der
> Brume 2 nicht anbietet (Fehlermeldung `sftp-server: not found`).
> Alternativ geht WinSCP mit Protokoll **SCP**.

## Schritt 2 – Entpacken und installieren

```bash
ssh root@192.168.8.1
cd /tmp && unzip -o MQTT-Dashboard-MK3_v2.0.1.zip && sh MQTT-Dashboard-MK3/install.sh
```

Am Ende zeigt der Installer die Adresse(n) an, z. B.:

```
  ======================================================
   MQTT Dashboard v2.0.1 laeuft.
  ======================================================
   http://192.168.8.1:5001
   http://192.168.178.40:5001
```

Die Zip in `/tmp` kann danach bleiben – `/tmp` wird beim Neustart
ohnehin geleert, das Programm liegt in `/root/MQTT-Dashboard-MK3`.

## Schritt 3 – Einrichten

Webseite im Browser öffnen. Beim ersten Start öffnen sich direkt die
**Einstellungen**: MQTT-Broker eintragen (läuft Mosquitto auf demselben
Brume 2: `127.0.0.1`), danach Diagramme, Schalter und Anzeigen anlegen.

---

## Was der Installer genau macht

1. Prüft Python 3 (ab 3.9) und installiert es bei Bedarf per `opkg`.
2. Kopiert das Programm nach `/root/MQTT-Dashboard-MK3`.
   **Eine vorhandene `config.json` wird nie überschrieben.**
3. Sucht eine alte v1.x-Konfiguration (`mqtt_dashboard_config.json`)
   unter `/root`, `/opt`, `/srv` und übernimmt sie automatisch
   (Broker, Gruppen, Diagramme, Schalter, Anzeigen, virtuelle Topics).
4. Stoppt eine alte v1.x-Instanz, die ebenfalls Port 5001 belegt:
   eigener Dienst in `/etc/init.d` wird deaktiviert, eine Zeile in
   `/etc/rc.local` auskommentiert (Sicherung wird angelegt). Die alten
   Dateien bleiben liegen. Startskripte, die zusätzlich andere
   Programme starten, werden nicht angefasst – dann gibt es nur einen
   Hinweis. (Abschalten mit `KEEP_OLD=1 sh install.sh`.)
5. Legt den Dienst `/etc/init.d/mqtt-dashboard-mk3` an (procd, startet
   nach jedem Neustart und nach einem Absturz automatisch neu).
6. Gibt den Port in der Firewall frei – auch über den WAN-Anschluss,
   damit das Dashboard erreichbar ist, wenn der Brume 2 über WAN im
   Heimnetz hängt. (Abschalten mit `NO_FIREWALL=1 sh install.sh`.)
7. Prüft, ob die Webseite antwortet, und zeigt die Adressen an.

## Update auf eine neue Version

Genau wie die Erstinstallation: neue Zip kopieren, entpacken,
`sh MQTT-Dashboard-MK3/install.sh`. Einstellungen bleiben erhalten.

## Nützliche Befehle

```bash
/etc/init.d/mqtt-dashboard-mk3 restart     # neu starten (z. B. nach Portwechsel)
/etc/init.d/mqtt-dashboard-mk3 stop        # anhalten
logread | grep python3 | tail -n 40        # Protokoll
sh /root/MQTT-Dashboard-MK3/uninstall.sh   # Dienst entfernen (Ordner bleibt)
sh /root/MQTT-Dashboard-MK3/uninstall.sh --all   # alles entfernen
```

## Probleme

| Meldung | Lösung |
|---|---|
| `sftp-server: not found` beim Kopieren | `scp -O …` verwenden (siehe Schritt 1) |
| `unzip: not found` | `opkg update && opkg install unzip` (braucht Internet) |
| `opkg update fehlgeschlagen` | Python fehlt und der Brume 2 hat kein Internet – kurz mit dem Internet verbinden und `install.sh` erneut starten |
| `Port 5001 ist noch belegt` | anderes Programm nutzt 5001 → in `config.json` unter `server.port` einen freien Port eintragen, dann `/etc/init.d/mqtt-dashboard-mk3 restart` und `sh /root/MQTT-Dashboard-MK3/install.sh` (passt die Firewall-Freigabe an) |
| Webseite nicht erreichbar | Protokoll ansehen (siehe oben); manuell testen mit `PYTHONPATH=/root/MQTT-Dashboard-MK3/vendor python3 /root/MQTT-Dashboard-MK3/app.py` |
