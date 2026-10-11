"""Statistiche aggregate: cosa e' successo all'incrocio in ogni periodo.

Ogni data.stats_interval_s secondi produce un messaggio "stats" che descrive SOLO quel
periodo (from_ts -> to_ts): dopo l'invio i contatori ripartono da zero. Per ogni camera:
- conteggi per classe, movimenti (da quale via a quale), attraversamenti delle strisce;
- velocita' per classe: media, mediana (p50) e V85 (85esimo percentile, l'indicatore
  standard dell'ingegneria del traffico: la velocita' non superata dall'85% dei mezzi);
- durate delle tracce, occupazione delle zone (oggetti medi e massimi per frame, frazione
  di frame occupati, permanenza media);
- heatmap delle posizioni a terra: ogni traccia conta al massimo una volta per cella,
  cosi' un'auto ferma al semaforo non "brucia" la sua cella.
Memoria limitata: velocita' campionate a serbatoio, tetto alle celle e alle chiavi.
"""
import math
import random
import threading
from collections import Counter

CLASSI = ("vehicle", "person", "cyclist", "emergency")
MAX_SPEED_SAMPLES = 2000  # per classe e periodo; oltre si tiene un campione casuale uniforme
MAX_CELLS = 5000          # celle della heatmap per camera e periodo
MAX_SEEN = 50000          # coppie (traccia, cella) ricordate nel periodo
MAX_KEYS = 200            # chiavi diverse per contatore (es. kind sconosciuti arrivati dal server)
MAX_CAMERAS = 16
MAX_PRINTED = 3           # errori stampati per punto del codice


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _inc(counter, key, n=1):
    """Incrementa senza far crescere il contatore oltre MAX_KEYS chiavi."""
    if key in counter or len(counter) < MAX_KEYS:
        counter[key] += n
    else:
        counter["altro"] += n


def percentile(sorted_vals, p):
    """Percentile p (0-100) di una lista GIA' ORDINATA, interpolando tra i due ranghi vicini
    (stesso risultato di numpy.percentile). None se la lista e' vuota."""
    n = len(sorted_vals)
    if n == 0:
        return None
    k = (n - 1) * p / 100.0
    f = int(math.floor(k))
    c = min(f + 1, n - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class _Speed:
    """Velocita' di una classe nel periodo: n, media e massimo esatti, percentili da un
    campione a serbatoio (uniforme anche con migliaia di tracce)."""
    __slots__ = ("n", "total", "max", "sample")

    def __init__(self):
        self.n, self.total, self.max, self.sample = 0, 0.0, None, []

    def add(self, v, rng):
        self.n += 1
        self.total += v
        self.max = v if self.max is None else max(self.max, v)
        if len(self.sample) < MAX_SPEED_SAMPLES:
            self.sample.append(v)
        else:
            j = rng.randrange(self.n)
            if j < MAX_SPEED_SAMPLES:
                self.sample[j] = v

    def summary(self, unit):
        s = sorted(self.sample)
        return {"n": self.n, "mean": round(self.total / self.n, 2), "p50": round(percentile(s, 50), 2),
                "p85": round(percentile(s, 85), 2), "max": round(self.max, 2), "unit": unit}


class _Camera:
    """Accumulatori di una camera per il periodo in corso."""

    def __init__(self, zones=(), space="image"):
        self.space = space
        self.zone_names = list(zones)  # restano tra un periodo e l'altro: zone vuote = 0, non assenti
        self.reset()

    def reset(self):
        self.frames = 0
        self.active_max = 0
        self.counts = Counter({c: 0 for c in CLASSI})
        self.movements = {}        # "da->a" -> Counter(classe)
        self.crossings = {}        # direzione -> Counter(classe)
        self.speeds = {}           # (classe, unita') -> _Speed
        self.durations = {}        # classe -> [n, somma_s, max_s]
        self.zone_sum = Counter()  # somma degli oggetti presenti, frame per frame
        self.zone_max = Counter()
        self.zone_occ = Counter()  # frame con almeno un oggetto
        self.zone_cls_occ = {}     # zona -> Counter(classe): frame con almeno un oggetto di quella classe
        self.dwell = {}            # zona -> [n tracce, somma_s] dai riepiloghi delle tracce
        self.heatmap = {}          # classe -> Counter("cx,cy")
        self.cells = 0
        self.seen = set()          # (id traccia, cella) gia' contate nel periodo
        self.events = Counter()


class StatsAggregator:
    def __init__(self, cfg, node_id):
        data = cfg.get("data") or {}
        self.node = node_id
        self.interval = self._float(data.get("stats_interval_s"), 60.0)  # <= 0: statistiche spente
        self.cell_m = self._float(data.get("heatmap_cell_m"), 0.5)
        self.cell_img = self._float(data.get("heatmap_cell_img"), 0.02)
        self._cfg_cams = cfg.get("_cameras") or {}
        self._default_cam = next(iter(self._cfg_cams), "cam0")
        self._lock = threading.RLock()
        self._rng = random.Random()
        self._errors = Counter()
        self.cams = {}
        for cam_id in self._cfg_cams:  # tutte le camere configurate compaiono sempre (frames 0 = problema)
            self._cam(cam_id)
        self._events = Counter()
        self._remote = Counter()
        self._start = None      # inizio del periodo in corso (s)
        self._pending = False   # arrivato qualcosa prima del primo frame (es. pericolo remoto)

    # ------------------------------------------------------------------ ingresso

    def on_frame(self, t, camera_id, tracks, space, zones_of):
        if self.interval <= 0 or not _num(t):  # un istante non valido rovinerebbe il periodo
            return
        try:
            with self._lock:
                if self._start is None:
                    self._start = t
                cam = self._cam(camera_id or self._default_cam)
                if cam is None:
                    return
                if space in ("world", "image"):
                    cam.space = space
                tracks = list(tracks or [])
                cam.frames += 1
                cam.active_max = max(cam.active_max, len(tracks))
                per_zona, classi_zona = Counter(), {}
                for tr in tracks:
                    cls = getattr(tr, "label", None) or "?"
                    for z in self._zones(zones_of, tr):
                        per_zona[z] += 1
                        classi_zona.setdefault(z, set()).add(cls)
                    self._heat(cam, tr, cls)
                for z in per_zona:
                    if z not in cam.zone_names and len(cam.zone_names) < MAX_KEYS:
                        cam.zone_names.append(z)
                for z in cam.zone_names:
                    n = per_zona.get(z, 0)
                    cam.zone_sum[z] += n
                    if n > cam.zone_max[z]:
                        cam.zone_max[z] = n
                    if n:
                        cam.zone_occ[z] += 1
                        occ = cam.zone_cls_occ.setdefault(z, Counter())
                        for cls in classi_zona[z]:
                            _inc(occ, cls)
        except Exception as e:  # una statistica sbagliata non deve fermare il nodo
            self._err("on_frame", e)

    def on_event(self, msg):
        """hazard (solo action new, una volta per pericolo) e remote_hazard (cancel esclusi)."""
        if self.interval <= 0 or not isinstance(msg, dict):
            return
        try:
            with self._lock:
                tipo, kind = msg.get("type"), str(msg.get("kind") or "?")
                if tipo == "hazard":
                    if msg.get("action", "new") != "new":
                        return
                    _inc(self._events, kind)
                    cam = self._cam(msg.get("camera")) if msg.get("camera") else None
                    if cam is not None:
                        _inc(cam.events, kind)
                elif tipo == "remote_hazard":
                    if msg.get("action") == "cancel":
                        return
                    _inc(self._remote, kind)
                else:
                    return
                self._pending = True
        except Exception as e:
            self._err("on_event", e)

    def on_track(self, summary):
        """Riepilogo di una traccia chiusa (messaggio "track" di situation.py)."""
        if self.interval <= 0 or not isinstance(summary, dict) or summary.get("type", "track") != "track":
            return
        try:
            with self._lock:
                cam = self._cam(summary.get("camera") or self._default_cam)
                if cam is None:
                    return
                self._pending = True
                cls = str(summary.get("class") or "?")
                _inc(cam.counts, cls)
                da, a = summary.get("from"), summary.get("to")
                if da and a:
                    self._inc2(cam.movements, f"{da}->{a}", cls)
                cross = summary.get("crosswalk_crossing")
                if isinstance(cross, dict) and cross.get("direction"):
                    self._inc2(cam.crossings, str(cross["direction"]), cls)
                v, unit = self._track_speed(summary)
                if v is not None and ((cls, unit) in cam.speeds or len(cam.speeds) < MAX_KEYS):
                    cam.speeds.setdefault((cls, unit), _Speed()).add(v, self._rng)
                d = summary.get("duration_s")
                if _num(d) and d >= 0 and (cls in cam.durations or len(cam.durations) < MAX_KEYS):
                    dur = cam.durations.setdefault(cls, [0, 0.0, 0.0])
                    dur[0] += 1
                    dur[1] += d
                    dur[2] = max(dur[2], d)
                zones = summary.get("zones")
                for z, s in (zones.items() if isinstance(zones, dict) else ()):
                    if _num(s) and s > 0 and (z in cam.dwell or len(cam.dwell) < MAX_KEYS):
                        dw = cam.dwell.setdefault(z, [0, 0.0])
                        dw[0] += 1
                        dw[1] += s
        except Exception as e:
            self._err("on_track", e)

    # ------------------------------------------------------------------ uscita

    def maybe_flush(self, t):
        """[] oppure [messaggio stats]: il primo stats_interval_s dopo il primo frame."""
        if self.interval <= 0 or not _num(t):
            return []
        try:
            with self._lock:
                if self._start is None:
                    if self._pending:  # niente frame ma eventi/tracce: il periodo parte adesso
                        self._start = t
                    return []
                if t < self._start:  # orologio tornato indietro: riparto da qui
                    self._start = t
                    return []
                if t - self._start < self.interval:
                    return []
                msg = self._message(t)
                self._reset(t)
                return [msg]
        except Exception as e:
            self._err("maybe_flush", e)
            try:  # azzero comunque: memoria limitata anche se il messaggio non si costruisce
                with self._lock:
                    self._reset(t)
            except Exception:
                pass
            return []

    def _message(self, t):
        cameras = {}
        for cam_id, cam in self.cams.items():
            cameras[cam_id] = self._camera_block(cam)
        return {"v": 1, "type": "stats", "node": self.node, "ts": int(t * 1000),
                "from_ts": int(self._start * 1000), "to_ts": int(t * 1000),
                "period_s": round(t - self._start, 1),
                "events": dict(self._events), "remote_events": dict(self._remote), "cameras": cameras}

    def _camera_block(self, cam):
        f = cam.frames
        zones = {}
        for z in cam.zone_names:
            n_dw, tot_dw = cam.dwell.get(z, (0, 0.0))
            zones[z] = {"avg_objects": round(cam.zone_sum[z] / f, 3) if f else 0.0,
                        "max_objects": cam.zone_max[z],
                        "occupancy_ratio": round(cam.zone_occ[z] / f, 3) if f else 0.0,
                        "occupancy_by_class": {c: round(n / f, 3) for c, n in cam.zone_cls_occ.get(z, {}).items()},
                        "dwell_mean_s": round(tot_dw / n_dw, 1) if n_dw else 0.0,
                        "dwell_n": n_dw}
        for z, (n_dw, tot_dw) in cam.dwell.items():  # zone viste solo nei riepiloghi (es. config cambiata)
            if z not in zones:
                zones[z] = {"avg_objects": 0.0, "max_objects": 0, "occupancy_ratio": 0.0,
                            "occupancy_by_class": {}, "dwell_mean_s": round(tot_dw / n_dw, 1), "dwell_n": n_dw}

        speeds = {}
        for (cls, unit), sp in cam.speeds.items():
            # una camera ha un solo spazio, ma se le unita' si mescolano vince quella con piu' dati
            if cls not in speeds or sp.n > speeds[cls]["n"]:
                speeds[cls] = sp.summary(unit)

        world = cam.space == "world"
        return {
            "counts": dict(cam.counts),
            "movements": {k: dict(v) for k, v in cam.movements.items()},
            "crosswalk_crossings": {k: dict(v) for k, v in cam.crossings.items()},
            "speeds": speeds,
            "durations": {c: {"mean_s": round(d[1] / d[0], 1), "max_s": round(d[2], 1)}
                          for c, d in cam.durations.items() if d[0]},
            "zones": zones,
            "events": dict(cam.events),
            "active_max": cam.active_max, "frames": f,
            "heatmap": {c: dict(h) for c, h in cam.heatmap.items() if h},
            "heatmap_cell": self.cell_m if world else self.cell_img,
            "heatmap_unit": "m" if world else "img",
        }

    def _reset(self, t):
        for cam in self.cams.values():
            cam.reset()
        self._events = Counter()
        self._remote = Counter()
        self._start = t
        self._pending = False

    # ------------------------------------------------------------------ aiuti

    def _cam(self, cam_id):
        cam_id = str(cam_id)
        cam = self.cams.get(cam_id)
        if cam is None and len(self.cams) < MAX_CAMERAS:
            conf = self._cfg_cams.get(cam_id) or {}
            cal = conf.get("calibration") or {}
            space = "world" if cal.get("image_points") and cal.get("world_points_m") else "image"
            cam = self.cams[cam_id] = _Camera((conf.get("zones") or {}).keys(), space)
        return cam

    def _zones(self, zones_of, tr):
        if zones_of is None:
            return ()
        try:
            return [str(z) for z in (zones_of(tr) or ())]
        except Exception as e:  # una traccia strana non deve saltare le altre
            self._err("zones_of", e)
            return ()

    def _heat(self, cam, tr, cls):
        """Cella del punto a terra: metri se la camera e' calibrata, altrimenti immagine 0..1."""
        if cam.space == "world":
            p, cell = getattr(tr, "world", None), self.cell_m
        else:
            p, cell = getattr(tr, "img", None), self.cell_img
        if p is None or len(p) < 2 or not (_num(p[0]) and _num(p[1])) or cell <= 0:
            return
        key = f"{math.floor(p[0] / cell)},{math.floor(p[1] / cell)}"
        visto = (getattr(tr, "id", None), key)
        if visto in cam.seen or len(cam.seen) >= MAX_SEEN:
            return
        hm = cam.heatmap.get(cls)
        if hm is None:
            if len(cam.heatmap) >= MAX_KEYS:
                return
            hm = cam.heatmap[cls] = Counter()
        if key not in hm:
            if cam.cells >= MAX_CELLS:
                return
            cam.cells += 1
        cam.seen.add(visto)
        hm[key] += 1

    @staticmethod
    def _inc2(d, key, cls):
        """d[key][cls] += 1 con tetto sulle chiavi."""
        sub = d.get(key)
        if sub is None:
            if len(d) >= MAX_KEYS:
                return
            sub = d[key] = Counter()
        _inc(sub, cls)

    @staticmethod
    def _track_speed(summary):
        """Velocita' rappresentativa della traccia: media in km/h (camera calibrata) o relativa."""
        for key, unit in (("speed_kmh", "km/h"), ("speed_rel", "rel")):
            s = summary.get(key)
            if isinstance(s, dict) and _num(s.get("avg")):
                return float(s["avg"]), unit
        if _num(summary.get("avg_speed_mps")):  # pedoni/ciclisti con camera calibrata
            return summary["avg_speed_mps"] * 3.6, "km/h"
        return None, None

    @staticmethod
    def _float(v, default):
        try:
            v = float(v)
            return v if math.isfinite(v) else default
        except (TypeError, ValueError):
            return default

    def _err(self, where, e):
        self._errors[where] += 1
        if self._errors[where] <= MAX_PRINTED:
            coda = " (i successivi non vengono stampati)" if self._errors[where] == MAX_PRINTED else ""
            print(f"[statistiche] errore in {where}: {e!r}{coda}")
