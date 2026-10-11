"""Il nodo incrocio: mette insieme camere, regole, dati, LED e server.

                    +--> SituationEngine (una per camera) --> hazard / track
detections -------->|                                          |
                    +--> Health (FPS, tempi, temperatura)       v
                                                  Node._on_message
                                                   |  |  |  |  |
              Storage (SQLite) <-------------------+  |  |  |  +--> CameraFusion (piu' camere)
              Publisher (server HTTP/MQTT, UI) <------+  |  +-----> LedController (4 LED via Bridge)
              StatsAggregator (conteggi, velocita', heatmap) <+
Server --> RemoteLink (pericoli degli incroci vicini) --> LED + Storage + UI
Incroci vicini / wearable <--> PeerLink (UDP multicast, ESP-NOW): stessi pericoli SENZA server

process() va chiamato a ogni frame del modello, tick() circa una volta al secondo
(anche quando il modello non vede niente: chiude le tracce, manda stato e statistiche).
"""
import threading
import time
from collections import OrderedDict
from pathlib import Path

from api import LocalAPI
from config import public_config
from fusion import CameraFusion
from health import Health
from leds import LedController
from peer import PeerLink
from publisher import Publisher
from remote import RemoteLink
from situation import SituationEngine
from stats import StatsAggregator
from storage import Storage

REMOTE_SEEN_MAX = 2000  # pericoli remoti gia' gestiti (lo stesso arriva dal server E dal p2p)


class Node:
    def __init__(self, cfg, ui=None, base_dir=".", bridge=None, start_threads=True):
        self.cfg = cfg
        self.node_id = cfg.get("node_id", "node")
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()
        self.started = time.time()
        data = cfg.get("data") or {}

        self.storage = None
        if data.get("store_db", True):
            try:
                self.storage = Storage(self.base_dir / data.get("db_file", "smartcrossing.db"),
                                       data.get("retention_days", 7))
            except Exception as e:  # disco o permessi: il nodo funziona lo stesso, senza archivio
                print("[nodo] archivio SQLite non disponibile:", repr(e))

        self.publisher = Publisher(cfg.get("publish") or {}, self.node_id, storage=self.storage, ui=ui,
                                   base_dir=self.base_dir, server=cfg.get("server") or {},
                                   start_thread=start_threads, store_scene=bool(data.get("store_scene", False)))
        self.leds = LedController(cfg.get("leds") or {}, bridge=bridge, start_thread=start_threads)
        self.engines = {cam: SituationEngine(cfg, on_event=self._on_message, camera_id=cam)
                        for cam in cfg["_cameras"]}
        self.stats = StatsAggregator(cfg, self.node_id)
        self.health = Health(cfg, self.node_id)
        self.fusion = CameraFusion(cfg, self.node_id) if len(self.engines) > 1 else None
        self.remote = RemoteLink(cfg.get("server") or {}, self.node_id, on_message=self._on_server,
                                 start_thread=start_threads)
        self.peer = PeerLink(cfg.get("p2p") or {}, self.node_id, on_message=self._on_peer,
                             neighbors=cfg.get("neighbors") or {}, start_thread=start_threads)
        self._remote_seen = OrderedDict()
        self.latest_scene = []
        self.latest_status = None
        self.latest_stats = None
        self._last_update = {cam: None for cam in self.engines}
        self._last_scene = 0.0
        self._last_cleanup = 0.0
        self.api = None
        if (cfg.get("api") or {}).get("enabled"):
            self.api = LocalAPI(cfg["api"], self)
            if start_threads:
                self.api.start()

    # ------------------------------------------------------------------ ingresso

    def process(self, detections, camera_id=None, t=None):
        """Da chiamare a ogni frame con il dict del brick Video Object Detection."""
        cam = camera_id or next(iter(self.engines))
        t = time.time() if t is None else t
        with self._lock:
            engine = self.engines.get(cam)
            if engine is None:
                self.health.on_error("process", KeyError(f"camera sconosciuta {cam}"))
                return []
            t0 = time.perf_counter()
            try:
                out = engine.update(detections, t)
            except Exception as e:  # mai fermare il nodo per un frame
                self.health.on_error("engine", e)
                return []
            proc_ms = (time.perf_counter() - t0) * 1000
            self._last_update[cam] = t
            n_det = sum(len(v) for v in (detections or {}).values() if isinstance(v, (list, tuple))) \
                if isinstance(detections, dict) else 0
            self.health.on_frame(t, cam, proc_ms, n_det)
            recenti = engine.tracker_recent(t)
            self._safe(self.stats.on_frame, t, cam, recenti, engine.space, engine.zones_of)
            # LED in tempo reale: chi c'e' adesso davanti alla camera, senza aspettare il tick
            self._safe(self.leds.set_presence, {tr.label for tr in recenti}, t, [tr.id for tr in recenti])
            self._safe(self.leds.push, t)
            self._scene(t)
            return out

    def tick(self, t=None):
        """Da chiamare circa ogni secondo da un thread a parte."""
        t = time.time() if t is None else t
        with self._lock:
            for cam, engine in self.engines.items():
                last = self._last_update[cam]
                if last is None or t - last >= 1.0:  # nessun frame: il tempo passa lo stesso
                    try:
                        engine.update({}, t)
                    except Exception as e:
                        self.health.on_error("engine_tick", e)
            for msg in self._safe(self.stats.maybe_flush, t) or []:
                self.latest_stats = msg
                self._publish(msg)
            extra = {
                "active_tracks": sum(len(e.tracker.tracks) for e in self.engines.values()),
                "outbox": self._safe(self.publisher.backlog) or 0,
                "db_bytes": self._safe(self.storage.size_bytes) if self.storage else None,
                "rule_errors": {f"{c}:{k}": v for c, e in self.engines.items() for k, v in e.rule_errors.items()},
                "server_online": self.remote.online,
                "server": self._safe(self.remote.state),
                "p2p": self._safe(self.peer.state),
                "leds": self._safe(self.leds.state),
                "spaces": {c: e.space for c, e in self.engines.items()},
            }
            status = self._safe(self.health.maybe_status, t, extra)
            if status:
                self.latest_status = status
                self._publish(status)
            if self.storage and t - self._last_cleanup > 3600:
                self._last_cleanup = t
                self._safe(self.storage.cleanup, t)
        # LED di stato acceso se il nodo parla con qualcuno: server oppure incroci vicini in p2p
        self._safe(self.leds.tick, t, self.remote.online or self.peer.online)

    # ------------------------------------------------------------------ messaggi

    def _on_message(self, msg):
        """Messaggi prodotti dalle regole (hazard) e dalle tracce chiuse (track)."""
        tipo = msg.get("type")
        if tipo == "hazard":
            # prima il LED (decide il nodo, subito), poi archivio e invio al server
            self._safe(self.leds.on_hazard, msg, True, msg.get("ts", 0) / 1000)
            self._safe(self.leds.push, msg.get("ts", 0) / 1000 or None)
        self._publish(msg)
        if tipo == "hazard":
            self._safe(self.stats.on_event, msg)
            self._safe(self.peer.send_hazard, msg)  # agli incroci vicini e ai wearable, senza server
        elif tipo == "track":
            self._safe(self.stats.on_track, msg)
            if self.fusion:
                for m in self._safe(self.fusion.on_track, msg) or []:
                    self._publish(m)

    def _on_server(self, msg):
        """Messaggio arrivato dal server: oltre a gestirlo lo ripeto via radio ai wearable vicini."""
        if self._on_remote(msg, via="server") and isinstance(msg, dict) and msg.get("type", "remote_hazard") == "remote_hazard":
            self._safe(self.peer.forward_remote, msg)

    def _on_peer(self, msg):
        """Messaggio arrivato in p2p: pericolo di un incrocio vicino o presenza di un wearable."""
        if isinstance(msg, dict) and msg.get("type") == "vru_presence":
            self._publish(msg)  # utente vulnerabile con braccialetto nei paraggi: lo archivio
            return
        self._on_remote(msg, via="p2p")

    def _on_remote(self, msg, via="server"):
        """Pericolo segnalato da un incrocio vicino. Lo stesso pericolo puo' arrivare sia dal server
        sia in p2p: lo gestisco una volta sola. Ritorna True se era nuovo."""
        if not isinstance(msg, dict):
            return False
        with self._lock:
            m = dict(msg)
            m.setdefault("type", "remote_hazard")
            m.setdefault("via", via)
            if m["type"] == "remote_hazard" and m.get("id"):
                chiave = (m["id"], m.get("action", "new"))
                if chiave in self._remote_seen:
                    return False
                self._remote_seen[chiave] = time.time()
                while len(self._remote_seen) > REMOTE_SEEN_MAX:
                    self._remote_seen.popitem(last=False)
            self._publish(m)
            if m["type"] == "remote_hazard":
                self._safe(self.leds.on_hazard, m, False, time.time())
                self._safe(self.leds.push, time.time())
                self._safe(self.stats.on_event, m)
            return True

    def _publish(self, msg):
        self._safe(self.publisher.publish, msg)

    def _scene(self, t):
        hz = (self.cfg.get("data") or {}).get("scene_hz", 1.0) or 0
        if hz <= 0 or t - self._last_scene < 1.0 / hz:
            return
        self._last_scene = t
        objs = []
        for e in self.engines.values():
            objs += self._safe(e.scene, t) or []
        self.latest_scene = objs
        self._publish({"v": 1, "type": "scene", "node": self.node_id, "ts": int(t * 1000), "objects": objs})

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # un modulo accessorio non deve mai fermare il nodo
            self.health.on_error(getattr(fn, "__qualname__", str(fn)), e)
            return None

    # ------------------------------------------------------------------ per l'API

    def status_snapshot(self):
        return {"node": self.node_id, "status": self.latest_status, "stats": self.latest_stats,
                "scene": self.latest_scene, "server_online": self.remote.online,
                "p2p": self._safe(self.peer.state), "leds": self._safe(self.leds.state),
                "config": public_config(self.cfg)}

    def close(self):
        for comp in (self.api, self.remote, self.peer, self.leds, self.publisher, self.storage):
            if comp is not None and hasattr(comp, "close"):
                self._safe(comp.close)
