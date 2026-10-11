"""Uscita dei messaggi del nodo: archivio, server (il computer), MQTT, pagina web, console.

publish(msg) fa, in quest'ordine (ogni passo e' isolato: se uno fallisce gli altri vanno avanti):
  1. archivio: SQLite (Storage) oppure, se non c'e', una riga JSONL in log_file;
  2. coda di invio verso il server: tabella outbox di SQLite (sopravvive ai riavvii) oppure,
     senza Storage, una coda in memoria (max 5000 messaggi);
  3. MQTT, se configurato e se paho-mqtt e' installato (facoltativo);
  4. pagina web dell'app (WebUI di App Lab): hazard, remote_hazard e status;
  5. una riga leggibile in console per ogni pericolo.

Un thread prende i messaggi piu' vecchi dalla coda, li manda al server con una POST
(batch_size 1 = un oggetto JSON per POST, > 1 = un array) e li toglie dalla coda solo dopo
una risposta 2xx: se il server non risponde ritenta con attesa crescente 1, 2, 4 ... 30 s.
Cosi' il nodo lavora anche offline e, quando la rete torna, il server riceve tutto, in ordine.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:  # MQTT facoltativo: senza paho-mqtt si usa solo HTTP
    mqtt = None

MEM_OUTBOX_MAX = 5000
BACKOFF_MAX_S = 30.0
LOG_MAX_BYTES = 50 * 1024 * 1024  # oltre, log_file viene ruotato in log_file.1
UI_TYPES = ("hazard", "remote_hazard", "status")
DEFAULT_SEND = {"hazard": True, "track": True, "stats": True, "status": True, "movement": True, "scene": False}
# Il server dice che il contenuto e' sbagliato: ritentare non serve e bloccherebbe la coda.
# Questi messaggi finiscono in rejected.jsonl (non vanno persi) e la coda va avanti.
REJECTED = (400, 413, 415, 422)


def _dumps(msg):
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)


def _num(v, default, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


class Publisher:
    def __init__(self, cfg, node_id, storage=None, ui=None, base_dir=".", server=None, start_thread=True,
                 store_scene=None):
        cfg = cfg or {}
        server = server or {}
        self.node_id = node_id
        self.storage = storage
        self.ui = ui
        self.base_dir = Path(base_dir)

        # URL del server: publish.url oppure, se vuoto, <server.url>/api/events
        self.url = str(cfg.get("url") or "").strip()
        if not self.url and str(server.get("url") or "").strip():
            self.url = str(server["url"]).strip().rstrip("/") + "/api/events"
        self.token = str(cfg.get("token") or server.get("token") or "")
        self.timeout_s = max(0.5, _num(cfg.get("timeout_s"), 3.0))
        self.batch_size = max(1, _num(cfg.get("batch_size"), 1, int))
        send = cfg.get("send")
        self.send = {**DEFAULT_SEND, **send} if isinstance(send, dict) else dict(DEFAULT_SEND)
        # scene a 1 Hz riempirebbe l'archivio: si salva solo se richiesto (data.store_scene)
        self.store_scene = bool(cfg.get("store_scene", False) if store_scene is None else store_scene)
        log_file = cfg.get("log_file", "events.jsonl")
        self.log_path = self.base_dir / log_file if log_file else None
        self.rejected_path = self.base_dir / "rejected.jsonl"

        self._lock = threading.Lock()       # coda in memoria e contatori
        self._file_lock = threading.Lock()  # file JSONL
        self._send_lock = threading.Lock()  # un solo invio alla volta (thread + flush_once dei test)
        self._mem = deque()                 # (seq, testo JSON): coda senza Storage o se SQLite fallisce
        self._seq = 0
        self._errors = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._failed = False
        self._fail_streak = 0
        self.sent = 0
        self.rejected = 0
        self.dropped = 0
        self.last_ok = None
        self.last_error = None

        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # server in LAN: niente proxy
        self._mqtt = None
        self._mqtt_topic = f"sc/v1/node/{node_id}"
        self._mqtt_qos = 1
        try:
            self._mqtt = self._mqtt_start(cfg.get("mqtt") or {})
        except Exception as e:
            print("[mqtt] non disponibile:", repr(e))

        self._thread = None
        if self.url:
            print(f"[invio] server: {self.url} (lotti da {self.batch_size}, timeout {self.timeout_s:g} s)")
            if start_thread:
                self._thread = threading.Thread(target=self._run, name="publisher", daemon=True)
                self._thread.start()
        else:
            print("[invio] nessun server configurato (publish.url / server.url): dati solo in locale")

    # ------------------------------------------------------------------ ingresso

    def publish(self, msg):
        if not isinstance(msg, dict):
            self._err("publish", TypeError(f"messaggio non valido: {type(msg).__name__}"))
            return
        tipo = msg.get("type")
        self._step("archivio", self._archive, msg, tipo)
        if self.url and self.send.get(tipo):
            self._step("coda", self._enqueue, msg)
        if self._mqtt is not None:
            self._step("mqtt", self._mqtt_publish, msg, tipo)
        if self.ui is not None and tipo in UI_TYPES:
            self._step("ui", self.ui.send_message, "situation", msg)
        if tipo in ("hazard", "remote_hazard"):
            self._step("console", self._print_hazard, msg, tipo)

    def _step(self, where, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # un'uscita guasta (UI, disco, MQTT) non deve fermare le altre
            self._err(where, e)

    def _err(self, where, e):
        n = self._errors[where] = self._errors.get(where, 0) + 1
        if n <= 3 or n % 100 == 0:
            print(f"[invio] errore in {where} (x{n}): {e!r}")

    def _archive(self, msg, tipo):
        if tipo == "scene" and not self.store_scene:
            return
        if self.storage is not None and self._call_storage("save", msg):
            return
        if self.log_path is not None:  # niente SQLite (o SQLite in errore): riga JSONL
            self._append_jsonl(self.log_path, [_dumps(msg)])

    def _call_storage(self, name, msg):
        """True se Storage ha preso il messaggio (None conta come ok: interfaccia della specifica)."""
        try:
            return getattr(self.storage, name)(msg) is not False
        except Exception as e:  # SQLite rotto: si ripiega su file / memoria
            self._err(name, e)
            return False

    def _append_jsonl(self, path, lines):
        with self._file_lock:
            try:
                if path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in lines))

    def _enqueue(self, msg):
        if self.storage is None or not self._call_storage("outbox_put", msg):
            text = _dumps(msg)
            with self._lock:
                if len(self._mem) >= MEM_OUTBOX_MAX:
                    self._mem.popleft()
                    self.dropped += 1
                    if self.dropped == 1 or self.dropped % 1000 == 0:
                        print(f"[invio] coda in memoria piena ({MEM_OUTBOX_MAX}): scartati {self.dropped} "
                              f"messaggi piu' vecchi")
                self._seq += 1
                self._mem.append((self._seq, text))
        self._wake.set()

    def _print_hazard(self, msg, tipo):
        a = msg.get("actor") or {}
        if tipo == "hazard":
            print(f"[PERICOLO] {msg.get('kind')} sev={msg.get('severity')} azione={msg.get('action', 'new')} "
                  f"traccia={a.get('track', '-')} classe={a.get('class', '-')} camera={msg.get('camera', '-')}")
        else:
            print(f"[PERICOLO VICINO] {msg.get('kind')} sev={msg.get('severity')} "
                  f"azione={msg.get('action', 'new')} da={msg.get('from_node', '?')}")

    # ------------------------------------------------------------------ invio al server

    def backlog(self):
        """Messaggi in coda non ancora arrivati al server."""
        n = len(self._mem)
        if self.storage is not None:
            try:
                n += int(self.storage.outbox_size() or 0)
            except Exception as e:
                self._err("backlog", e)
        return n

    @property
    def online(self):
        """True se l'ultimo invio al server e' andato a buon fine."""
        return bool(self.url) and self.last_ok is not None and not self._failed

    def info(self):
        return {"url": self.url, "backlog": self.backlog(), "sent": self.sent, "rejected": self.rejected,
                "dropped": self.dropped, "online": self.online, "last_error": self.last_error,
                "mqtt": self._mqtt is not None}

    def _take(self, n):
        """[(sorgente, id, testo JSON)] dei messaggi piu' vecchi: prima SQLite, poi la memoria."""
        if self.storage is not None:
            try:
                items = self.storage.outbox_take(n) or []
            except Exception as e:
                self._err("outbox_take", e)
                items = []
            if items:
                return [("db", i, _dumps(m)) for i, m in items]
        with self._lock:
            return [("mem", seq, text) for seq, text in list(self._mem)[:n]]

    def _ack(self, items):
        db_ids = [i for src, i, _ in items if src == "db"]
        if db_ids:
            self.storage.outbox_ack(db_ids)
        mem = [i for src, i, _ in items if src == "mem"]
        if mem:
            last = max(mem)
            with self._lock:  # i presi sono i piu' vecchi: tolgo dalla testa fino all'ultimo inviato
                while self._mem and self._mem[0][0] <= last:
                    self._mem.popleft()

    def flush_once(self):
        """Invia un lotto. Ritorna quanti messaggi sono usciti dalla coda (0 se vuota o server giu')."""
        if not self.url:
            return 0
        with self._send_lock:
            try:
                items = self._take(self.batch_size)
                if not items:
                    self._failed = False
                    return 0
                texts = [t for _, _, t in items]
                body = texts[0] if self.batch_size == 1 else "[" + ",".join(texts) + "]"
                code, err = self._post(body.encode("utf-8"))
                if 200 <= code < 300:
                    self._ack(items)
                    self.sent += len(items)
                    if self._failed:
                        print(f"[invio] server di nuovo raggiungibile: {self.backlog()} messaggi in coda")
                    self._failed, self._fail_streak = False, 0
                    self.last_ok, self.last_error = time.time(), None
                    return len(items)
                if code in REJECTED:
                    self._reject(texts, err)
                    self._ack(items)
                    return len(items)
                self._fail(err)
                return 0
            except Exception as e:
                self._fail(repr(e))
                return 0

    def _post(self, body):
        """POST al server. Ritorna (codice HTTP o 0 se non raggiungibile, errore o None)."""
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8", "User-Agent": f"smartcrossing/{self.node_id}"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener.open(req, timeout=self.timeout_s) as r:
                r.read()
                return r.status, None
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(200).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            return e.code, f"HTTP {e.code} {detail}".strip()
        except Exception as e:  # rete giu', timeout, server spento, URL sbagliato
            return 0, repr(getattr(e, "reason", None) or e)

    def _fail(self, err):
        self._fail_streak += 1
        self.last_error = err
        if not self._failed or self._fail_streak % 10 == 0:
            print(f"[invio] server non raggiungibile ({err}): ritento, {self.backlog()} messaggi in coda")
        self._failed = True

    def _reject(self, texts, err):
        self.rejected += len(texts)
        print(f"[invio] il server ha rifiutato {len(texts)} messaggi ({err}): salvati in {self.rejected_path.name}")
        try:
            self._append_jsonl(self.rejected_path, texts)
        except Exception as e:  # restano comunque nell'archivio locale
            self._err("rejected", e)

    def _run(self):
        attesa = 1.0
        while not self._stop.is_set():
            if self.flush_once() > 0:
                attesa = 1.0
                continue  # c'e' altro in coda: avanti subito
            if self._failed:
                self._stop.wait(attesa)  # i nuovi messaggi non accorciano l'attesa
                attesa = min(attesa * 2, BACKOFF_MAX_S)
            else:
                attesa = 1.0
                self._wake.wait(1.0)  # coda vuota: mi sveglia publish()
                self._wake.clear()

    # ------------------------------------------------------------------ MQTT (facoltativo)

    def _mqtt_start(self, mq):
        host = str(mq.get("host") or "").strip()
        if not host:
            return None
        if mqtt is None:
            print("[mqtt] paho-mqtt non installato: MQTT disattivato (pip install paho-mqtt)")
            return None
        port = _num(mq.get("port"), 1883, int)
        self._mqtt_topic = f"{str(mq.get('topic_prefix') or 'sc/v1').rstrip('/')}/node/{self.node_id}"
        self._mqtt_qos = min(2, max(0, _num(mq.get("qos"), 1, int)))
        cid = f"smartcrossing-{self.node_id}"
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        else:
            client = mqtt.Client(client_id=cid)
        if mq.get("username"):
            client.username_pw_set(mq["username"], mq.get("password") or None)
        # se il nodo sparisce senza salutare, il broker pubblica "offline" al posto suo
        client.will_set(self._mqtt_topic + "/status", self._offline_msg(), qos=self._mqtt_qos, retain=True)
        client.on_connect = self._mqtt_on_connect
        client.reconnect_delay_set(1, 30)
        client.max_queued_messages_set(1000)  # con il broker giu' non riempio la memoria
        client.connect_async(host, port, keepalive=30)
        client.loop_start()
        print(f"[mqtt] connessione a {host}:{port}, topic {self._mqtt_topic}/<tipo>")
        return client

    def _offline_msg(self):
        return _dumps({"v": 1, "type": "status", "node": self.node_id, "ts": int(time.time() * 1000), "online": False})

    def _mqtt_on_connect(self, client, userdata, flags, rc, *rest):
        if getattr(rc, "is_failure", rc != 0):
            print(f"[mqtt] connessione rifiutata: {rc}")
            return
        print("[mqtt] connesso")
        try:  # sostituisce subito l'eventuale "offline" lasciato dal broker
            client.publish(self._mqtt_topic + "/status", _dumps(
                {"v": 1, "type": "status", "node": self.node_id, "ts": int(time.time() * 1000), "online": True}),
                qos=self._mqtt_qos, retain=True)
        except Exception as e:
            self._err("mqtt", e)

    def _mqtt_publish(self, msg, tipo):
        self._mqtt.publish(f"{self._mqtt_topic}/{tipo or 'message'}", _dumps(msg), qos=self._mqtt_qos,
                           retain=(tipo == "status"))

    def _mqtt_stop(self, client):
        try:
            client.publish(self._mqtt_topic + "/status", self._offline_msg(), qos=self._mqtt_qos, retain=True)
            client.disconnect()
            client.loop_stop()
        except Exception as e:
            self._err("mqtt", e)

    # ------------------------------------------------------------------ chiusura

    def close(self):
        """Ferma il thread di invio (quello che resta in coda SQLite parte al prossimo avvio)."""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=self.timeout_s + 2)  # al massimo un invio in corso
        client, self._mqtt = self._mqtt, None
        if client is not None:  # in un thread a parte: un broker bloccato non deve bloccare la chiusura
            s = threading.Thread(target=self._mqtt_stop, args=(client,), daemon=True)
            s.start()
            s.join(timeout=2.0)
