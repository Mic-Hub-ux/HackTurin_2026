"""Collegamento con il server (il computer centrale): riceve i pericoli degli incroci vicini.

Il server tiene una "casella" per ogni incrocio. Quando un vicino vede un pericolo che si sta
spostando verso di noi (es. un'auto che ha invaso la ciclabile e viene da questa parte), il
server ci mette un messaggio "remote_hazard". Un thread legge la casella ogni poll_s secondi:
    GET {url}/api/inbox/{node_id}?after={ultimo seq letto}  ->  {"messages": [...], "last": n}
e passa ogni messaggio a on_message (nel nodo: LED + archivio + pagina web).
Ogni lettura vale anche come "sono vivo" per il server. Senza rete il nodo continua a
funzionare: si riprova con attesa crescente (max MAX_ATTESA_S) e online diventa False.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

MAX_ATTESA_S = 5.0          # attesa massima tra due tentativi quando il server non risponde
SFASAMENTO_MAX_MS = 5000    # oltre questo sfasamento tra board e server si correggono ts/expires
MAX_RISPOSTA = 5_000_000    # byte: una casella piena (100 messaggi) sta molto sotto


def _num(v, default, minimo):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(minimo, v) if v == v else default  # v != v: NaN


def _base_url(url):
    """Indirizzo base del server. Tollera "192.168.1.10:8000" e l'url completo dell'invio."""
    url = str(url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    for coda in ("/api/events", "/api"):
        if url.endswith(coda):
            url = url[: -len(coda)]
    return url


def _descrivi(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return f"HTTP {e.code}: token rifiutato, controlla server.token"
        if e.code == 404:
            return "HTTP 404: server.url deve essere l'indirizzo base, es. http://192.168.1.10:8000"
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"rete: {e.reason}"
    if isinstance(e, (TimeoutError, OSError)):
        return f"rete: {e}"
    if isinstance(e, ValueError):
        return f"risposta non valida: {e}"
    return repr(e)


class RemoteLink:
    def __init__(self, cfg, node_id, on_message, start_thread=True):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.node_id = str(node_id)
        self.on_message = on_message
        self.url = _base_url(cfg.get("url"))
        self.token = str(cfg.get("token") or "")
        self.poll_s = _num(cfg.get("poll_s"), 1.0, 0.2)
        self.timeout_s = _num(cfg.get("timeout_s"), 3.0, 0.5)
        self.last_seq = 0           # ultimo seq letto dalla casella
        self.received = 0           # messaggi consegnati a on_message
        self.errors = 0
        self.last_error = ""
        self.clock_offset_ms = 0    # orologio board - orologio server (0 se allineati)
        self._last_ok = None        # time.monotonic() dell'ultima risposta valida
        self._ok = None             # esito dell'ultimo tentativo: serve a stampare solo i cambi di stato
        self._more = False
        self._seen = deque(maxlen=500)   # seq gia' consegnati (protezione dai doppioni)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # niente proxy di sistema: il server e' un PC della rete locale
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._thread = None
        if not self.url:
            print("[server] server.url vuoto: nessun collegamento (i pericoli degli incroci vicini non arrivano)")
        elif start_thread:
            self._thread = threading.Thread(target=self._run, name="remote-link", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ pubblico

    @property
    def enabled(self):
        return bool(self.url)

    @property
    def online(self):
        """True se il server ha risposto da poco (entro 3 giri di lettura + timeout)."""
        last = self._last_ok
        return last is not None and time.monotonic() - last <= 3 * self.poll_s + self.timeout_s

    def poll_once(self):
        """Una lettura della casella. Ritorna quanti messaggi sono stati consegnati. Mai eccezioni."""
        if not self.url:
            return 0
        try:
            with self._lock:  # il thread e le chiamate dirette (test) non si sovrappongono
                return self._handle(self._get())
        except Exception as e:
            self._failed(e)
            return 0

    def state(self):
        return {"url": self.url, "online": self.online, "last_seq": self.last_seq,
                "received": self.received, "errors": self.errors, "last_error": self.last_error,
                "clock_offset_ms": self.clock_offset_ms}

    def close(self):
        self._stop.set()
        t = self._thread
        try:
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=self.timeout_s + 1)
        except Exception:
            pass

    # ------------------------------------------------------------------ interno

    def _run(self):
        attesa = self.poll_s
        while not self._stop.is_set():
            try:
                n = self.poll_once()
                if self._ok:
                    # casella con piu' di 100 messaggi: leggo subito il resto
                    attesa = 0 if (self._more and n) else self.poll_s
                else:
                    attesa = min(max(attesa, self.poll_s) * 2, MAX_ATTESA_S)
            except Exception as e:  # il thread non deve morire mai
                print("[server] errore nel thread di lettura:", repr(e))
                attesa = MAX_ATTESA_S
            if attesa:
                self._stop.wait(attesa)

    def _get(self):
        q = urllib.parse.urlencode({"after": self.last_seq})
        url = f"{self.url}/api/inbox/{urllib.parse.quote(self.node_id, safe='')}?{q}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with self._opener.open(req, timeout=self.timeout_s) as r:
            body = r.read(MAX_RISPOSTA)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("messages", []), list):
            raise ValueError("manca la lista 'messages'")
        return data

    def _handle(self, data):
        locale_ms = time.time() * 1000
        self._last_ok = time.monotonic()
        if self._ok is not True:
            print(f"[server] collegato a {self.url} (casella di {self.node_id})")
        self._ok = True
        self._clock(data.get("server_ts"), locale_ms)

        last = data.get("last")
        if not isinstance(last, int) or isinstance(last, bool) or last < 0:
            last = None
        base = self.last_seq
        if last is not None and last < base:
            # il server e' ripartito con un database nuovo: i seq ricominciano da capo
            print(f"[server] la casella e' ripartita da capo (seq {base} -> {last}): riallineo")
            base = self.last_seq = 0
            self._seen.clear()

        n = 0
        for m in data.get("messages") or []:
            if not isinstance(m, dict):
                continue
            seq = m.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                if seq <= base or seq in self._seen:
                    continue  # gia' consegnato
                self._seen.append(seq)
                self.last_seq = max(self.last_seq, seq)
            try:
                self.on_message(self._fix_clock(m))
                n += 1
            except Exception as e:  # un messaggio che fa errore non blocca i successivi
                self.errors += 1
                print(f"[server] errore nel gestire il messaggio {seq}: {e!r}")
        if last is not None:
            self.last_seq = max(self.last_seq, last)
        self.received += n
        self._more = bool(data.get("more"))
        return n

    def _clock(self, server_ts, locale_ms):
        """Se l'orologio della board e' sfasato (niente NTP), i LED leggerebbero male expires."""
        if not isinstance(server_ts, (int, float)) or isinstance(server_ts, bool):
            return
        off = int(locale_ms - server_ts)
        nuovo = off if abs(off) > SFASAMENTO_MAX_MS else 0
        if abs(nuovo - self.clock_offset_ms) > 2000:
            if nuovo:
                print(f"[server] orologio della board sfasato di {nuovo / 1000:+.1f} s rispetto al server: "
                      f"correggo gli orari dei pericoli remoti")
            else:
                print("[server] orologio della board di nuovo allineato al server")
            self.clock_offset_ms = nuovo

    def _fix_clock(self, m):
        off = self.clock_offset_ms
        if not off:
            return m
        m = dict(m)
        for k in ("ts", "expires"):
            if isinstance(m.get(k), (int, float)) and not isinstance(m.get(k), bool):
                m[k] = int(m[k] + off)
        m["clock_fix_ms"] = off
        return m

    def _failed(self, e):
        self.errors += 1
        self.last_error = _descrivi(e)
        if self._ok is not False:
            print(f"[server] {self.url} non raggiungibile ({self.last_error}): "
                  f"riprovo da solo, intanto il nodo lavora offline")
        self._ok = False
