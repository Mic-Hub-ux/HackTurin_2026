"""Comunicazione diretta tra incroci e con i wearable, senza server: protocollo SC-P2P v1.

Perche': se il server o il Wi-Fi/Internet cadono, gli incroci vicini devono comunque avvisarsi
dei pericoli, e un braccialetto (ESP32 + motorino a vibrazione) deve ricevere l'avviso direttamente.

Due trasporti, stesso messaggio:
  - UDP multicast 239.255.42.99:47999 (TTL 1: solo la rete locale, basta un router/hotspot anche
    senza Internet) + elenco opzionale di peer unicast "ip:porta" (per le reti che bloccano il multicast);
  - ESP-NOW tramite un ESP32 "gateway" collegato in USB seriale (115200 baud, una riga JSON per
    messaggio, "\\n" finale): funziona anche senza nessun router. Il gateway trasmette in broadcast
    (FF:FF:FF:FF:FF:FF) ogni riga ricevuta e stampa sulla seriale ogni frame ESP-NOW ricevuto.
    Gateway e wearable usano lo stesso canale Wi-Fi fisso (default 1).

Messaggio = un oggetto JSON compatto UTF-8 di massimo 240 byte (sta in un frame ESP-NOW da 250):
  hz  pericolo   {"v":1,"t":"hz","id","n","a":"n|c","k","c","s","cc","ts","e","tw","nx","tg","h","sig"}
  hb  heartbeat  {"v":1,"t":"hb","n","ts","h":0,"pos"}               ogni heartbeat_s
  vru wearable   {"v":1,"t":"vru","d","p":"cyclist|person|vehicle","ts","h":0}   ogni 2 s
Regole:
  - "tg" con tutti e tre i profili non viene scritto (per il protocollo "tg" assente = tutti);
  - dimensione: se supera 240 byte si tolgono "tw", poi "cc" (ricavabile da "k"), poi "nx", poi
    si accorcia "k". Conta solo per chi invia: i riceventi accettano qualunque sottoinsieme;
  - pericoli solo locali (non propagate): se local_alerts, partono con h=0 e nx=[questo nodo],
    cosi' li ricevono i wearable vicini ma gli altri incroci li ignorano ("nx" qui non si toglie mai);
  - firma (se c'e' una chiave condivisa): sig = primi 16 hex di HMAC-SHA256(chiave, JSON canonico
    senza "sig" e "h", chiavi ordinate, separatori (",", ":"), UTF-8 non escapato). Con la chiave si
    scartano i messaggi non firmati o con firma sbagliata; senza chiave si accetta tutto.
    Eccezione: i beacon "vru" dei wearable non firmati sono accettati (se firmati, la firma va verificata);
  - dedup: (id, a) ricordati per max(scadenza, 60 s) (solo se il messaggio era pertinente o da
    rilanciare: una copia non pertinente con h=0 non deve "nascondere" una copia pertinente);
  - rilancio: se h > 0 e mai visto, il NODO ritrasmette lo stesso oggetto con h-1 su tutti i
    trasporti (cosi' fa anche da ponte UDP <-> ESP-NOW). I wearable non rilanciano mai;
  - pertinenza al nodo: proprio id in "nx"; oppure "nx" assente/vuoto e (mittente tra i vicini
    configurati oppure nessun vicino configurato);
  - wearable (firmware ESP32, documentato qui per coerenza): vibra se il suo profilo e' in "tg"
    (o "tg" manca), il messaggio non e' scaduto (senza orologio: valido per e - ts ms dalla
    ricezione) e s >= 2 oppure c == "emergency". Puo' ignorare la firma.

Prova da riga di comando (sulla board o sul PC):
  python peer.py listen                      mostra tutto il traffico P2P
  python peer.py send --kind emergency_vehicle --to incrocio-2
  aggiungere --serial /dev/ttyACM0 per usare il gateway ESP-NOW (non mentre l'app la sta usando).
"""
import hashlib
import hmac
import json
import os
import select
import socket
import struct
import threading
import time

MAX_BYTES = 240            # payload massimo (frame ESP-NOW = 250 byte)
SEEN_MIN_S = 60            # dedup: ricorda (id, a) almeno per 60 s
VRU_REPORT_S = 30          # vru_presence al massimo ogni 30 s per dispositivo
SKEW_MS = 5_000            # ts sfasato oltre 5 s (nodo offline senza NTP): uso l'ora di ricezione
                           # (via radio il ritardo e' di millisecondi; come SFASAMENTO_MAX_MS di remote.py)
ALL_TARGETS = ["person", "cyclist", "vehicle"]
DROP_ORDER = ("tw", "cc", "nx")   # campi tolti (in quest'ordine) se il messaggio supera 240 byte
P2P_DEFAULTS = {"enabled": False, "group": "239.255.42.99", "port": 47999, "mcast_ttl": 1,
                "peers": [], "key": "", "hops": 2, "heartbeat_s": 5, "bind_port": None,
                "local_alerts": True, "serial": {"port": "", "baud": 115200}}


def _log(text):
    print("[p2p]", text, flush=True)


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _now_ms():
    return int(time.time() * 1000)


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------- codifica

def sign(obj, key):
    """Primi 16 caratteri hex di HMAC-SHA256(key, JSON canonico senza "sig" e "h")."""
    body = {k: v for k, v in obj.items() if k not in ("sig", "h")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canon, hashlib.sha256).hexdigest()[:16]


def verify(obj, key):
    """Senza chiave accetta tutto; con la chiave serve una firma valida."""
    if not key:
        return True
    sig = obj.get("sig")
    return isinstance(sig, str) and hmac.compare_digest(sig, sign(obj, key))


def encode(obj, key="", keep=()):
    """Oggetto -> bytes <= 240 (firmati se c'e' la chiave). Se non ci sta toglie "tw", "cc", "nx"
    (tranne quelli in keep), poi accorcia "k". None se non ci sta comunque."""
    o = {k: v for k, v in obj.items() if k != "sig"}

    def dump():
        if key:
            o["sig"] = sign(o, key)
        return _dumps(o)

    data = dump()
    for field in DROP_ORDER:
        if len(data) > MAX_BYTES and field in o and field not in keep:
            del o[field]
            data = dump()
    while len(data) > MAX_BYTES and o.get("k"):
        o["k"] = o["k"][:max(0, len(o["k"]) - (len(data) - MAX_BYTES))]
        data = dump()
    return data if len(data) <= MAX_BYTES else None


def decode(data):
    """bytes -> dict SC-P2P v1, oppure None se non valido."""
    try:
        o = json.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
    except Exception:          # ValueError, UnicodeDecodeError, RecursionError (JSON molto annidato)...
        return None
    if isinstance(o, dict) and o.get("v") == 1 and o.get("t") in ("hz", "hb", "vru"):
        return o
    return None


def hazard_to_p2p(msg, node_id, hops=2):
    """Messaggio "hazard" del nodo (vedi SPEC.md) -> oggetto compatto "hz"."""
    actor = msg.get("actor") or {}
    ts = _int(msg.get("ts"), _now_ms())
    o = {"v": 1, "t": "hz", "id": str(msg.get("id") or f"{node_id}-{ts}"), "n": str(node_id),
         "a": "c" if msg.get("action") == "cancel" else "n",
         "k": str(msg.get("kind") or ""), "c": str(msg.get("category") or "info"),
         "s": max(1, min(3, _int(msg.get("severity"), 1)))}
    if isinstance(msg.get("cause"), int):
        o["cc"] = msg["cause"]
    o["ts"] = ts
    o["e"] = _int(msg.get("expires"), ts + 30_000)
    if actor.get("towards"):
        o["tw"] = str(actor["towards"])
    nx = [str(x) for x in (msg.get("next_nodes") or []) if x]
    if nx:
        o["nx"] = nx
    tg = [str(x) for x in (msg.get("targets") or []) if x]
    if tg and not set(ALL_TARGETS) <= set(tg):   # tutti i profili = "tg" assente (37 byte risparmiati)
        o["tg"] = tg
    o["h"] = max(0, _int(hops, 0))
    return o


def p2p_to_remote(o, ts, expires, transport):
    """Oggetto "hz" ricevuto -> messaggio "remote_hazard" per il nodo."""
    m = {"v": 1, "type": "remote_hazard", "via": "p2p", "transport": transport, "id": o["id"],
         "kind": str(o.get("k") or ""), "category": str(o.get("c") or "info"),
         "severity": _int(o.get("s"), 1), "action": "cancel" if o.get("a") == "c" else "new",
         "from_node": o.get("n"), "ts": ts, "expires": expires,
         "targets": o.get("tg") or list(ALL_TARGETS), "towards": o.get("tw"),
         "next_nodes": o.get("nx") or []}
    if isinstance(o.get("cc"), int):
        m["cause"] = o["cc"]
    return m


# ---------------------------------------------------------------------------- collegamento

class PeerLink:
    """Canale P2P del nodo: invia i pericoli, riceve/rilancia quelli dei vicini, ascolta i wearable.

    on_message(dict) riceve "remote_hazard" (via "p2p") e "vru_presence". Nessun metodo solleva
    eccezioni: se un trasporto non e' disponibile si logga una volta e si continua senza.
    """

    def __init__(self, cfg, node_id, on_message, neighbors=(), start_thread=True):
        cfg = cfg if isinstance(cfg, dict) else {}
        ser = cfg.get("serial")
        ser = {"port": ser} if isinstance(ser, str) else (ser if isinstance(ser, dict) else {})
        c = {**P2P_DEFAULTS, **cfg, "serial": {**P2P_DEFAULTS["serial"], **ser}}
        self.cfg = c
        self.node_id = str(node_id)
        self.on_message = on_message
        if isinstance(neighbors, dict):
            neighbors = neighbors.values()
        self.neighbors = {str(n) for n in (neighbors or ()) if n}
        self.enabled = bool(c.get("enabled"))
        self.key = str(c.get("key") or "")
        self.hops = max(0, min(_int(c.get("hops"), 2), 5))
        try:
            self.heartbeat_s = max(0.2, float(c.get("heartbeat_s") or 5))
        except (TypeError, ValueError):
            self.heartbeat_s = 5.0
        self.group = str(c.get("group") or P2P_DEFAULTS["group"])
        self.port = _int(c.get("port"), 47999)
        if not 0 < self.port < 65536:
            self.port = 47999
        self.bind_port = _int(c.get("bind_port"), self.port) if c.get("bind_port") not in (None, "") else self.port
        if not 0 <= self.bind_port < 65536:   # 0 = porta libera qualsiasi (test)
            self.bind_port = self.port
        self.local_alerts = bool(c.get("local_alerts", True))
        self.relay = True              # False = solo ascolto (strumento da riga di comando)
        self.multicast = False
        self.counters = dict.fromkeys(("tx", "rx", "delivered", "relayed", "dup", "ignored", "bad_sig",
                                       "invalid"), 0)
        self._lock = threading.Lock()
        self._ser_lock = threading.RLock()
        self._stop = threading.Event()
        self._seen = {}                # (id, a) -> scadenza (s)
        self._peers = {}               # node_id -> {"last_seen", "addr"}
        self._vrus = {}                # device -> {"profile", "last_seen", "via"}
        self._vru_reported = {}        # device -> ultimo vru_presence emesso (s)
        self._logged = set()
        self._sock = None
        self._ser_fd = None
        self._ser_buf = b""
        self._ser_retry = 0.0
        self._last_hb = self._last_join = self._last_prune = 0.0
        self._thread = None
        self.unicast = []
        if not self.enabled:
            return
        try:
            self._parse_peers(c.get("peers"))
            self._open_udp()
            self._open_serial()
            if start_thread:
                self._thread = threading.Thread(target=self._run, name="p2p", daemon=True)
                self._thread.start()
        except Exception as e:  # mai bloccare l'avvio del nodo per il P2P
            _log(f"avvio P2P incompleto ({e!r}): continuo con quello che funziona")

    def _parse_peers(self, peers):
        """Peer unicast "ip:porta" (o solo "ip"). Accetta anche una stringa singola."""
        if isinstance(peers, str):
            peers = [peers]
        for p in peers if isinstance(peers, (list, tuple)) else []:
            try:
                host, sep, port = str(p).strip().rpartition(":")
                if not sep:
                    host, port = port, ""
                port = _int(port, -1) if port else self.port
                if not host or not 0 < port < 65536:
                    raise ValueError("serve ip:porta")
                self.unicast.append((socket.gethostbyname(host), port))
            except Exception as e:
                _log(f"peer '{p}' non valido ({e}): ignorato")

    # ------------------------------------------------------------------ API

    def send_hazard(self, msg) -> bool:
        """Trasmette un "hazard" locale. Mai eccezioni.
        - propagate o categoria emergency: a tutti (h=hops, nx=next_nodes se noti);
        - solo locale (local_alerts): solo i "new" con severita' >= 2, h=0 e nx=[questo nodo]:
          servono ai wearable vicini (es. collision_risk), gli altri incroci li ignorano."""
        try:
            if not self.enabled or self._stop.is_set() or msg.get("type", "hazard") != "hazard":
                return False
            if msg.get("propagate") or msg.get("category") == "emergency":
                return self._send_obj(hazard_to_p2p(msg, self.node_id, self.hops))
            if self.local_alerts and msg.get("action", "new") == "new" and _int(msg.get("severity"), 1) >= 2:
                o = hazard_to_p2p({**msg, "next_nodes": [self.node_id]}, self.node_id, 0)
                return self._send_obj(o, keep=("nx",))
            return False
        except Exception as e:
            self._log_once("send_hazard", f"invio pericolo fallito: {e!r}")
            return False

    def forward_remote(self, msg) -> bool:
        """Opzionale: ritrasmette via radio (h=0, nx=[questo nodo]) un remote_hazard arrivato dal
        SERVER, cosi' lo ricevono anche i wearable vicini a questo incrocio. Ignora quelli gia' p2p
        e quelli gia' scaduti (es. arretrati della casella dopo una disconnessione)."""
        try:
            if not self.enabled or self._stop.is_set() or msg.get("type") != "remote_hazard" \
                    or msg.get("via") == "p2p":
                return False
            act = "c" if msg.get("action") == "cancel" else "n"
            with self._lock:   # gia' ricevuto (e rilanciato) via p2p: non serve
                if (msg.get("id"), act) in self._seen:
                    return False
            now = _now_ms()
            exp = _int(msg.get("expires"), 0)
            if act == "n" and exp <= now:
                return False
            orig = msg.get("original") if isinstance(msg.get("original"), dict) else {}
            src = {**orig, **{k: v for k, v in msg.items() if v is not None}, "next_nodes": [self.node_id]}
            # ts = adesso: un wearable senza orologio considera valido il messaggio per e - ts
            src["ts"], src["expires"] = now, (exp if act == "n" else now)
            return self._send_obj(hazard_to_p2p(src, msg.get("from_node") or self.node_id, 0), keep=("nx",))
        except Exception as e:
            self._log_once("forward_remote", f"inoltro remote_hazard fallito: {e!r}")
            return False

    def send_heartbeat(self) -> bool:
        try:
            o = {"v": 1, "t": "hb", "n": self.node_id, "ts": _now_ms(), "h": 0}
            pos = self.cfg.get("pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                o["pos"] = list(pos)
            self._last_hb = time.time()
            data = encode(o, self.key)
            return bool(data) and self._send_bytes(data)
        except Exception as e:
            self._log_once("heartbeat", f"heartbeat non inviato: {e!r}")
            return False

    def poll_once(self, timeout=0.0) -> int:
        """Elabora i datagrammi/righe seriali in attesa. Ritorna quanti messaggi validi ha letto."""
        if not self.enabled or self._stop.is_set():
            return 0
        n = 0
        try:
            fds = [x for x in (self._sock, self._ser_fd) if x is not None]
            if not fds:
                self._stop.wait(timeout)
                return 0
            ready, _, _ = select.select(fds, [], [], timeout)
            if self._sock is not None and self._sock in ready:
                for _ in range(200):
                    try:
                        data, addr = self._sock.recvfrom(2048)
                    except (BlockingIOError, InterruptedError):
                        break
                    except ConnectionResetError:   # Windows: ICMP "porta chiusa" di un invio precedente
                        continue
                    n += self._handle(data, "udp", f"{addr[0]}:{addr[1]}")
            if self._ser_fd is not None and self._ser_fd in ready:
                n += self._read_serial()
        except Exception as e:
            if not self._stop.is_set():
                self._log_once("poll", f"errore in ricezione: {e!r}")
        return n

    def peers(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._peers.items()}

    def vrus(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._vrus.items()}

    @property
    def online(self) -> bool:
        """Almeno un incrocio sentito negli ultimi 3 heartbeat."""
        now = time.time()
        with self._lock:
            return any(now - p["last_seen"] <= 3 * self.heartbeat_s for p in self._peers.values())

    def state(self) -> dict:
        """Riassunto per lo stato del nodo."""
        return {"enabled": self.enabled, "online": self.online, "udp": self._sock is not None,
                "multicast": self.multicast, "serial": self._ser_fd is not None,
                "peers": sorted(self.peers()), "vrus": len(self._vrus), "counters": dict(self.counters)}

    def close(self):
        self._stop.set()
        try:
            t = self._thread
            if t is not None and t is not threading.current_thread():   # close() dentro on_message
                t.join(timeout=2.0)
            s, self._sock = self._sock, None
            if s is not None:
                s.close()
        except Exception as e:
            _log(f"chiusura UDP: {e!r}")
        self._serial_close(retry=False)

    # ------------------------------------------------------------------ ricezione

    def _handle(self, data, transport, addr):
        o = decode(data)
        if o is None:
            self.counters["invalid"] += 1
            return 0
        self.counters["rx"] += 1
        try:
            if o["t"] == "vru":
                self._on_vru(o, transport)
                return 1
            if not isinstance(o.get("n"), str) or o["n"] == self.node_id:
                return 1                     # messaggio mio (loopback multicast o rilancio altrui)
            if not verify(o, self.key):
                self.counters["bad_sig"] += 1
                self._log_once("sig:" + o["n"], f"firma mancante o errata da '{o['n']}' ({addr}): scartato")
                return 1
            if o["t"] == "hb":
                with self._lock:
                    p = {"last_seen": time.time(), "addr": addr}
                    if isinstance(o.get("pos"), list):
                        p["pos"] = o["pos"]
                    self._peers[o["n"]] = p
            else:
                self._on_hz(o, transport)
        except Exception as e:
            self.counters["invalid"] += 1
            self._log_once("handle", f"messaggio non gestibile ({e!r}): {data[:80]!r}")
        return 1

    def _on_hz(self, o, transport):
        mid, act = o.get("id"), o.get("a")
        if not isinstance(mid, str) or act not in ("n", "c"):
            self.counters["invalid"] += 1
            return
        now = time.time()
        now_ms = int(now * 1000)
        ts = _int(o.get("ts"), now_ms)
        exp = _int(o.get("e"), ts)
        if abs(now_ms - ts) > SKEW_MS:       # orologi non sincronizzati: riporto i tempi sul mio
            ts, exp = now_ms, now_ms + max(0, exp - ts)
        h = _int(o.get("h"), 0)
        relay = self.relay and h > 0
        relevant = self._relevant(o)
        with self._lock:
            if (mid, act) in self._seen:
                self.counters["dup"] += 1
                return
            if not (relevant or relay):      # es. allerta locale di un altro incrocio (h=0, nx=[lui])
                self.counters["ignored"] += 1
                return
            self._seen[(mid, act)] = min(max(exp / 1000, now + SEEN_MIN_S), now + 3600)
        if relay:
            data = _dumps({**o, "h": h - 1})    # stessa firma: "h" non e' firmato
            if len(data) <= MAX_BYTES and self._send_bytes(data):
                self.counters["relayed"] += 1
        if relevant:
            self.counters["delivered"] += 1
            self._emit(p2p_to_remote(o, ts, exp, transport))

    def _relevant(self, o):
        nx = o.get("nx")
        if isinstance(nx, list) and nx:
            return self.node_id in nx
        return not self.neighbors or o.get("n") in self.neighbors

    def _on_vru(self, o, transport):
        dev, prof = o.get("d"), o.get("p")
        if not isinstance(dev, str) or not dev:
            self.counters["invalid"] += 1
            return
        if self.key and "sig" in o and not verify(o, self.key):
            self.counters["bad_sig"] += 1
            return
        now = time.time()
        with self._lock:
            prev = self._vrus.get(dev)
            report = (prev is None or prev["profile"] != prof
                      or now - self._vru_reported.get(dev, 0) >= VRU_REPORT_S)
            self._vrus[dev] = {"profile": prof, "last_seen": now, "via": transport}
            if report:
                self._vru_reported[dev] = now
        if report:
            self._emit({"v": 1, "type": "vru_presence", "device": dev, "profile": prof,
                        "ts": int(now * 1000), "via": transport})

    def _emit(self, msg):
        try:
            self.on_message(msg)
        except Exception as e:  # un errore del nodo non deve fermare la ricezione
            self._log_once("callback", f"errore in on_message: {e!r}")

    # ------------------------------------------------------------------ invio

    def _send_obj(self, o, keep=()):
        data = encode(o, self.key, keep)
        if data is None:
            self._log_once("size", f"messaggio {o.get('id')} troppo lungo anche dopo il taglio: non inviato")
            return False
        if o.get("t") == "hz":
            with self._lock:   # non riconsegnarlo a me stesso (loopback, rilanci altrui)
                self._seen[(o["id"], o["a"])] = time.time() + SEEN_MIN_S
        return self._send_bytes(data)

    def _send_bytes(self, data):
        ok = False
        s = self._sock
        if s is not None:
            for addr in [(self.group, self.port)] + self.unicast:
                try:
                    s.sendto(data, addr)
                    ok = True
                except Exception as e:   # OSError, ma anche socket chiuso nel frattempo
                    self._log_once(f"tx:{addr}", f"invio a {addr[0]}:{addr[1]} fallito: {e}")
        if self._ser_fd is not None:
            ok = self._serial_write(data) or ok
        if ok:
            self.counters["tx"] += 1
        return ok

    # ------------------------------------------------------------------ UDP

    def _open_udp(self):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            if hasattr(socket, "SIO_UDP_CONNRESET"):   # Windows: niente errori ICMP in ricezione
                try:
                    s.ioctl(socket.SIO_UDP_CONNRESET, False)
                except (OSError, ValueError):
                    pass
            s.bind(("", self.bind_port))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, max(1, _int(self.cfg.get("mcast_ttl"), 1)))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setblocking(False)
            self._sock = s
        except Exception as e:
            if s is not None:
                s.close()
            _log(f"UDP non disponibile sulla porta {self.bind_port} ({e}): resta solo la seriale")
            return
        self._join()

    def _join(self):
        self._last_join = time.time()
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.multicast = True
            _log(f"multicast {self.group}:{self.port} attivo (porta locale {self.bind_port})")
        except Exception as e:
            self._log_once("join", f"multicast non disponibile ({e}): solo peer unicast/seriale, riprovo ogni 30 s")

    # ------------------------------------------------------------------ seriale (gateway ESP-NOW)

    def _open_serial(self):
        port = str(self.cfg["serial"].get("port") or "")
        if not port:
            return
        if os.name != "posix":
            self._log_once("ser_os", "gateway ESP-NOW su seriale supportato solo su Linux (UNO Q): disattivato")
            return
        baud = _int(self.cfg["serial"].get("baud"), 115200)
        try:
            import termios
            import tty
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                tty.setraw(fd)
                attrs = termios.tcgetattr(fd)
                speed = getattr(termios, f"B{baud}", termios.B115200)
                attrs[2] |= termios.CLOCAL | termios.CREAD
                attrs[4] = attrs[5] = speed
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                termios.tcflush(fd, termios.TCIOFLUSH)
            except Exception:
                os.close(fd)
                raise
            self._ser_fd, self._ser_buf = fd, b""
            self._logged.discard("ser_open")
            _log(f"gateway ESP-NOW su {port} a {baud} baud")
        except Exception as e:
            self._ser_retry = time.time() + 10
            self._log_once("ser_open", f"seriale {port} non apribile ({e}): riprovo ogni 10 s")

    def _serial_close(self, retry=True):
        with self._ser_lock:   # (RLock) mai chiudere il fd mentre un altro thread ci scrive
            fd, self._ser_fd = self._ser_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            if retry:
                _log("gateway ESP-NOW scollegato: riprovo ogni 10 s")
        self._ser_retry = time.time() + 10

    def _serial_write(self, data):
        line, deadline = data + b"\n", time.time() + 0.5
        with self._ser_lock:
            try:
                while line:
                    try:
                        line = line[os.write(self._ser_fd, line):]
                    except BlockingIOError:
                        if time.time() > deadline:
                            raise TimeoutError("buffer seriale pieno")
                        time.sleep(0.005)
                return True
            except (OSError, TypeError) as e:
                self._log_once("ser_tx", f"scrittura sul gateway fallita: {e}")
                self._serial_close()
                return False

    def _read_serial(self):
        try:
            chunk = os.read(self._ser_fd, 4096)
        except (BlockingIOError, InterruptedError):
            return 0
        except (OSError, TypeError):
            self._serial_close()
            return 0
        if not chunk:                      # dispositivo scollegato
            self._serial_close()
            return 0
        *lines, self._ser_buf = (self._ser_buf + chunk).split(b"\n")
        if len(self._ser_buf) > 2048:
            self._ser_buf = b""
        n = 0
        for line in lines:
            if line.lstrip().startswith(b"#"):
                continue                   # riga di stato/errore del gateway
            i = line.find(b"{")            # le altre righe senza "{" sono log del boot
            if i >= 0:
                n += self._handle(line[i:].strip(), "espnow", "espnow")
        return n

    # ------------------------------------------------------------------ thread

    def _run(self):
        while not self._stop.is_set():
            self.poll_once(0.2)
            try:
                self._tick(time.time())
            except Exception as e:
                self._log_once("tick", f"errore di manutenzione: {e!r}")

    def _tick(self, now):
        if now - self._last_hb >= self.heartbeat_s:
            self.send_heartbeat()
        if self._sock is not None and not self.multicast and now - self._last_join >= 30:
            self._join()
        if self.cfg["serial"].get("port") and self._ser_fd is None and os.name == "posix" and now >= self._ser_retry:
            self._open_serial()
        if now - self._last_prune >= 10:
            self._last_prune = now
            with self._lock:
                self._seen = {k: v for k, v in self._seen.items() if v > now}
                for d in [d for d, v in self._vrus.items() if now - v["last_seen"] > 600]:
                    self._vrus.pop(d, None)
                    self._vru_reported.pop(d, None)

    def _log_once(self, key, text):
        if key not in self._logged:
            self._logged.add(key)
            _log(text)


# ---------------------------------------------------------------------------- riga di comando

if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Prova del canale P2P SC-P2P v1")
    ap.add_argument("cmd", choices=["listen", "send"])
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--node", help="id del nodo (default: node_id del config + '-cli')")
    ap.add_argument("--kind", default="emergency_vehicle")
    ap.add_argument("--severity", type=int, default=3)
    ap.add_argument("--to", help="nodo destinatario (nx)")
    ap.add_argument("--cancel", action="store_true", help="invia l'annullamento dell'evento --id")
    ap.add_argument("--id", default=None)
    ap.add_argument("--serial", help="porta del gateway ESP-NOW, es. /dev/ttyACM0")
    args = ap.parse_args()

    try:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    pcfg = {**(raw.get("p2p") or {}), "enabled": True}
    if args.serial:
        pcfg["serial"] = {**(pcfg.get("serial") or {}), "port": args.serial}
    node = args.node or f"{raw.get('node_id', 'incrocio-1')}-cli"
    link = PeerLink(pcfg, node, lambda m: print(json.dumps(m, ensure_ascii=False), flush=True))
    try:
        if args.cmd == "send":
            try:
                from events import KINDS
                cause, _, targets, _, validity, _, cat = KINDS[args.kind]
            except (ImportError, KeyError):
                cause, targets, validity, cat = 99, ALL_TARGETS, 30, "risk"
            t = _now_ms()
            msg = {"type": "hazard", "id": args.id or f"{node}-{t}", "kind": args.kind, "category": cat,
                   "severity": args.severity, "cause": cause, "targets": targets, "propagate": True,
                   "action": "cancel" if args.cancel else "new", "ts": t,
                   "expires": t if args.cancel else t + validity * 1000,
                   "next_nodes": [args.to] if args.to else []}
            print("inviato" if link.send_hazard(msg) else "NON inviato", msg["id"])
            time.sleep(0.5)
        else:
            link.relay = False
            link._relevant = lambda o: True    # in ascolto mostro tutto
            print(f"in ascolto come '{node}' (Ctrl+C per uscire)")
            while True:
                time.sleep(5)
                print("peers:", {k: round(time.time() - v["last_seen"], 1) for k, v in link.peers().items()},
                      "wearable:", list(link.vrus()), link.counters)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
