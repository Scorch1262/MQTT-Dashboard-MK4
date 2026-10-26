# -*- coding: utf-8 -*-
"""
MQTT Dashboard (MK4) - v2
=========================

Lokales Web-Dashboard fuer MQTT-Topics: Diagramme, Schalter, Slider und
Anzeigen, frei in Gruppen sortierbar. Ein Python-Skript (`app.py`), keine
Cloud, kein Account.

Seit v2.0.0 im Design und mit dem Bedienkonzept des Drucker Dashboards
(MK6):

  * ANSICHT (Standard): nur anzeigen und bedienen - Diagramme, Schalter,
    Slider, Anzeigen. Kein Hinzufuegen/Loeschen hier.
  * EINSTELLUNGEN (Knopf oben rechts): Broker, Gruppen, Diagramme,
    Schalter/Anzeigen und virtuelle Topics anlegen, BEARBEITEN, kopieren,
    sortieren und entfernen.

Alle Einstellungen liegen menschenlesbar in `config.json` neben der exe
bzw. neben diesem Skript. Eine vorhandene `mqtt_dashboard_config.json`
aus v1.x wird beim ersten Start automatisch uebernommen.

Abhaengigkeiten: flask, paho-mqtt (1.6.x und 2.x werden beide
unterstuetzt). Kein Flask-SocketIO mehr (siehe UEBERGABE.md).

Versionierung: APP_VERSION ist die einzige Quelle der Wahrheit - der
GitHub-Actions-Workflow liest sie aus und benennt Release/Tag danach.
"""

APP_VERSION = "2.0.2"

import os
import sys
import json
import time
import math
import ast
import uuid
import copy
import socket
import colorsys
import threading
from collections import deque

# v2.0.1: mitgelieferte Python-Bibliotheken (Ordner "vendor" neben app.py,
# z. B. fuer die Offline-Installation auf dem GL.iNet Brume 2 per
# install.sh). Fehlt der Ordner oder laeuft die gebaute exe, wird ganz
# normal aus den installierten Paketen geladen.
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if not getattr(sys, "frozen", False) and os.path.isdir(_VENDOR_DIR):
    sys.path.insert(0, _VENDOR_DIR)

from flask import Flask, Response, jsonify, request
import paho.mqtt.client as mqtt


app = Flask(__name__)


# ----------------------------------------------------------------------
# Pfade / Konstanten
# ----------------------------------------------------------------------
def base_dir() -> str:
    """Ordner der exe (PyInstaller --onefile) bzw. dieses Skripts."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(base_dir(), "config.json")
# v1.x-Konfiguration - wird beim ersten Start von v2 einmalig uebernommen
LEGACY_CONFIG_PATH = os.path.join(base_dir(), "mqtt_dashboard_config.json")

RANGE_CHOICES_H = [1, 6, 12, 24, 48]
MAX_RANGE_H = 48
RETAIN_MS = (MAX_RANGE_H + 1) * 3600 * 1000
MAX_POINTS_PER_TOPIC = 20000      # wie v1.9
STORE_MIN_INTERVAL_MS = 1000      # max. 1 gespeicherter Messpunkt/s je Topic (wie v1.9)
MAX_SEND_POINTS = 1500            # max. Punkte je Topic beim Erstabruf
VAR_NAMES = "abcdefgh"            # Variablen fuer virtuelle Topics (v1.9: a-f)
SELF_PUBLISH_WINDOW_S = 5.0

WIDGET_TYPES = ("switch", "slider", "display")
CHART_HEIGHTS = ("s", "m", "l")
DEVICE_KEYS = (
    "lamp", "fan", "camera", "shutter", "motion", "plug", "heating", "ac",
    "lock", "speaker", "sensor", "temperature", "power", "solar", "water",
    "door", "washer", "server", "generic",
)

# Akzentfarben im MK6-Stil (Gruppen + Standardfarben der Diagrammlinien)
GROUP_PALETTE = [
    "#ff9142", "#3ddc97", "#4aa3ff", "#ff5d5d", "#e8b23d", "#b38cff",
    "#2ed3d3", "#ff7ac6", "#9aa5b1", "#7bd88f", "#c49a6c", "#5b8cff",
]
# Gruppenfarben aus v1.9 (dunkle Hintergruende) -> gleiche Position in GROUP_PALETTE
LEGACY_GROUP_COLORS = [
    "#1e2d1e", "#1e1e2d", "#2d1e1e", "#2d2a1e", "#1e2a2d", "#2a1e2d",
    "#263020", "#202630", "#302020", "#303025", "#202530", "#252030",
]

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 5001},
    "title": "MQTT Dashboard",
    "default_range_h": 24,
    "broker": {
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "",
    },
    "groups": [],
    "charts": [],
    "widgets": [],
    "virtual_topics": [],
}

COLLECTIONS = ("groups", "charts", "widgets", "virtual_topics")
ID_PREFIX = {"groups": "grp", "charts": "chart", "widgets": "w", "virtual_topics": "vt"}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


# ----------------------------------------------------------------------
# Kleine Helfer fuer Validierung
# ----------------------------------------------------------------------
def _str(v, maxlen=200) -> str:
    if v is None:
        return ""
    return str(v).strip()[:maxlen]


def _float_or_none(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("'%s' ist keine Zahl." % v)
    if not math.isfinite(f):
        raise ValueError("'%s' ist keine gueltige Zahl." % v)
    return f


def _hex_color(v, fallback):
    s = _str(v, 7).lower()
    if len(s) == 7 and s[0] == "#":
        try:
            int(s[1:], 16)
            return s
        except ValueError:
            pass
    return fallback


def _hsl_to_hex(value, fallback):
    """v1.9 speicherte Linienfarben als 'hsl(h,70%,50%)'."""
    try:
        s = str(value).strip()
        if s.startswith("#"):
            return _hex_color(s, fallback)
        inner = s[s.index("(") + 1:s.index(")")]
        h, sat, light = [p.strip().rstrip("%") for p in inner.split(",")]
        r, g, b = colorsys.hls_to_rgb(float(h) / 360.0, float(light) / 100.0, float(sat) / 100.0)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        return fallback


# ----------------------------------------------------------------------
# Sichere Formeln fuer virtuelle Topics
# ----------------------------------------------------------------------
# v1.9 nutzte eval() mit leeren __builtins__ - das ist ueber Attributzugriffe
# ((). __class__ ...) aushebelbar. Seit v2.0.0 wird die Formel vorab per AST
# auf eine Positivliste geprueft: nur Zahlen, Variablen a-h, Rechen-/Vergleichs-
# operatoren und die Funktionen unten.
SAFE_FUNCS = {
    "abs": abs, "min": min, "max": max, "round": round, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
SAFE_CONSTS = {"pi": math.pi}
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def compile_formula(formula: str, n_vars: int):
    formula = (formula or "").strip()
    if not formula:
        raise ValueError("Formel fehlt.")
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError:
        raise ValueError("Formel ist syntaktisch ungueltig.")
    allowed_vars = set(VAR_NAMES[:n_vars])
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError("Nicht erlaubter Ausdruck in der Formel: %s" % type(node).__name__)
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("In der Formel sind nur Zahlen als Konstanten erlaubt.")
        if isinstance(node, ast.Name):
            if node.id not in allowed_vars and node.id not in SAFE_FUNCS and node.id not in SAFE_CONSTS:
                raise ValueError(
                    "Unbekannter Name '%s' - erlaubt sind %s sowie %s."
                    % (node.id, ", ".join(sorted(allowed_vars)) or "(keine Variablen)",
                       ", ".join(sorted(list(SAFE_FUNCS) + list(SAFE_CONSTS))))
                )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCS:
                raise ValueError("Nur die erlaubten Funktionen koennen aufgerufen werden.")
            if node.keywords:
                raise ValueError("Funktionsaufrufe mit Schluesselwort-Argumenten sind nicht erlaubt.")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float)) \
                    and abs(node.right.value) > 100:
                raise ValueError("Exponent zu gross (max. 100).")
    return compile(tree, "<formel>", "eval")


def eval_formula(code, values):
    env = dict(SAFE_FUNCS)
    env.update(SAFE_CONSTS)
    env.update(dict(zip(VAR_NAMES, values)))
    result = eval(code, {"__builtins__": {}}, env)  # noqa: S307 - per AST geprueft
    result = float(result)
    if not math.isfinite(result):
        raise ValueError("Ergebnis ist keine endliche Zahl.")
    return result


# ----------------------------------------------------------------------
# Validierung der Konfigurations-Eintraege
# ----------------------------------------------------------------------
def _group_ids(cfg):
    return {g["id"] for g in cfg.get("groups", [])}


def _clean_group_id(raw, cfg):
    gid = _str(raw, 64)
    if not gid or gid == "default":
        return None
    return gid if gid in _group_ids(cfg) else None


def validate_group(data, cfg, item_id=None):
    name = _str(data.get("name"), 60)
    if not name:
        raise ValueError("Bitte einen Namen fuer die Gruppe angeben.")
    return {"name": name, "color": _hex_color(data.get("color"), GROUP_PALETTE[0])}


def validate_chart(data, cfg, item_id=None):
    name = _str(data.get("name"), 80) or "Diagramm"
    series_in = data.get("series") or []
    if not isinstance(series_in, list):
        raise ValueError("Ungueltige Topic-Liste.")
    series = []
    for i, s in enumerate(series_in):
        if not isinstance(s, dict):
            continue
        topic = _str(s.get("topic"), 300)
        if not topic:
            continue
        series.append({
            "topic": topic,
            "label": _str(s.get("label"), 60),
            "color": _hex_color(s.get("color"), GROUP_PALETTE[i % len(GROUP_PALETTE)]),
        })
    if not series:
        raise ValueError("Bitte mindestens ein Topic fuer das Diagramm angeben.")
    if len(series) > 12:
        raise ValueError("Maximal 12 Topics je Diagramm.")
    y_min = _float_or_none(data.get("y_min"))
    y_max = _float_or_none(data.get("y_max"))
    if y_min is not None and y_max is not None and y_min >= y_max:
        raise ValueError("Y-Minimum muss kleiner als Y-Maximum sein.")
    range_h = data.get("range_h")
    try:
        range_h = int(range_h) if range_h not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        range_h = None
    if range_h is not None and range_h not in RANGE_CHOICES_H:
        range_h = None
    height = _str(data.get("height"), 1) or "m"
    return {
        "name": name,
        "group_id": _clean_group_id(data.get("group_id"), cfg),
        "series": series,
        "unit": _str(data.get("unit"), 16),
        "y_min": y_min,
        "y_max": y_max,
        "height": height if height in CHART_HEIGHTS else "m",
        "stepped": bool(data.get("stepped")),
        "fill": bool(data.get("fill")),
        "range_h": range_h,
    }


def validate_widget(data, cfg, item_id=None):
    wtype = _str(data.get("type"), 10)
    if wtype not in WIDGET_TYPES:
        raise ValueError("Unbekannter Element-Typ.")
    name = _str(data.get("name"), 60)
    topic = _str(data.get("topic"), 300)
    if not name:
        raise ValueError("Bitte eine Bezeichnung angeben.")
    if not topic:
        raise ValueError("Bitte ein Topic angeben.")
    device = _str(data.get("device"), 20)
    out = {
        "type": wtype,
        "name": name,
        "topic": topic,
        "device": device if device in DEVICE_KEYS else "generic",
        "group_id": _clean_group_id(data.get("group_id"), cfg),
        "unit": _str(data.get("unit"), 16),
    }
    if wtype in ("switch", "slider"):
        out["command_topic"] = _str(data.get("command_topic"), 300)
        out["retain"] = bool(data.get("retain"))
    if wtype == "switch":
        on = _str(data.get("payload_on"), 100)
        off = _str(data.get("payload_off"), 100)
        if on == "" or off == "":
            raise ValueError("Bitte Payload fuer AN und AUS angeben (z. B. 1 / 0 oder ON / OFF).")
        if on == off:
            raise ValueError("Payload fuer AN und AUS muessen sich unterscheiden.")
        out["payload_on"] = on
        out["payload_off"] = off
    elif wtype == "slider":
        mn = _float_or_none(data.get("min"))
        mx = _float_or_none(data.get("max"))
        st = _float_or_none(data.get("step"))
        mn = 0.0 if mn is None else mn
        mx = 100.0 if mx is None else mx
        st = 1.0 if st is None else st
        if mn >= mx:
            raise ValueError("Minimum muss kleiner als Maximum sein.")
        if st <= 0:
            raise ValueError("Schrittweite muss groesser als 0 sein.")
        out.update({"min": mn, "max": mx, "step": st})
    else:  # display
        out["payload_on"] = _str(data.get("payload_on"), 100)
        dec = data.get("decimals")
        try:
            dec = int(dec) if dec not in (None, "") else None
        except (TypeError, ValueError):
            dec = None
        if dec is not None:
            dec = max(0, min(6, dec))
        out["decimals"] = dec
    return out


def validate_virtual(data, cfg, item_id=None):
    name = _str(data.get("name"), 300)
    if not name:
        raise ValueError("Bitte einen Namen (Topic) fuer das virtuelle Topic angeben.")
    sources = [_str(s, 300) for s in (data.get("sources") or []) if _str(s, 300)]
    if not sources:
        raise ValueError("Bitte mindestens ein Quell-Topic angeben.")
    if len(sources) > len(VAR_NAMES):
        raise ValueError("Maximal %d Quell-Topics (Variablen %s)." % (len(VAR_NAMES), ", ".join(VAR_NAMES)))
    if name in sources:
        raise ValueError("Ein virtuelles Topic darf nicht von sich selbst abhaengen.")
    for vt in cfg.get("virtual_topics", []):
        if vt["id"] != item_id and vt["name"] == name:
            raise ValueError("Es gibt bereits ein virtuelles Topic mit diesem Namen.")
    formula = _str(data.get("formula"), 500)
    compile_formula(formula, len(sources))  # wirft ValueError mit Klartext
    return {"name": name, "sources": sources, "formula": formula,
            "unit": _str(data.get("unit"), 16)}


VALIDATORS = {
    "groups": validate_group,
    "charts": validate_chart,
    "widgets": validate_widget,
    "virtual_topics": validate_virtual,
}


# ----------------------------------------------------------------------
# Konfiguration (config.json)
# ----------------------------------------------------------------------
def migrate_legacy(old: dict) -> dict:
    """Uebernimmt eine v1.x-Konfiguration (mqtt_dashboard_config.json)."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["broker"]["host"] = _str((old.get("broker") or {}).get("ip"), 200)
    items = old.get("items", old.get("widgets", [])) or []
    groups, charts, widgets, vts = [], [], [], []
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        t = it.get("type")
        gid = it.get("group_id")
        gid = None if gid in (None, "", "default") else gid
        if t == "group":
            col = (it.get("color") or "").lower()
            if col in LEGACY_GROUP_COLORS:
                col = GROUP_PALETTE[LEGACY_GROUP_COLORS.index(col)]
            groups.append({"id": it["id"], "name": _str(it.get("name"), 60) or "Gruppe",
                           "color": _hex_color(col, GROUP_PALETTE[len(groups) % len(GROUP_PALETTE)])})
        elif t == "virtual":
            vts.append({"id": it["id"], "name": _str(it.get("vname"), 300),
                        "sources": list(it.get("sources") or []),
                        "formula": _str(it.get("formula"), 500), "unit": ""})
        elif t == "chart":
            series = []
            for i, (topic, col) in enumerate((it.get("topics") or {}).items()):
                fb = GROUP_PALETTE[i % len(GROUP_PALETTE)]
                series.append({"topic": topic, "label": "", "color": _hsl_to_hex(col, fb)})
            charts.append({"id": it["id"], "name": _str(it.get("name"), 80) or "Diagramm",
                           "group_id": gid, "series": series, "unit": "", "y_min": None,
                           "y_max": None, "height": "m", "stepped": False, "fill": False,
                           "range_h": None})
        elif t in ("button", "slider", "display"):
            w = {"id": it["id"], "name": _str(it.get("name"), 60) or "Element",
                 "topic": _str(it.get("topic"), 300), "group_id": gid, "unit": "",
                 "device": it.get("device") if it.get("device") in DEVICE_KEYS else "generic"}
            if t == "button":
                w.update({"type": "switch", "payload_on": _str(it.get("payload_on"), 100) or "1",
                          "payload_off": _str(it.get("payload_off"), 100) or "0",
                          "command_topic": "", "retain": False})
            elif t == "slider":
                w.update({"type": "slider", "min": float(it.get("slider_min", 0) or 0),
                          "max": float(it.get("slider_max", 100) or 100),
                          "step": float(it.get("slider_step", 1) or 1),
                          "command_topic": "", "retain": False})
            else:
                w.update({"type": "display", "payload_on": _str(it.get("payload_on"), 100),
                          "decimals": None})
            widgets.append(w)
    cfg["groups"], cfg["charts"], cfg["widgets"], cfg["virtual_topics"] = groups, charts, widgets, vts
    return cfg


def normalize_config(cfg: dict) -> dict:
    for key, default in DEFAULT_CONFIG.items():
        if key not in cfg or not isinstance(cfg[key], type(default)):
            cfg[key] = copy.deepcopy(default)
    for key, default in DEFAULT_CONFIG["server"].items():
        cfg["server"].setdefault(key, default)
    for key, default in DEFAULT_CONFIG["broker"].items():
        cfg["broker"].setdefault(key, default)
    try:
        cfg["server"]["port"] = int(cfg["server"]["port"])
    except (TypeError, ValueError):
        cfg["server"]["port"] = DEFAULT_CONFIG["server"]["port"]
    try:
        cfg["broker"]["port"] = int(cfg["broker"]["port"])
    except (TypeError, ValueError):
        cfg["broker"]["port"] = 1883
    if cfg.get("default_range_h") not in RANGE_CHOICES_H:
        cfg["default_range_h"] = 24
    gids = _group_ids(cfg)
    for coll in COLLECTIONS:
        clean = []
        for idx, item in enumerate(cfg[coll]):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item.setdefault("order", idx)
            if coll in ("charts", "widgets"):
                if item.get("group_id") not in gids:
                    item["group_id"] = None
            clean.append(item)
        cfg[coll] = clean
    return cfg


class ConfigStore:
    def __init__(self, path: str, legacy_path: str):
        self.path = path
        self.lock = threading.RLock()
        self.migrated_from_legacy = False
        self.cfg = self._load(legacy_path)

    def _load(self, legacy_path):
        cfg = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                broken = "%s.defekt-%d" % (self.path, int(time.time()))
                print("[Config] config.json nicht lesbar (%s) - gesichert als %s, starte mit Standardwerten." % (e, broken))
                try:
                    os.replace(self.path, broken)
                except OSError:
                    pass
                cfg = None
        if cfg is None and os.path.exists(legacy_path):
            try:
                with open(legacy_path, "r", encoding="utf-8") as f:
                    cfg = migrate_legacy(json.load(f))
                self.migrated_from_legacy = True
                print("[Config] v1.x-Konfiguration uebernommen: %s" % legacy_path)
            except Exception as e:
                print("[Config] v1.x-Konfiguration nicht lesbar:", e)
                cfg = None
        if cfg is None:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg = normalize_config(cfg)
        self.cfg = cfg
        self.save()
        return cfg

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.cfg, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception as e:
                print("[Config] Speicherfehler:", e)

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.cfg)


# ----------------------------------------------------------------------
# Messwerte (im Speicher, wie v1.9)
# ----------------------------------------------------------------------
def _bisect_ts(seq, ts):
    """Erster Index mit seq[i][0] >= ts (seq: Liste von (ts, wert))."""
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _decimate_minmax(points, max_points):
    """Reduziert auf ca. max_points Punkte, behaelt dabei Spitzen (min/max je Abschnitt)."""
    n = len(points)
    if n <= max_points or max_points < 4:
        return points
    buckets = max_points // 2
    size = n / float(buckets)
    out = []
    for b in range(buckets):
        start = int(b * size)
        end = min(n, int((b + 1) * size)) or start + 1
        chunk = points[start:end]
        if not chunk:
            continue
        mn = min(range(len(chunk)), key=lambda i: chunk[i][1])
        mx = max(range(len(chunk)), key=lambda i: chunk[i][1])
        for i in sorted({mn, mx}):
            out.append(chunk[i])
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


class TopicStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}    # topic -> deque[(ts_ms, float)]
        self.state = {}   # topic -> {value, numeric, ts, source}

    @staticmethod
    def parse_numeric(payload: str):
        s = payload.strip()
        if not s or len(s) > 40:
            return None
        try:
            f = float(s.replace(",", ".")) if s.count(",") == 1 and "." not in s else float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None

    def update(self, topic, payload, source="remote", numeric=None):
        if numeric is None:
            numeric = self.parse_numeric(payload)
        with self.lock:
            ts = now_ms()  # bewusst INNERHALB der Sperre, siehe live_since()
            self.state[topic] = {"value": payload, "numeric": numeric, "ts": ts, "source": source}
            if numeric is not None:
                d = self.data.get(topic)
                if d is None:
                    d = self.data[topic] = deque(maxlen=MAX_POINTS_PER_TOPIC)
                if not d or ts - d[-1][0] >= STORE_MIN_INTERVAL_MS:
                    d.append((ts, numeric))
                cutoff = ts - RETAIN_MS
                while d and d[0][0] < cutoff:
                    d.popleft()
        return numeric

    def latest_numeric(self, topic):
        with self.lock:
            st = self.state.get(topic)
            return None if st is None else st.get("numeric")

    def get_state(self, topic):
        with self.lock:
            st = self.state.get(topic)
            return dict(st) if st else None

    def states_for(self, topics):
        with self.lock:
            return {t: dict(self.state[t]) for t in topics if t in self.state}

    def history(self, topics, since, max_points):
        out = {}
        with self.lock:
            now = now_ms()
            for t in topics:
                d = self.data.get(t)
                if not d:
                    out[t] = []
                    continue
                seq = list(d)
                i = _bisect_ts(seq, since)
                # einen Punkt vor dem Fenster mitliefern (Linie beginnt am linken Rand)
                pts = seq[max(0, i - 1):]
                out[t] = [[p[0], p[1]] for p in _decimate_minmax(pts, max_points)]
        return now, out

    def live_since(self, topics, since):
        out = {}
        with self.lock:
            now = now_ms()
            for t in topics:
                d = self.data.get(t)
                if not d or d[-1][0] <= since:
                    continue
                seq = list(d)
                i = _bisect_ts(seq, since + 1)
                pts = seq[i:]
                if pts:
                    out[t] = [[p[0], p[1]] for p in _decimate_minmax(pts, 2000)]
        return now, out

    def topic_list(self, limit=3000):
        with self.lock:
            items = sorted(self.state.items(), key=lambda kv: kv[0])[:limit]
            return [{"topic": t, "value": s["value"][:80], "numeric": s["numeric"] is not None,
                     "source": s["source"], "ts": s["ts"]} for t, s in items]

    def count(self):
        with self.lock:
            return len(self.state)


# ----------------------------------------------------------------------
# Virtuelle Topics
# ----------------------------------------------------------------------
class VirtualEngine:
    def __init__(self, store: TopicStore):
        self.store = store
        self.lock = threading.Lock()
        self.items = []     # [(vt, code)]
        self.by_source = {}
        self.errors = {}    # vt_id -> Fehlertext

    def rebuild(self, cfg):
        items, by_source, errors = [], {}, {}
        for vt in cfg.get("virtual_topics", []):
            try:
                code = compile_formula(vt.get("formula", ""), len(vt.get("sources", [])))
            except ValueError as e:
                errors[vt["id"]] = str(e)
                continue
            items.append((vt, code))
            for src in vt.get("sources", []):
                by_source.setdefault(src, []).append((vt, code))
        with self.lock:
            self.items, self.by_source, self.errors = items, by_source, errors
        for vt, code in items:   # sofort mit vorhandenen Werten berechnen
            self._compute(vt, code, 0)

    def on_update(self, topic, depth=0):
        if depth > 3:
            return
        with self.lock:
            targets = list(self.by_source.get(topic, []))
        for vt, code in targets:
            self._compute(vt, code, depth)

    def _compute(self, vt, code, depth):
        values = []
        for src in vt["sources"]:
            v = self.store.latest_numeric(src)
            if v is None:
                return
            values.append(v)
        try:
            result = eval_formula(code, values)
        except Exception as e:
            with self.lock:
                self.errors[vt["id"]] = "Rechenfehler: %s" % e
            return
        with self.lock:
            self.errors.pop(vt["id"], None)
        txt = ("%.6f" % result).rstrip("0").rstrip(".")
        self.store.update(vt["name"], txt, source="virtual", numeric=result)
        self.on_update(vt["name"], depth + 1)

    def error_map(self):
        with self.lock:
            return dict(self.errors)


# ----------------------------------------------------------------------
# MQTT
# ----------------------------------------------------------------------
def _make_mqtt_client(client_id: str):
    """paho-mqtt 2.x verlangt die Callback-API-Version, 1.6.x kennt sie nicht."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id or "")
    return mqtt.Client(client_id=client_id or "")


def _rc_ok(rc) -> bool:
    try:
        return int(getattr(rc, "value", rc)) == 0
    except (TypeError, ValueError):
        return False


class MqttManager:
    def __init__(self, on_message):
        self._on_message_cb = on_message
        self.lock = threading.Lock()
        self.client = None
        self.connected = False
        self.last_error = ""
        self.host = ""
        self.port = 1883

    def start(self, broker: dict):
        self.stop()
        host = _str(broker.get("host"), 200)
        port = int(broker.get("port") or 1883)
        with self.lock:
            self.host, self.port = host, port
            self.connected = False
            self.last_error = "" if host else "Kein Broker eingetragen."
        if not host:
            return
        client = _make_mqtt_client(_str(broker.get("client_id"), 100))
        if broker.get("username"):
            client.username_pw_set(broker.get("username"), broker.get("password") or None)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        try:
            client.connect_async(host, port, 60)
            client.loop_start()
        except Exception as e:
            with self.lock:
                self.last_error = "Verbindung fehlgeschlagen: %s" % e
            print("[MQTT]", self.last_error)
            return
        with self.lock:
            self.client = client
        print("[MQTT] Verbinde mit %s:%d ..." % (host, port))

    def stop(self):
        with self.lock:
            client, self.client = self.client, None
            self.connected = False
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, rc, *args):
        if _rc_ok(rc):
            with self.lock:
                self.connected = True
                self.last_error = ""
            client.subscribe("#")
            print("[MQTT] Verbunden, abonniere '#'.")
        else:
            with self.lock:
                self.connected = False
                self.last_error = "Broker lehnt Verbindung ab (Code %s) - Benutzername/Passwort pruefen." % rc
            print("[MQTT]", self.last_error)

    def _on_disconnect(self, client, userdata, *args):
        with self.lock:
            if self.client is client:
                self.connected = False
                if not self.last_error:
                    self.last_error = "Verbindung getrennt - neuer Versuch laeuft automatisch."

    def _on_message(self, client, userdata, msg):
        try:
            self._on_message_cb(msg.topic, msg.payload)
        except Exception as e:
            print("[MQTT] Fehler bei Nachricht auf %s: %s" % (msg.topic, e))

    def publish(self, topic, payload, retain=False):
        with self.lock:
            client, ok = self.client, self.connected
        if client is None or not ok:
            raise RuntimeError("Nicht mit dem MQTT-Broker verbunden.")
        info = client.publish(topic, payload, qos=0, retain=bool(retain))
        if not _rc_ok(info.rc):
            raise RuntimeError("Senden fehlgeschlagen (Code %s)." % info.rc)

    def status(self):
        with self.lock:
            return {"connected": self.connected, "host": self.host, "port": self.port,
                    "error": self.last_error}


# ----------------------------------------------------------------------
# Zusammenbau
# ----------------------------------------------------------------------
config = ConfigStore(CONFIG_PATH, LEGACY_CONFIG_PATH)
store = TopicStore()
virtual = VirtualEngine(store)
_pending_self = {}           # topic -> (payload, ablauf)
_pending_lock = threading.Lock()


def handle_mqtt_message(topic, payload_bytes):
    try:
        payload = payload_bytes.decode("utf-8")
    except UnicodeDecodeError:
        payload = payload_bytes.decode("latin-1", "replace")
    source = "remote"
    with _pending_lock:
        pend = _pending_self.get(topic)
        if pend and pend[0] == payload and pend[1] >= time.time():
            source = "self"
            _pending_self.pop(topic, None)
    store.update(topic, payload, source=source)
    virtual.on_update(topic)


mqtt_manager = MqttManager(handle_mqtt_message)


def _public_config(cfg: dict) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["broker"]["password_set"] = bool(cfg["broker"].get("password"))
    cfg["broker"]["password"] = ""
    return cfg


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ----------------------------------------------------------------------
# REST-API
# ----------------------------------------------------------------------
@app.route("/api/version", methods=["GET"])
def api_version():
    return jsonify({"version": APP_VERSION})


@app.route("/api/config", methods=["GET"])
def api_config():
    out = _public_config(config.snapshot())
    out["version"] = APP_VERSION
    out["migrated_from_legacy"] = config.migrated_from_legacy
    out["range_choices_h"] = RANGE_CHOICES_H
    return jsonify(out)


@app.route("/api/settings", methods=["PUT"])
def api_settings():
    data = request.get_json(silent=True) or {}
    title = _str(data.get("title"), 60) or DEFAULT_CONFIG["title"]
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return _err("Port muss eine Zahl sein.")
    if not (1 <= port <= 65535):
        return _err("Port muss zwischen 1 und 65535 liegen.")
    try:
        rng = int(data.get("default_range_h") or 24)
    except (TypeError, ValueError):
        rng = 24
    if rng not in RANGE_CHOICES_H:
        rng = 24
    with config.lock:
        old_port = config.cfg["server"]["port"]
        config.cfg["title"] = title
        config.cfg["server"]["port"] = port
        config.cfg["default_range_h"] = rng
        config.save()
    return jsonify({"ok": True, "restart_required": port != old_port, "port": port})


@app.route("/api/broker", methods=["PUT"])
def api_broker():
    data = request.get_json(silent=True) or {}
    host = _str(data.get("host"), 200)
    try:
        port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        return _err("Broker-Port muss eine Zahl sein.")
    if not (1 <= port <= 65535):
        return _err("Broker-Port muss zwischen 1 und 65535 liegen.")
    with config.lock:
        b = config.cfg["broker"]
        b["host"] = host
        b["port"] = port
        b["username"] = _str(data.get("username"), 200)
        if data.get("clear_password"):
            b["password"] = ""
        elif data.get("password"):
            b["password"] = str(data.get("password"))[:200]
        b["client_id"] = _str(data.get("client_id"), 100)
        config.save()
        broker = dict(b)
    mqtt_manager.start(broker)
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def api_status():
    cfg = config.snapshot()
    errors = virtual.error_map()
    vstate = {}
    for vt in cfg["virtual_topics"]:
        st = store.get_state(vt["name"])
        vstate[vt["id"]] = {"value": st["value"] if st else None, "error": errors.get(vt["id"])}
    return jsonify({"broker": mqtt_manager.status(), "topic_count": store.count(),
                    "virtual": vstate, "now": now_ms()})


@app.route("/api/topics", methods=["GET"])
def api_topics():
    return jsonify(store.topic_list())


@app.route("/api/history", methods=["POST"])
def api_history():
    data = request.get_json(silent=True) or {}
    topics = [str(t) for t in (data.get("topics") or [])][:500]
    chart_topics = [str(t) for t in (data.get("chart_topics") or [])][:200]
    try:
        rel = int(data.get("range_ms") or 24 * 3600 * 1000)
    except (TypeError, ValueError):
        rel = 24 * 3600 * 1000
    rel = max(60000, min(rel, MAX_RANGE_H * 3600 * 1000))
    try:
        max_points = int(data.get("max_points") or MAX_SEND_POINTS)
    except (TypeError, ValueError):
        max_points = MAX_SEND_POINTS
    max_points = max(100, min(max_points, 5000))
    now, points = store.history(chart_topics, now_ms() - rel, max_points)
    return jsonify({"now": now, "points": points, "states": store.states_for(topics),
                    "connected": mqtt_manager.status()["connected"]})


@app.route("/api/live", methods=["POST"])
def api_live():
    data = request.get_json(silent=True) or {}
    topics = [str(t) for t in (data.get("topics") or [])][:500]
    chart_topics = [str(t) for t in (data.get("chart_topics") or [])][:200]
    try:
        since = int(data.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    now, points = store.live_since(chart_topics, since)
    st = mqtt_manager.status()
    return jsonify({"now": now, "points": points, "states": store.states_for(topics),
                    "connected": st["connected"], "broker": st})


@app.route("/api/publish", methods=["POST"])
def api_publish():
    data = request.get_json(silent=True) or {}
    topic = _str(data.get("topic"), 300)
    if not topic:
        return _err("Topic fehlt.")
    payload = "" if data.get("payload") is None else str(data.get("payload"))[:1000]
    with _pending_lock:
        _pending_self[topic] = (payload, time.time() + SELF_PUBLISH_WINDOW_S)
    try:
        mqtt_manager.publish(topic, payload, retain=bool(data.get("retain")))
    except RuntimeError as e:
        return _err(str(e), 503)
    return jsonify({"ok": True})


@app.route("/api/virtual_topics/test", methods=["POST"])
def api_virtual_test():
    data = request.get_json(silent=True) or {}
    sources = [_str(s, 300) for s in (data.get("sources") or []) if _str(s, 300)]
    if not sources:
        return _err("Bitte mindestens ein Quell-Topic angeben.")
    if len(sources) > len(VAR_NAMES):
        return _err("Maximal %d Quell-Topics." % len(VAR_NAMES))
    try:
        code = compile_formula(data.get("formula"), len(sources))
    except ValueError as e:
        return _err(str(e))
    values = []
    for i, s in enumerate(sources):
        v = store.latest_numeric(s)
        if v is None:
            return _err("Fuer %s = '%s' liegt noch kein Zahlenwert vor." % (VAR_NAMES[i], s))
        values.append(v)
    try:
        result = eval_formula(code, values)
    except Exception as e:
        return _err("Rechenfehler: %s" % e)
    return jsonify({"value": result, "inputs": dict(zip(VAR_NAMES, values))})


def _check_collection(coll):
    return coll in COLLECTIONS


def _after_change(coll):
    if coll == "virtual_topics":
        virtual.rebuild(config.snapshot())


@app.route("/api/<coll>", methods=["POST"])
def api_create(coll):
    if not _check_collection(coll):
        return _err("Unbekannter Bereich.", 404)
    data = request.get_json(silent=True) or {}
    with config.lock:
        try:
            item = VALIDATORS[coll](data, config.cfg)
        except ValueError as e:
            return _err(str(e))
        item = dict({"id": new_id(ID_PREFIX[coll])}, **item)
        items = config.cfg[coll]
        item["order"] = (max([i.get("order", 0) for i in items]) + 1) if items else 0
        items.append(item)
        config.save()
    _after_change(coll)
    return jsonify(item), 201


@app.route("/api/<coll>/<item_id>", methods=["PUT"])
def api_update(coll, item_id):
    if not _check_collection(coll):
        return _err("Unbekannter Bereich.", 404)
    data = request.get_json(silent=True) or {}
    with config.lock:
        items = config.cfg[coll]
        idx = next((i for i, it in enumerate(items) if it["id"] == item_id), None)
        if idx is None:
            return _err("Eintrag nicht gefunden (evtl. inzwischen geloescht).", 404)
        try:
            clean = VALIDATORS[coll](data, config.cfg, item_id)
        except ValueError as e:
            return _err(str(e))
        item = dict({"id": item_id}, **clean)
        item["order"] = items[idx].get("order", idx)
        items[idx] = item
        config.save()
    _after_change(coll)
    return jsonify(item)


@app.route("/api/<coll>/<item_id>", methods=["DELETE"])
def api_delete(coll, item_id):
    if not _check_collection(coll):
        return _err("Unbekannter Bereich.", 404)
    with config.lock:
        before = len(config.cfg[coll])
        config.cfg[coll] = [it for it in config.cfg[coll] if it["id"] != item_id]
        if len(config.cfg[coll]) == before:
            return _err("Eintrag nicht gefunden.", 404)
        if coll == "groups":
            # Gruppe loeschen loescht KEINE Diagramme/Elemente - sie landen unter "Ohne Gruppe"
            for c in ("charts", "widgets"):
                for it in config.cfg[c]:
                    if it.get("group_id") == item_id:
                        it["group_id"] = None
        config.save()
    _after_change(coll)
    return jsonify({"ok": True})


@app.route("/api/<coll>/reorder", methods=["POST"])
def api_reorder(coll):
    if not _check_collection(coll):
        return _err("Unbekannter Bereich.", 404)
    order = (request.get_json(silent=True) or {}).get("order") or []
    with config.lock:
        pos = {iid: i for i, iid in enumerate(order)}
        for it in config.cfg[coll]:
            if it["id"] in pos:
                it["order"] = pos[it["id"]]
        config.save()
    return jsonify({"ok": True})


@app.route("/")
def index():
    # bewusst KEIN render_template_string: das Frontend wird nicht durch
    # Jinja geschickt (geschweifte Klammern im JS bleiben unangetastet)
    resp = Response(INDEX_HTML, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp



# ----------------------------------------------------------------------
# Frontend (Ansicht + Einstellungen in EINER Seite, wie MK6)
# ----------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a0c0f">
<title>MQTT Dashboard</title>
<style>
  /* Design 1:1 aus dem Drucker Dashboard (MK6) uebernommen (Farben, Schriften,
     Header, Karten, Einstellungs-Abschnitte, Modals, Toasts). Die Ansicht
     selbst nutzt bewusst Flexbox statt CSS-Grid und kein "gap" - damit
     laeuft sie wie v1.9 auch auf alten iPads (iOS 9.3+) als Wandanzeige. */
  :root{
    --bg:#0a0c0f;
    --panel:#12151a;
    --panel-2:#171b21;
    --border:#242a33;
    --text:#e5e8ec;
    --text-dim:#8891a0;
    --accent:#ff9142;
    --accent-2:#3ddc97;
    --danger:#ff5d5d;
    --info:#4aa3ff;
    --mono: 'JetBrains Mono', 'Consolas', 'SFMono-Regular', Menlo, monospace;
    --sans: 'Inter', 'Segoe UI', -apple-system, 'Helvetica Neue', Arial, sans-serif;
  }
  *{ -webkit-box-sizing:border-box; box-sizing:border-box; }
  html{ -webkit-text-size-adjust:100%; }
  body{
    margin:0; background:#0a0c0f; background:var(--bg); color:var(--text);
    font-family:var(--sans); letter-spacing:0.1px;
    -webkit-tap-highlight-color:transparent;
  }
  header{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-align-items:center; align-items:center;
    -webkit-justify-content:space-between; justify-content:space-between;
    -webkit-flex-wrap:wrap; flex-wrap:wrap;
    padding:16px 32px 10px; border-bottom:1px solid var(--border);
    background:linear-gradient(180deg,#0d1014,#0a0c0f);
  }
  header h1{
    font-size:18px; font-weight:600; margin:0 24px 6px 0; letter-spacing:0.5px;
    text-transform:uppercase; color:var(--text); white-space:nowrap;
  }
  header h1 span{ color:var(--accent); }
  .ver-badge{
    font-family:var(--mono); font-size:11px; color:var(--text-dim);
    font-weight:400; vertical-align:middle; margin-left:4px; text-transform:none;
  }
  header h1 .ver-badge{ color:var(--text-dim); }
  .head-controls{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-align-items:center; align-items:center;
    -webkit-flex-wrap:wrap; flex-wrap:wrap; -webkit-justify-content:flex-end; justify-content:flex-end;
  }
  .head-controls > *{ margin:0 0 6px 12px; }
  .btn{
    background:var(--accent); color:#12100c; border:none; border-radius:6px;
    padding:10px 18px; font-weight:600; font-size:13px; cursor:pointer;
    letter-spacing:0.3px; transition:filter .15s ease; font-family:var(--sans);
  }
  .btn:hover{ filter:brightness(1.1); }
  .btn-ghost{
    background:transparent; color:var(--text-dim); border:1px solid var(--border);
  }
  .btn-ghost:hover{ color:var(--text); border-color:#3a4250; }
  .btn-mini{
    background:#1b2027; color:var(--text); border:1px solid var(--border);
    border-radius:5px; padding:5px 10px; font-size:11px; cursor:pointer;
    font-family:var(--mono);
  }
  .btn-mini:hover{ border-color:var(--accent-2); }
  .btn-mini:disabled{ opacity:0.35; cursor:default; }
  .btn-mini.btn-delete{ color:var(--danger); border-color:#c0392b66; }
  .btn-mini.btn-delete:hover{ border-color:var(--danger); filter:brightness(1.15); }

  .layout-switch{
    display:-webkit-box; display:-webkit-flex; display:flex;
    border:1px solid var(--border); border-radius:6px; overflow:hidden;
  }
  .layout-btn{
    background:#1b2027; color:var(--text-dim); border:none; border-right:1px solid var(--border);
    padding:9px 12px; font-size:12px; font-family:var(--mono); cursor:pointer;
  }
  .layout-btn:last-child{ border-right:none; }
  .layout-btn:hover{ color:var(--text); }
  .layout-btn.active{ background:var(--accent); color:#12100c; font-weight:600; }

  .conn-pill{
    display:-webkit-inline-box; display:-webkit-inline-flex; display:inline-flex;
    -webkit-align-items:center; align-items:center;
    font-family:var(--mono); font-size:11px; color:var(--text-dim);
    border:1px solid var(--border); border-radius:20px; padding:6px 12px; background:#11151a;
    max-width:260px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  .status-dot{
    width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:8px;
    background:var(--danger); -webkit-flex-shrink:0; flex-shrink:0;
  }
  .status-dot.online{ background:var(--accent-2); box-shadow:0 0 6px var(--accent-2); }

  /* ---------------- Ansicht ---------------- */
  #viewRoot{ padding:24px 32px 40px; max-width:1100px; margin:0 auto; }
  #viewRoot.cols-2{ max-width:1600px; }
  #viewRoot.cols-3{ max-width:2000px; }
  #viewRoot.cols-4{ max-width:2400px; }

  .grp{ margin-bottom:26px; }
  .room-header{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-align-items:center; align-items:center;
    font-family:var(--mono); font-size:12px; letter-spacing:1px;
    text-transform:uppercase; color:var(--text-dim); padding-bottom:8px;
    border-bottom:1px solid var(--border); margin-bottom:14px; cursor:pointer;
    -webkit-user-select:none; user-select:none;
  }
  .room-header:hover{ color:var(--text); }
  .grp-caret{ display:inline-block; width:16px; font-size:10px; -webkit-transition:-webkit-transform .15s; transition:transform .15s; }
  .grp.collapsed .grp-caret{ -webkit-transform:rotate(-90deg); transform:rotate(-90deg); }
  .grp.collapsed .grp-body{ display:none; }
  .grp.collapsed .room-header{ margin-bottom:0; }
  .grp-dot{ width:10px; height:10px; border-radius:3px; margin-right:10px; display:inline-block; }
  .grp-count{ margin-left:auto; font-size:10.5px; letter-spacing:0.5px; text-transform:none; }

  /* Kacheln (Schalter / Slider / Anzeigen) */
  .tiles{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-flex-wrap:wrap; flex-wrap:wrap; margin:-6px -6px 12px;
  }
  .tile{
    position:relative; width:-webkit-calc(16.666% - 12px); width:calc(16.666% - 12px);
    min-width:150px; margin:6px; min-height:124px; padding:14px 12px 22px;
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-box-orient:vertical; -webkit-flex-direction:column; flex-direction:column;
    -webkit-align-items:center; align-items:center; -webkit-justify-content:center; justify-content:center;
    text-align:center; overflow:hidden; -webkit-user-select:none; user-select:none;
    -webkit-transition:background-color .25s, border-color .25s; transition:background-color .25s, border-color .25s;
  }
  .tile-switch{ cursor:pointer; }
  .tile-switch:active{ opacity:.8; }
  .tile-icon{ font-size:28px; line-height:1.15; opacity:.55; -webkit-transition:opacity .25s; transition:opacity .25s; }
  .tile.on .tile-icon{ opacity:1; }
  .tile-name{
    font-size:12px; font-weight:600; margin-top:6px; max-width:100%;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .tile-value{
    font-family:var(--mono); font-size:13px; color:var(--text-dim); margin-top:4px;
    max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .tile.on .tile-value{ color:var(--text); }
  .tile-display .tile-value{ font-size:20px; color:var(--text); font-weight:600; }
  .tile-display .tile-value small{ font-size:12px; color:var(--text-dim); font-weight:400; margin-left:3px; }
  .tile-src{
    position:absolute; left:10px; bottom:6px; font-family:var(--mono); font-size:9.5px;
    color:var(--text-dim); opacity:.7;
  }
  .tile-badge{
    position:absolute; right:8px; top:8px; font-family:var(--mono); font-size:9.5px;
    padding:1px 7px; border-radius:20px; border:1px solid var(--border); color:var(--text-dim);
    text-transform:uppercase;
  }
  .tile.on .tile-badge{ color:#12100c; border-color:transparent; }
  .tile-range{
    width:100%; margin:10px 0 0; -webkit-appearance:none; appearance:none;
    height:6px; border-radius:3px; background:#20252c; outline:none; cursor:pointer;
    border:1px solid var(--border);
  }
  .tile-range::-webkit-slider-thumb{
    -webkit-appearance:none; width:20px; height:20px; border-radius:50%;
    background:var(--accent); cursor:pointer; box-shadow:0 1px 5px rgba(0,0,0,.5);
  }
  .tile-range::-moz-range-thumb{ width:20px; height:20px; border-radius:50%; background:var(--accent); cursor:pointer; border:none; }

  /* Diagramme: Flexbox mit 1/2/3/4 Spalten (MK6-Layout-Umschalter) */
  .charts{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-flex-wrap:wrap; flex-wrap:wrap; margin:-11px;
  }
  .chart-card{
    width:-webkit-calc(100% - 22px); width:calc(100% - 22px); margin:11px;
    background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden;
  }
  #viewRoot.cols-2 .chart-card{ width:-webkit-calc(50% - 22px); width:calc(50% - 22px); }
  #viewRoot.cols-3 .chart-card{ width:-webkit-calc(33.333% - 22px); width:calc(33.333% - 22px); }
  #viewRoot.cols-4 .chart-card{ width:-webkit-calc(25% - 22px); width:calc(25% - 22px); }
  @media(max-width:1500px){ #viewRoot.cols-4 .chart-card{ width:-webkit-calc(33.333% - 22px); width:calc(33.333% - 22px); } }
  @media(max-width:1150px){ #viewRoot.cols-3 .chart-card, #viewRoot.cols-4 .chart-card{ width:-webkit-calc(50% - 22px); width:calc(50% - 22px); } }
  @media(max-width:760px){ #viewRoot.cols-2 .chart-card, #viewRoot.cols-3 .chart-card, #viewRoot.cols-4 .chart-card{ width:-webkit-calc(100% - 22px); width:calc(100% - 22px); } }
  .card-head{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-align-items:center; align-items:center; -webkit-justify-content:space-between; justify-content:space-between;
    padding:13px 18px; border-bottom:1px solid var(--border); background:var(--panel-2);
  }
  .card-head .name{ font-size:15px; font-weight:600; }
  .type-badge{
    font-family:var(--mono); font-size:10px; padding:2px 8px; border-radius:20px;
    border:1px solid var(--border); color:var(--text-dim); text-transform:uppercase; margin-left:10px;
    white-space:nowrap;
  }
  .legend{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-flex-wrap:wrap; flex-wrap:wrap; padding:10px 14px 0;
  }
  .legend-chip{
    display:-webkit-inline-box; display:-webkit-inline-flex; display:inline-flex;
    -webkit-align-items:center; align-items:center; margin:0 6px 6px 0; max-width:100%;
    background:#1b2027; border:1px solid var(--border); border-radius:6px;
    padding:5px 9px; font-family:var(--mono); font-size:11.5px; color:var(--text-dim);
  }
  .legend-chip i{ width:10px; height:10px; border-radius:2px; margin-right:7px; -webkit-flex-shrink:0; flex-shrink:0; }
  .legend-chip .lg-label{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-right:8px; }
  .legend-chip b{ color:var(--text); white-space:nowrap; font-size:12.5px; }
  .chart-box{ position:relative; margin:4px 10px 10px; height:240px; }
  .chart-box.h-s{ height:160px; }
  .chart-box.h-l{ height:360px; }
  .chart-box canvas{ position:absolute; left:0; top:0; width:100%; height:100%; cursor:crosshair; }
  .chart-empty{
    position:absolute; left:0; right:0; top:42%; text-align:center; pointer-events:none;
    font-family:var(--mono); font-size:11.5px; color:var(--text-dim); display:none;
  }

  .empty-state{ text-align:center; padding:70px 20px; color:var(--text-dim); }
  .empty-state h2{ color:var(--text); font-size:16px; font-weight:600; margin:0 0 8px; }
  .empty-state .btn{ margin-top:16px; }
  .notice{
    border:1px solid #2bb98755; color:var(--accent-2); background:#0f1d17; border-radius:8px;
    padding:10px 14px; font-size:12.5px; margin-bottom:18px;
  }

  @media(max-width:760px){
    header{ padding:12px 14px 6px; }
    header h1{ font-size:16px; }
    .head-controls > *{ margin:0 0 6px 8px; }
    .layout-btn{ padding:8px 9px; }
    .btn{ padding:9px 13px; }
    .conn-pill{ max-width:170px; }
    #viewRoot{ padding:16px 12px 30px; }
    #colSwitch{ display:none; }
    .tile{ width:-webkit-calc(50% - 12px); width:calc(50% - 12px); min-width:0; }
    #settingsPanel{ padding:16px 12px 30px; }
    .settings-section{ padding:16px 14px; }
  }
  @media(min-width:761px) and (max-width:1100px){
    .tile{ width:-webkit-calc(25% - 12px); width:calc(25% - 12px); }
  }

  /* ---------------- Einstellungen (wie MK6) ---------------- */
  #settingsPanel{ padding:28px 32px; max-width:960px; margin:0 auto; }
  .settings-section{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:20px 24px; margin-bottom:22px;
  }
  .settings-section h2{
    font-size:14px; margin:0 0 10px 0; text-transform:uppercase; letter-spacing:0.5px;
  }
  .settings-section label, .modal label{
    font-size:11px; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.5px;
    display:block; margin-bottom:6px;
  }
  .settings-section input, .settings-section select,
  .modal input, .modal select{
    background:#0d1014; border:1px solid var(--border); color:var(--text);
    padding:9px 10px; border-radius:6px; font-family:var(--mono); font-size:13px;
  }
  .settings-section input:focus, .settings-section select:focus,
  .modal input:focus, .modal select:focus{ outline:none; border-color:var(--accent); }
  .field-grid{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-flex-wrap:wrap; flex-wrap:wrap;
    margin:0 -8px;
  }
  .field-grid > .fld{ -webkit-box-flex:1; -webkit-flex:1 1 200px; flex:1 1 200px; margin:0 8px 14px; min-width:0; }
  .field-grid > .fld.narrow{ -webkit-flex:0 1 130px; flex:0 1 130px; }
  .fld input, .fld select{ width:100%; }
  .settings-inline-form{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-flex-wrap:wrap; flex-wrap:wrap;
    -webkit-align-items:center; align-items:center;
  }
  .settings-inline-form > *{ margin:0 10px 8px 0; }
  .settings-inline-form input{ -webkit-flex:1; flex:1; min-width:180px; }
  .hint-text{ font-size:11.5px; color:var(--text-dim); margin:0 0 14px 0; line-height:1.45; }
  .hint-text code{ font-family:var(--mono); color:var(--text); }
  .status-line{ font-family:var(--mono); font-size:12px; margin-top:6px; color:var(--text-dim); }
  .status-line.ok{ color:var(--accent-2); }
  .status-line.err{ color:var(--danger); }
  .manage-row{
    display:-webkit-box; display:-webkit-flex; display:flex;
    -webkit-align-items:center; align-items:center; -webkit-justify-content:space-between; justify-content:space-between;
    padding:10px 0; border-bottom:1px solid var(--border);
  }
  .manage-row:last-child{ border-bottom:none; }
  .manage-row > *{ margin-right:10px; }
  .manage-row > *:last-child{ margin-right:0; }
  .manage-info{ font-size:13px; -webkit-flex:1; flex:1; min-width:0; }
  .manage-info .manage-sub{
    color:var(--text-dim); font-size:11px; font-family:var(--mono); margin-top:3px;
    word-break:break-all;
  }
  .manage-info .manage-sub.err{ color:var(--danger); }
  .manage-actions{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-align-items:center; align-items:center;
    -webkit-flex-shrink:0; flex-shrink:0; -webkit-flex-wrap:wrap; flex-wrap:wrap; -webkit-justify-content:flex-end; justify-content:flex-end;
  }
  .manage-actions > *{ margin-left:6px; margin-top:2px; margin-bottom:2px; }
  .manage-actions select{ max-width:150px; padding:5px 7px; font-size:11.5px; }
  .order-btns{ display:-webkit-box; display:-webkit-flex; display:flex; -webkit-flex-direction:column; flex-direction:column; -webkit-flex-shrink:0; flex-shrink:0; }
  .order-btns .btn-mini{ padding:2px 7px; line-height:1; margin:1px 0; }
  .swatch{ display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:8px; vertical-align:-1px; }
  .emoji-ico{ font-size:18px; width:28px; text-align:center; -webkit-flex-shrink:0; flex-shrink:0; }
  @media(max-width:600px){
    .manage-row{ -webkit-flex-wrap:wrap; flex-wrap:wrap; }
    .manage-actions{ width:100%; margin-top:6px; }
  }

  /* ---------------- Modals (wie MK6) ---------------- */
  .modal-backdrop{
    display:none; position:fixed; left:0; top:0; right:0; bottom:0; background:rgba(0,0,0,.6);
    z-index:50; overflow-y:auto; -webkit-overflow-scrolling:touch; padding:30px 12px;
  }
  .modal-backdrop.show{ display:block; }
  .modal{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    width:100%; max-width:560px; padding:24px; margin:0 auto;
  }
  .modal.wide{ max-width:720px; }
  .modal h2{ font-size:15px; margin:0 0 18px 0; text-transform:uppercase; letter-spacing:0.5px; }
  .modal-actions{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-justify-content:flex-end; justify-content:flex-end;
    margin-top:6px;
  }
  .modal-actions .btn{ margin-left:10px; }
  .error-msg{
    color:var(--danger); font-size:12px; margin-bottom:12px; display:none;
    background:#2a1414; border:1px solid #4a1f1f; border-radius:6px; padding:8px 10px; font-family:var(--mono);
  }
  .checkbox-row{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-align-items:center; align-items:center;
    margin:2px 0 14px 0; font-size:12.5px; color:var(--text-dim); cursor:pointer;
  }
  .checkbox-row input{ width:auto; margin:0 8px 0 0; accent-color:var(--accent-2); }
  .field-label{ font-size:11px; text-transform:uppercase; letter-spacing:0.6px; color:var(--text-dim); margin:4px 0 8px; }
  .series-row{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-align-items:center; align-items:center;
    margin-bottom:8px;
  }
  .series-row > *{ margin-right:6px; }
  .series-row > *:last-child{ margin-right:0; }
  .series-row .sr-idx{
    font-family:var(--mono); font-size:11px; color:var(--text-dim); width:18px; text-align:center;
    -webkit-flex-shrink:0; flex-shrink:0;
  }
  .series-row .sr-topic{ -webkit-flex:2; flex:2; min-width:0; }
  .series-row .sr-label{ -webkit-flex:1; flex:1; min-width:0; }
  .series-row .sr-color{ width:42px; height:36px; padding:2px; -webkit-flex-shrink:0; flex-shrink:0; cursor:pointer; }
  .discover{
    border:1px solid var(--border); border-radius:8px; margin:4px 0 16px; background:#0d1014;
  }
  .discover input{ width:100%; border:none; border-bottom:1px solid var(--border); border-radius:8px 8px 0 0; background:transparent; }
  .discover-list{ max-height:170px; overflow-y:auto; -webkit-overflow-scrolling:touch; padding:4px 10px; }
  .discover-row{
    display:-webkit-box; display:-webkit-flex; display:flex; -webkit-justify-content:space-between; justify-content:space-between;
    padding:5px 0; cursor:pointer; font-family:var(--mono); font-size:11.5px; border-bottom:1px dashed #1b2027;
  }
  .discover-row:last-child{ border-bottom:none; }
  .discover-row:hover{ color:var(--accent); }
  .discover-row .dv-t{ word-break:break-all; margin-right:12px; }
  .discover-row .dv-v{ color:var(--text-dim); white-space:nowrap; max-width:40%; overflow:hidden; text-overflow:ellipsis; }
  .discover-empty{ font-size:11.5px; color:var(--text-dim); padding:8px 0; font-style:italic; }
  .vt-letter{
    font-family:var(--mono); font-size:13px; font-weight:600; color:var(--accent); width:22px; text-align:center;
    -webkit-flex-shrink:0; flex-shrink:0;
  }

  .toast-container{
    position:fixed; top:18px; right:18px; z-index:80; max-width:340px;
  }
  .toast{
    background:var(--panel); border:1px solid var(--border); border-radius:8px; margin-bottom:10px;
    padding:12px 14px; font-size:13px; box-shadow:0 6px 18px rgba(0,0,0,.45);
  }
  .toast.ok{ border-color:#2bb98755; color:var(--accent-2); }
  .toast.err{ border-color:#c0392b55; color:var(--danger); }
</style>
</head>
<body>

<header>
  <h1 id="appTitle">MQTT<span>Dashboard</span> <span class="ver-badge" id="verBadge"></span></h1>

  <!-- Ansicht (Standard): nur anzeigen/bedienen -->
  <div class="head-controls" id="operatorControls">
    <span class="conn-pill" id="connPill" title="Verbindung zum MQTT-Broker"><span class="status-dot" id="connDot"></span><span id="connText">verbinde ...</span></span>
    <div class="layout-switch" id="rangeSwitch" title="Zeitraum der Diagramme"></div>
    <div class="layout-switch" id="colSwitch" title="Diagramm-Spalten">
      <button type="button" class="layout-btn" data-cols="1" onclick="setLayoutCols(1)">1</button>
      <button type="button" class="layout-btn" data-cols="2" onclick="setLayoutCols(2)">2</button>
      <button type="button" class="layout-btn" data-cols="3" onclick="setLayoutCols(3)">3</button>
      <button type="button" class="layout-btn" data-cols="4" onclick="setLayoutCols(4)">4</button>
    </div>
    <button class="btn" onclick="enterSettingsMode()">&#9881; Einstellungen</button>
  </div>

  <!-- Einstellungen-Modus -->
  <div class="head-controls" id="settingsModeControls" style="display:none;">
    <button class="btn" onclick="exitSettingsMode()">&larr; Zur Ansicht</button>
    <button class="btn" onclick="saveGeneralSettings()">Speichern</button>
  </div>
</header>

<main id="viewRoot"></main>

<div id="settingsPanel" style="display:none;">

  <div class="notice" id="migrationNotice" style="display:none;">
    Die Konfiguration aus v1.x (<code>mqtt_dashboard_config.json</code>) wurde automatisch
    &uuml;bernommen und liegt jetzt in <code>config.json</code>. Die alte Datei bleibt unver&auml;ndert liegen.
  </div>

  <div class="settings-section">
    <h2>MQTT-Broker</h2>
    <div class="hint-text">
      Das Dashboard abonniert alle Topics (<code>#</code>) dieses Brokers. &Auml;nderungen gelten sofort,
      die Verbindung wird automatisch neu aufgebaut.
    </div>
    <div class="field-grid">
      <div class="fld"><label>Broker-Adresse</label><input id="bk_host" placeholder="192.168.1.5"></div>
      <div class="fld narrow"><label>Port</label><input id="bk_port" placeholder="1883" inputmode="numeric"></div>
    </div>
    <div class="field-grid">
      <div class="fld"><label>Benutzername (optional)</label><input id="bk_user" autocomplete="off"></div>
      <div class="fld"><label>Passwort (optional, leer = unver&auml;ndert)</label><input id="bk_pass" type="password" autocomplete="new-password"></div>
      <div class="fld"><label>Client-ID (optional)</label><input id="bk_cid" placeholder="automatisch"></div>
    </div>
    <label class="checkbox-row" id="bk_clear_row" style="display:none;"><input type="checkbox" id="bk_clear"> Gespeichertes Passwort entfernen</label>
    <div class="settings-inline-form">
      <button class="btn" onclick="saveBroker()">Broker-Einstellungen speichern</button>
    </div>
    <div class="status-line" id="brokerStatus"></div>
  </div>

  <div class="settings-section">
    <h2>Gruppen</h2>
    <div class="hint-text">
      Diagramme und Schalter/Anzeigen lassen sich in der Ansicht nach Gruppe (z.&nbsp;B. Raum)
      sortiert anzeigen. Eine gel&ouml;schte Gruppe l&ouml;scht <b>keine</b> Diagramme oder Elemente -
      sie erscheinen danach unter &bdquo;Ohne Gruppe&ldquo;.
    </div>
    <div class="settings-inline-form">
      <input id="newGroupName" placeholder="Name der neuen Gruppe (z. B. Wohnzimmer)">
      <select id="newGroupColor"></select>
      <button class="btn" onclick="createGroup()">+ Gruppe anlegen</button>
    </div>
    <div id="groupsManageList" style="margin-top:6px;"></div>
  </div>

  <div class="settings-section">
    <h2>Diagramme</h2>
    <div class="hint-text">
      Jedes Diagramm bleibt jederzeit bearbeitbar: Name, Gruppe, Topics, Linienfarben,
      Einheit, Y-Achse, H&ouml;he, Linienart und eigener Zeitraum.
    </div>
    <button class="btn" onclick="openChartModal(null)">+ Diagramm hinzuf&uuml;gen</button>
    <div id="chartsManageList" style="margin-top:14px;"></div>
  </div>

  <div class="settings-section">
    <h2>Schalter, Slider &amp; Anzeigen</h2>
    <div class="hint-text">
      Kacheln in der Ansicht: <b>Schalter</b> senden AN/AUS-Payloads, <b>Slider</b> senden einen Zahlenwert,
      <b>Anzeigen</b> zeigen den aktuellen Wert eines Topics.
    </div>
    <button class="btn" onclick="openWidgetModal(null)">+ Element hinzuf&uuml;gen</button>
    <div id="widgetsManageList" style="margin-top:14px;"></div>
  </div>

  <div class="settings-section">
    <h2>Virtuelle Topics</h2>
    <div class="hint-text">
      Berechnete Werte aus bis zu acht Topics (Variablen <code>a</code>&ndash;<code>h</code>), z.&nbsp;B.
      <code>a - b</code> oder <code>(a + b) / 2</code>. Virtuelle Topics k&ouml;nnen wie echte Topics in
      Diagrammen und Anzeigen verwendet werden.
    </div>
    <button class="btn" onclick="openVirtualModal(null)">+ Virtuelles Topic hinzuf&uuml;gen</button>
    <div id="virtualManageList" style="margin-top:14px;"></div>
  </div>

  <div class="settings-section">
    <h2>Allgemein</h2>
    <div class="field-grid">
      <div class="fld"><label>Titel des Dashboards</label><input id="gen_title" placeholder="MQTT Dashboard"></div>
      <div class="fld narrow"><label>Webserver-Port</label><input id="gen_port" inputmode="numeric"></div>
      <div class="fld narrow"><label>Standard-Zeitraum</label><select id="gen_range"></select></div>
    </div>
    <div class="hint-text">
      Wird &uuml;ber den &bdquo;Speichern&ldquo;-Knopf oben neben &bdquo;&larr; Zur Ansicht&ldquo; gesichert.
      Ein ge&auml;nderter Port gilt nach dem n&auml;chsten Neustart des Programms. Der Standard-Zeitraum gilt
      f&uuml;r Ger&auml;te, auf denen in der Ansicht noch kein Zeitraum gew&auml;hlt wurde.
    </div>
  </div>
</div>

<!-- Modal: Gruppe bearbeiten -->
<div class="modal-backdrop" id="groupModal">
  <div class="modal" style="max-width:420px;">
    <h2>Gruppe bearbeiten</h2>
    <div class="error-msg" id="groupModalError"></div>
    <label>Name</label>
    <input id="gm_name" style="width:100%; margin-bottom:14px;">
    <label>Farbe</label>
    <select id="gm_color" style="width:100%; margin-bottom:14px;"></select>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal('groupModal')">Abbrechen</button>
      <button class="btn" onclick="submitGroupModal()">Speichern</button>
    </div>
  </div>
</div>

<!-- Modal: Diagramm anlegen/bearbeiten -->
<div class="modal-backdrop" id="chartModal">
  <div class="modal wide">
    <h2 id="chartModalTitle">Diagramm hinzuf&uuml;gen</h2>
    <div class="error-msg" id="chartModalError"></div>
    <div class="field-grid">
      <div class="fld"><label>Name</label><input id="cm_name" placeholder="z. B. Temperaturen Wohnzimmer"></div>
      <div class="fld"><label>Gruppe</label><select id="cm_group"></select></div>
    </div>
    <div class="field-label">Topics &amp; Linienfarben</div>
    <div id="cm_series"></div>
    <button class="btn-mini" onclick="addSeries('')" style="margin-bottom:12px;">+ Topic hinzuf&uuml;gen</button>
    <div class="hint-text" style="margin-bottom:6px;">Empfangene Topics - antippen zum Hinzuf&uuml;gen:</div>
    <div class="discover">
      <input id="cm_filter" placeholder="Filtern ..." oninput="renderDiscover('cm')">
      <div class="discover-list" id="cm_discover"></div>
    </div>
    <div class="field-grid">
      <div class="fld narrow"><label>Einheit</label><input id="cm_unit" placeholder="z. B. &deg;C"></div>
      <div class="fld narrow"><label>Y-Minimum</label><input id="cm_ymin" placeholder="auto"></div>
      <div class="fld narrow"><label>Y-Maximum</label><input id="cm_ymax" placeholder="auto"></div>
    </div>
    <div class="field-grid">
      <div class="fld narrow"><label>H&ouml;he</label>
        <select id="cm_height"><option value="s">Klein</option><option value="m">Mittel</option><option value="l">Gro&szlig;</option></select></div>
      <div class="fld"><label>Linienart</label>
        <select id="cm_stepped"><option value="0">Linie</option><option value="1">Stufen (f&uuml;r Schaltzust&auml;nde)</option></select></div>
      <div class="fld"><label>Zeitraum</label><select id="cm_range"></select></div>
    </div>
    <label class="checkbox-row"><input type="checkbox" id="cm_fill"> Fl&auml;che unter den Linien f&uuml;llen</label>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal('chartModal')">Abbrechen</button>
      <button class="btn" onclick="submitChartModal()">Speichern</button>
    </div>
  </div>
</div>

<!-- Modal: Schalter/Slider/Anzeige anlegen/bearbeiten -->
<div class="modal-backdrop" id="widgetModal">
  <div class="modal">
    <h2 id="widgetModalTitle">Element hinzuf&uuml;gen</h2>
    <div class="error-msg" id="widgetModalError"></div>
    <div class="field-grid">
      <div class="fld"><label>Typ</label>
        <select id="wm_type" onchange="updateWidgetTypeFields()">
          <option value="switch">Schalter (AN/AUS)</option>
          <option value="slider">Slider (Zahlenwert)</option>
          <option value="display">Anzeige (nur lesen)</option>
        </select></div>
      <div class="fld"><label>Symbol</label><select id="wm_device"></select></div>
    </div>
    <div class="field-grid">
      <div class="fld"><label>Bezeichnung</label><input id="wm_name" placeholder="z. B. Wohnzimmer Licht"></div>
      <div class="fld"><label>Gruppe</label><select id="wm_group"></select></div>
    </div>
    <label>Status-Topic</label>
    <input id="wm_topic" list="topicDatalist" placeholder="z. B. stat/wz/POWER" style="width:100%; margin-bottom:6px;">
    <div class="discover">
      <input id="wm_filter" placeholder="Empfangene Topics filtern ..." oninput="renderDiscover('wm')">
      <div class="discover-list" id="wm_discover"></div>
    </div>
    <div id="wm_cmdFields">
      <label>Befehls-Topic (optional)</label>
      <input id="wm_cmd" list="topicDatalist" placeholder="leer = an das Status-Topic senden" style="width:100%; margin-bottom:6px;">
      <div class="hint-text">Z.&nbsp;B. bei Tasmota: Status <code>stat/&hellip;/POWER</code>, Befehl <code>cmnd/&hellip;/POWER</code>.</div>
    </div>
    <div id="wm_switchFields" class="field-grid">
      <div class="fld"><label>Payload AN</label><input id="wm_on" placeholder="z. B. 1 oder ON"></div>
      <div class="fld"><label>Payload AUS</label><input id="wm_off" placeholder="z. B. 0 oder OFF"></div>
    </div>
    <div id="wm_sliderFields" class="field-grid">
      <div class="fld narrow"><label>Minimum</label><input id="wm_min" value="0"></div>
      <div class="fld narrow"><label>Maximum</label><input id="wm_max" value="100"></div>
      <div class="fld narrow"><label>Schritt</label><input id="wm_step" value="1"></div>
    </div>
    <div id="wm_displayFields" class="field-grid">
      <div class="fld narrow"><label>Nachkommastellen</label>
        <select id="wm_dec"><option value="">auto</option><option>0</option><option>1</option><option>2</option><option>3</option></select></div>
      <div class="fld"><label>Wert f&uuml;r &bdquo;aktiv&ldquo; (optional)</label><input id="wm_active" placeholder="z. B. ON - f&auml;rbt die Kachel ein"></div>
    </div>
    <div class="field-grid">
      <div class="fld narrow"><label>Einheit</label><input id="wm_unit" placeholder="z. B. %"></div>
    </div>
    <label class="checkbox-row" id="wm_retainRow"><input type="checkbox" id="wm_retain"> Nachrichten mit Retain-Flag senden</label>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeModal('widgetModal')">Abbrechen</button>
      <button class="btn" onclick="submitWidgetModal()">Speichern</button>
    </div>
  </div>
</div>

<!-- Modal: Virtuelles Topic anlegen/bearbeiten -->
<div class="modal-backdrop" id="virtualModal">
  <div class="modal">
    <h2 id="virtualModalTitle">Virtuelles Topic hinzuf&uuml;gen</h2>
    <div class="error-msg" id="virtualModalError"></div>
    <label>Name (Topic)</label>
    <input id="vm_name" placeholder="z. B. virtual/differenz" style="width:100%; margin-bottom:14px;">
    <div class="field-label">Quell-Topics</div>
    <div id="vm_sources"></div>
    <button class="btn-mini" onclick="addVirtualSource('')" style="margin-bottom:12px;">+ Quell-Topic hinzuf&uuml;gen</button>
    <div class="discover">
      <input id="vm_filter" placeholder="Empfangene Topics filtern ..." oninput="renderDiscover('vm')">
      <div class="discover-list" id="vm_discover"></div>
    </div>
    <div class="field-grid">
      <div class="fld"><label>Formel</label><input id="vm_formula" placeholder="z. B. a - b"></div>
      <div class="fld narrow"><label>Einheit</label><input id="vm_unit" placeholder="z. B. W"></div>
    </div>
    <div class="hint-text">
      Erlaubt: <code>+ - * / // % **</code>, Vergleiche, <code>x if bedingung else y</code> sowie
      <code>abs min max round pow sqrt log log10 exp floor ceil sin cos tan pi</code>.
    </div>
    <div class="status-line" id="vm_testResult" style="margin:-6px 0 12px;"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="testVirtual()">Testen</button>
      <button class="btn btn-ghost" onclick="closeModal('virtualModal')">Abbrechen</button>
      <button class="btn" onclick="submitVirtualModal()">Speichern</button>
    </div>
  </div>
</div>

<datalist id="topicDatalist"></datalist>
<div class="toast-container" id="toastContainer"></div>
<script>
/* ======================================================================
   MQTT Dashboard v2 - Frontend
   Bewusst ES5 (var/function/XMLHttpRequest, keine Arrow-Functions,
   keine Template-Strings, kein fetch): laeuft wie v1.9 auch auf alten
   iPads (iOS 9.3+) als Wandanzeige.
   Diagramme: eigener, kleiner Canvas-Renderer statt Chart.js/moment -
   dadurch KEINE Abhaengigkeit von CDN/Internet mehr.
   ====================================================================== */

var APP = {
  cfg: null,
  states: {},          // topic -> {value, numeric, ts, source}
  data: {},            // topic -> {x:[ms], y:[wert]}
  offset: 0,           // Serverzeit - Browserzeit
  lastSince: 0,
  fetchedRangeMs: 0,
  rangeH: 24,
  cols: 1,
  settingsMode: false,
  pollTimer: null,
  statusTimer: null,
  pollSeq: 0,
  topics: [],          // fuer die Topic-Auswahl in den Dialogen
  hover: null,         // {cid, x}
  sliderBusy: {},      // widget-id -> Zeitpunkt der letzten Bedienung
  sliderTimers: {},
  geo: {}              // chart-id -> Geometrie fuer Tooltip
};

var RANGE_CHOICES = [1, 6, 12, 24, 48];

var DEVICES = {
  lamp:        { icon: "&#128161;", color: "#f5c518", label: "Lampe" },
  fan:         { icon: "&#127744;", color: "#18a8f5", label: "L&uuml;fter" },
  camera:      { icon: "&#128247;", color: "#f5603c", label: "Kamera" },
  shutter:     { icon: "&#9776;",   color: "#78b5f5", label: "Rollladen" },
  motion:      { icon: "&#128065;", color: "#f5d018", label: "Bewegungsmelder" },
  plug:        { icon: "&#128268;", color: "#3ddc97", label: "Steckdose" },
  heating:     { icon: "&#128293;", color: "#ff9142", label: "Heizung" },
  ac:          { icon: "&#10052;",  color: "#2ed3d3", label: "Klimaanlage" },
  lock:        { icon: "&#128274;", color: "#b38cff", label: "Schloss" },
  speaker:     { icon: "&#128266;", color: "#18f5e1", label: "Lautsprecher" },
  sensor:      { icon: "&#128202;", color: "#ff7ac6", label: "Sensor" },
  temperature: { icon: "&#127777;", color: "#ff5d5d", label: "Temperatur" },
  power:       { icon: "&#9889;",   color: "#e8b23d", label: "Strom/Leistung" },
  solar:       { icon: "&#9728;",   color: "#ffc93c", label: "Solaranlage" },
  water:       { icon: "&#128167;", color: "#4aa3ff", label: "Wasser/Feuchte" },
  door:        { icon: "&#128682;", color: "#c49a6c", label: "T&uuml;r/Fenster" },
  washer:      { icon: "&#129530;", color: "#5b8cff", label: "Waschmaschine" },
  server:      { icon: "&#128421;", color: "#9aa5b1", label: "Server/PC" },
  generic:     { icon: "&#9881;",   color: "#7bd88f", label: "Ger&auml;t" }
};
var DEVICE_ORDER = ["lamp","plug","heating","ac","fan","shutter","lock","door","motion","camera",
                    "speaker","temperature","water","power","solar","washer","server","sensor","generic"];

var GROUP_COLORS = [
  ["#ff9142","Orange"],["#3ddc97","Gr&uuml;n"],["#4aa3ff","Blau"],["#ff5d5d","Rot"],
  ["#e8b23d","Gelb"],["#b38cff","Lila"],["#2ed3d3","T&uuml;rkis"],["#ff7ac6","Pink"],
  ["#9aa5b1","Grau"],["#7bd88f","Hellgr&uuml;n"],["#c49a6c","Braun"],["#5b8cff","Indigo"]
];
var SERIES_COLORS = ["#ff9142","#3ddc97","#4aa3ff","#ff5d5d","#e8b23d","#b38cff","#2ed3d3","#ff7ac6","#9aa5b1","#7bd88f","#c49a6c","#5b8cff"];
var MONO = "Consolas, 'SFMono-Regular', Menlo, monospace";

/* ---------------------------------------------------------------- Helfer */
function $(id){ return document.getElementById(id); }
function esc(s){
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
function api(method, url, body, cb){
  var x = new XMLHttpRequest();
  x.open(method, url, true);
  x.setRequestHeader("Content-Type", "application/json");
  x.timeout = 15000;
  x.onreadystatechange = function(){
    if(x.readyState !== 4) return;
    var d = null;
    try { d = JSON.parse(x.responseText); } catch(e) {}
    if(x.status >= 200 && x.status < 300){ if(cb) cb(null, d); }
    else if(cb){
      cb((d && d.error) || (x.status === 0 ? "Server nicht erreichbar." : "Fehler " + x.status), d);
    }
  };
  x.send(body ? JSON.stringify(body) : null);
}
function showToast(msg, kind){
  var c = $("toastContainer");
  var el = document.createElement("div");
  el.className = "toast " + (kind === "err" ? "err" : "ok");
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(function(){ if(el.parentNode) el.parentNode.removeChild(el); }, 6000);
}
function lsGet(k, def){ try { var v = localStorage.getItem(k); return v === null ? def : v; } catch(e){ return def; } }
function lsSet(k, v){ try { localStorage.setItem(k, v); } catch(e){} }
function byOrder(a, b){ return (a.order || 0) - (b.order || 0); }
function serverNow(){ return Date.now() + APP.offset; }
function pad2(n){ return n < 10 ? "0" + n : "" + n; }
function fmtClock(ms, sec){
  var d = new Date(ms);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + (sec ? ":" + pad2(d.getSeconds()) : "");
}
function fmtDay(ms){ var d = new Date(ms); return pad2(d.getDate()) + "." + pad2(d.getMonth() + 1) + "."; }
function fmtNum(v, decimals){
  if(v === null || v === undefined || isNaN(v)) return "–";
  var s;
  if(decimals !== null && decimals !== undefined && decimals !== ""){
    s = Number(v).toFixed(parseInt(decimals, 10));
  } else {
    var a = Math.abs(v);
    var dec = a >= 1000 ? 0 : (a >= 100 ? 1 : (a >= 1 ? 2 : 3));
    s = String(parseFloat(Number(v).toFixed(dec)));
  }
  return s.replace(".", ",");
}
function hexToRgba(hex, a){
  var h = (hex || "#888888").replace("#", "");
  if(h.length !== 6) h = "888888";
  return "rgba(" + parseInt(h.substr(0,2),16) + "," + parseInt(h.substr(2,2),16) + "," + parseInt(h.substr(4,2),16) + "," + a + ")";
}
function findById(list, id){ for(var i = 0; i < list.length; i++){ if(list[i].id === id) return list[i]; } return null; }
function uniq(arr){ var seen = {}, out = []; for(var i = 0; i < arr.length; i++){ if(arr[i] && !seen[arr[i]]){ seen[arr[i]] = 1; out.push(arr[i]); } } return out; }
function bisect(xs, t){ var lo = 0, hi = xs.length; while(lo < hi){ var m = (lo + hi) >>> 1; if(xs[m] < t) lo = m + 1; else hi = m; } return lo; }
function seriesColor(s, i){ return s.color || SERIES_COLORS[i % SERIES_COLORS.length]; }
function groupName(gid){
  var g = gid ? findById(APP.cfg.groups, gid) : null;
  return g ? g.name : "Ohne Gruppe";
}

/* ---------------------------------------------------------- Titel/Header */
function applyTitle(){
  var t = (APP.cfg && APP.cfg.title) || "MQTT Dashboard";
  var parts = t.split(" ");
  var html = parts.length > 1
    ? esc(parts.slice(0, -1).join(" ")) + "<span>" + esc(parts[parts.length - 1]) + "</span>"
    : "<span>" + esc(t) + "</span>";
  $("appTitle").innerHTML = html + ' <span class="ver-badge" id="verBadge">v' + esc(APP.cfg.version) + "</span>";
  document.title = t;
}
function buildRangeSwitch(){
  var h = "";
  for(var i = 0; i < RANGE_CHOICES.length; i++){
    var r = RANGE_CHOICES[i];
    h += '<button type="button" class="layout-btn' + (r === APP.rangeH ? " active" : "") + '" onclick="setRange(' + r + ')">' + r + "h</button>";
  }
  $("rangeSwitch").innerHTML = h;
}
function setRange(h){
  APP.rangeH = h;
  lsSet("mqttDashRangeH", String(h));
  buildRangeSwitch();
  if(maxRangeMs() > APP.fetchedRangeMs) loadHistory(); else drawAllCharts();
}
function getLayoutCols(){ var v = parseInt(lsGet("mqttDashLayoutCols", "1"), 10); return (v >= 1 && v <= 4) ? v : 1; }
function setLayoutCols(cols){
  APP.cols = cols;
  var root = $("viewRoot");
  root.className = cols > 1 ? "cols-" + cols : "";
  var btns = $("colSwitch").getElementsByTagName("button");
  for(var i = 0; i < btns.length; i++){
    var on = parseInt(btns[i].getAttribute("data-cols"), 10) === cols;
    btns[i].className = "layout-btn" + (on ? " active" : "");
  }
  lsSet("mqttDashLayoutCols", String(cols));
  drawAllCharts();
}
function setConn(ok, broker, err){
  $("connDot").className = "status-dot" + (ok ? " online" : "");
  var txt;
  if(err) txt = err;
  else if(!broker || !broker.host) txt = "kein Broker";
  else txt = (ok ? "" : "getrennt · ") + broker.host + (broker.port && broker.port !== 1883 ? ":" + broker.port : "");
  $("connText").textContent = txt;
  $("connPill").title = (broker && broker.error) ? broker.error : "Verbindung zum MQTT-Broker";
}

/* -------------------------------------------------------- Konfiguration */
function loadConfig(cb){
  api("GET", "/api/config", null, function(err, d){
    if(err){ showToast("Konfiguration konnte nicht geladen werden: " + err, "err"); setTimeout(function(){ loadConfig(cb); }, 4000); return; }
    APP.cfg = d;
    applyTitle();
    if(cb) cb();
  });
}
function sortedGroups(){ return APP.cfg.groups.slice().sort(byOrder); }
function chartTopics(){
  var t = [];
  var cs = APP.cfg.charts;
  for(var i = 0; i < cs.length; i++) for(var j = 0; j < cs[i].series.length; j++) t.push(cs[i].series[j].topic);
  return uniq(t);
}
function viewTopics(){
  var t = chartTopics();
  var ws = APP.cfg.widgets;
  for(var i = 0; i < ws.length; i++) t.push(ws[i].topic);
  return uniq(t);
}
function maxRangeMs(){
  var h = APP.rangeH;
  var cs = APP.cfg ? APP.cfg.charts : [];
  for(var i = 0; i < cs.length; i++) if(cs[i].range_h && cs[i].range_h > h) h = cs[i].range_h;
  return h * 3600000;
}

/* ============================================================== ANSICHT */
function itemGroup(item, known){ return (item.group_id && known[item.group_id]) ? item.group_id : null; }

function renderView(){
  var cfg = APP.cfg, root = $("viewRoot");
  var groups = sortedGroups();
  var charts = cfg.charts.slice().sort(byOrder);
  var widgets = cfg.widgets.slice().sort(byOrder);
  APP.geo = {};
  if(!charts.length && !widgets.length){
    root.innerHTML = '<div class="empty-state"><h2>Noch nichts angelegt</h2>' +
      '<div>' + (cfg.broker.host ? "Diagramme, Schalter und Anzeigen werden in den Einstellungen angelegt."
                                 : "Zuerst in den Einstellungen den MQTT-Broker eintragen, danach Diagramme, Schalter und Anzeigen anlegen.") + '</div>' +
      '<button class="btn" onclick="enterSettingsMode()">&#9881; Zu den Einstellungen</button></div>';
    return;
  }
  var known = {};
  for(var g = 0; g < groups.length; g++) known[groups[g].id] = 1;
  var sections = [];
  for(g = 0; g < groups.length; g++) sections.push({ id: groups[g].id, name: groups[g].name, color: groups[g].color });
  sections.push({ id: null, name: "Ohne Gruppe", color: "#3a4250" });

  var html = "";
  for(var s = 0; s < sections.length; s++){
    var sec = sections[s], ws = [], cs = [], i;
    for(i = 0; i < widgets.length; i++) if(itemGroup(widgets[i], known) === sec.id) ws.push(widgets[i]);
    for(i = 0; i < charts.length; i++) if(itemGroup(charts[i], known) === sec.id) cs.push(charts[i]);
    if(!ws.length && !cs.length) continue;
    var key = sec.id || "none";
    var collapsed = groups.length && lsGet("mqttDashCollapsed_" + key, "0") === "1";
    html += '<section class="grp' + (collapsed ? " collapsed" : "") + '" id="sec_' + key + '">';
    if(groups.length){
      var cnt = [];
      if(ws.length) cnt.push(ws.length + (ws.length === 1 ? " Element" : " Elemente"));
      if(cs.length) cnt.push(cs.length + (cs.length === 1 ? " Diagramm" : " Diagramme"));
      html += '<div class="room-header" onclick="toggleGroup(\'' + key + '\')"><span class="grp-caret">&#9660;</span>' +
        '<span class="grp-dot" style="background:' + esc(sec.color) + '"></span>' + esc(sec.name) +
        '<span class="grp-count">' + cnt.join(" · ") + "</span></div>";
    }
    html += '<div class="grp-body">';
    if(ws.length){
      html += '<div class="tiles">';
      for(i = 0; i < ws.length; i++) html += tileHtml(ws[i]);
      html += "</div>";
    }
    if(cs.length){
      html += '<div class="charts">';
      for(i = 0; i < cs.length; i++) html += chartCardHtml(cs[i]);
      html += "</div>";
    }
    html += "</div></section>";
  }
  root.innerHTML = html;
  bindChartEvents();
  updateTiles();
  updateLegends();
  drawAllCharts();
}

function toggleGroup(key){
  var el = $("sec_" + key);
  if(!el) return;
  var collapsed = el.className.indexOf("collapsed") < 0;
  el.className = "grp" + (collapsed ? " collapsed" : "");
  lsSet("mqttDashCollapsed_" + key, collapsed ? "1" : "0");
  if(!collapsed) drawAllCharts();
}

/* ------------------------------------------------------------- Kacheln */
function tileHtml(w){
  var dev = DEVICES[w.device] || DEVICES.generic;
  var h = '<div class="tile tile-' + w.type + '" id="tile_' + w.id + '"' +
    (w.type === "switch" ? ' onclick="toggleSwitch(\'' + w.id + '\')"' : "") + ">";
  if(w.type === "switch") h += '<span class="tile-badge" id="tb_' + w.id + '">&ndash;</span>';
  h += '<div class="tile-icon">' + dev.icon + '</div><div class="tile-name">' + esc(w.name) + "</div>";
  h += '<div class="tile-value" id="tv_' + w.id + '">&ndash;</div>';
  if(w.type === "slider"){
    h += '<input type="range" class="tile-range" id="ts_' + w.id + '" min="' + w.min + '" max="' + w.max + '" step="' + w.step +
      '" value="' + w.min + '" oninput="sliderInput(\'' + w.id + '\', this)" onchange="sliderCommit(\'' + w.id + '\', this)">';
  }
  h += '<div class="tile-src" id="tsrc_' + w.id + '"></div></div>';
  return h;
}
function widgetIsOn(w, st){
  if(!st) return false;
  if(w.type === "switch") return st.value === w.payload_on;
  if(w.type === "display") return !!w.payload_on && st.value === w.payload_on;
  return false;
}
function updateTiles(){
  var ws = APP.cfg.widgets;
  for(var i = 0; i < ws.length; i++){
    var w = ws[i], tile = $("tile_" + w.id);
    if(!tile) continue;
    var st = APP.states[w.topic], dev = DEVICES[w.device] || DEVICES.generic;
    var on = widgetIsOn(w, st);
    tile.className = "tile tile-" + w.type + (on ? " on" : "");
    tile.style.borderColor = on ? dev.color : "";
    tile.style.backgroundColor = on ? hexToRgba(dev.color, 0.16) : "";
    var val = "–", unit = w.unit ? " " + w.unit : "";
    if(st){
      if(w.type === "switch"){
        val = st.value === w.payload_on ? "AN" : (st.value === w.payload_off ? "AUS" : st.value);
      } else if(w.type === "display"){
        val = st.numeric !== null && st.numeric !== undefined ? fmtNum(st.numeric, w.decimals) : st.value;
      } else {
        val = st.numeric !== null && st.numeric !== undefined ? fmtNum(st.numeric, null) : st.value;
      }
    }
    var tv = $("tv_" + w.id);
    if(w.type === "display" && st && unit) tv.innerHTML = esc(val) + "<small>" + esc(w.unit) + "</small>";
    else tv.textContent = val + (st && w.type !== "switch" ? unit : "");
    var tb = $("tb_" + w.id);
    if(tb){ tb.textContent = on ? "an" : "aus"; tb.style.backgroundColor = on ? dev.color : ""; }
    var src = $("tsrc_" + w.id);
    src.textContent = !st ? "" : (st.source === "self" ? "● eigen" : (st.source === "virtual" ? "◆ virtuell" : "○ extern"));
    if(w.type === "slider" && st && st.numeric !== null && st.numeric !== undefined){
      var busy = APP.sliderBusy[w.id] && (Date.now() - APP.sliderBusy[w.id] < 2500);
      if(!busy) $("ts_" + w.id).value = st.numeric;
    }
  }
}
function publish(topic, payload, retain){
  api("POST", "/api/publish", { topic: topic, payload: payload, retain: !!retain }, function(err){
    if(err) showToast(err, "err");
  });
}
function toggleSwitch(id){
  var w = findById(APP.cfg.widgets, id);
  if(!w) return;
  var on = widgetIsOn(w, APP.states[w.topic]);
  publish(w.command_topic || w.topic, on ? w.payload_off : w.payload_on, w.retain);
}
function sliderInput(id, el){
  var w = findById(APP.cfg.widgets, id);
  if(!w) return;
  APP.sliderBusy[id] = Date.now();
  $("tv_" + id).textContent = fmtNum(parseFloat(el.value), null) + (w.unit ? " " + w.unit : "");
  clearTimeout(APP.sliderTimers[id]);
  APP.sliderTimers[id] = setTimeout(function(){ publish(w.command_topic || w.topic, el.value, w.retain); }, 250);
}
function sliderCommit(id, el){
  var w = findById(APP.cfg.widgets, id);
  if(!w) return;
  APP.sliderBusy[id] = Date.now();
  clearTimeout(APP.sliderTimers[id]);
  publish(w.command_topic || w.topic, el.value, w.retain);
}

/* ---------------------------------------------------------- Diagramme */
function chartCardHtml(c){
  var h = '<div class="chart-card" id="cc_' + c.id + '"><div class="card-head"><div><span class="name">' + esc(c.name) + "</span>";
  if(c.unit) h += '<span class="type-badge">' + esc(c.unit) + "</span>";
  if(c.range_h) h += '<span class="type-badge">' + c.range_h + " h</span>";
  h += '</div></div><div class="legend">';
  for(var i = 0; i < c.series.length; i++){
    var s = c.series[i];
    h += '<span class="legend-chip" title="' + esc(s.topic) + '"><i style="background:' + esc(seriesColor(s, i)) + '"></i>' +
      '<span class="lg-label">' + esc(s.label || s.topic) + '</span><b id="lv_' + c.id + "_" + i + '">&ndash;</b></span>';
  }
  h += '</div><div class="chart-box h-' + (c.height || "m") + '"><canvas id="cv_' + c.id + '"></canvas>' +
    '<div class="chart-empty" id="ce_' + c.id + '">Keine Daten im Zeitraum</div></div></div>';
  return h;
}
function updateLegends(){
  var cs = APP.cfg.charts;
  for(var i = 0; i < cs.length; i++){
    for(var j = 0; j < cs[i].series.length; j++){
      var el = $("lv_" + cs[i].id + "_" + j);
      if(!el) continue;
      var st = APP.states[cs[i].series[j].topic];
      el.textContent = st ? (st.numeric !== null && st.numeric !== undefined ? fmtNum(st.numeric, null) + (cs[i].unit ? " " + cs[i].unit : "") : st.value) : "–";
    }
  }
}
function bindChartEvents(){
  var cs = APP.cfg.charts;
  for(var i = 0; i < cs.length; i++){
    var cv = $("cv_" + cs[i].id);
    if(!cv) continue;
    (function(cid, canvas){
      function at(ev){
        var r = canvas.getBoundingClientRect();
        var p = ev.touches && ev.touches.length ? ev.touches[0] : ev;
        APP.hover = { cid: cid, x: p.clientX - r.left };
        drawChartById(cid);
      }
      function leave(){ if(APP.hover && APP.hover.cid === cid){ APP.hover = null; drawChartById(cid); } }
      canvas.addEventListener("mousemove", at);
      canvas.addEventListener("mouseleave", leave);
      canvas.addEventListener("touchstart", at);
      canvas.addEventListener("touchmove", at);
      canvas.addEventListener("touchend", function(){ setTimeout(leave, 2500); });
    })(cs[i].id, cv);
  }
}
function drawAllCharts(){
  if(!APP.cfg || APP.settingsMode) return;
  for(var i = 0; i < APP.cfg.charts.length; i++) drawChart(APP.cfg.charts[i]);
}
function drawChartById(cid){ var c = findById(APP.cfg.charts, cid); if(c) drawChart(c); }

function niceStep(raw){
  if(raw <= 0 || !isFinite(raw)) return 1;
  var p = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
var TIME_STEPS = [60000, 300000, 600000, 900000, 1800000, 3600000, 7200000, 10800000, 21600000, 43200000, 86400000];

/* Pixelweise min/max-Reduktion: haelt Spitzen sichtbar, egal wie viele Punkte */
function pathIndices(d, from, end, X){
  var out = [], col = null, mn = 0, mx = 0, first = 0, last = 0;
  function flush(){
    if(col === null) return;
    var idx = [first, mn, mx, last].sort(function(a, b){ return a - b; });
    for(var q = 0; q < idx.length; q++) if(!out.length || out[out.length - 1] !== idx[q]) out.push(idx[q]);
  }
  for(var k = from; k < end; k++){
    var c = Math.floor(X(d.x[k]));
    if(c !== col){ flush(); col = c; mn = mx = first = last = k; }
    else { if(d.y[k] < d.y[mn]) mn = k; if(d.y[k] > d.y[mx]) mx = k; last = k; }
  }
  flush();
  return out;
}

function drawChart(c){
  var cv = $("cv_" + c.id);
  if(!cv || !cv.getContext) return;
  var box = cv.parentNode, w = box.clientWidth, h = box.clientHeight;
  if(!w || !h) return;
  var dpr = window.devicePixelRatio || 1;
  if(cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)){
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  var ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  var rangeMs = (c.range_h || APP.rangeH) * 3600000;
  var t1 = serverNow(), t0 = t1 - rangeMs;
  var ymin = Infinity, ymax = -Infinity, any = false, ser = [], i, k;
  for(i = 0; i < c.series.length; i++){
    var s = c.series[i], d = APP.data[s.topic], col = seriesColor(s, i);
    if(!d || !d.x.length){ ser.push({ s: s, col: col, d: null }); continue; }
    var st = bisect(d.x, t0), en = d.x.length;
    for(k = st; k < en; k++){ var v = d.y[k]; if(v < ymin) ymin = v; if(v > ymax) ymax = v; any = true; }
    var from = st;
    if(st > 0){
      from = st - 1;
      if(c.stepped || st < en){ var pv = d.y[st - 1]; if(pv < ymin) ymin = pv; if(pv > ymax) ymax = pv; any = true; }
    }
    ser.push({ s: s, col: col, d: d, from: from, end: en, st: st });
  }
  $("ce_" + c.id).style.display = any ? "none" : "block";
  var fixedMin = c.y_min !== null && c.y_min !== undefined, fixedMax = c.y_max !== null && c.y_max !== undefined;
  if(!any){ ymin = 0; ymax = 1; }
  if(ymax - ymin < 1e-9){ ymin -= 1; ymax += 1; }
  else { var padY = (ymax - ymin) * 0.08; if(!fixedMin) ymin -= padY; if(!fixedMax) ymax += padY; }
  if(fixedMin) ymin = c.y_min;
  if(fixedMax) ymax = c.y_max;
  if(ymax <= ymin) ymax = ymin + 1;

  var yStep = niceStep((ymax - ymin) / (h < 200 ? 3 : 5));
  var yt = [], yv = Math.ceil(ymin / yStep - 1e-9) * yStep;
  for(; yv <= ymax + 1e-9 && yt.length < 20; yv += yStep) yt.push(yv);
  ctx.font = "10px " + MONO;
  var labW = 0;
  for(i = 0; i < yt.length; i++){ var lw = ctx.measureText(fmtNum(yt[i], null)).width; if(lw > labW) labW = lw; }
  var padL = Math.ceil(labW) + 12, padR = 10, padT = 8, padB = 20;
  var pw = Math.max(10, w - padL - padR), ph = Math.max(10, h - padT - padB);
  function X(t){ return padL + (t - t0) / rangeMs * pw; }
  function Y(v){ return padT + (ymax - v) / (ymax - ymin) * ph; }

  /* Raster + Y-Beschriftung */
  ctx.strokeStyle = "#242a33"; ctx.lineWidth = 1; ctx.fillStyle = "#8891a0";
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  for(i = 0; i < yt.length; i++){
    var yy = Math.round(Y(yt[i])) + 0.5;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + pw, yy); ctx.stroke();
    ctx.fillText(fmtNum(yt[i], null), padL - 6, yy);
  }
  /* Zeitachse (in lokaler Zeit ausgerichtet) */
  var maxTicks = Math.max(2, Math.floor(pw / 72)), xStep = TIME_STEPS[TIME_STEPS.length - 1];
  for(i = 0; i < TIME_STEPS.length; i++){ if(rangeMs / TIME_STEPS[i] <= maxTicks){ xStep = TIME_STEPS[i]; break; } }
  var tz = -new Date(t0).getTimezoneOffset() * 60000;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for(var tt = Math.ceil((t0 + tz) / xStep) * xStep - tz; tt <= t1; tt += xStep){
    var xx = Math.round(X(tt)) + 0.5;
    ctx.beginPath(); ctx.moveTo(xx, padT); ctx.lineTo(xx, padT + ph); ctx.stroke();
    var dt = new Date(tt);
    var lbl = (xStep >= 86400000 || (dt.getHours() === 0 && dt.getMinutes() === 0 && rangeMs > 6 * 3600000)) ? fmtDay(tt) : fmtClock(tt);
    ctx.fillText(lbl, Math.min(Math.max(xx, padL + 14), padL + pw - 14), padT + ph + 5);
  }

  /* Linien */
  ctx.save();
  ctx.beginPath(); ctx.rect(padL, padT - 1, pw + 1, ph + 2); ctx.clip();
  ctx.lineJoin = "round"; ctx.lineCap = "round";
  for(i = 0; i < ser.length; i++){
    var sr = ser[i];
    if(!sr.d) continue;
    var idx = pathIndices(sr.d, sr.from, sr.end, X);
    if(!idx.length) continue;
    var dd = sr.d, firstX = null, lastX = null, lastY = null;
    ctx.beginPath();
    for(k = 0; k < idx.length; k++){
      var px = X(dd.x[idx[k]]), py = Y(dd.y[idx[k]]);
      if(firstX === null){ ctx.moveTo(px, py); firstX = px; }
      else if(c.stepped){ ctx.lineTo(px, lastY); ctx.lineTo(px, py); }
      else ctx.lineTo(px, py);
      lastX = px; lastY = py;
    }
    if(c.stepped){ ctx.lineTo(padL + pw, lastY); lastX = padL + pw; }
    if(idx.length === 1 && !c.stepped){ ctx.arc(lastX, lastY, 1.5, 0, Math.PI * 2); }
    ctx.strokeStyle = sr.col; ctx.lineWidth = 1.8; ctx.stroke();
    if(c.fill){
      var base = Y(Math.max(ymin, Math.min(ymax, 0 >= ymin && 0 <= ymax ? 0 : ymin)));
      ctx.lineTo(lastX, base); ctx.lineTo(firstX, base); ctx.closePath();
      ctx.fillStyle = hexToRgba(sr.col, 0.12); ctx.fill();
    }
  }
  ctx.restore();

  APP.geo[c.id] = { t0: t0, rangeMs: rangeMs, padL: padL, pw: pw, padT: padT, ph: ph, Y: Y, ser: ser };
  if(APP.hover && APP.hover.cid === c.id) drawTooltip(c, ctx, w, h);
}

function drawTooltip(c, ctx, w, h){
  var g = APP.geo[c.id], hx = APP.hover.x;
  if(!g || hx < g.padL || hx > g.padL + g.pw) return;
  var t = g.t0 + (hx - g.padL) / g.pw * g.rangeMs;
  ctx.strokeStyle = "rgba(229,232,236,.35)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(Math.round(hx) + 0.5, g.padT); ctx.lineTo(Math.round(hx) + 0.5, g.padT + g.ph); ctx.stroke();
  var lines = [], i, tShown = null;
  for(i = 0; i < g.ser.length; i++){
    var sr = g.ser[i];
    if(!sr.d) continue;
    var d = sr.d, k = bisect(d.x, t), best = -1;
    if(c.stepped){ best = k < d.x.length && d.x[k] === t ? k : k - 1; }
    else {
      if(k < d.x.length) best = k;
      if(k > 0 && (best < 0 || Math.abs(d.x[k - 1] - t) < Math.abs(d.x[best] - t))) best = k - 1;
    }
    if(best < 0 || best >= d.x.length || d.x[best] < g.t0 - g.rangeMs * 0.02 && !c.stepped) continue;
    if(!c.stepped && Math.abs(d.x[best] - t) > g.rangeMs / 12) continue;
    var py = g.Y(d.y[best]);
    var px = c.stepped ? hx : g.padL + (d.x[best] - g.t0) / g.rangeMs * g.pw;
    if(!c.stepped && tShown === null) tShown = d.x[best];
    ctx.fillStyle = sr.col; ctx.beginPath(); ctx.arc(px, py, 3.5, 0, Math.PI * 2); ctx.fill();
    lines.push({ col: sr.col, txt: (sr.s.label || sr.s.topic) + ": " + fmtNum(d.y[best], null) + (c.unit ? " " + c.unit : "") });
  }
  if(!lines.length) return;
  var tsTxt = fmtDay(tShown || t) + " " + fmtClock(tShown || t, true);
  ctx.font = "11px " + MONO;
  var bw = ctx.measureText(tsTxt).width;
  for(i = 0; i < lines.length; i++){ if(lines[i].txt.length > 48) lines[i].txt = lines[i].txt.substr(0, 46) + "…"; bw = Math.max(bw, ctx.measureText(lines[i].txt).width + 16); }
  bw += 16;
  var bh = 22 + lines.length * 16, bx = hx + 12, by = g.padT + 6;
  if(bx + bw > w - 4) bx = hx - 12 - bw;
  if(bx < 2) bx = 2;
  ctx.fillStyle = "rgba(18,21,26,.95)"; ctx.strokeStyle = "#3a4250";
  ctx.beginPath(); ctx.rect(bx, by, bw, bh); ctx.fill(); ctx.stroke();
  ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillStyle = "#8891a0";
  ctx.fillText(tsTxt, bx + 8, by + 6);
  for(i = 0; i < lines.length; i++){
    ctx.fillStyle = lines[i].col; ctx.fillRect(bx + 8, by + 25 + i * 16, 8, 8);
    ctx.fillStyle = "#e5e8ec"; ctx.fillText(lines[i].txt, bx + 22, by + 22 + i * 16);
  }
}

/* --------------------------------------------------------- Datenabruf */
function loadHistory(){
  var range = maxRangeMs(), seq = ++APP.pollSeq;
  clearTimeout(APP.pollTimer);
  api("POST", "/api/history", { topics: viewTopics(), chart_topics: chartTopics(), range_ms: range, max_points: 1500 }, function(err, d){
    if(seq !== APP.pollSeq) return;
    if(err){ setConn(false, null, err); APP.pollTimer = setTimeout(loadHistory, 4000); return; }
    APP.offset = d.now - Date.now();
    APP.lastSince = d.now;
    APP.fetchedRangeMs = range;
    APP.data = {};
    for(var t in d.points){
      if(!d.points.hasOwnProperty(t)) continue;
      var xs = [], ys = [], pts = d.points[t];
      for(var i = 0; i < pts.length; i++){ xs.push(pts[i][0]); ys.push(pts[i][1]); }
      APP.data[t] = { x: xs, y: ys };
    }
    mergeStates(d.states);
    updateTiles(); updateLegends(); drawAllCharts();
    APP.pollTimer = setTimeout(poll, 2000);
  });
}
function mergeStates(states){ for(var t in states){ if(states.hasOwnProperty(t)) APP.states[t] = states[t]; } }
function trimData(){
  var cutoff = serverNow() - APP.fetchedRangeMs - 60000;
  for(var t in APP.data){
    if(!APP.data.hasOwnProperty(t)) continue;
    var d = APP.data[t], k = bisect(d.x, cutoff);
    if(k > 1){ d.x = d.x.slice(k - 1); d.y = d.y.slice(k - 1); }
    if(d.x.length > 6000){       /* aeltere Haelfte ausduennen, juengste Werte bleiben exakt */
      var keep = 1000, nx = [], ny = [], cut = d.x.length - keep;
      for(var i = 0; i < cut; i += 2){ nx.push(d.x[i]); ny.push(d.y[i]); }
      d.x = nx.concat(d.x.slice(cut)); d.y = ny.concat(d.y.slice(cut));
    }
  }
}
function poll(){
  clearTimeout(APP.pollTimer);
  if(APP.settingsMode) return;
  var seq = ++APP.pollSeq;
  api("POST", "/api/live", { since: APP.lastSince, topics: viewTopics(), chart_topics: chartTopics() }, function(err, d){
    if(seq !== APP.pollSeq || APP.settingsMode) return;
    if(err){ setConn(false, null, err); APP.pollTimer = setTimeout(poll, 4000); return; }
    APP.offset = d.now - Date.now();
    APP.lastSince = d.now;
    setConn(d.connected, d.broker);
    mergeStates(d.states);
    for(var t in d.points){
      if(!d.points.hasOwnProperty(t)) continue;
      var arr = APP.data[t] || (APP.data[t] = { x: [], y: [] }), pts = d.points[t];
      for(var i = 0; i < pts.length; i++){
        if(arr.x.length && pts[i][0] <= arr.x[arr.x.length - 1]) continue;
        arr.x.push(pts[i][0]); arr.y.push(pts[i][1]);
      }
    }
    trimData();
    updateTiles(); updateLegends(); drawAllCharts();
    APP.pollTimer = setTimeout(poll, 2000);
  });
}

/* ========================================================= EINSTELLUNGEN */
function enterSettingsMode(){
  APP.settingsMode = true;
  APP.pollSeq++;
  clearTimeout(APP.pollTimer);
  $("operatorControls").style.display = "none";
  $("settingsModeControls").style.display = "";
  $("viewRoot").style.display = "none";
  $("settingsPanel").style.display = "block";
  window.scrollTo(0, 0);
  loadConfig(function(){
    fillBrokerForm();
    fillGeneralForm();
    refreshSettingsLists();
    $("migrationNotice").style.display = APP.cfg.migrated_from_legacy ? "block" : "none";
  });
  loadTopics();
  pollStatus();
}
function exitSettingsMode(){
  APP.settingsMode = false;
  clearTimeout(APP.statusTimer);
  $("operatorControls").style.display = "";
  $("settingsModeControls").style.display = "none";
  $("settingsPanel").style.display = "none";
  $("viewRoot").style.display = "";
  window.scrollTo(0, 0);
  loadConfig(function(){ renderView(); loadHistory(); });
}
function reloadConfigAndLists(){ loadConfig(refreshSettingsLists); }
function refreshSettingsLists(){
  renderGroupsList(); renderChartsList(); renderWidgetsList(); renderVirtualList();
}
function pollStatus(){
  clearTimeout(APP.statusTimer);
  if(!APP.settingsMode) return;
  api("GET", "/api/status", null, function(err, d){
    if(!APP.settingsMode) return;
    var el = $("brokerStatus");
    if(err){ el.className = "status-line err"; el.textContent = err; }
    else {
      var b = d.broker;
      if(!b.host){ el.className = "status-line"; el.textContent = "Kein Broker eingetragen."; }
      else if(b.connected){ el.className = "status-line ok"; el.textContent = "● Verbunden mit " + b.host + ":" + b.port + " · " + d.topic_count + " Topics empfangen"; }
      else { el.className = "status-line err"; el.textContent = "○ Nicht verbunden mit " + b.host + ":" + b.port + (b.error ? " – " + b.error : ""); }
      APP.vstatus = d.virtual;
      updateVirtualStatus();
    }
    APP.statusTimer = setTimeout(pollStatus, 3000);
  });
}

/* ------------------------------------------------- Allgemein & Broker */
function fillGeneralForm(){
  $("gen_title").value = APP.cfg.title || "";
  $("gen_port").value = APP.cfg.server.port;
  var h = "";
  for(var i = 0; i < RANGE_CHOICES.length; i++) h += '<option value="' + RANGE_CHOICES[i] + '"' + (RANGE_CHOICES[i] === APP.cfg.default_range_h ? " selected" : "") + ">" + RANGE_CHOICES[i] + " Stunden</option>";
  $("gen_range").innerHTML = h;
}
function saveGeneralSettings(){
  api("PUT", "/api/settings", { title: $("gen_title").value, port: $("gen_port").value, default_range_h: parseInt($("gen_range").value, 10) }, function(err, d){
    if(err){ showToast(err, "err"); return; }
    showToast(d.restart_required ? "Gespeichert. Der neue Port " + d.port + " gilt nach einem Neustart des Programms." : "Einstellungen gespeichert.");
    loadConfig(null);
  });
}
function fillBrokerForm(){
  var b = APP.cfg.broker;
  $("bk_host").value = b.host || "";
  $("bk_port").value = b.port || 1883;
  $("bk_user").value = b.username || "";
  $("bk_pass").value = "";
  $("bk_pass").placeholder = b.password_set ? "(gespeichert)" : "";
  $("bk_cid").value = b.client_id || "";
  $("bk_clear").checked = false;
  $("bk_clear_row").style.display = b.password_set ? "" : "none";
}
function saveBroker(){
  api("PUT", "/api/broker", {
    host: $("bk_host").value, port: $("bk_port").value, username: $("bk_user").value,
    password: $("bk_pass").value, client_id: $("bk_cid").value, clear_password: $("bk_clear").checked
  }, function(err){
    if(err){ showToast(err, "err"); return; }
    showToast("Broker-Einstellungen gespeichert - verbinde neu ...");
    loadConfig(fillBrokerForm);
    setTimeout(pollStatus, 1200);
    setTimeout(loadTopics, 3000);
  });
}

/* ---------------------------------------------------- Sortieren/Zuweisen */
function moveItem(coll, id, dir){
  var ids = APP.cfg[coll].slice().sort(byOrder).map(function(it){ return it.id; });
  var i = ids.indexOf(id), j = i + dir;
  if(i < 0 || j < 0 || j >= ids.length) return;
  var tmp = ids[i]; ids[i] = ids[j]; ids[j] = tmp;
  api("POST", "/api/" + coll + "/reorder", { order: ids }, function(err){
    if(err) showToast(err, "err");
    reloadConfigAndLists();
  });
}
function assignGroup(coll, id, gid){
  var it = findById(APP.cfg[coll], id);
  if(!it) return;
  var copy = JSON.parse(JSON.stringify(it));
  copy.group_id = gid || null;
  api("PUT", "/api/" + coll + "/" + id, copy, function(err){
    if(err) showToast(err, "err");
    reloadConfigAndLists();
  });
}
function deleteItem(coll, id, label){
  if(!window.confirm("„" + label + "“ wirklich entfernen?")) return;
  api("DELETE", "/api/" + coll + "/" + id, null, function(err){
    if(err) showToast(err, "err"); else showToast("Entfernt.");
    reloadConfigAndLists();
  });
}
function orderBtns(coll, id, i, n){
  return '<div class="order-btns">' +
    '<button class="btn-mini" ' + (i === 0 ? "disabled " : "") + 'title="Nach oben" onclick="moveItem(\'' + coll + "','" + id + '\',-1)">&uarr;</button>' +
    '<button class="btn-mini" ' + (i === n - 1 ? "disabled " : "") + 'title="Nach unten" onclick="moveItem(\'' + coll + "','" + id + '\',1)">&darr;</button></div>';
}
function groupOptions(selected){
  var h = '<option value="">Ohne Gruppe</option>', gs = sortedGroups();
  for(var i = 0; i < gs.length; i++) h += '<option value="' + gs[i].id + '"' + (gs[i].id === selected ? " selected" : "") + ">" + esc(gs[i].name) + "</option>";
  return h;
}
function groupSelect(coll, it){
  if(!APP.cfg.groups.length) return "";
  return '<select title="Gruppe" onchange="assignGroup(\'' + coll + "','" + it.id + '\', this.value)">' + groupOptions(it.group_id) + "</select>";
}
function colorOptions(selected){
  var h = "", found = false;
  for(var i = 0; i < GROUP_COLORS.length; i++){
    var on = GROUP_COLORS[i][0] === selected; if(on) found = true;
    h += '<option value="' + GROUP_COLORS[i][0] + '"' + (on ? " selected" : "") + ' style="color:' + GROUP_COLORS[i][0] + '">&#9632; ' + GROUP_COLORS[i][1] + "</option>";
  }
  if(selected && !found) h += '<option value="' + esc(selected) + '" selected>&#9632; eigene Farbe</option>';
  return h;
}

/* ------------------------------------------------------------ Gruppen */
function renderGroupsList(){
  var el = $("groupsManageList"), gs = sortedGroups();
  if(!$("newGroupColor").innerHTML) $("newGroupColor").innerHTML = colorOptions(GROUP_COLORS[gs.length % GROUP_COLORS.length][0]);
  if(!gs.length){ el.innerHTML = '<div class="hint-text">Noch keine Gruppen angelegt.</div>'; return; }
  var h = "";
  for(var i = 0; i < gs.length; i++){
    var g = gs[i], n = 0, j;
    for(j = 0; j < APP.cfg.charts.length; j++) if(APP.cfg.charts[j].group_id === g.id) n++;
    for(j = 0; j < APP.cfg.widgets.length; j++) if(APP.cfg.widgets[j].group_id === g.id) n++;
    h += '<div class="manage-row">' + orderBtns("groups", g.id, i, gs.length) +
      '<div class="manage-info"><span class="swatch" style="background:' + esc(g.color) + '"></span>' + esc(g.name) +
      '<div class="manage-sub">' + n + (n === 1 ? " Eintrag" : " Einträge") + "</div></div>" +
      '<div class="manage-actions"><button class="btn-mini" onclick="openGroupModal(\'' + g.id + '\')">Bearbeiten</button>' +
      '<button class="btn-mini btn-delete" title="Entfernen" onclick="deleteItem(\'groups\',\'' + g.id + '\', ' + esc(JSON.stringify(g.name)) + ')">&times;</button></div></div>';
  }
  el.innerHTML = h;
}
function createGroup(){
  var name = $("newGroupName").value.replace(/^\s+|\s+$/g, "");
  if(!name){ showToast("Bitte einen Namen für die Gruppe eingeben.", "err"); return; }
  api("POST", "/api/groups", { name: name, color: $("newGroupColor").value }, function(err){
    if(err){ showToast(err, "err"); return; }
    $("newGroupName").value = "";
    $("newGroupColor").innerHTML = "";
    showToast("Gruppe angelegt.");
    reloadConfigAndLists();
  });
}
var editGroupId = null;
function openGroupModal(id){
  var g = findById(APP.cfg.groups, id);
  if(!g) return;
  editGroupId = id;
  $("gm_name").value = g.name;
  $("gm_color").innerHTML = colorOptions(g.color);
  hideError("groupModalError");
  openModal("groupModal");
}
function submitGroupModal(){
  api("PUT", "/api/groups/" + editGroupId, { name: $("gm_name").value, color: $("gm_color").value }, function(err){
    if(err){ showError("groupModalError", err); return; }
    closeModal("groupModal"); showToast("Gruppe gespeichert."); reloadConfigAndLists();
  });
}

/* ------------------------------------------------------------- Modals */
function openModal(id){ $(id).className = "modal-backdrop show"; $(id).scrollTop = 0; }
function closeModal(id){ $(id).className = "modal-backdrop"; }
function showError(id, msg){ var e = $(id); e.textContent = msg; e.style.display = "block"; }
function hideError(id){ $(id).style.display = "none"; }
function fillGroupSelect(id, selected){ $(id).innerHTML = groupOptions(selected); }

/* Topic-Auswahl (empfangene Topics) - funktioniert auch ohne <datalist>-Unterstuetzung */
function loadTopics(cb){
  api("GET", "/api/topics", null, function(err, d){
    if(err) return;
    APP.topics = d || [];
    var h = "";
    for(var i = 0; i < APP.topics.length && i < 1500; i++) h += '<option value="' + esc(APP.topics[i].topic) + '">';
    $("topicDatalist").innerHTML = h;
    renderDiscover("cm"); renderDiscover("wm"); renderDiscover("vm");
    if(cb) cb();
  });
}
var DISCOVER_PICK = { cm: "pickChartTopic", wm: "pickWidgetTopic", vm: "pickVirtualTopic" };
function renderDiscover(prefix){
  var list = $(prefix + "_discover");
  if(!list) return;
  var f = ($(prefix + "_filter").value || "").toLowerCase(), h = "", n = 0;
  var numericOnly = prefix !== "wm";
  for(var i = 0; i < APP.topics.length && n < 250; i++){
    var t = APP.topics[i];
    if(numericOnly && !t.numeric) continue;
    if(f && t.topic.toLowerCase().indexOf(f) < 0) continue;
    n++;
    h += '<div class="discover-row" data-t="' + esc(t.topic) + '" onclick="' + DISCOVER_PICK[prefix] + '(this.getAttribute(\'data-t\'))">' +
      '<span class="dv-t">' + esc(t.topic) + (t.source === "virtual" ? " ◆" : "") + '</span><span class="dv-v">' + esc(t.value) + "</span></div>";
  }
  list.innerHTML = h || '<div class="discover-empty">' + (APP.topics.length ? "Keine passenden Topics." : "Noch keine Topics empfangen - Broker verbunden?") + "</div>";
}

/* ---------------------------------------------------------- Diagramme */
function renderChartsList(){
  var el = $("chartsManageList"), cs = APP.cfg.charts.slice().sort(byOrder);
  if(!cs.length){ el.innerHTML = '<div class="hint-text">Noch keine Diagramme angelegt.</div>'; return; }
  var h = "";
  for(var i = 0; i < cs.length; i++){
    var c = cs[i], names = [];
    for(var j = 0; j < c.series.length; j++) names.push('<span class="swatch" style="background:' + esc(seriesColor(c.series[j], j)) + ';margin-right:4px;"></span>' + esc(c.series[j].label || c.series[j].topic));
    var meta = [];
    if(c.unit) meta.push(esc(c.unit));
    if(c.range_h) meta.push(c.range_h + " h");
    if(c.stepped) meta.push("Stufen");
    h += '<div class="manage-row">' + orderBtns("charts", c.id, i, cs.length) +
      '<div class="manage-info">' + esc(c.name) + (meta.length ? ' <span class="type-badge">' + meta.join(" · ") + "</span>" : "") +
      '<div class="manage-sub">' + names.join(" &nbsp; ") + "</div></div>" +
      '<div class="manage-actions">' + groupSelect("charts", c) +
      '<button class="btn-mini" onclick="openChartModal(\'' + c.id + '\')">Bearbeiten</button>' +
      '<button class="btn-mini" onclick="duplicateItem(\'charts\',\'' + c.id + '\')">Kopieren</button>' +
      '<button class="btn-mini btn-delete" title="Entfernen" onclick="deleteItem(\'charts\',\'' + c.id + '\', ' + esc(JSON.stringify(c.name)) + ')">&times;</button></div></div>';
  }
  el.innerHTML = h;
}
function duplicateItem(coll, id){
  var it = findById(APP.cfg[coll], id);
  if(!it) return;
  var copy = JSON.parse(JSON.stringify(it));
  delete copy.id; delete copy.order;
  copy.name = (copy.name || "") + " (Kopie)";
  api("POST", "/api/" + coll, copy, function(err){
    if(err){ showToast(err, "err"); return; }
    showToast("Kopie angelegt."); reloadConfigAndLists();
  });
}
var chartEdit = { id: null, series: [] };
function openChartModal(id){
  var c = id ? findById(APP.cfg.charts, id) : null;
  chartEdit.id = c ? c.id : null;
  chartEdit.series = c ? JSON.parse(JSON.stringify(c.series)) : [{ topic: "", label: "", color: SERIES_COLORS[0] }];
  $("chartModalTitle").textContent = c ? "Diagramm bearbeiten" : "Diagramm hinzufügen";
  $("cm_name").value = c ? c.name : "";
  fillGroupSelect("cm_group", c ? c.group_id : null);
  $("cm_unit").value = c ? c.unit : "";
  $("cm_ymin").value = c && c.y_min !== null ? String(c.y_min).replace(".", ",") : "";
  $("cm_ymax").value = c && c.y_max !== null ? String(c.y_max).replace(".", ",") : "";
  $("cm_height").value = c ? c.height : "m";
  $("cm_stepped").value = c && c.stepped ? "1" : "0";
  $("cm_fill").checked = !!(c && c.fill);
  var rh = '<option value="">wie Ansicht (Umschalter oben)</option>';
  for(var i = 0; i < RANGE_CHOICES.length; i++) rh += '<option value="' + RANGE_CHOICES[i] + '"' + (c && c.range_h === RANGE_CHOICES[i] ? " selected" : "") + ">immer " + RANGE_CHOICES[i] + " Stunden</option>";
  $("cm_range").innerHTML = rh;
  $("cm_filter").value = "";
  hideError("chartModalError");
  renderSeriesRows();
  renderDiscover("cm");
  openModal("chartModal");
  loadTopics();
}
function renderSeriesRows(){
  var h = "";
  for(var i = 0; i < chartEdit.series.length; i++){
    var s = chartEdit.series[i];
    h += '<div class="series-row"><span class="sr-idx">' + (i + 1) + "</span>" +
      '<input class="sr-topic" list="topicDatalist" placeholder="Topic" value="' + esc(s.topic) + '">' +
      '<input class="sr-label" placeholder="Bezeichnung (optional)" value="' + esc(s.label) + '">' +
      '<input class="sr-color" type="color" value="' + esc(seriesColor(s, i)) + '">' +
      '<button class="btn-mini btn-delete" title="Topic entfernen" onclick="removeSeries(' + i + ')">&times;</button></div>';
  }
  $("cm_series").innerHTML = h || '<div class="hint-text">Noch kein Topic gewählt.</div>';
}
function syncSeries(){
  var rows = $("cm_series").getElementsByClassName("series-row"), out = [];
  for(var i = 0; i < rows.length; i++){
    out.push({
      topic: rows[i].getElementsByClassName("sr-topic")[0].value.replace(/^\s+|\s+$/g, ""),
      label: rows[i].getElementsByClassName("sr-label")[0].value.replace(/^\s+|\s+$/g, ""),
      color: rows[i].getElementsByClassName("sr-color")[0].value
    });
  }
  chartEdit.series = out;
}
function nextSeriesColor(){
  var used = {};
  for(var i = 0; i < chartEdit.series.length; i++) used[(chartEdit.series[i].color || "").toLowerCase()] = 1;
  for(i = 0; i < SERIES_COLORS.length; i++) if(!used[SERIES_COLORS[i]]) return SERIES_COLORS[i];
  return SERIES_COLORS[chartEdit.series.length % SERIES_COLORS.length];
}
function addSeries(topic){
  syncSeries();
  var last = chartEdit.series[chartEdit.series.length - 1];
  if(topic && last && !last.topic) last.topic = topic;
  else chartEdit.series.push({ topic: topic || "", label: "", color: nextSeriesColor() });
  renderSeriesRows();
}
function pickChartTopic(t){
  syncSeries();
  for(var i = 0; i < chartEdit.series.length; i++) if(chartEdit.series[i].topic === t){ showToast("Topic ist bereits im Diagramm.", "err"); return; }
  addSeries(t);
  if(!$("cm_name").value) $("cm_name").value = t;
}
function removeSeries(i){ syncSeries(); chartEdit.series.splice(i, 1); renderSeriesRows(); }
function submitChartModal(){
  syncSeries();
  var payload = {
    name: $("cm_name").value, group_id: $("cm_group").value || null, series: chartEdit.series,
    unit: $("cm_unit").value, y_min: $("cm_ymin").value, y_max: $("cm_ymax").value,
    height: $("cm_height").value, stepped: $("cm_stepped").value === "1", fill: $("cm_fill").checked,
    range_h: $("cm_range").value ? parseInt($("cm_range").value, 10) : null
  };
  var url = chartEdit.id ? "/api/charts/" + chartEdit.id : "/api/charts";
  api(chartEdit.id ? "PUT" : "POST", url, payload, function(err){
    if(err){ showError("chartModalError", err); $("chartModal").scrollTop = 0; return; }
    closeModal("chartModal");
    showToast(chartEdit.id ? "Diagramm gespeichert." : "Diagramm angelegt.");
    reloadConfigAndLists();
  });
}

/* ------------------------------------------------ Schalter/Anzeigen */
var WTYPE_LABEL = { "switch": "Schalter", "slider": "Slider", "display": "Anzeige" };
function renderWidgetsList(){
  var el = $("widgetsManageList"), ws = APP.cfg.widgets.slice().sort(byOrder);
  if(!ws.length){ el.innerHTML = '<div class="hint-text">Noch keine Schalter, Slider oder Anzeigen angelegt.</div>'; return; }
  var h = "";
  for(var i = 0; i < ws.length; i++){
    var w = ws[i], dev = DEVICES[w.device] || DEVICES.generic, sub = esc(w.topic);
    if(w.command_topic) sub += " → " + esc(w.command_topic);
    if(w.type === "switch") sub += " · AN=" + esc(w.payload_on) + " / AUS=" + esc(w.payload_off);
    if(w.type === "slider") sub += " · " + fmtNum(w.min) + " … " + fmtNum(w.max) + (w.unit ? " " + esc(w.unit) : "");
    h += '<div class="manage-row">' + orderBtns("widgets", w.id, i, ws.length) +
      '<span class="emoji-ico">' + dev.icon + "</span>" +
      '<div class="manage-info">' + esc(w.name) + ' <span class="type-badge">' + WTYPE_LABEL[w.type] + "</span>" +
      '<div class="manage-sub">' + sub + "</div></div>" +
      '<div class="manage-actions">' + groupSelect("widgets", w) +
      '<button class="btn-mini" onclick="openWidgetModal(\'' + w.id + '\')">Bearbeiten</button>' +
      '<button class="btn-mini" onclick="duplicateItem(\'widgets\',\'' + w.id + '\')">Kopieren</button>' +
      '<button class="btn-mini btn-delete" title="Entfernen" onclick="deleteItem(\'widgets\',\'' + w.id + '\', ' + esc(JSON.stringify(w.name)) + ')">&times;</button></div></div>';
  }
  el.innerHTML = h;
}
var widgetEditId = null;
function openWidgetModal(id){
  var w = id ? findById(APP.cfg.widgets, id) : null;
  widgetEditId = w ? w.id : null;
  $("widgetModalTitle").textContent = w ? "Element bearbeiten" : "Element hinzufügen";
  var dh = "";
  for(var i = 0; i < DEVICE_ORDER.length; i++){
    var k = DEVICE_ORDER[i];
    dh += '<option value="' + k + '"' + ((w ? w.device : "lamp") === k ? " selected" : "") + ">" + DEVICES[k].icon + " " + DEVICES[k].label + "</option>";
  }
  $("wm_device").innerHTML = dh;
  $("wm_type").value = w ? w.type : "switch";
  $("wm_name").value = w ? w.name : "";
  fillGroupSelect("wm_group", w ? w.group_id : null);
  $("wm_topic").value = w ? w.topic : "";
  $("wm_cmd").value = w && w.command_topic ? w.command_topic : "";
  $("wm_on").value = w && w.type === "switch" ? w.payload_on : "";
  $("wm_off").value = w && w.type === "switch" ? w.payload_off : "";
  $("wm_min").value = w && w.type === "slider" ? w.min : 0;
  $("wm_max").value = w && w.type === "slider" ? w.max : 100;
  $("wm_step").value = w && w.type === "slider" ? w.step : 1;
  $("wm_dec").value = w && w.type === "display" && w.decimals !== null && w.decimals !== undefined ? String(w.decimals) : "";
  $("wm_active").value = w && w.type === "display" ? (w.payload_on || "") : "";
  $("wm_unit").value = w ? (w.unit || "") : "";
  $("wm_retain").checked = !!(w && w.retain);
  $("wm_filter").value = "";
  hideError("widgetModalError");
  updateWidgetTypeFields();
  renderDiscover("wm");
  openModal("widgetModal");
  loadTopics();
}
function updateWidgetTypeFields(){
  var t = $("wm_type").value;
  $("wm_cmdFields").style.display = t === "display" ? "none" : "";
  $("wm_retainRow").style.display = t === "display" ? "none" : "";
  $("wm_switchFields").style.display = t === "switch" ? "" : "none";
  $("wm_sliderFields").style.display = t === "slider" ? "" : "none";
  $("wm_displayFields").style.display = t === "display" ? "" : "none";
}
function pickWidgetTopic(t){
  $("wm_topic").value = t;
  if(!$("wm_name").value){ var p = t.split("/"); $("wm_name").value = p[p.length - 1]; }
}
function submitWidgetModal(){
  var t = $("wm_type").value;
  var payload = {
    type: t, name: $("wm_name").value, topic: $("wm_topic").value, device: $("wm_device").value,
    group_id: $("wm_group").value || null, unit: $("wm_unit").value,
    command_topic: $("wm_cmd").value, retain: $("wm_retain").checked
  };
  if(t === "switch"){ payload.payload_on = $("wm_on").value; payload.payload_off = $("wm_off").value; }
  if(t === "slider"){ payload.min = $("wm_min").value; payload.max = $("wm_max").value; payload.step = $("wm_step").value; }
  if(t === "display"){ payload.decimals = $("wm_dec").value; payload.payload_on = $("wm_active").value; }
  api(widgetEditId ? "PUT" : "POST", widgetEditId ? "/api/widgets/" + widgetEditId : "/api/widgets", payload, function(err){
    if(err){ showError("widgetModalError", err); $("widgetModal").scrollTop = 0; return; }
    closeModal("widgetModal");
    showToast(widgetEditId ? "Element gespeichert." : "Element angelegt.");
    reloadConfigAndLists();
  });
}

/* --------------------------------------------------- Virtuelle Topics */
function renderVirtualList(){
  var el = $("virtualManageList"), vs = APP.cfg.virtual_topics.slice().sort(byOrder);
  if(!vs.length){ el.innerHTML = '<div class="hint-text">Noch keine virtuellen Topics angelegt.</div>'; return; }
  var h = "";
  for(var i = 0; i < vs.length; i++){
    var v = vs[i], src = [];
    for(var j = 0; j < v.sources.length; j++) src.push("abcdefgh".charAt(j) + " = " + esc(v.sources[j]));
    h += '<div class="manage-row"><div class="manage-info">' + esc(v.name) +
      ' <span class="type-badge" id="vtv_' + v.id + '">&ndash;</span>' +
      '<div class="manage-sub">' + src.join(", ") + " &nbsp;⇒&nbsp; " + esc(v.formula) + "</div>" +
      '<div class="manage-sub err" id="vte_' + v.id + '" style="display:none;"></div></div>' +
      '<div class="manage-actions"><button class="btn-mini" onclick="openVirtualModal(\'' + v.id + '\')">Bearbeiten</button>' +
      '<button class="btn-mini btn-delete" title="Entfernen" onclick="deleteItem(\'virtual_topics\',\'' + v.id + '\', ' + esc(JSON.stringify(v.name)) + ')">&times;</button></div></div>';
  }
  el.innerHTML = h;
  updateVirtualStatus();
}
function updateVirtualStatus(){
  if(!APP.vstatus || !APP.cfg) return;
  var vs = APP.cfg.virtual_topics;
  for(var i = 0; i < vs.length; i++){
    var st = APP.vstatus[vs[i].id], b = $("vtv_" + vs[i].id), e = $("vte_" + vs[i].id);
    if(!b || !st) continue;
    b.textContent = st.value !== null && st.value !== undefined ? st.value.replace(".", ",") + (vs[i].unit ? " " + vs[i].unit : "") : "noch kein Wert";
    e.style.display = st.error ? "" : "none";
    e.textContent = st.error || "";
  }
}
var virtualEdit = { id: null, sources: [] };
function openVirtualModal(id){
  var v = id ? findById(APP.cfg.virtual_topics, id) : null;
  virtualEdit.id = v ? v.id : null;
  virtualEdit.sources = v ? v.sources.slice() : [""];
  $("virtualModalTitle").textContent = v ? "Virtuelles Topic bearbeiten" : "Virtuelles Topic hinzufügen";
  $("vm_name").value = v ? v.name : "";
  $("vm_formula").value = v ? v.formula : "";
  $("vm_unit").value = v ? (v.unit || "") : "";
  $("vm_filter").value = "";
  $("vm_testResult").textContent = "";
  hideError("virtualModalError");
  renderVirtualSources();
  renderDiscover("vm");
  openModal("virtualModal");
  loadTopics();
}
function renderVirtualSources(){
  var h = "";
  for(var i = 0; i < virtualEdit.sources.length; i++){
    h += '<div class="series-row"><span class="vt-letter">' + "abcdefgh".charAt(i) + "</span>" +
      '<input class="sr-topic vm-src" list="topicDatalist" placeholder="Quell-Topic" value="' + esc(virtualEdit.sources[i]) + '">' +
      '<button class="btn-mini btn-delete" title="Entfernen" onclick="removeVirtualSource(' + i + ')">&times;</button></div>';
  }
  $("vm_sources").innerHTML = h;
}
function syncVirtualSources(){
  var els = $("vm_sources").getElementsByClassName("vm-src"), out = [];
  for(var i = 0; i < els.length; i++) out.push(els[i].value.replace(/^\s+|\s+$/g, ""));
  virtualEdit.sources = out;
}
function addVirtualSource(t){
  syncVirtualSources();
  if(virtualEdit.sources.length >= 8 && !(t && !virtualEdit.sources[virtualEdit.sources.length - 1])){ showToast("Maximal 8 Quell-Topics (a–h).", "err"); return; }
  var last = virtualEdit.sources.length - 1;
  if(t && last >= 0 && !virtualEdit.sources[last]) virtualEdit.sources[last] = t;
  else virtualEdit.sources.push(t || "");
  renderVirtualSources();
}
function pickVirtualTopic(t){ addVirtualSource(t); }
function removeVirtualSource(i){ syncVirtualSources(); virtualEdit.sources.splice(i, 1); renderVirtualSources(); }
function testVirtual(){
  syncVirtualSources();
  var out = $("vm_testResult");
  api("POST", "/api/virtual_topics/test", { sources: virtualEdit.sources, formula: $("vm_formula").value }, function(err, d){
    if(err){ out.className = "status-line err"; out.textContent = err; return; }
    var inp = [];
    for(var k in d.inputs) if(d.inputs.hasOwnProperty(k)) inp.push(k + "=" + fmtNum(d.inputs[k], null));
    out.className = "status-line ok";
    out.textContent = "Ergebnis mit aktuellen Werten (" + inp.join(", ") + "): " + fmtNum(d.value, null) + ($("vm_unit").value ? " " + $("vm_unit").value : "");
  });
}
function submitVirtualModal(){
  syncVirtualSources();
  var payload = { name: $("vm_name").value, sources: virtualEdit.sources, formula: $("vm_formula").value, unit: $("vm_unit").value };
  api(virtualEdit.id ? "PUT" : "POST", virtualEdit.id ? "/api/virtual_topics/" + virtualEdit.id : "/api/virtual_topics", payload, function(err){
    if(err){ showError("virtualModalError", err); $("virtualModal").scrollTop = 0; return; }
    closeModal("virtualModal");
    showToast(virtualEdit.id ? "Virtuelles Topic gespeichert." : "Virtuelles Topic angelegt.");
    reloadConfigAndLists();
    setTimeout(pollStatus, 500);
  });
}

/* ================================================================ Start */
(function init(){
  var resizeT = null;
  window.addEventListener("resize", function(){ clearTimeout(resizeT); resizeT = setTimeout(drawAllCharts, 150); });
  document.addEventListener("keydown", function(e){
    if(e.keyCode !== 27) return;
    var ms = ["chartModal", "widgetModal", "virtualModal", "groupModal"];
    for(var i = 0; i < ms.length; i++) closeModal(ms[i]);
  });
  APP.cols = getLayoutCols();
  loadConfig(function(){
    var stored = parseInt(lsGet("mqttDashRangeH", ""), 10);
    APP.rangeH = RANGE_CHOICES.indexOf(stored) >= 0 ? stored : (APP.cfg.default_range_h || 24);
    buildRangeSwitch();
    setLayoutCols(APP.cols);
    setConn(false, APP.cfg.broker);
    renderView();
    loadHistory();
    if(!APP.cfg.broker.host && !APP.cfg.charts.length && !APP.cfg.widgets.length) enterSettingsMode();
  });
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------
def _lan_ip() -> str:
    """Beste Schaetzung der eigenen LAN-IP (nur fuer die Startmeldung)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    cfg = config.snapshot()
    host = cfg["server"].get("host", "0.0.0.0")
    port = int(cfg["server"].get("port", 5001))
    virtual.rebuild(cfg)
    mqtt_manager.start(cfg["broker"])
    print("MQTT Dashboard v%s" % APP_VERSION)
    print("Konfiguration: %s" % CONFIG_PATH)
    print("Dashboard laeuft auf http://%s:%d  (im lokalen Netz: http://%s:%d)" % (host, port, _lan_ip(), port))
    try:
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except OSError as e:
        print("[Start] Port %d kann nicht geoeffnet werden: %s" % (port, e))
        print("[Start] Anderen Port in config.json unter server.port eintragen und neu starten.")
        if getattr(sys, "frozen", False):
            input("Enter zum Beenden ...")
        sys.exit(1)
    finally:
        mqtt_manager.stop()


if __name__ == "__main__":
    main()
