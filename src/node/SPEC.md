# Smart Crossing — specifica tecnica dei moduli

Documento di riferimento per chi sviluppa il codice. Tutto il codice del nodo è **Python 3
standard library** (gira dentro App Lab sulla UNO Q, Debian aarch64): niente numpy/requests sul
nodo. Unica dipendenza opzionale: `paho-mqtt` (se manca, l'MQTT si disattiva con un avviso).
Il server sul PC è anch'esso solo stdlib. Gli strumenti in `node/tools/` girano sul PC (venv in
`dataset/.venv`, che ha opencv, numpy, ultralytics, matplotlib).

Lingua: commenti e messaggi in italiano, nomi delle chiavi JSON in inglese. Ogni modulo non deve
MAI sollevare eccezioni verso il chiamante nei metodi chiamati a runtime (log + continua).

## Mappa dei file

```
node/python/            (sulla board, cartella python/ dell'app App Lab)
  main.py               avvio su App Lab (brick + watchdog)                     [fatto]
  node.py               orchestratore: chiama tutti i moduli                    [fatto]
  config.py             caricamento/validazione config + DEFAULTS               [fatto]
  situation.py          motore regole per camera (RULES, SOGLIE)                [fatto]
  tracker.py            tracker                                                 [fatto]
  events.py             catalogo eventi (KINDS) + EventFactory                  [fatto]
  geometry.py, safety.py, traffic_light.py                                      [fatto]
  storage.py            archivio SQLite                                         [fatto]  
  publisher.py          invio a server HTTP (+MQTT opz.), UI, log               [fatto]  
  stats.py              statistiche aggregate + heatmap                         [fatto]  
  health.py             stato di salute del nodo                                [fatto]  
  api.py                API HTTP locale (opzionale)                             [fatto]  
  fusion.py             collegamento tracce tra camere diverse                  [fatto]  
  leds.py               4 LED via Bridge verso l'MCU                            [fatto]  
  remote.py             riceve dal server i pericoli degli incroci vicini       [fatto]  
node/sketch/sketch.ino  sketch MCU (STM32) che pilota i 4 LED                   [fatto]  
server/                 server sul PC: riceve dati, inoltra ai vicini, dashboard [fatto]  
node/tools/             replay.py, simulate.py, zone_editor.py, export_data.py, heatmap.py, test_suite.py
```

## Messaggi (tutti dict JSON, chiave "type")

Comuni: `"v": 1`, `"node"`, `"ts"` (ms epoch).

| type | prodotto da | contenuto principale |
|---|---|---|
| `hazard` | situation.py | `id` (stabile), `action` new/cancel, `kind`, `category` (emergency/violation/risk/info), `severity` 1-3, `cause`, `cause_txt`, `targets`, `propagate`, `expires`, `norma`, `camera`, `actor` {track, class, from, towards, next_node, img, world, speed_kmh/speed_rel, heading_deg, confidence}, `where`, `details`, `next_nodes` |
| `track` | situation.py | riepilogo di una traccia chiusa: `id`, `camera`, `class`, `ts_start`, `ts_end`, `duration_s`, `from`, `to`, `towards`, `space`, `path_length`, `unit`, `zones` {zona: secondi}, `events` [kind], `trajectory` [[t_rel, x, y]], `speed_kmh`/`speed_rel` {avg,min,max,p85}, `max_speed_kmh`, `crosswalk_crossing` {direction, duration_s}, `labels`, `confidence`, `hits` |
| `scene` | node.py | `objects`: oggetti attivi (id, class, camera, img, world, speed_kmh/speed_rel, heading_deg, zones, towards, conf, age_s) |
| `stats` | stats.py | vedi StatsAggregator |
| `status` | health.py | vedi Health |
| `movement` | fusion.py | oggetto passato da una camera all'altra |
| `remote_hazard` | server → remote.py | pericolo segnalato da un altro incrocio |

## Interfacce da implementare

### storage.py
```python
class Storage:
    def __init__(self, path, retention_days=7)        # sqlite3, check_same_thread=False, WAL, threading.Lock
    def save(self, msg: dict) -> None                  # instrada per msg["type"]:
        # hazard, remote_hazard, movement -> tabella events(id TEXT, ts INT, type, kind, action, severity, json)
        # track  -> tracks(id, ts_start, ts_end, camera, class, from_ap, to_ap, json)
        # stats  -> stats(ts, json) e in piu' heatmap: upsert dei conteggi in msg["cameras"][cam]["heatmap"][cls]["cx,cy"]
        # status -> status(ts, json)
        # scene  -> NON salvata (solo se cfg data.store_scene, gestito dal Publisher che allora la passa)
        # altri  -> messages(ts, type, json)
    def query(self, table, since_ms=0, limit=100, kind=None) -> list[dict]   # table in events/tracks/stats/status; ordine ts crescente; ritorna i JSON originali
    def latest(self, table) -> dict | None
    def heatmap(self, camera=None, cls=None) -> list[dict]   # [{"camera","class","cx","cy","count"}]
    def outbox_put(self, msg: dict) -> None
    def outbox_take(self, n: int) -> list[tuple[int, dict]]  # i piu' vecchi, NON li cancella
    def outbox_ack(self, ids: list[int]) -> None             # cancella quelli inviati
    def outbox_size(self) -> int                             # massimo 50000 righe: oltre, scarta i piu' vecchi
    def size_bytes(self) -> int
    def cleanup(self, now_s: float) -> int                   # cancella righe piu' vecchie di retention_days
    def close(self) -> None
```

### publisher.py
```python
class Publisher:
    def __init__(self, cfg, node_id, storage=None, ui=None, base_dir=".", server=None, start_thread=True)
        # cfg = config["publish"]: url, token, timeout_s, batch_size, log_file, send{type: bool}, mqtt{...}
        # server = config["server"]: se cfg["url"] vuoto e server["url"] pieno -> url = server["url"].rstrip("/") + "/api/events"
        #          token: cfg["token"] oppure server["token"] (header Authorization: Bearer)
    def publish(self, msg: dict) -> None
        # 1) storage.save(msg) se storage (scene solo se store_scene); se storage e' None -> append JSONL su log_file
        # 2) se url e send.get(type) -> storage.outbox_put(msg) (o coda in memoria max 5000 se storage None)
        # 3) MQTT se configurato: topic f"{topic_prefix}/node/{node_id}/{type}", qos da cfg, status retained
        # 4) UI: per hazard, remote_hazard, status -> ui.send_message("situation", msg) (try/except)
        # 5) stampa una riga leggibile per gli hazard: "[PERICOLO] kind sev=.. azione=.. traccia=.."
    def backlog(self) -> int
    def flush_once(self) -> int   # invia un lotto (batch_size; 1 = un JSON per POST, >1 = array JSON); usato dal thread e dai test
    def close(self) -> None
    # thread di invio: ritenta con backoff esponenziale (1 -> 30 s) se il server non risponde; mai perdere messaggi
```

### stats.py
```python
class StatsAggregator:
    def __init__(self, cfg, node_id)      # cfg completo; usa cfg["data"]: stats_interval_s, heatmap_cell_m, heatmap_cell_img
    def on_frame(self, t, camera_id, tracks, space, zones_of) -> None
        # tracks = lista di tracker.Track attivi e confermati (attributi: id, label, img, world, size, hits,
        #          first_seen, last_seen, speed(space), scaled_speed(), state); zones_of(track) -> [nomi zone]
        # accumula: occupazione zone (media e massimo oggetti per zona per frame), oggetti attivi max,
        #           heatmap: cella del punto a terra (world se space=="world" con cella heatmap_cell_m, altrimenti img con heatmap_cell_img)
        #           contando ogni traccia al massimo una volta per cella per periodo
    def on_event(self, msg) -> None       # hazard (solo action new) e remote_hazard: conteggio per kind
    def on_track(self, summary) -> None   # conteggi per classe, movimenti from->to, attraversamenti strisce per direzione,
                                          # velocita' (speed_kmh.avg o speed_rel.avg), dwell per zona, durate
    def maybe_flush(self, t) -> list[dict]   # [] oppure [messaggio stats] ogni stats_interval_s (il primo dopo stats_interval_s dal primo frame)
```
Messaggio `stats`:
```json
{"v":1,"type":"stats","node":"...","ts":0,"from_ts":0,"to_ts":0,"period_s":60,
 "events":{"kind":n}, "remote_events":{"kind":n},
 "cameras":{"cam0":{
    "counts":{"vehicle":n,"person":n,"cyclist":n,"emergency":n},
    "movements":{"via->incrocio":{"vehicle":n}},
    "crosswalk_crossings":{"A->B":{"person":n}},
    "speeds":{"vehicle":{"n":0,"mean":0,"p50":0,"p85":0,"max":0,"unit":"km/h|rel"}},
    "durations":{"vehicle":{"mean_s":0,"max_s":0}},
    "zones":{"crosswalk":{"avg_objects":0.0,"max_objects":0,"occupancy_ratio":0.0,"dwell_mean_s":0.0}},
    "active_max":0, "frames":0,
    "heatmap":{"vehicle":{"12,7":3}}, "heatmap_cell":0.5, "heatmap_unit":"m|img"}}}
```

### health.py
```python
class Health:
    def __init__(self, cfg, node_id)      # cfg["data"]["status_interval_s"]
    def on_frame(self, t, camera_id, proc_ms, n_detections) -> None
    def on_error(self, where: str, err: Exception) -> None     # conta e stampa le prime 3 per 'where'
    def maybe_status(self, t, extra: dict) -> dict | None      # ogni status_interval_s
    def warnings(self, t) -> list[str]
```
Messaggio `status`: `uptime_s`, `cameras` {cam: {frames, fps, proc_ms_avg, proc_ms_max, detections_avg,
last_frame_age_s}}, `errors` {where: n}, `system` {cpu_temp_c (da /sys/class/thermal se esiste),
load_avg, mem_available_mb (da /proc/meminfo), disk_free_mb}, `warnings` (es. "nessun frame da 30 s",
"FPS basso", "temperatura alta", "coda di invio lunga", "server non raggiungibile"), più le chiavi di `extra`.

### api.py (solo se config api.enabled)
```python
class LocalAPI:
    def __init__(self, cfg_api, node)    # node.storage, node.latest_scene, node.status_snapshot(), node.cfg
    def start(self) / stop(self)         # ThreadingHTTPServer in thread daemon
```
GET `/api/health`, `/api/status`, `/api/scene`, `/api/events?since=&kind=&limit=`, `/api/tracks?since=&limit=`,
`/api/stats?since=&limit=`, `/api/heatmap?camera=&class=`, `/api/config` (public_config). JSON, CORS `*`,
404 JSON per il resto. Su App Lab la porta va dichiarata in app.yaml (`ports: [8100]`).

### fusion.py (solo con piu' camere)
```python
class CameraFusion:
    def __init__(self, cfg, node_id)     # cfg["fusion"]: center_approach, max_gap_s
    def on_track(self, summary) -> list[dict]
```
Una traccia A (camera X) che finisce con `to == center_approach` si collega a una traccia B (camera Y != X,
stessa famiglia: persone/ciclisti oppure veicoli) che inizia con `from == center_approach` entro
`max_gap_s` dalla fine di A (scegli il gap minimo, ogni traccia collegata una sola volta). Le tracce
arrivano a fine traccia (B arriva dopo A). Messaggio:
`{"type":"movement","node","ts","class","from":"camX:<A.from>","to":"camY:<B.to>","tracks":[A.id,B.id],"gap_s"}`.
Le tracce in attesa scadono dopo max_gap_s + 30 s.

### leds.py
```python
class LedController:
    def __init__(self, cfg, bridge=None, start_thread=True)
        # cfg = config["leds"]: enabled, roles (lista di 4: local|remote|emergency|status|any|none), hold_s,
        #       min_severity, refresh_s, ttl_ms
        # bridge: oggetto con .call(name, *args) (arduino.app_utils.Bridge sulla board); None -> solo log
    def on_hazard(self, msg, local: bool, t: float) -> None   # action new accende, cancel spegne quell'id
    def tick(self, t: float, online: bool) -> None             # calcola i modi e li manda se cambiati o ogni refresh_s
    def state(self) -> dict                                    # {"modes":[...], "roles":[...], "active":[ids]}
    def close(self) -> None
```
Modi per LED: 0 spento, 1 acceso, 2 lampeggio lento (1 Hz), 3 lampeggio veloce (4 Hz).
- `local`: pericoli locali attivi con severity >= min_severity (attivo fino a cancel o per hold_s): sev 3 -> 3, sev 2 -> 1, sev 1 -> 2
- `remote`: remote_hazard ancora validi (expires) -> 2, se severity 3 -> 3
- `emergency`: kind emergency_vehicle (locale o remoto) attivo -> 3
- `status`: online -> 1, offline -> 2
- `any`: il massimo tra local e remote
Chiamata all'MCU: `bridge.call("set_leds", packed, ttl_ms)` con `packed = sum(mode_i << (2*i))`,
fatta in un thread separato (coda "solo l'ultimo valore"): se il Bridge si blocca il nodo non si ferma.

### node/sketch/sketch.ino
MCU della UNO Q (core Zephyr). `#include <Arduino_RouterBridge.h>`, `Bridge.begin()`,
`Bridge.provide("set_leds", set_leds)` con `void set_leds(int packed, int ttl_ms)` che salva solo i valori.
Nel `loop()` (senza delay lunghi, millis()) applica i modi ai pin `LED_PINS[4] = {2, 3, 4, 5}`.
Fail-safe: se non arriva un comando entro ttl_ms -> tutti spenti tranne il LED 4 che lampeggia ogni 2 s
("cervello offline"). All'avvio: test dei LED uno dopo l'altro (200 ms ciascuno).
Collegamento: pin -> resistenza -> anodo (gamba lunga) LED, catodo -> GND. Pin a 3,3 V, max ~8 mA.

### remote.py
```python
class RemoteLink:
    def __init__(self, cfg, node_id, on_message, start_thread=True)
        # cfg = config["server"]: url, token, poll_s, timeout_s. url vuoto -> disattivato (online False)
    def poll_once(self) -> int     # GET {url}/api/inbox/{node_id}?after={last_seq}; risposta {"messages":[...],"last":n}
                                   # chiama on_message(msg) per ognuno; aggiorna last_seq; mai eccezioni
    @property online -> bool       # True se l'ultima risposta OK e' piu' recente di 3*poll_s + timeout
    def close(self)
```

### peer.py — peer-to-peer tra incroci e verso i wearable, SENZA server  [fatto]
```python
class PeerLink:
    def __init__(self, cfg, node_id, on_message, neighbors=(), start_thread=True)   # cfg = config["p2p"]
    def send_hazard(self, msg) -> bool        # hazard locali con propagate o categoria emergency (+ allerte locali per i wearable)
    def forward_remote(self, msg) -> bool     # ripete via radio i pericoli arrivati dal server (per i wearable vicini)
    def poll_once(self, timeout=0.0) -> int
    def peers(self) / vrus(self) / state(self); online; close()
```
Protocollo "SC-P2P v1": un oggetto JSON compatto <= 240 byte (sta in un frame ESP-NOW), chiavi corte
(`t` hz/hb/vru, `id`, `n` nodo, `a` n/c, `k` kind, `c` categoria, `s` severita', `cc` causeCode, `ts`, `e`
scadenza, `tw` verso, `nx` incroci destinatari, `tg` destinatari, `h` salti rimasti, `sig` HMAC-SHA256
troncato a 16 hex se c'e' `p2p.key`). Trasporti: UDP multicast 239.255.42.99:47999 (+ elenco unicast
`p2p.peers` per reti che bloccano il multicast) ed ESP-NOW tramite un ESP32 "gateway" in USB
(`p2p.serial.port`, una riga JSON per messaggio). Deduplica per (id, azione), inoltro con h-1 (max 2 salti),
pertinenza: `nx` contiene questo nodo, oppure niente `nx` e mittente tra i vicini. Il nodo deduplica anche
tra server e p2p (stesso pericolo arrivato due volte = gestito una volta).
Firmware in `firmware/`: `esp32_gateway` (seriale <-> ESP-NOW) ed `esp32_wearable` (braccialetto che vibra
per i pericoli che riguardano il suo profilo e manda un beacon "vru" di presenza ogni 2 s). Vedi firmware/README.md.

### web/app.html — pagina per il telefono  [fatto]
Servita dall'API locale su `GET /app` (serve `api.enabled`). Scelta del profilo (ciclista/pedone/conducente),
polling di `/api/events`, banner colorato e vibrazione (Android) per i pericoli pertinenti, cancel = banner via.

### Strumenti sul PC (node/tools)  [fatto]
`test_suite.py` (83 controlli: regole, riepiloghi, robustezza, config, nodo+server+LED, p2p, API, fusione),
`simulate.py`, `replay.py` (nodo completo su un video, `--server` per la dashboard), `zone_editor.py`,
`export_data.py` (archivio -> CSV/JSON), `heatmap.py` (mappa di calore PNG).

### server/ (sul PC)  [fatto]
`server/server.py` (stdlib: http.server ThreadingHTTPServer + sqlite3), avvio:
`python server.py --port 8000 --topology topology.json --db server.db`
- `POST /api/events` — un messaggio o un array. Salva tutto. Per ogni `hazard` con `propagate`:
  destinatari = `next_nodes` se presenti, altrimenti il vicino di quel nodo nella direzione
  `actor.towards` (topology), altrimenti (se `forward_unknown_direction`) tutti i vicini.
  Mette in inbox di ciascuno un `remote_hazard`: `{"type":"remote_hazard","seq","id","kind","category",
  "severity","action","from_node","expires","ts","targets","original"}`. Anche i `cancel` vengono inoltrati.
- `GET /api/inbox/<node>?after=<seq>` — messaggi con seq > after (max 100), `{"messages":[...],"last":seq}`.
  Ogni chiamata aggiorna last_seen del nodo (vale come heartbeat).
- `GET /api/nodes` — nodi della topologia + nodi visti: last_seen, online, ultimo status, contatori.
- `GET /api/events?after=&limit=&node=&type=` — ultimi messaggi ricevuti (per la dashboard).
- `POST /api/simulate` `{"from_node","to_node","kind","severity","towards"}` — crea un pericolo come se lo
  avesse visto from_node e lo consegna a to_node (demo con un solo dispositivo).
- `GET /` — dashboard HTML/JS senza librerie esterne (funziona offline): nodi online/offline, flusso
  eventi in tempo reale (polling 1 s), contatori per tipo, ultimo stato/statistiche del nodo,
  pulsanti "simula pericolo da incrocio vicino".
- Token (`SC_TOKEN` nel `.env`, oppure `--token`): se impostato, richiesto come `Authorization: Bearer` per
  tutti i POST e per `/api/inbox/*`; pagine e letture della dashboard restano pubbliche. Il nodo lo legge
  dal `.env` accanto a `config.json` se `server.token` è vuoto (lo stesso per `SC_P2P_KEY` -> `p2p.key`).
- `topology.json`: `{"forward_unknown_direction": true, "nodes": {"incrocio-1": {"name": "...",
  "neighbors": {"via": "incrocio-2", "incrocio": "incrocio-3"}}, "incrocio-2": {...}}}`
