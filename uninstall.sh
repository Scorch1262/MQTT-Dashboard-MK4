#!/bin/sh
# MQTT Dashboard (MK4) - Deinstallation auf dem GL.iNet Brume 2
#
#   sh uninstall.sh          Dienst + Firewall-Regel entfernen, Ordner bleibt
#   sh uninstall.sh --all    zusaetzlich den Programmordner samt config.json loeschen

APP_NAME="MQTT-Dashboard-MK4"
INIT="/etc/init.d/mqtt-dashboard-mk4"
TARGET="${TARGET:-/root/$APP_NAME}"

if [ -x "$INIT" ]; then
    "$INIT" stop >/dev/null 2>&1
    "$INIT" disable >/dev/null 2>&1
    rm -f "$INIT"
    echo "Dienst entfernt."
fi

if command -v uci >/dev/null 2>&1; then
    IDX="$(uci show firewall 2>/dev/null | grep "\.name='$APP_NAME'" | head -n 1 | cut -d. -f2)"
    if [ -n "$IDX" ]; then
        uci delete "firewall.$IDX"
        uci commit firewall
        /etc/init.d/firewall reload >/dev/null 2>&1
        echo "Firewall-Regel entfernt."
    fi
fi

if [ "$1" = "--all" ]; then
    cd / && rm -rf "$TARGET"
    echo "Ordner $TARGET geloescht."
else
    echo "Ordner $TARGET (inkl. config.json) bleibt erhalten - loeschen mit: sh uninstall.sh --all"
fi
