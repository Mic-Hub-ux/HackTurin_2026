"""Caricamento e validazione della configurazione del nodo (config.json).

- Riempie i valori mancanti con i default (DEFAULTS).
- Accetta sia il formato "una camera" (zone & co. al primo livello) sia il formato
  "piu' camere" ("cameras": {"cam0": {...}, "cam1": {...}}).
- Non blocca mai l'avvio per un errore di configurazione: restituisce una lista di
  avvisi leggibili e disattiva solo la parte sbagliata.
"""
import copy
import json
from pathlib import Path

CAMERA_KEYS = ("frame_size", "calibration", "zones", "lines", "lanes", "approaches",
               "crosswalk_direction_names", "forbidden_movements")

# Zone con un significato per le regole (tutte facoltative)
KNOWN_ZONES = ("roadway", "bike_lane", "crosswalk", "sidewalk", "box", "waiting_area")

DEFAULTS = {
    "node_id": "incrocio-1",
    "confidence": 0.45,
    "debug": False,
    "frame_size": [640, 480],
    "track_max_age_s": 2.0,
    "traffic_light": {"mode": "none"},
    "thresholds": {},
    "rules": {},                      # {"speeding": {"enabled": false}, ...}
    "no_stop_zones": ["crosswalk", "bike_lane", "box"],
    "forbidden_movements": [],        # [["via", "incrocio"], ...] da -> a vietati
    "neighbors": {},                  # {"via": "incrocio-2"}: dove porta ogni via
    "crosswalk_direction_names": ["A->B", "B->A"],
    "data": {
        "store_db": True,
        "db_file": "smartcrossing.db",
        "retention_days": 7,
        "stats_interval_s": 60,
        "status_interval_s": 10,
        "scene_hz": 1.0,
        "store_scene": False,
        "trajectory_max_points": 50,
        "heatmap_cell_m": 0.5,
        "heatmap_cell_img": 0.02,
    },
    "publish": {
        "url": "",
        "token": "",
        "timeout_s": 3,
        "batch_size": 1,
        "log_file": "events.jsonl",
        "send": {"hazard": True, "track": True, "stats": True, "status": True,
                 "movement": True, "scene": False},
        "mqtt": {"host": "", "port": 1883, "username": "", "password": "",
                 "topic_prefix": "sc/v1", "qos": 1},
    },
    "api": {"enabled": False, "host": "0.0.0.0", "port": 8100},
    "fusion": {"center_approach": "incrocio", "max_gap_s": 4.0},
    # Il computer-server: riceve tutti i dati e inoltra i pericoli agli incroci vicini.
    # Se publish.url e' vuoto, gli eventi vanno a <server.url>/api/events.
    "server": {"url": "", "token": "", "poll_s": 1.0, "timeout_s": 3},
    # I 4 LED collegati ai pin dell'MCU (vedi node/sketch/sketch.ino). Ruoli possibili:
    # local = pericolo visto qui, remote = pericolo arrivato dal server (incrocio vicino),
    # emergency = mezzo di emergenza, status = acceso se il server risponde / lampeggia se offline,
    # any = qualsiasi pericolo (locale o remoto)
    "leds": {"enabled": True, "roles": ["local", "remote", "emergency", "status"],
             "hold_s": 10, "min_severity": 2, "refresh_s": 1.0, "ttl_ms": 3000},
    # Peer-to-peer tra incroci e verso i wearable, SENZA server (vedi peer.py e firmware/):
    # UDP multicast sulla rete locale (basta un hotspot, niente internet) e/o ESP-NOW via un
    # ESP32 "radio" collegato in USB (serial.port, es. /dev/ttyACM0): funziona senza nessuna rete.
    "p2p": {"enabled": False, "group": "239.255.42.99", "port": 47999, "mcast_ttl": 1,
            "peers": [], "key": "", "hops": 2, "heartbeat_s": 5, "bind_port": None,
            "local_alerts": True, "serial": {"port": "", "baud": 115200}},
}


def _merge(base, extra):
    """Unione ricorsiva: i valori di extra vincono, i dict vengono uniti."""
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _is_point(p):
    return (isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p))


def _check_points(pts, min_n, where, warnings, normalized=True):
    if not isinstance(pts, (list, tuple)) or len(pts) < min_n or not all(_is_point(p) for p in pts):
        warnings.append(f"{where}: servono almeno {min_n} punti [x, y] -> ignorato")
        return False
    if normalized and any(not (-0.05 <= c <= 1.05) for p in pts for c in p):
        warnings.append(f"{where}: coordinate fuori da 0..1 (sono in pixel?) -> ignorato")
        return False
    return True


def camera_configs(cfg):
    """{camera_id: configurazione della camera} anche per il formato a una camera."""
    if isinstance(cfg.get("cameras"), dict) and cfg["cameras"]:
        out = {}
        for cam_id, cam in cfg["cameras"].items():
            base = {k: copy.deepcopy(cfg[k]) for k in CAMERA_KEYS if k in cfg}
            out[str(cam_id)] = _merge(base, cam or {})
        return out
    cam_id = cfg.get("camera_id", "cam0")
    return {cam_id: {k: copy.deepcopy(cfg[k]) for k in CAMERA_KEYS if k in cfg}}


def validate_camera(cam, cam_id):
    """Pulisce la configurazione di una camera. Ritorna (camera pulita, avvisi)."""
    w = []
    cam = copy.deepcopy(cam)
    fs = cam.get("frame_size", [640, 480])
    if not (isinstance(fs, (list, tuple)) and len(fs) == 2 and all(isinstance(v, (int, float)) and v > 0 for v in fs)):
        w.append(f"[{cam_id}] frame_size non valido -> uso [640, 480]")
        cam["frame_size"] = [640, 480]

    cal = cam.get("calibration") or {}
    if cal.get("image_points") or cal.get("world_points_m"):
        ok = (_check_points(cal.get("image_points"), 4, f"[{cam_id}] calibration.image_points", w)
              and _check_points(cal.get("world_points_m"), 4, f"[{cam_id}] calibration.world_points_m", w,
                                normalized=False)
              and len(cal["image_points"]) == 4 and len(cal["world_points_m"]) == 4)
        if ok:
            try:
                from geometry import Homography
                Homography(cal["image_points"], cal["world_points_m"])
            except Exception as e:  # punti allineati o ripetuti
                w.append(f"[{cam_id}] calibrazione non utilizzabile ({e}) -> velocita' solo relative")
                ok = False
        if not ok:
            cam["calibration"] = {"image_points": None, "world_points_m": None}

    zones = {}
    for name, poly in (cam.get("zones") or {}).items():
        if poly is None:
            continue
        if _check_points(poly, 3, f"[{cam_id}] zones.{name}", w):
            zones[name] = [list(p) for p in poly]
        if name not in KNOWN_ZONES:
            w.append(f"[{cam_id}] zona '{name}' non usata da nessuna regola (nomi utili: {', '.join(KNOWN_ZONES)})")
    cam["zones"] = zones

    lines = {}
    for name, line in (cam.get("lines") or {}).items():
        if not isinstance(line, dict):
            w.append(f"[{cam_id}] lines.{name}: deve essere {{'type': ..., 'points': [...]}} -> ignorata")
            continue
        if line.get("type") not in ("solid", "stop"):
            w.append(f"[{cam_id}] lines.{name}: type deve essere 'solid' o 'stop' -> ignorata")
            continue
        if _check_points(line.get("points"), 2, f"[{cam_id}] lines.{name}.points", w):
            lines[name] = {"type": line["type"], "points": [list(p) for p in line["points"]]}
    cam["lines"] = lines

    lanes = {}
    for name, lane in (cam.get("lanes") or {}).items():
        if not isinstance(lane, dict):
            continue
        if not _check_points(lane.get("polygon"), 3, f"[{cam_id}] lanes.{name}.polygon", w):
            continue
        if not _check_points(lane.get("direction"), 2, f"[{cam_id}] lanes.{name}.direction", w):
            continue
        d = lane["direction"]
        if d[0] == d[1]:
            w.append(f"[{cam_id}] lanes.{name}.direction: i due punti coincidono -> ignorata")
            continue
        lanes[name] = {"polygon": [list(p) for p in lane["polygon"]], "direction": [list(p) for p in d[:2]]}
    cam["lanes"] = lanes

    approaches = {}
    for name, poly in (cam.get("approaches") or {}).items():
        if poly is not None and _check_points(poly, 3, f"[{cam_id}] approaches.{name}", w):
            approaches[name] = [list(p) for p in poly]
    cam["approaches"] = approaches

    fm = []
    for m in cam.get("forbidden_movements") or []:
        if isinstance(m, (list, tuple)) and len(m) == 2 and all(isinstance(x, str) for x in m):
            fm.append([m[0], m[1]])
            for x in m:
                if x not in approaches:
                    w.append(f"[{cam_id}] forbidden_movements: la via '{x}' non e' tra gli approaches")
        else:
            w.append(f"[{cam_id}] forbidden_movements: {m!r} deve essere [\"da\", \"a\"] -> ignorato")
    cam["forbidden_movements"] = fm

    names = cam.get("crosswalk_direction_names")
    if not (isinstance(names, (list, tuple)) and len(names) == 2 and all(isinstance(n, str) for n in names)):
        cam["crosswalk_direction_names"] = ["A->B", "B->A"]
    return cam, w


def prepare(raw):
    """Config grezza (dict) -> (config completa con default, lista di avvisi)."""
    w = []
    if not isinstance(raw, dict):
        w.append("config.json non contiene un oggetto JSON -> uso solo i default")
        raw = {}
    cfg = _merge(DEFAULTS, raw)

    from situation import RULES, SOGLIE  # import qui per evitare cicli
    for k in cfg.get("thresholds") or {}:
        if k not in SOGLIE:
            w.append(f"thresholds.{k}: soglia sconosciuta (ignorata). Quelle valide sono in situation.SOGLIE")
    for k, v in (cfg.get("rules") or {}).items():
        if k not in RULES:
            w.append(f"rules.{k}: regola sconosciuta. Regole: {', '.join(RULES)}")
        elif not isinstance(v, dict):
            w.append(f"rules.{k}: deve essere {{\"enabled\": true/false}}")
    mode = (cfg.get("traffic_light") or {}).get("mode", "none")
    if mode not in ("none", "simulated", "external"):
        w.append(f"traffic_light.mode '{mode}' non valido -> 'none'")
        cfg["traffic_light"]["mode"] = "none"

    cams = {}
    for cam_id, cam in camera_configs(cfg).items():
        cams[cam_id], cw = validate_camera(cam, cam_id)
        w += cw
    cfg["_cameras"] = cams
    return cfg, w


def load_config(path):
    """Legge config.json (accetta anche il BOM di Notepad). Ritorna (cfg, avvisi)."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return prepare({})[0], [f"{p} non trovato -> uso solo i default"]
    except json.JSONDecodeError as e:
        cfg, w = prepare({})
        return cfg, [f"{p}: JSON non valido (riga {e.lineno}, colonna {e.colno}: {e.msg}) -> uso solo i default"] + w
    return prepare(raw)


def public_config(cfg):
    """Config senza segreti (per l'API locale)."""
    c = copy.deepcopy({k: v for k, v in cfg.items() if not k.startswith("_")})
    pub = c.get("publish") or {}
    if pub.get("token"):
        pub["token"] = "***"
    mq = pub.get("mqtt") or {}
    if mq.get("password"):
        mq["password"] = "***"
    for sezione, chiave in (("server", "token"), ("p2p", "key")):
        if (c.get(sezione) or {}).get(chiave):
            c[sezione][chiave] = "***"
    return c
