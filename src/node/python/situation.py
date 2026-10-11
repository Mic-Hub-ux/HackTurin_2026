"""Situation awareness: dalle detection del modello agli eventi di pericolo e ai dati.

Per ogni camera il motore:
  1. normalizza le detection (pixel -> 0..1), ricava i ciclisti (persona sopra una bici),
     prende il punto a terra di ogni oggetto e, se la camera e' calibrata, lo porta in metri;
  2. segue ogni oggetto nel tempo (tracker): traiettoria, velocita', direzione, zone;
  3. applica le regole (vedi RULES) e produce eventi "hazard" (formato C-ITS);
  4. quando un oggetto esce di scena produce il suo riepilogo "track": da che via e' entrato,
     da quale e' uscito, velocita', tempo passato in ogni zona, attraversamento delle strisce
     e direzione, eventi causati, traiettoria.

Il prototipo sta al centro dell'incrocio e non vede i semafori: le regole sul rosso si
accendono solo se lo stato del semaforo e' noto (traffic_light.mode != "none").
Ogni regola e' isolata: se una va in errore le altre continuano.
"""
import math
import statistics
import time
from collections import Counter, deque

from events import STATION_TYPE, EventFactory
from geometry import (Homography, angle_diff_deg, crosses_polyline, distance, main_axis,
                      point_in_polygon, polygon_centroid)
from safety import cpa, lateral
from tracker import Tracker
from traffic_light import TrafficLight

# Etichette del modello -> classi del nodo. Contiene anche i nomi COCO del modello di
# default di App Lab (YoloX) e di YOLO sul PC, cosi' il motore funziona con entrambi.
LABEL_MAP = {
    "person": "person", "adult": "person", "child": "person", "pedestrian": "person",
    "cyclist": "cyclist", "bicycle": "bicycle",
    "vehicle": "vehicle", "car": "vehicle", "truck": "vehicle", "bus": "vehicle",
    "motorcycle": "motorcycle", "motorbike": "motorcycle",
    "emergency": "emergency", "ambulance": "emergency",
}
VEICOLI = {"vehicle", "emergency"}
UTENTI_DEBOLI = {"person", "cyclist"}

RULES = [
    "emergency_vehicle", "speeding", "sudden_acceleration", "harsh_braking",
    "solid_line_crossing", "wrong_way", "u_turn", "forbidden_movement",
    "bike_lane_invasion", "bike_lane_hotspot", "illegal_stop", "congestion",
    "pedestrian_not_yielded", "near_miss", "collision_risk", "close_pass",
    "pedestrian_outside_crosswalk", "slow_pedestrian", "pedestrian_waiting", "crowd_on_crosswalk",
    "cyclist_on_sidewalk", "red_light_violation", "late_yellow", "pedestrian_red_crossing",
]

SOGLIE = {
    "min_confidence": 0.0,            # detection sotto questa confidenza ignorate (il brick filtra gia')
    "bike_lane_min_s": 0.6,           # secondi dentro la ciclabile prima di segnalare
    "bike_lane_exit_s": 0.6,          # secondi fuori per considerarla "uscita" (anti-sfarfallio)
    "bike_hotspot_count": 3,          # invasioni nella finestra -> incrocio a rischio
    "bike_hotspot_window_s": 600,
    "speed_limit_kmh": 50,            # con camera calibrata
    "speed_confirm_n": 2,             # misure consecutive sopra il limite prima di segnalare
    "relative_speed_factor": 2.5,     # senza calibrazione: N volte piu' veloce della velocita' tipica
    "accel_kmh": 15, "accel_window_s": 3.0, "accel_min_kmh": 20,
    "brake_kmh": 20, "brake_window_s": 2.5, "brake_min_kmh": 25,
    "min_track_s": 0.6,               # durata minima di una traccia prima di misurare la velocita'
    "stationary_img_speed": 0.02,     # sotto questa velocita' (frazione d'immagine al s) e' fermo
    "illegal_stop_s": 5.0,            # fermo in una zona di divieto di sosta per piu' di N s
    "cancel_hold_s": 2.0,             # per quanto la situazione deve essere finita prima del "cancel"
    "near_miss_m": 2.0,               # distanza veicolo-pedone/ciclista (camera calibrata)
    "near_miss_img": 0.05,            # stessa cosa senza calibrazione (frazione d'immagine)
    "near_miss_min_kmh": 10,
    "ttc_s": 2.0,                     # rischio collisione: incontro previsto entro N secondi...
    "collision_d_m": 0.8,             # ...a meno di N metri
    "collision_d_img": 0.02,
    "close_pass_lat_m": 1.5,          # sorpasso di un ciclista a meno di 1,5 m di lato
    "close_pass_lon_m": 3.0,
    "close_pass_min_diff_kmh": 5,
    "wrong_way_min_s": 1.0,           # secondi contromano prima di segnalare
    "wrong_way_min_img_speed": 0.03,
    "u_turn_deg": 150, "u_turn_window_s": 6.0, "u_turn_min_kmh": 5, "u_turn_min_scaled": 0.3,
    "u_turn_max_step_img": 0.12,      # spostamento tra due frame oltre il quale la traccia "salta"
    "congestion_min_vehicles": 3, "congestion_min_s": 10, "congestion_clear_s": 10,
    "jaywalk_min_s": 1.5,             # pedone in carreggiata fuori dalle strisce
    "slow_ped_mps": 0.8,              # pedone lento sulle strisce (m/s, camera calibrata)
    "slow_ped_scaled": 0.45,          # stessa cosa in "altezze del corpo al secondo"
    "slow_ped_min_s": 2.0,
    "ped_waiting_s": 20,              # pedone fermo nell'area d'attesa da piu' di N s
    "crowd_min": 6, "crowd_cooldown_s": 60,
    "sidewalk_cyclist_min_s": 2.0,
    "crosswalk_min_fraction": 0.3,    # quanto delle strisce va percorso per contare un attraversamento
    "red_grace_s": 0.5, "late_yellow_fraction": 0.5,
}


def _sopra(persona, mezzo):
    """Quanto la persona (box xyxy 0..1) sta in sella al mezzo: 0 = per niente."""
    px1, py1, px2, py2 = persona
    mx1, my1, mx2, my2 = mezzo
    area_p = (px2 - px1) * (py2 - py1)
    cx = (px1 + px2) / 2
    if area_p <= 0 or not (mx1 <= cx <= mx2) or py2 < my1 + 0.3 * (my2 - my1):
        return 0.0
    iw = max(0.0, min(px2, mx2) - max(px1, mx1))
    ih = max(0.0, min(py2, my2) - max(py1, my1))
    return iw * ih / area_p


def _stats(values):
    if not values:
        return None
    s = sorted(values)
    return {"avg": round(sum(s) / len(s), 2), "min": round(s[0], 2), "max": round(s[-1], 2),
            "p85": round(s[min(len(s) - 1, int(0.85 * len(s)))], 2)}


class SituationEngine:
    def __init__(self, cfg, on_event=print, camera_id=None):
        if "_cameras" not in cfg:  # config grezza (es. nei test): completa e valida
            from config import prepare
            cfg, avvisi = prepare(cfg)
            for a in avvisi:
                print("[config]", a)
        self.cfg = cfg
        self.node = cfg.get("node_id", "node")
        self.camera = camera_id or next(iter(cfg["_cameras"]))
        cam = cfg["_cameras"][self.camera]
        self.cam = cam
        self.frame_w, self.frame_h = cam.get("frame_size", [640, 480])
        self.label_map = {**LABEL_MAP, **cfg.get("label_map", {})}
        cal = cam.get("calibration") or {}
        self.homography = None
        if cal.get("image_points") and cal.get("world_points_m"):
            self.homography = Homography(cal["image_points"], cal["world_points_m"])
        self.space = "world" if self.homography else "image"
        self.tracker = Tracker(self.space, self.camera, cfg.get("track_max_age_s", 2.0))
        self.light = TrafficLight(cfg.get("traffic_light", {}))
        self.zones = cam.get("zones") or {}
        self.lines = cam.get("lines") or {}
        self.lanes = cam.get("lanes") or {}
        self.approaches = cam.get("approaches") or {}
        self.forbidden = {tuple(m) for m in cam.get("forbidden_movements") or []}
        self.crosswalk_names = cam.get("crosswalk_direction_names") or ["A->B", "B->A"]
        self.no_stop_zones = [z for z in cfg.get("no_stop_zones", []) if z in self.zones]
        self.neighbors = cfg.get("neighbors") or {}
        self.th = {**SOGLIE, **(cfg.get("thresholds") or {})}
        rules_cfg = cfg.get("rules") or {}
        self.enabled = {r: bool((rules_cfg.get(r) or {}).get("enabled", True)) for r in RULES}
        self.traj_max = (cfg.get("data") or {}).get("trajectory_max_points", 50)
        self.debug = cfg.get("debug", False)
        self.on_event = on_event
        self.factory = EventFactory(self.node, self.camera)
        self.flows = Counter()            # (classe, da, a) -> passaggi
        self.rule_errors = Counter()      # regola -> quante volte e' andata in errore
        self.frames = 0
        self._speeds = deque(maxlen=200)  # velocita' tipiche dei veicoli (confronto relativo)
        self._bike_invasions = deque()
        self._last_hotspot = -1e18
        self._last_crowd = -1e18
        self._congestion = {"since": None, "clear_since": None, "event": None}
        self._last_status = 0.0
        self._last_t = None
        self._max_raw = (0.0, 0.0)

    # ------------------------------------------------------------------ ingresso

    def update(self, detections, t=None):
        """detections: dict del brick {"label": [{"confidence", "bounding_box_xyxy"}, ...]}.

        Ritorna la lista dei messaggi prodotti (hazard e track), gia' passati a on_event.
        """
        t = time.time() if t is None else t
        if self._last_t is not None and t <= self._last_t:  # orologio che torna indietro
            t = self._last_t + 1e-3
        self._last_t = t
        self.frames += 1
        tracks, closed = self.tracker.update(self._observations(detections or {}), t)
        light, light_age = self.light.state(t)
        ctx = {"light": light, "light_age": light_age, "deboli": self.tracker_recent(t, UTENTI_DEBOLI)}
        out = []
        for tr in tracks:
            out += self._rules(tr, t, ctx)
        out += self._safe("congestion", self._congestion_rule, t)
        out += self._safe("crowd_on_crosswalk", self._crowd_rule, t)
        for tr in closed:
            out += self._close(tr, t)
        for msg in out:
            self.on_event(msg)
        if self.debug and t - self._last_status >= 5:
            self._last_status = t
            print(f"[stato {self.camera}] tracce={len(self.tracker.tracks)} semaforo={light} "
                  f"coordinate max viste={self._max_raw} spazio={self.space}")
        return out

    def _observations(self, detections):
        boxes = []
        if not isinstance(detections, dict):
            return []
        for raw_label, items in detections.items():
            label = self.label_map.get(str(raw_label).lower())
            if label is None or not isinstance(items, (list, tuple)):
                continue
            for it in items:
                try:
                    x1, y1, x2, y2 = (float(v) for v in it["bounding_box_xyxy"])
                    conf = float(it.get("confidence", 1.0))
                except (KeyError, TypeError, ValueError):
                    continue  # detection malformata: la salto
                if conf < self.th["min_confidence"] or not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
                    continue
                self._max_raw = (max(self._max_raw[0], x2), max(self._max_raw[1], y2))
                if max(x2, y2) > 1.5:  # pixel -> 0..1
                    x1, x2 = x1 / self.frame_w, x2 / self.frame_w
                    y1, y2 = y1 / self.frame_h, y2 / self.frame_h
                x1, x2 = sorted((min(max(x1, 0.0), 1.0), min(max(x2, 0.0), 1.0)))
                y1, y2 = sorted((min(max(y1, 0.0), 1.0), min(max(y2, 0.0), 1.0)))
                if x2 - x1 <= 0 or y2 - y1 <= 0:
                    continue
                boxes.append([label, (x1, y1, x2, y2), conf])

        # Modello COCO: persona in sella a una bici = ciclista, in sella a una moto = veicolo
        persone = [b for b in boxes if b[0] == "person"]
        for b in boxes:
            if b[0] not in ("bicycle", "motorcycle"):
                continue
            libere = [p for p in persone if p[0] == "person"]
            migliore = max(libere, key=lambda p: _sopra(p[1], b[1]), default=None)
            nuova = "cyclist" if b[0] == "bicycle" else "vehicle"
            if migliore is not None and _sopra(migliore[1], b[1]) >= 0.25:
                (px1, py1, px2, py2), (mx1, my1, mx2, my2) = migliore[1], b[1]
                migliore[0] = "_usata"
                b[0], b[1] = nuova, (min(px1, mx1), min(py1, my1), max(px2, mx2), max(py2, my2))
                b[2] = max(b[2], migliore[2])
            else:
                b[0] = "vehicle" if b[0] == "motorcycle" else "_bici_parcheggiata"

        obs = []
        for label, (x1, y1, x2, y2), conf in boxes:
            if label.startswith("_"):
                continue
            img = ((x1 + x2) / 2, y2)  # punto a terra: centro del lato inferiore
            world = self.homography.apply(img) if self.homography else None
            obs.append((label, img, world, y2 - y1, (x1, y1, x2, y2), conf))
        return obs

    # ------------------------------------------------------------------ aiuti

    def tracker_recent(self, t, classi=None):
        """Tracce confermate viste nell'ultimo mezzo secondo (opzionalmente di certe classi)."""
        return [o for o in self.tracker.active(t) if classi is None or o.label in classi]

    def zones_of(self, tr):
        return [z for z, poly in self.zones.items() if point_in_polygon(tr.img, poly)]

    def _in(self, tr, zona):
        poly = self.zones.get(zona)
        return bool(poly) and point_in_polygon(tr.img, poly)

    def _stationary(self, tr):
        if tr.age < 1.5:
            return False
        v = tr.speed("image", window=1.5)
        return v is not None and v < self.th["stationary_img_speed"]

    def _timer(self, st, chiave, attivo, t, durata):
        """True la prima volta che la condizione resta vera per 'durata' secondi."""
        if not attivo:
            st.pop(chiave, None)
            return False
        st.setdefault(chiave, t)
        if t - st[chiave] >= durata and not st.get(chiave + "_done"):
            st[chiave + "_done"] = True
            return True
        return False

    def _on(self, kind):
        return self.enabled.get(kind, True)

    def _safe(self, name, fn, *args):
        try:
            return fn(*args) or []
        except Exception as e:  # una regola sbagliata non deve fermare le altre
            self.rule_errors[name] += 1
            if self.rule_errors[name] <= 3:
                print(f"[errore regola {name}] {e!r}")
            return []

    def _event(self, kind, tr, t, severity, extra=None):
        ev = self.factory.hazard(kind, t, severity, self._actor(tr) if tr is not None else None, extra)
        if tr is not None:
            tr.state.setdefault("events", []).append(kind)
            if ev.get("actor", {}).get("next_node"):
                ev["next_nodes"] = [ev["actor"]["next_node"]]
        return ev

    def _actor(self, tr):
        a = {"track": tr.id, "class": tr.label, "station_type": STATION_TYPE.get(tr.label, 0),
             "confidence": round(tr.confidence, 2)}
        st = tr.state
        if st.get("from"):
            a["from"] = st["from"]
        if st.get("towards"):
            a["towards"] = st["towards"]
            if self.neighbors.get(st["towards"]):
                a["next_node"] = self.neighbors[st["towards"]]
        a["img"] = [round(tr.img[0], 4), round(tr.img[1], 4)]
        if self.space == "world":
            a["world"] = [round(tr.world[0], 2), round(tr.world[1], 2)]
            v = tr.velocity("world")
            if v is not None:
                a["speed_kmh"] = round(math.hypot(*v) * 3.6, 1)
                a["heading_deg"] = round(math.degrees(math.atan2(v[0], v[1])) % 360)
        else:
            s = tr.scaled_speed()
            if s is not None:
                a["speed_rel"] = round(s, 2)
        return a

    def _towards(self, tr):
        """Verso quale via (approach) si sta muovendo l'oggetto."""
        v = tr.velocity("image", window=1.0)
        if v is None or math.hypot(*v) < self.th["wrong_way_min_img_speed"] or not self.approaches:
            return None
        best, best_cos = None, 0.7
        for name, poly in self.approaches.items():
            if point_in_polygon(tr.img, poly):
                continue
            c = polygon_centroid(poly)
            dx, dy = c[0] - tr.img[0], c[1] - tr.img[1]
            n = math.hypot(dx, dy) * math.hypot(*v)
            if n > 0 and (v[0] * dx + v[1] * dy) / n > best_cos:
                best, best_cos = name, (v[0] * dx + v[1] * dy) / n
        return best

    # ------------------------------------------------------------------ regole per traccia

    def _rules(self, tr, t, ctx):
        st, ev = tr.state, []
        prev_t = tr.history[-2][0] if len(tr.history) >= 2 else t
        zones_now = self.zones_of(tr)
        dwell = st.setdefault("dwell", Counter())
        for z in zones_now:
            dwell[z] += min(t - prev_t, 1.0)
        traj = st.setdefault("traj", [])
        if not traj or t - traj[-1][0] >= 0.5:
            p = tr.point(self.space)
            traj.append((t, round(p[0], 4), round(p[1], 4)))
            if len(traj) > 400:
                del traj[1::2]  # dimezza tenendo il primo punto

        if not st.get("_init"):  # appena confermata: guarda anche i punti visti prima della conferma
            st["_init"] = True
            for h in list(tr.history)[:-1]:
                for name, poly in self.approaches.items():
                    if point_in_polygon(h[1], poly):
                        st.setdefault("from", name)
                        st["to"] = name
        for name, poly in self.approaches.items():  # da che via entra, verso quale va
            if point_in_polygon(tr.img, poly):
                st.setdefault("from", name)
                st["to"] = name
        towards = self._towards(tr)
        if towards:
            st["towards"] = towards

        ctx = {**ctx, "zones": zones_now, "fermo": self._stationary(tr)}
        if tr.label in VEICOLI:
            ctx["speed_kmh"] = self._speed_kmh(tr, t)
            if ctx["speed_kmh"] is not None:
                st["max_kmh"] = max(st.get("max_kmh", 0.0), ctx["speed_kmh"])

        if tr.label == "emergency":
            if self._on("emergency_vehicle") and not st.get("emergency_ev"):
                st["emergency_ev"] = self._event("emergency_vehicle", tr, t, 3)
                ev.append(st["emergency_ev"])
            return ev  # i mezzi di emergenza in servizio sono esenti dalle altre regole

        if tr.label == "vehicle":
            ev += self._safe("speed", self._speed_rules, tr, t, ctx)
            ev += self._safe("bike_lane_invasion", self._bike_lane, tr, t, ctx)
            ev += self._safe("illegal_stop", self._illegal_stop, tr, t, ctx)
            ev += self._safe("vru", self._vru_rules, tr, t, ctx)
            ev += self._safe("lines", self._line_rules, tr, t, ctx)
            ev += self._safe("wrong_way", self._wrong_way, tr, t)
            ev += self._safe("u_turn", self._u_turn, tr, t, ctx)
        elif tr.label == "person":
            ev += self._safe("person", self._person_rules, tr, t, ctx)
        elif tr.label == "cyclist":
            ev += self._safe("wrong_way", self._wrong_way, tr, t)
            if self._on("cyclist_on_sidewalk") and self._timer(
                    st, "sidewalk", "sidewalk" in zones_now, t, self.th["sidewalk_cyclist_min_s"]):
                ev.append(self._event("cyclist_on_sidewalk", tr, t, 2, {"where": {"zone": "sidewalk"}}))
        return ev

    def _speed_kmh(self, tr, t):
        if tr.age < self.th["min_track_s"] or self.space != "world":
            return None
        v = tr.speed("world")
        if v is None:
            return None
        kmh = v * 3.6
        tr.state.setdefault("speeds", deque(maxlen=120)).append((t, kmh))
        return kmh

    def _speed_rules(self, tr, t, ctx):
        st, ev = tr.state, []
        speed_kmh = ctx.get("speed_kmh")
        if self.space == "world":
            if speed_kmh is None:
                return ev
            st["all_kmh"] = st.get("all_kmh", []) + [speed_kmh]
            st["over_n"] = st.get("over_n", 0) + 1 if speed_kmh > self.th["speed_limit_kmh"] else 0
            if self._on("speeding") and st["over_n"] >= self.th["speed_confirm_n"] and not st.get("speed_done"):
                st["speed_done"] = True
                ev.append(self._event("speeding", tr, t, 3 if speed_kmh > 1.5 * self.th["speed_limit_kmh"] else 2,
                                      {"details": {"speed_kmh": round(speed_kmh, 1),
                                                   "limit_kmh": self.th["speed_limit_kmh"]}}))
            acc = [k for (ts, k) in st["speeds"] if t - ts <= self.th["accel_window_s"]]
            if (self._on("sudden_acceleration") and acc and not st.get("accel_done")
                    and speed_kmh >= self.th["accel_min_kmh"] and speed_kmh - min(acc) >= self.th["accel_kmh"]):
                st["accel_done"] = True
                ev.append(self._event("sudden_acceleration", tr, t, 2, {"details": {
                    "from_kmh": round(min(acc), 1), "to_kmh": round(speed_kmh, 1)}}))
            frena = [k for (ts, k) in st["speeds"] if t - ts <= self.th["brake_window_s"]]
            if (self._on("harsh_braking") and frena and not st.get("brake_done")
                    and max(frena) >= self.th["brake_min_kmh"] and max(frena) - speed_kmh >= self.th["brake_kmh"]):
                st["brake_done"] = True
                ev.append(self._event("harsh_braking", tr, t, 2, {"details": {
                    "from_kmh": round(max(frena), 1), "to_kmh": round(speed_kmh, 1)}}))
            return ev

        # Senza calibrazione: confronto con la velocita' tipica dei veicoli IN MOVIMENTO visti
        # qui, misurata in "altezze del riquadro al secondo" (indipendente dalla prospettiva).
        if tr.age < self.th["min_track_s"] or ctx.get("fermo"):
            return ev
        v = tr.scaled_speed()
        if v is None:
            return ev
        st["scaled"] = st.get("scaled", []) + [v]
        media = statistics.median(st["scaled"])
        if (self._on("speeding") and len(self._speeds) >= 8 and not st.get("speed_done")
                and len(st["scaled"]) >= 3):
            tipica = statistics.median(self._speeds)
            if tipica > 0 and media > self.th["relative_speed_factor"] * tipica:
                st["speed_done"] = True
                ev.append(self._event("speeding", tr, t, 2, {"details": {
                    "relative": True, "ratio": round(media / tipica, 1)}}))
        if not st.get("speed_counted") and len(st["scaled"]) >= 3:
            st["speed_counted"] = True  # ogni veicolo pesa una volta sola nella velocita' tipica
            self._speeds.append(media)
        return ev

    def _bike_lane(self, tr, t, ctx):
        poly = self.zones.get("bike_lane")
        if not poly or not self._on("bike_lane_invasion"):
            return []
        st = tr.state
        if "bike_lane" in ctx["zones"]:
            st.pop("bike_out_since", None)
            st.setdefault("bike_since", t)
            if not st.get("bike_reported") and t - st["bike_since"] >= self.th["bike_lane_min_s"]:
                st["bike_reported"] = True
                st["bike_count"] = st.get("bike_count", 0) + 1
                count = st["bike_count"]
                ciclisti = [o for o in ctx["deboli"] if o.label == "cyclist" and self._in(o, "bike_lane")]
                sev = 3 if count >= 2 or ciclisti else 2
                out = [self._event("bike_lane_invasion", tr, t, sev, {
                    "where": {"zone": "bike_lane"},
                    "details": {"count": count, "repeated": count >= 2, "cyclists_present": len(ciclisti)}})]
                return out + self._safe("bike_lane_hotspot", self._bike_hotspot, t)
        else:
            st.setdefault("bike_out_since", t)
            if t - st["bike_out_since"] >= self.th["bike_lane_exit_s"]:
                st.pop("bike_since", None)
                st["bike_reported"] = False
        return []

    def _bike_hotspot(self, t):
        w = self.th["bike_hotspot_window_s"]
        self._bike_invasions.append(t)
        while self._bike_invasions and t - self._bike_invasions[0] > w:
            self._bike_invasions.popleft()
        if (self._on("bike_lane_hotspot") and len(self._bike_invasions) >= self.th["bike_hotspot_count"]
                and t - self._last_hotspot > w):
            self._last_hotspot = t
            return [self._event("bike_lane_hotspot", None, t, 2, {
                "where": {"zone": "bike_lane"},
                "details": {"invasions": len(self._bike_invasions), "window_s": w},
            })]
        return []

    def _illegal_stop(self, tr, t, ctx):
        """Fermo in una zona di divieto di sosta: evento "new", poi "cancel" quando riparte."""
        if not self._on("illegal_stop"):
            return []
        st, ev = tr.state, []
        attivi = st.setdefault("stop_events", {})
        for zona in self.no_stop_zones:
            dentro = ctx["fermo"] and zona in ctx["zones"]
            if self._timer(st, f"stop_{zona}", dentro, t, self.th["illegal_stop_s"]):
                attivi[zona] = self._event("illegal_stop", tr, t, 2, {"where": {"zone": zona}})
                ev.append(attivi[zona])
            if zona in attivi:
                if dentro:
                    st.pop(f"stop_clear_{zona}", None)
                elif t - st.setdefault(f"stop_clear_{zona}", t) >= self.th["cancel_hold_s"]:
                    ev.append(self.factory.cancel(attivi.pop(zona), t, "ripartito"))
                    st.pop(f"stop_{zona}_done", None)
        return ev

    def _vru_rules(self, tr, t, ctx):
        """Regole veicolo <-> pedoni e ciclisti."""
        st, ev = tr.state, []
        deboli = ctx["deboli"]
        fermo, speed_kmh = ctx["fermo"], ctx.get("speed_kmh")

        # Mancata precedenza: il veicolo passa sulle strisce mentre c'e' un pedone sopra
        if (self._on("pedestrian_not_yielded") and not st.get("yield_done") and not fermo
                and "crosswalk" in ctx["zones"]):
            pedoni = [o for o in deboli if o.label == "person" and self._in(o, "crosswalk")]
            if pedoni:
                st["yield_done"] = True
                ev.append(self._event("pedestrian_not_yielded", tr, t, 3, {
                    "where": {"zone": "crosswalk"}, "details": {"pedestrians": len(pedoni)}}))

        if self.space == "world":
            in_moto = speed_kmh is not None and speed_kmh >= self.th["near_miss_min_kmh"]
            soglia, soglia_cpa, unita = self.th["near_miss_m"], self.th["collision_d_m"], "m"
        else:
            in_moto = not fermo and tr.speed("image") is not None
            soglia, soglia_cpa, unita = self.th["near_miss_img"], self.th["collision_d_img"], "img"
        if not in_moto:
            return ev
        p_v, v_v = tr.point(self.space), tr.velocity(self.space, window=1.0)
        gia_vicini = st.setdefault("near_ids", set())
        gia_rischio = st.setdefault("risk_ids", set())
        gia_sorpasso = st.setdefault("pass_ids", set())
        for o in deboli:
            p_o = o.point(self.space)
            d = distance(p_v, p_o)
            v_o = o.velocity(self.space, window=1.0) or (0.0, 0.0)
            # Mancato investimento: veicolo in movimento troppo vicino a un pedone o ciclista
            vicino = d < soglia
            if vicino and self._on("near_miss") and o.id not in gia_vicini:
                gia_vicini.add(o.id)
                ev.append(self._event("near_miss", tr, t, 3, {"details": {
                    "other": o.id, "other_class": o.label, "distance": round(d, 2), "unit": unita}}))
            # Rischio di collisione previsto (solo se non sono gia' vicini: li' vale il near_miss)
            if (not vicino and self._on("collision_risk") and v_v is not None
                    and o.id not in gia_rischio):
                t_cpa, d_cpa = cpa(p_v, v_v, p_o, v_o)
                if 0 < t_cpa <= self.th["ttc_s"] and d_cpa <= soglia_cpa:
                    gia_rischio.add(o.id)
                    ev.append(self._event("collision_risk", tr, t, 3, {"details": {
                        "other": o.id, "other_class": o.label, "ttc_s": round(t_cpa, 2),
                        "min_distance": round(d_cpa, 2), "unit": unita}}))
            # Sorpasso di un ciclista a meno di 1,5 m (serve la calibrazione: misura in metri)
            if (self._on("close_pass") and self.space == "world" and o.label == "cyclist"
                    and v_v is not None and o.id not in gia_sorpasso):
                n_v, n_o = math.hypot(*v_v), math.hypot(*v_o)
                if n_v > 0 and n_o > 0.5 and (v_v[0] * v_o[0] + v_v[1] * v_o[1]) / (n_v * n_o) > 0.8 \
                        and (n_v - n_o) * 3.6 >= self.th["close_pass_min_diff_kmh"]:
                    lon, lat = lateral(p_v, v_v, p_o)
                    if abs(lon) <= self.th["close_pass_lon_m"] and abs(lat) <= self.th["close_pass_lat_m"]:
                        gia_sorpasso.add(o.id)
                        ev.append(self._event("close_pass", tr, t, 3, {"details": {
                            "other": o.id, "lateral_m": round(abs(lat), 2),
                            "min_m": self.th["close_pass_lat_m"]}}))
        return ev

    def _line_rules(self, tr, t, ctx):
        st, ev = tr.state, []
        prev = tr.previous()
        if prev is None:
            return ev
        light, light_age = ctx["light"], ctx["light_age"]
        for name, line in self.lines.items():
            if st.get(f"line_{name}") or not crosses_polyline(prev[1], tr.img, line["points"]):
                continue
            st[f"line_{name}"] = True
            if line.get("type") == "solid" and self._on("solid_line_crossing"):
                ev.append(self._event("solid_line_crossing", tr, t, 2, {"where": {"line": name}}))
            elif line.get("type") == "stop":
                if light == "red" and light_age >= self.th["red_grace_s"] and self._on("red_light_violation"):
                    ev.append(self._event("red_light_violation", tr, t, 3, {
                        "where": {"line": name}, "details": {"red_since_s": round(light_age, 1)}}))
                elif (light == "yellow" and self._on("late_yellow")
                      and light_age >= self.th["late_yellow_fraction"] * self.light.yellow_s):
                    ev.append(self._event("late_yellow", tr, t, 1, {
                        "where": {"line": name}, "details": {"yellow_since_s": round(light_age, 1)}}))
        return ev

    def _wrong_way(self, tr, t):
        if not self._on("wrong_way"):
            return []
        st = tr.state
        v = tr.velocity("image", window=1.0)
        contromano, corsia = False, None
        if v is not None and math.hypot(*v) >= self.th["wrong_way_min_img_speed"]:
            for name, lane in self.lanes.items():
                if not point_in_polygon(tr.img, lane["polygon"]):
                    continue
                d = lane["direction"]
                dx, dy = d[1][0] - d[0][0], d[1][1] - d[0][1]
                coseno = (v[0] * dx + v[1] * dy) / (math.hypot(*v) * math.hypot(dx, dy) or 1)
                if coseno < -0.5:
                    contromano, corsia = True, name
        if self._timer(st, "wrong_way", contromano, t, self.th["wrong_way_min_s"]):
            return [self._event("wrong_way", tr, t, 3, {"where": {"lane": corsia}})]
        return []

    def _u_turn(self, tr, t, ctx):
        """Inversione a U: la direzione di marcia si ribalta (>150 gradi) in pochi secondi.

        Per non confonderla con uno scambio di tracce tra due auto che si incrociano in versi
        opposti servono anche: una direzione stabile prima e dopo (almeno 2 misure ciascuna) e un
        rallentamento in mezzo (per girarsi un'auto deve frenare: velocita' minima <= meta' di prima).
        """
        if not self._on("u_turn"):
            return []
        st = tr.state
        if self.space == "world":
            v = ctx.get("speed_kmh")
            soglia = self.th["u_turn_min_kmh"]
        else:
            v = tr.scaled_speed()
            soglia = self.th["u_turn_min_scaled"]
        if v is None:
            return []
        h = tr.heading(self.space, window=1.0) if v >= soglia else None
        storia = st.setdefault("headings", deque(maxlen=80))
        prev = tr.previous()
        if prev is not None and distance(prev[1], tr.img) > self.th["u_turn_max_step_img"]:
            storia.clear()  # salto improvviso (taglio di scena, tracce scambiate): non e' una manovra
        while storia and t - storia[0][0] > self.th["u_turn_window_s"]:
            storia.popleft()
        storia.append((t, h, v))
        if st.get("u_turn_done") or h is None:
            return []
        dopo = [x for x in storia if x[1] is not None and angle_diff_deg(x[1], h) < 45][-2:]
        prima = [x for x in storia if x[1] is not None and angle_diff_deg(x[1], h) >= self.th["u_turn_deg"]]
        if len(dopo) < 2 or len(prima) < 2 or prima[-1][0] >= dopo[0][0]:
            return []
        v_prima = statistics.median(x[2] for x in prima)
        in_mezzo = [x[2] for x in storia if prima[-1][0] <= x[0] <= dopo[0][0]]
        if not in_mezzo or min(in_mezzo) > 0.5 * v_prima:
            return []
        st["u_turn_done"] = True
        return [self._event("u_turn", tr, t, 3, {"details": {"speed_before": round(v_prima, 1),
                                                             "min_speed": round(min(in_mezzo), 1)}})]

    def _person_rules(self, tr, t, ctx):
        st, ev = tr.state, []
        zones = ctx["zones"]
        sulle_strisce = "crosswalk" in zones

        fuori = "roadway" in zones and not sulle_strisce
        if self._on("pedestrian_outside_crosswalk") and self._timer(st, "jaywalk", fuori, t, self.th["jaywalk_min_s"]):
            ev.append(self._event("pedestrian_outside_crosswalk", tr, t, 2, {"where": {"zone": "roadway"}}))

        if self._on("slow_pedestrian") and sulle_strisce:
            if self.space == "world":
                v = tr.speed("world")
                lento = v is not None and v < self.th["slow_ped_mps"]
                misura = {"speed_mps": round(v, 2)} if v is not None else {}
            else:
                v = tr.scaled_speed()
                lento = v is not None and v < self.th["slow_ped_scaled"]
                misura = {"speed_rel": round(v, 2)} if v is not None else {}
        else:
            lento, misura = False, {}
        if self._timer(st, "slow_ped", lento, t, self.th["slow_ped_min_s"]):
            ev.append(self._event("slow_pedestrian", tr, t, 2, {"where": {"zone": "crosswalk"}, "details": misura}))

        in_attesa = ctx["fermo"] and "waiting_area" in zones
        if self._on("pedestrian_waiting") and self._timer(st, "waiting", in_attesa, t, self.th["ped_waiting_s"]):
            st["waiting_ev"] = self._event("pedestrian_waiting", tr, t, 1, {
                "where": {"zone": "waiting_area"}, "details": {"waited_s": self.th["ped_waiting_s"]}})
            ev.append(st["waiting_ev"])
        elif st.get("waiting_ev") and not in_attesa:
            ev.append(self.factory.cancel(st.pop("waiting_ev"), t, "non piu' in attesa"))

        if (self._on("pedestrian_red_crossing") and ctx["light"] == "green" and sulle_strisce
                and not st.get("was_on_crosswalk") and not st.get("red_cross_done") and len(tr.history) >= 2):
            st["red_cross_done"] = True
            ev.append(self._event("pedestrian_red_crossing", tr, t, 2, {"where": {"zone": "crosswalk"}}))
        st["was_on_crosswalk"] = sulle_strisce
        return ev

    # ------------------------------------------------------------------ regole di scena

    def _congestion_rule(self, t):
        if not self._on("congestion"):
            return []
        c = self._congestion
        fermi = [tr for tr in self.tracker_recent(t, {"vehicle"})
                 if self._stationary(tr) and (not self.zones.get("roadway") or self._in(tr, "roadway"))]
        coda = len(fermi) >= self.th["congestion_min_vehicles"]
        if coda:
            c["clear_since"] = None
            c["since"] = c["since"] or t
            if c["event"] is None and t - c["since"] >= self.th["congestion_min_s"]:
                c["event"] = self._event("congestion", None, t, 1, {"details": {"stopped_vehicles": len(fermi)}})
                return [c["event"]]
        else:
            c["since"] = None
            if c["event"] is not None:
                c["clear_since"] = c["clear_since"] or t
                if t - c["clear_since"] >= self.th["congestion_clear_s"]:
                    ev, c["event"], c["clear_since"] = c["event"], None, None
                    return [self.factory.cancel(ev, t, "traffico scorrevole")]
        return []

    def _crowd_rule(self, t):
        if not self._on("crowd_on_crosswalk") or "crosswalk" not in self.zones:
            return []
        pedoni = [o for o in self.tracker_recent(t, {"person"}) if self._in(o, "crosswalk")]
        if len(pedoni) >= self.th["crowd_min"] and t - self._last_crowd >= self.th["crowd_cooldown_s"]:
            self._last_crowd = t
            return [self._event("crowd_on_crosswalk", None, t, 1, {
                "where": {"zone": "crosswalk"}, "details": {"pedestrians": len(pedoni)}})]
        return []

    # ------------------------------------------------------------------ fine traccia

    def _crosswalk_crossing(self, tr):
        """Il pedone/ciclista ha attraversato le strisce? In che direzione?"""
        poly = self.zones.get("crosswalk")
        if not poly or tr.label not in UTENTI_DEBOLI:
            return None
        dentro = [h for h in tr.history if point_in_polygon(h[1], poly)]
        if len(dentro) < 2:
            return None
        ax = main_axis(poly)
        proiezioni = [p[0] * ax[0] + p[1] * ax[1] for p in poly]
        lunghezza = max(proiezioni) - min(proiezioni)
        a, b = dentro[0][1], dentro[-1][1]
        spost = (b[0] - a[0]) * ax[0] + (b[1] - a[1]) * ax[1]
        if lunghezza <= 0 or abs(spost) < self.th["crosswalk_min_fraction"] * lunghezza:
            return None
        return {"direction": self.crosswalk_names[0] if spost > 0 else self.crosswalk_names[1],
                "duration_s": round(dentro[-1][0] - dentro[0][0], 1)}

    def _close(self, tr, t):
        """Traccia sparita: riepilogo completo + eventi che si chiudono con lei."""
        st, out = tr.state, []
        da, a = st.get("from"), st.get("to")
        self.flows[(tr.label, da, a)] += 1

        if (self._on("forbidden_movement") and tr.label == "vehicle" and da and a and da != a
                and (da, a) in self.forbidden):
            out += [self._event("forbidden_movement", tr, t, 2, {"details": {"from": da, "to": a}})]
        if st.get("emergency_ev"):
            out.append(self.factory.cancel(st["emergency_ev"], t, "uscito dalla scena"))
        for ev in (st.get("stop_events") or {}).values():
            out.append(self.factory.cancel(ev, t, "uscito dalla scena"))
        if st.get("waiting_ev"):
            out.append(self.factory.cancel(st["waiting_ev"], t, "uscito dalla scena"))

        traj = st.get("traj", [])
        if len(traj) > self.traj_max:
            passo = len(traj) / self.traj_max
            traj = [traj[int(i * passo)] for i in range(self.traj_max - 1)] + [traj[-1]]
        summary = {
            "v": 1, "type": "track", "kind": "transit", "id": tr.id, "node": self.node, "camera": self.camera,
            "ts": int(t * 1000), "ts_start": int(tr.first_seen * 1000), "ts_end": int(tr.last_seen * 1000),
            "duration_s": round(tr.age, 1), "class": tr.label, "labels": dict(tr.labels),
            "confidence": round(tr.confidence, 2), "hits": tr.hits,
            "from": da, "to": a, "towards": st.get("towards"), "space": self.space,
            "path_length": round(tr.path_length(self.space), 3), "unit": "m" if self.space == "world" else "img",
            "zones": {z: round(s, 1) for z, s in (st.get("dwell") or {}).items()},
            "events": st.get("events", []),
            "trajectory": [[round(p[0] - tr.first_seen, 1), p[1], p[2]] for p in traj],
        }
        if tr.label in VEICOLI:
            if self.space == "world" and st.get("all_kmh"):
                summary["speed_kmh"] = _stats(st["all_kmh"])
            elif st.get("scaled"):
                summary["speed_rel"] = _stats(st["scaled"])
        elif tr.label in UTENTI_DEBOLI and self.space == "world" and tr.age > 0:
            summary["avg_speed_mps"] = round(tr.path_length("world") / tr.age, 2)
        crossing = self._crosswalk_crossing(tr)
        if crossing:
            summary["crosswalk_crossing"] = crossing
        if st.get("max_kmh"):
            summary["max_speed_kmh"] = round(st["max_kmh"], 1)
        out.append(summary)
        return out

    # ------------------------------------------------------------------ dati per la scena

    def scene(self, t):
        """Fotografia degli oggetti attivi (stile CPM, Collective Perception)."""
        objs = []
        for tr in self.tracker.active(t):
            o = {"id": tr.id, "class": tr.label, "camera": self.camera, "img": [round(tr.img[0], 4), round(tr.img[1], 4)],
                 "zones": self.zones_of(tr), "conf": round(tr.confidence, 2), "age_s": round(tr.age, 1)}
            if self.space == "world":
                o["world"] = [round(tr.world[0], 2), round(tr.world[1], 2)]
                v = tr.speed("world")
                if v is not None:
                    o["speed_kmh"] = round(v * 3.6, 1)
                h = tr.heading("world")
                if h is not None:
                    o["heading_deg"] = round(h)
            else:
                s = tr.scaled_speed()
                if s is not None:
                    o["speed_rel"] = round(s, 2)
            if tr.state.get("towards"):
                o["towards"] = tr.state["towards"]
            objs.append(o)
        return objs
