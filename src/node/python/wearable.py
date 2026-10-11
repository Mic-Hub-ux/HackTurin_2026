"""Bracciale del ciclista: canale DIRETTO incrocio -> dispositivo personale (niente server, niente internet).

E' un canale diverso da quello tra incroci (peer.py): l'incrocio manda pacchetti UDP piccoli
direttamente all'indirizzo del bracciale (src/firmware/esp32_bracciale), sulla porta 48100.
Serve solo una rete Wi-Fi locale in comune (anche l'hotspot di un telefono, senza internet).

    {"v":1,"t":"wr","n":"incrocio-1","seq":12,"on":1,"k":"bike_lane_invasion","sev":2,
     "txt":"Auto nella ciclabile","ttl":2500}

- "on":1 finche' il pericolo c'e', "on":0 quando finisce (anche come keep-alive: il bracciale sa
  che l'incrocio e' vicino e collegato).
- Il messaggio viene rimandato ogni period_s: se il bracciale non riceve niente per ttl ms spegne
  l'allarme da solo (un pacchetto perso non lascia mai un allarme acceso, ne' uno spento a lungo).
- Pericoli che interessano il ciclista (config wearable.rules): auto che entra nella ciclabile,
  auto ferma nella ciclabile, sorpasso troppo vicino. L'allarme dura finche' la camera vede
  ancora l'auto che lo ha causato (come il LED rosso dell'incrocio), al massimo max_s.

Sviluppo previsto, senza nessuna rete Wi-Fi: lo stesso messaggio trasmesso in ESP-NOW da un ESP32
"radio" collegato all'MCU dell'incrocio (per esempio 4 fili digitali D6-D9 con il codice dell'allarme).
Il bracciale riceve allora direttamente dalla radio dell'incrocio, anche dove non c'e' nessuna rete.
"""
import json
import socket
import threading

DEFAULT_RULES = [{"kind": "bike_lane_invasion"},
                 {"kind": "illegal_stop", "zone": "bike_lane"},
                 {"kind": "close_pass"}]
TESTI = {"bike_lane_invasion": "Auto nella ciclabile", "illegal_stop": "Auto ferma nella ciclabile",
         "close_pass": "Sorpasso troppo vicino", "emergency_vehicle": "Mezzo di emergenza"}
VISTO_HOLD_S = 0.5     # l'auto e' "ancora vista" per mezzo secondo dopo l'ultimo frame
MAX_ATTIVI = 100


def _num(x, default):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _dest(s, porta):
    """'192.168.1.30' oppure '192.168.1.30:48100' -> (ip, porta)."""
    s = str(s).strip()
    if not s:
        return None
    if ":" in s:
        ip, p = s.rsplit(":", 1)
        try:
            return ip.strip(), int(p)
        except ValueError:
            return None
    return s, porta


class WearableLink:
    def __init__(self, cfg, node_id):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.node_id = str(node_id)
        self.enabled = bool(cfg.get("enabled", False))
        self.port = int(_num(cfg.get("port"), 48100))
        self.period_s = max(0.2, _num(cfg.get("period_s"), 0.5))
        self.ttl_ms = int(max(1000, _num(cfg.get("ttl_ms"), 2500)))
        self.max_s = max(1.0, _num(cfg.get("max_s"), 30.0))
        self.rules = [r for r in (cfg.get("rules") or DEFAULT_RULES) if isinstance(r, dict) and r.get("kind")]
        self.devices = [d for d in (_dest(x, self.port) for x in (cfg.get("devices") or [])) if d]
        if cfg.get("broadcast", True):
            self.devices.append(("255.255.255.255", self.port))
        self._lock = threading.Lock()
        self._active = {}      # id pericolo -> {"kind", "sev", "track", "until"}
        self._tracks = {}      # traccia -> ultimo istante in cui la camera l'ha vista
        self._state = None     # (kind, sev) dell'allarme in corso, None = nessuno
        self._last_sent = None
        self._seq = 0
        self.sent = self.errors = 0
        self.last_error = None
        self.sock = None
        if not self.enabled:
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as e:
            print(f"[bracciale] socket UDP non disponibile ({e}): bracciale disattivato")
            self.enabled = False
            return
        dove = ", ".join(f"{ip}:{p}" for ip, p in self.devices) or "nessun indirizzo (wearable.devices vuoto)"
        print(f"[bracciale] canale diretto attivo -> {dove}")

    # ------------------------------------------------------------------ ingresso

    def _match(self, msg):
        kind = msg.get("kind")
        zona = (msg.get("where") or {}).get("zone") if isinstance(msg.get("where"), dict) else None
        return any(r["kind"] == kind and (not r.get("zone") or r["zone"] == zona) for r in self.rules)

    def on_hazard(self, msg, t):
        """Pericolo visto da QUESTO incrocio ("new" accende, "cancel" spegne)."""
        if not self.enabled or not isinstance(msg, dict) or not self._match(msg):
            return
        hid = str(msg.get("id"))
        with self._lock:
            if msg.get("action") == "cancel":
                self._active.pop(hid, None)
                return
            actor = msg.get("actor") if isinstance(msg.get("actor"), dict) else {}
            track = actor.get("track") if actor.get("track") not in (None, 0, "") else None
            until = t + self.max_s
            exp = _num(msg.get("expires"), None)
            if exp:
                until = min(until, exp / 1000)
            self._active[hid] = {"kind": msg.get("kind"), "sev": int(_num(msg.get("severity"), 2)),
                                 "track": None if track is None else str(track), "until": until}
            if track is not None:
                self._tracks.setdefault(str(track), t)
            if len(self._active) > MAX_ATTIVI:
                del self._active[min(self._active, key=lambda k: self._active[k]["until"])]

    def set_tracks(self, track_ids, t):
        """Tracce viste dalla camera in questo frame."""
        if not self.enabled:
            return
        with self._lock:
            for tr in track_ids:
                self._tracks[str(tr)] = t
            if len(self._tracks) > 4 * MAX_ATTIVI:
                for k in [k for k, v in self._tracks.items() if t - v > 60]:
                    del self._tracks[k]

    # ------------------------------------------------------------------ invio

    def push(self, t):
        """Manda subito se l'allarme e' cambiato, altrimenti ogni period_s (keep-alive)."""
        if not self.enabled:
            return
        with self._lock:
            for k in [k for k, h in self._active.items() if h["until"] <= t or (
                    h["track"] is not None and t - self._tracks.get(h["track"], -1e9) > VISTO_HOLD_S)]:
                del self._active[k]
            top = max(self._active.values(), key=lambda h: h["sev"], default=None)
            state = (top["kind"], top["sev"]) if top else None
            cambiato = state != self._state
            if not cambiato and self._last_sent is not None and t - self._last_sent < self.period_s:
                return
            self._state, self._last_sent = state, t
            self._seq += 1
            seq = self._seq
        msg = {"v": 1, "t": "wr", "n": self.node_id, "seq": seq, "on": 1 if state else 0, "ttl": self.ttl_ms}
        if state:
            msg.update({"k": state[0], "sev": state[1], "txt": TESTI.get(state[0], state[0])})
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        for dest in self.devices:
            try:
                self.sock.sendto(data, dest)
                self.sent += 1
            except OSError as e:
                self.errors += 1
                if self.last_error != str(e):
                    print(f"[bracciale] invio a {dest[0]}:{dest[1]} fallito: {e}")
                self.last_error = str(e)
        if cambiato:
            print(f"[bracciale] ALLARME -> {msg['txt']}" if state else "[bracciale] fine allarme")

    tick = push

    # ------------------------------------------------------------------ stato

    def state(self):
        with self._lock:
            return {"enabled": self.enabled, "devices": [f"{ip}:{p}" for ip, p in self.devices],
                    "alert": self._state[0] if self._state else None, "sent": self.sent,
                    "errors": self.errors, "last_error": self.last_error}

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
