#!/bin/sh
# ======================================================================
#  MQTT Dashboard (MK3) - Installer fuer GL.iNet Brume 2 (OpenWrt)
#
#  Aufruf (im entpackten Ordner):   sh install.sh
#
#  Was passiert:
#   1. Python 3 pruefen (nur wenn es fehlt: "opkg install python3",
#      dafuer braucht der Brume 2 einmalig Internet)
#   2. Programm nach /root/MQTT-Dashboard-MK3 kopieren
#      (vorhandene config.json bleibt erhalten = Update-faehig)
#   3. alte v1.x-Konfiguration (mqtt_dashboard_config.json) uebernehmen
#   4. alte v1.x-Instanz auf demselben Port stoppen
#   5. Autostart-Dienst /etc/init.d/mqtt-dashboard-mk3 einrichten + starten
#   6. Firewall-Freigabe fuer den Port (auch ueber den WAN-Anschluss)
#   7. Funktionstest + Ausgabe der Adresse
#
#  Die Python-Bibliotheken (Flask, paho-mqtt) liegen im Ordner "vendor"
#  bei - dafuer ist KEIN Internet und kein pip noetig.
#
#  Optionen (Umgebungsvariablen):
#   TARGET=/pfad      anderer Installationsordner (Standard /root/MQTT-Dashboard-MK3)
#   NO_FIREWALL=1     keine Firewall-Regel anlegen
#   KEEP_OLD=1        alte v1.x-Instanz NICHT stoppen
# ======================================================================

set -u

APP_NAME="MQTT-Dashboard-MK3"
SERVICE="mqtt-dashboard-mk3"
TARGET="${TARGET:-/root/$APP_NAME}"
INIT="/etc/init.d/$SERVICE"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_PORT=5001

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;35m  !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*"; exit 1; }

echo ""
echo "  MQTT Dashboard (MK3) - Installation auf dem Brume 2"
echo "  ---------------------------------------------------"

[ "$(id -u)" = "0" ] || die "Bitte als root ausfuehren (ssh root@<IP-des-Brume>)."
[ -f "$SRC/app.py" ] || die "app.py nicht gefunden - install.sh bitte im entpackten Ordner $APP_NAME ausfuehren."
[ -d "$SRC/vendor" ] || die "Ordner 'vendor' fehlt - bitte die vollstaendige Zip-Datei verwenden."

APP_VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$SRC/app.py" | head -n 1)"
say "Version $APP_VERSION, Quelle: $SRC, Ziel: $TARGET"

# ----------------------------------------------------------------------
# 1. Python 3
# ----------------------------------------------------------------------
say "Pruefe Python 3 ..."
PY_TEST='import sys, json, ast, uuid, socket, threading, colorsys, logging, email, http.server, decimal; sys.exit(0 if sys.version_info >= (3, 9) else 3)'

python_ok() {
    PY="$(command -v python3 2>/dev/null)"
    [ -n "$PY" ] && "$PY" -c "$PY_TEST" >/dev/null 2>&1
}

if ! python_ok; then
    if command -v python3 >/dev/null 2>&1; then
        PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
        warn "Python $PYV vorhanden, aber unvollstaendig oder aelter als 3.9 - ergaenze Pakete."
    else
        warn "Python 3 ist nicht installiert - installiere es (braucht einmalig Internet)."
    fi
    command -v opkg >/dev/null 2>&1 || die "opkg nicht gefunden - ist das wirklich ein OpenWrt/GL.iNet-Geraet?"
    opkg update || die "opkg update fehlgeschlagen - hat der Brume 2 Internet? (Test: ping -c1 openwrt.org)"
    opkg install python3 || die "Installation von python3 fehlgeschlagen."
    python_ok || die "Python 3 laeuft nach der Installation noch nicht korrekt (mind. 3.9 noetig)."
fi
PY="$(command -v python3)"
ok "$("$PY" --version 2>&1) ($PY)"

# mitgelieferte Bibliotheken pruefen
PYTHONPATH="$SRC/vendor" "$PY" -c 'import flask, paho.mqtt.client' >/dev/null 2>&1 \
    || die "Mitgelieferte Bibliotheken lassen sich nicht laden: PYTHONPATH=$SRC/vendor $PY -c 'import flask'"
ok "Flask und paho-mqtt (mitgeliefert)"

# ----------------------------------------------------------------------
# 2. Dateien kopieren
# ----------------------------------------------------------------------
say "Kopiere Programm nach $TARGET ..."
if [ -x "$INIT" ]; then
    "$INIT" stop >/dev/null 2>&1
fi
mkdir -p "$TARGET" || die "Ordner $TARGET kann nicht angelegt werden."
if [ "$SRC" != "$TARGET" ]; then
    rm -rf "$TARGET/vendor"
    for f in app.py vendor install.sh uninstall.sh README.md INSTALL-BRUME2.md LINUX-INSTALL.md UEBERGABE.md \
             config.example.json requirements.txt; do
        [ -e "$SRC/$f" ] && cp -a "$SRC/$f" "$TARGET/"
    done
    ok "Dateien kopiert (eine vorhandene config.json bleibt unveraendert)"
else
    ok "Installer laeuft bereits im Zielordner - nichts zu kopieren"
fi
chmod +x "$TARGET/install.sh" "$TARGET/uninstall.sh" 2>/dev/null

# ----------------------------------------------------------------------
# 3. alte v1.x-Konfiguration uebernehmen
# ----------------------------------------------------------------------
if [ ! -f "$TARGET/config.json" ] && [ ! -f "$TARGET/mqtt_dashboard_config.json" ]; then
    OLD_CFG="$(find /root /opt /srv -maxdepth 3 -name mqtt_dashboard_config.json 2>/dev/null | grep -v "^$TARGET/" | head -n 1)"
    if [ -n "$OLD_CFG" ]; then
        cp "$OLD_CFG" "$TARGET/mqtt_dashboard_config.json"
        ok "v1.x-Konfiguration gefunden und uebernommen: $OLD_CFG"
    fi
fi

# Port aus config.json (falls schon vorhanden)
PORT="$DEFAULT_PORT"
if [ -f "$TARGET/config.json" ]; then
    P="$("$PY" -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("server",{}).get("port",5001)))' "$TARGET/config.json" 2>/dev/null)"
    [ -n "$P" ] && PORT="$P"
fi

# ----------------------------------------------------------------------
# 4. alte v1.x-Instanz stoppen (gleicher Port 5001)
# ----------------------------------------------------------------------
if [ "${KEEP_OLD:-0}" != "1" ]; then
    for s in $(grep -l "mqtt_dashboard" /etc/init.d/* 2>/dev/null); do
        [ "$s" = "$INIT" ] && continue
        # Sammel-Startskripte (starten noch andere Programme) NICHT anfassen
        if grep -o '[A-Za-z0-9_./-]*\.py' "$s" | grep -v "mqtt_dashboard" | grep -q .; then
            warn "$s startet auch andere Programme - bitte die mqtt_dashboard-Zeile dort selbst entfernen."
            continue
        fi
        "$s" stop >/dev/null 2>&1
        "$s" disable >/dev/null 2>&1
        ok "alten Dienst $s gestoppt und deaktiviert (Dateien bleiben erhalten)"
    done
    if [ -f /etc/rc.local ] && grep -q "mqtt_dashboard" /etc/rc.local; then
        cp /etc/rc.local /etc/rc.local.vor-mqtt-dashboard-mk3
        sed -i '/mqtt_dashboard/s/^\([^#]\)/# \1/' /etc/rc.local
        ok "Autostart-Zeile in /etc/rc.local auskommentiert (Sicherung: /etc/rc.local.vor-mqtt-dashboard-mk3)"
    fi
    for pid in $(ps w | grep "[m]qtt_dashboard" | grep -v "$TARGET" | awk '{print $1}'); do
        kill "$pid" 2>/dev/null && ok "laufenden v1.x-Prozess $pid beendet"
    done
    sleep 1
fi

if netstat -tln 2>/dev/null | grep -q ":$PORT "; then
    warn "Port $PORT ist noch von einem anderen Programm belegt:"
    netstat -tlnp 2>/dev/null | grep ":$PORT " | sed 's/^/        /'
    warn "Das Dashboard startet trotzdem; ggf. Port in $TARGET/config.json (server.port) aendern und"
    warn "danach '$INIT restart' ausfuehren."
fi

# ----------------------------------------------------------------------
# 5. Autostart-Dienst
# ----------------------------------------------------------------------
say "Richte Autostart-Dienst $INIT ein ..."
cat > "$INIT" <<EOF
#!/bin/sh /etc/rc.common
# MQTT Dashboard (MK3) - angelegt von install.sh
START=99
STOP=10
USE_PROCD=1

APP_DIR="$TARGET"
PYTHON="$PY"

start_service() {
    procd_open_instance $SERVICE
    procd_set_param command "\$PYTHON" -u "\$APP_DIR/app.py"
    procd_set_param env PYTHONPATH="\$APP_DIR/vendor" PYTHONUNBUFFERED=1
    procd_set_param respawn 3600 5 0
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
EOF
chmod +x "$INIT"
"$INIT" enable
"$INIT" restart >/dev/null 2>&1 || "$INIT" start
ok "Dienst aktiviert (startet automatisch nach jedem Neustart)"

# ----------------------------------------------------------------------
# 6. Firewall
# ----------------------------------------------------------------------
if [ "${NO_FIREWALL:-0}" != "1" ] && command -v uci >/dev/null 2>&1; then
    if uci show firewall 2>/dev/null | grep -q "name='$APP_NAME'"; then
        IDX="$(uci show firewall | grep "\.name='$APP_NAME'" | head -n 1 | cut -d. -f2)"
        [ -n "$IDX" ] && uci set "firewall.$IDX.dest_port=$PORT"
    else
        uci add firewall rule >/dev/null
        uci set firewall.@rule[-1].name="$APP_NAME"
        uci set firewall.@rule[-1].src='wan'
        uci set firewall.@rule[-1].proto='tcp'
        uci set firewall.@rule[-1].dest_port="$PORT"
        uci set firewall.@rule[-1].target='ACCEPT'
    fi
    uci commit firewall
    /etc/init.d/firewall reload >/dev/null 2>&1
    ok "Firewall: TCP-Port $PORT auch ueber den WAN-Anschluss erreichbar"
fi

# ----------------------------------------------------------------------
# 7. Funktionstest
# ----------------------------------------------------------------------
say "Warte auf das Dashboard ..."
UP=0
i=0
while [ $i -lt 20 ]; do
    if wget -q -O - "http://127.0.0.1:$PORT/api/version" 2>/dev/null | grep -q version; then
        UP=1; break
    fi
    i=$((i + 1)); sleep 1
done

IPS="$(ip -4 -o addr show 2>/dev/null | awk '{print $2" "$4}' | grep -v '^lo ' | sed 's#/.*##' | awk '{print $2}')"
[ -z "$IPS" ] && IPS="$(uci -q get network.lan.ipaddr 2>/dev/null)"
[ -z "$IPS" ] && IPS="<IP-des-Brume>"

echo ""
if [ "$UP" = "1" ]; then
    echo "  ======================================================"
    echo "   MQTT Dashboard v$APP_VERSION laeuft."
    echo "  ======================================================"
    for ip in $IPS; do
        echo "   http://$ip:$PORT"
    done
    echo ""
    echo "   Ordner:      $TARGET  (Konfiguration: config.json)"
    echo "   Dienst:      $INIT {start|stop|restart}"
    echo "   Protokoll:   logread | grep python3"
    echo "   Deinstall.:  sh $TARGET/uninstall.sh"
    echo ""
else
    warn "Das Dashboard antwortet nach 20 s noch nicht auf Port $PORT."
    warn "Protokoll ansehen mit:  logread | grep python3 | tail -n 40"
    warn "Manuell testen mit:     PYTHONPATH=$TARGET/vendor $PY $TARGET/app.py"
    exit 1
fi
