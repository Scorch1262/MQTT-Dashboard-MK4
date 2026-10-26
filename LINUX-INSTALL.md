# Installation unter Linux / OpenWrt (z. B. GL.iNet Brume 2)

Das Dashboard braucht nur Python 3.8+, `flask` und `paho-mqtt`.

## Debian/Ubuntu/Raspberry Pi OS

```bash
sudo apt install python3 python3-venv
cd /opt && sudo git clone https://github.com/<user>/<repo>.git mqtt-dashboard
cd mqtt-dashboard
python3 -m venv venv && venv/bin/pip install flask paho-mqtt
venv/bin/python app.py        # -> http://<IP>:5001
```

Autostart per systemd (`/etc/systemd/system/mqtt-dashboard.service`):

```ini
[Unit]
Description=MQTT Dashboard
After=network-online.target

[Service]
WorkingDirectory=/opt/mqtt-dashboard
ExecStart=/opt/mqtt-dashboard/venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now mqtt-dashboard
```

## OpenWrt (GL.iNet Brume 2 u. a.)

```sh
opkg update
opkg install python3 python3-pip
pip3 install flask paho-mqtt
mkdir -p /root/MQTT-Dashboard-MK3 && cd /root/MQTT-Dashboard-MK3
# app.py hierher kopieren (z. B. per scp), dann:
python3 app.py
```

Autostart (`/etc/init.d/mqtt-dashboard`, danach `chmod +x` und
`/etc/init.d/mqtt-dashboard enable && /etc/init.d/mqtt-dashboard start`):

```sh
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1

start_service() {
    procd_open_instance
    procd_set_param command /usr/bin/python3 /root/MQTT-Dashboard-MK3/app.py
    procd_set_param respawn
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}
```

`config.json` liegt danach in `/root/MQTT-Dashboard-MK3/`. Läuft auf
demselben Gerät ein Mosquitto, als Broker `127.0.0.1` eintragen.
