"""I 4 LED di segnalazione, accesi dall'MCU (STM32) della UNO Q su comando del Python.

Ogni LED ha un ruolo (config leds.roles) e un modo: 0 spento, 1 acceso, 2 lampeggio lento (1 Hz),
3 lampeggio veloce (4 Hz). I 4 modi viaggiano in un solo intero (2 bit per LED, LED 1 = bit 0-1):
    bridge.call("set_leds", packed, ttl_ms)        -> node/sketch/sketch.ino, set_leds(int, int)
Se l'MCU non riceve comandi per ttl_ms spegne tutto da solo (fail-safe "cervello offline"):
per questo il valore viene rimandato ogni refresh_s anche quando non cambia.
La chiamata al Bridge gira in un thread a parte con una casella "solo l'ultimo valore":
Bridge.call e' bloccante (fino a 10 s di timeout), e un Bridge bloccato non deve fermare il nodo.
"""
import threading
import time

RUOLI = ("local", "remote", "emergency", "status", "any", "none")
NOMI_MODI = {0: "spento", 1: "acceso", 2: "lento", 3: "veloce"}
LOCAL_MODE = {3: 3, 2: 1, 1: 2}   # severity del pericolo locale -> modo
CALL_TIMEOUT_S = 2.0              # oltre ttl_ms l'MCU e' comunque gia' in fail-safe
MAX_REMOTE_S = 600                # un pericolo remoto non tiene acceso un LED piu' di cosi'
MAX_ATTIVI = 200                  # per lato: protezione da raffiche di messaggi


def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class LedController:
    def __init__(self, cfg, bridge=None, start_thread=True):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.bridge = bridge
        self.hold_s = _num(cfg.get("hold_s"), 10.0)
        self.min_severity = int(_num(cfg.get("min_severity"), 2))
        self.ttl_ms = int(min(max(_num(cfg.get("ttl_ms"), 3000), 500), 60000))
        # il keep-alive deve arrivare ben prima che scada il ttl dell'MCU
        self.refresh_s = min(max(_num(cfg.get("refresh_s"), 1.0), 0.2), self.ttl_ms / 2000)

        roles = list(cfg.get("roles") or [])[:4]
        roles += ["none"] * (4 - len(roles))
        self.roles = []
        for i, r in enumerate(roles):
            if r not in RUOLI:
                print(f"[led] LED {i + 1}: ruolo '{r}' sconosciuto -> 'none' (validi: {', '.join(RUOLI)})")
                r = "none"
            self.roles.append(r)

        self._lock = threading.Lock()
        self._local = {}    # id -> {"sev", "emergency", "until"}
        self._remote = {}
        self._modes = [0, 0, 0, 0]
        self._packed = None             # ultimo valore calcolato
        self._last_queued = None        # time.monotonic() dell'ultimo invio messo in coda
        self._warned = {}               # quante volte ho gia' stampato un certo avviso
        self._use_timeout = True
        self._send_ok = None            # esito dell'ultimo invio (None = mai provato)
        self._last_send = None
        self._sent = self._errors = 0
        self._last_err_print = 0.0

        self._cond = threading.Condition()
        self._slot = None               # "solo l'ultimo valore": il worker manda sempre il piu' recente
        self._closing = False
        self._thread = None
        if self.enabled and bridge is not None and start_thread:
            self._thread = threading.Thread(target=self._worker, name="leds-bridge", daemon=True)
            self._thread.start()
        if self.enabled:
            dove = "Bridge verso l'MCU" if bridge is not None else "nessun Bridge: solo log"
            print(f"[led] ruoli {self.roles}, {dove}")

    # ------------------------------------------------------------------ ingresso

    def on_hazard(self, msg, local, t):
        """hazard locale (local=True) o remote_hazard dal server: "new" accende, "cancel" spegne quell'id."""
        if not self.enabled:
            return
        try:
            self._on_hazard(msg, bool(local), float(t))
        except Exception as e:  # un messaggio strano non deve fermare il nodo
            self._warn("hazard", f"[led] messaggio ignorato ({e!r})")

    def _on_hazard(self, msg, local, t):
        hid = msg.get("id")
        if hid is None:
            return
        hid = str(hid)
        attivi = self._local if local else self._remote
        if msg.get("action") == "cancel":
            with self._lock:
                attivi.pop(hid, None)
            return

        sev = int(min(max(_num(msg.get("severity"), 1), 1), 3))
        emergency = msg.get("kind") == "emergency_vehicle" or msg.get("category") == "emergency"
        ts = _num(msg.get("ts"))
        exp = _num(msg.get("expires"))
        if local:
            # acceso fino al cancel, ma al massimo hold_s (e mai oltre la validita' dell'evento)
            base = ts / 1000 if ts else t
            until = base + self.hold_s
            if exp:
                until = min(until, exp / 1000)
        else:
            # "expires" viene dall'orologio di un altro incrocio: se la board e' indietro
            # non lo tengo acceso piu' della validita' dichiarata (expires - ts)
            until = exp / 1000 if exp else t + self.hold_s
            if exp and ts and exp > ts:
                until = min(until, t + (exp - ts) / 1000)
            until = min(until, t + MAX_REMOTE_S)
        with self._lock:
            if until <= t:
                attivi.pop(hid, None)
                if not local:
                    self._warn("scaduto", f"[led] pericolo remoto {hid} gia' scaduto all'arrivo "
                                          f"(messaggio vecchio o orologio della board non sincronizzato?)")
                return
            attivi[hid] = {"sev": sev, "emergency": emergency, "until": until}
            if len(attivi) > MAX_ATTIVI:
                del attivi[min(attivi, key=lambda k: attivi[k]["until"])]

    def tick(self, t, online):
        """Ricalcola i modi e li manda all'MCU se cambiati, o comunque ogni refresh_s (keep-alive)."""
        if not self.enabled:
            return
        try:
            t = time.time() if t is None else float(t)
            with self._lock:
                modes = self._compute(t, bool(online))
                packed = sum(m << (2 * i) for i, m in enumerate(modes))
                cambiato = packed != self._packed
                self._modes, self._packed = modes, packed
            if cambiato:
                print("[led] " + "  ".join(f"LED{i + 1}({r})={NOMI_MODI[m]}"
                                           for i, (r, m) in enumerate(zip(self.roles, modes))))
            if self.bridge is None:
                return
            # il keep-alive va a tempo reale (monotonic): il ttl dell'MCU non sa nulla del t simulato
            now = time.monotonic()
            if cambiato or self._last_queued is None or now - self._last_queued >= self.refresh_s - 0.05:
                self._last_queued = now
                self._enqueue(packed)
        except Exception as e:
            self._warn("tick", f"[led] errore in tick: {e!r}")

    def _compute(self, t, online):
        for attivi in (self._local, self._remote):
            for k in [k for k, h in attivi.items() if h["until"] <= t]:
                del attivi[k]
        local_sev = max((h["sev"] for h in self._local.values() if h["sev"] >= self.min_severity), default=0)
        local = LOCAL_MODE.get(local_sev, 0)
        remote_sev = max((h["sev"] for h in self._remote.values()), default=0)
        remote = 0 if not remote_sev else (3 if remote_sev >= 3 else 2)
        emerg = any(h["emergency"] for d in (self._local, self._remote) for h in d.values())
        per_ruolo = {"local": local, "remote": remote, "emergency": 3 if emerg else 0,
                     "status": 1 if online else 2, "any": max(local, remote), "none": 0}
        return [per_ruolo[r] for r in self.roles]

    # ------------------------------------------------------------------ invio all'MCU

    def _enqueue(self, packed):
        if self._thread is None:
            self._send(packed)  # start_thread=False (test/replay): invio sincrono
            return
        with self._cond:
            self._slot = packed
            self._cond.notify()

    def _worker(self):
        while True:
            with self._cond:
                while self._slot is None and not self._closing:
                    self._cond.wait(1.0)
                if self._closing:
                    return
                packed, self._slot = self._slot, None
            self._send(packed)

    def _send(self, packed):
        t0 = time.monotonic()
        try:
            self._call(int(packed), int(self.ttl_ms))
            ok, err = True, None
        except Exception as e:  # router assente, sketch non caricato, timeout...
            ok, err = False, repr(e)
        ms = round((time.monotonic() - t0) * 1000, 1)
        with self._lock:
            prima = self._send_ok
            self._send_ok = ok
            self._last_send = {"ok": ok, "packed": packed, "error": err, "ms": ms, "ts": int(time.time() * 1000)}
            if ok:
                self._sent += 1
            else:
                self._errors += 1
        if ok and prima is not True:
            print("[led] MCU raggiunto: set_leds OK" if prima is None else "[led] MCU di nuovo raggiungibile")
        elif not ok and (prima is not False or t0 - self._last_err_print > 60):
            self._last_err_print = t0
            print(f"[led] invio all'MCU fallito: {err} (lo sketch node/sketch/sketch.ino e' caricato? "
                  f"riprovo; intanto l'MCU va in fail-safe)")

    def _call(self, packed, ttl_ms):
        if self._use_timeout:
            try:
                return self.bridge.call("set_leds", packed, ttl_ms, timeout=CALL_TIMEOUT_S)
            except TypeError as e:
                if "timeout" not in str(e):
                    raise
                self._use_timeout = False  # Bridge senza parametro timeout (es. finto nei test)
        return self.bridge.call("set_leds", packed, ttl_ms)

    # ------------------------------------------------------------------ stato

    def state(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "modes": list(self._modes),
                "roles": list(self.roles),
                "active": sorted(self._local) + sorted(self._remote),
                "packed": self._packed,
                "bridge": self.bridge is not None,
                "last_send": dict(self._last_send) if self._last_send else None,
                "sent": self._sent,
                "send_errors": self._errors,
            }

    def close(self):
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _warn(self, chiave, testo, max_volte=3):
        n = self._warned.get(chiave, 0)
        if n < max_volte:
            self._warned[chiave] = n + 1
            print(testo)
