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

## OpenWrt / GL.iNet Brume 2

Dafuer gibt es einen eigenen Installer mit Autostart, Firewall-Freigabe
und mitgelieferten Bibliotheken - siehe **`INSTALL-BRUME2.md`**
(kurz: Zip per `scp -O` nach `/tmp`, entpacken, `sh MQTT-Dashboard-MK4/install.sh`).
