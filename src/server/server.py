#!/usr/bin/env python3
"""Smart Crossing - server centrale (gira sul PC).

  board incrocio-1 --POST /api/events-->  server  --casella di incrocio-2-->  incrocio-2
                   <--GET /api/inbox/incrocio-1-- (pericoli dei vicini, ogni secondo)

- Salva in SQLite tutto quello che arriva dai nodi (pericoli, tracce, statistiche, stato).
- Inoltra i pericoli con "propagate" SOLO dove serve: next_nodes del messaggio, altrimenti
  l'incrocio verso cui va il veicolo (actor.towards -> neighbors in topology.json), altrimenti
  (forward_unknown_direction) tutti i vicini. Anche la fine del pericolo (cancel) viene inoltrata.
- Pagine: /           dashboard con la mappa di Torino (templates/index.html)
          /cyclist    assistente per il ciclista (templates/cyclist.html)
          /app        pagina per il telefono con vibrazione (src/node/python/web/app.html)
          /console    dashboard tecnica senza librerie esterne (funziona anche senza internet)
  "Simula pericolo": con un solo dispositivo si finge che un incrocio vicino abbia visto qualcosa
  e si guarda la board accendere i LED.
- /api/traffic resta compatibile con il client.py di simulazione dei 12 nodi.
- Token (SC_TOKEN nel file .env, vedi .env.example): serve per tutto cio' che SCRIVE (POST dei nodi,
  /api/traffic, /api/simulate) e per la casella dei pericoli dei nodi. Pagine e letture della
  dashboard restano pubbliche, in sola lettura.

Solo libreria standard:   python server.py --port 8000 --topology topology.json --db server.db
Su un hosting Flask (es. PythonAnywhere) lo stesso server gira tramite flask_app.py.
"""
import argparse
import copy
import hmac
import importlib.util
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
PHONE_PAGE = HERE.parent / "node" / "python" / "web" / "app.html"  # la stessa pagina che serve il nodo
TEMPLATES, STATIC = HERE / "templates", HERE / "static"
PAGES = {"/": TEMPLATES / "index.html", "/index.html": TEMPLATES / "index.html", "/dashboard": TEMPLATES / "index.html",
         "/cyclist": TEMPLATES / "cyclist.html",
         "/console": HERE / "console.html"}  # console = dashboard tecnica che funziona anche senza internet
CTYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
          ".html": "text/html; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
          ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}
SCENE_FRESH_MS = 5000          # la scena di un nodo vale come "adesso" per 5 s
CLASS_TO_COUNT = {"person": "pedestrians", "cyclist": "cyclists", "emergency": "ambulances", "vehicle": "vehicles"}
MAX_BODY = 5_000_000          # byte per richiesta
INBOX_MAX = 100               # messaggi per lettura della casella
CLOCK_TOLERANCE_MS = 60_000   # oltre: l'orologio del nodo e' sbagliato, uso l'ora di arrivo
CANCEL_KEEP_MS = 60_000       # per quanto un "fine pericolo" resta utile a chi si ricollega
ACTIVE_WINDOW_MS = 6 * 3600 * 1000

KIND_LABELS = {
    "emergency_vehicle": "Mezzo di soccorso", "speeding": "Eccesso di velocita'",
    "sudden_acceleration": "Accelerazione brusca", "harsh_braking": "Frenata brusca",
    "solid_line_crossing": "Linea continua superata", "wrong_way": "Contromano",
    "u_turn": "Inversione a U", "forbidden_movement": "Manovra vietata",
    "bike_lane_invasion": "Invasione pista ciclabile", "bike_lane_hotspot": "Ciclabile spesso invasa",
    "illegal_stop": "Sosta vietata", "congestion": "Coda / traffico",
    "pedestrian_not_yielded": "Precedenza negata al pedone", "near_miss": "Quasi incidente",
    "collision_risk": "Rischio collisione", "close_pass": "Sorpasso troppo vicino al ciclista",
    "pedestrian_outside_crosswalk": "Pedone fuori dalle strisce", "slow_pedestrian": "Pedone lento sulle strisce",
    "pedestrian_waiting": "Pedone in attesa", "crowd_on_crosswalk": "Folla sulle strisce",
    "cyclist_on_sidewalk": "Bici sul marciapiede", "red_light_violation": "Passaggio col rosso",
    "late_yellow": "Giallo bruciato", "pedestrian_red_crossing": "Pedone col rosso",
}

# Copia ridotta di events.KINDS (kind: categoria, validita' s, inoltro), se events.py non si legge
_RISERVA = {
    "emergency_vehicle": ("emergency", 60, True), "speeding": ("violation", 30, True),
    "sudden_acceleration": ("risk", 20, True), "harsh_braking": ("risk", 15, False),
    "solid_line_crossing": ("violation", 30, True), "wrong_way": ("violation", 60, True),
    "u_turn": ("violation", 30, True), "forbidden_movement": ("violation", 30, True),
    "bike_lane_invasion": ("violation", 30, True), "bike_lane_hotspot": ("info", 600, True),
    "illegal_stop": ("violation", 120, False), "congestion": ("info", 120, True),
    "pedestrian_not_yielded": ("violation", 15, False), "near_miss": ("risk", 20, True),
    "collision_risk": ("risk", 10, False), "close_pass": ("violation", 20, True),
    "pedestrian_outside_crosswalk": ("risk", 15, False), "slow_pedestrian": ("risk", 15, False),
    "pedestrian_waiting": ("info", 60, False), "crowd_on_crosswalk": ("info", 30, False),
    "cyclist_on_sidewalk": ("violation", 20, False), "red_light_violation": ("violation", 30, True),
    "late_yellow": ("risk", 15, False), "pedestrian_red_crossing": ("risk", 15, False),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, node TEXT,
    type TEXT, kind TEXT, action TEXT, hid TEXT, simulated INTEGER DEFAULT 0, json TEXT);
CREATE INDEX IF NOT EXISTS messages_type ON messages(type, ts);
CREATE INDEX IF NOT EXISTS messages_hid ON messages(hid);
CREATE TABLE IF NOT EXISTS inbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT, ts INTEGER,
    exp INTEGER, from_node TEXT, kind TEXT, action TEXT, hid TEXT, reason TEXT,
    simulated INTEGER DEFAULT 0, json TEXT);
CREATE INDEX IF NOT EXISTS inbox_node ON inbox(node, seq);
CREATE INDEX IF NOT EXISTS inbox_hid ON inbox(hid);
CREATE TABLE IF NOT EXISTS nodes (node TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER,
    last_poll INTEGER, acked INTEGER DEFAULT 0, ip TEXT, status TEXT, stats TEXT);
"""


def now_ms():
    return int(time.time() * 1000)


def log(testo):
    print(time.strftime("%H:%M:%S"), testo, flush=True)


def load_env(start=HERE, levels=3):
    """Legge il primo .env trovato in start o nelle cartelle sopra (righe CHIAVE=valore, # commenti).
    Non sovrascrive le variabili d'ambiente gia' impostate. Ritorna il percorso letto o None."""
    start = Path(start)
    for d in [start, *list(start.parents)[:levels]]:
        p = d / ".env"
        if not p.is_file():
            continue
        for riga in p.read_text(encoding="utf-8-sig").splitlines():
            riga = riga.strip()
            if not riga or riga.startswith("#") or "=" not in riga:
                continue
            k, v = riga.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k:
                os.environ.setdefault(k, v)
        return p
    return None


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def load_kinds():
    """Stesso catalogo del nodo (node/python/events.py); se manca, la copia ridotta qui sopra."""
    path = HERE.parent / "node" / "python" / "events.py"
    try:
        spec = importlib.util.spec_from_file_location("sc_events", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = {}
        for k, (cause, cause_txt, targets, propagate, validity, norma, cat) in mod.KINDS.items():
            out[k] = {"category": cat, "validity_s": validity, "propagate": propagate,
                      "targets": list(targets), "cause": cause, "cause_txt": cause_txt, "norma": norma}
        return out
    except Exception as e:
        print(f"[avviso] catalogo eventi non letto da {path} ({e!r}): uso quello interno")
        return {k: {"category": c, "validity_s": v, "propagate": p, "targets": ["person", "cyclist", "vehicle"],
                    "cause": 99, "cause_txt": "dangerousSituation", "norma": "-"}
                for k, (c, v, p) in _RISERVA.items()}


def _coords(tn):
    lat, lon = tn.get("lat"), tn.get("lon")
    return {"lat": lat, "lon": lon} if _is_num(lat) and _is_num(lon) else None


def _status_brief(st):
    """Le voci dello stato del nodo che servono alla mappa."""
    if not isinstance(st, dict):
        return None
    cams = [c for c in (st.get("cameras") or {}).values() if isinstance(c, dict)]
    fps = [c["fps"] for c in cams if _is_num(c.get("fps"))]
    return {"fps": round(sum(fps) / len(fps), 1) if fps else None,
            "cpu_temp_c": (st.get("system") or {}).get("cpu_temp_c"),
            "active_tracks": st.get("active_tracks"), "warnings": st.get("warnings") or [],
            "server_online": st.get("server_online"), "p2p_online": (st.get("p2p") or {}).get("online"),
            "leds": st.get("leds"), "uptime_s": st.get("uptime_s"), "outbox": st.get("outbox")}


def actor_class(kind):
    if kind == "emergency_vehicle":
        return "emergency"
    if kind == "cyclist_on_sidewalk":
        return "cyclist"
    if kind.startswith("pedestrian") or kind in ("slow_pedestrian", "crowd_on_crosswalk"):
        return "person"
    return "vehicle"


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------------- topologia

class Topology:
    """topology.json, ricaricato da solo quando il file cambia (si puo' modificare a server acceso)."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"forward_unknown_direction": True, "nodes": {}}
        self._mtime = "mai"
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            try:
                mt = self.path.stat().st_mtime
            except OSError:
                mt = None
            if mt != self._mtime:
                self._mtime = mt
                nuovo = self._load(mt is not None)
                if nuovo is not None:
                    self.data = nuovo
            return self.data

    def _load(self, esiste):
        if not esiste:
            print(f"[topologia] {self.path} non trovato: nessun vicino (inoltro solo con next_nodes o dalla dashboard)")
            return {"forward_unknown_direction": True, "nodes": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            print(f"[topologia] {self.path} non valido ({e}): tengo la versione precedente")
            return None
        nodes = {}
        for nid, n in ((raw or {}).get("nodes") or {}).items():
            n = n if isinstance(n, dict) else {}
            nb = n.get("neighbors") if isinstance(n.get("neighbors"), dict) else {}
            nodes[str(nid)] = {**n, "neighbors": {str(k): (str(v) if v else None) for k, v in nb.items()}}
        for nid, n in nodes.items():
            for via, dest in n["neighbors"].items():
                if dest and dest not in nodes:
                    print(f"[topologia] {nid}.neighbors.{via} -> '{dest}' non e' tra i nodes (va bene solo se e' una board vera)")
        data = {"forward_unknown_direction": bool(raw.get("forward_unknown_direction", True)), "nodes": nodes}
        print(f"[topologia] {len(nodes)} incroci: {', '.join(nodes) or '-'}")
        return data


# ---------------------------------------------------------------------- logica

class Hub:
    def __init__(self, db_path, topology, offline_after_s=15.0, wal=True):
        self.topology = topology
        self.offline_ms = int(offline_after_s * 1000)
        self.kinds = load_kinds()
        self.started = now_ms()
        self.lock = threading.RLock()
        self.scenes = {}   # nodo -> (ricevuta ms, ultima scena): 1 al secondo, solo in memoria
        self.legacy = {}   # nodi del client.py di simulazione (POST /api/traffic): solo in memoria
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            # WAL e' piu' veloce sul PC; sui dischi di rete degli hosting (PythonAnywhere) si usa il modo classico
            self.db.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # ---------------------------------------------------------- ricezione

    def receive(self, payload, ip=None):
        """POST /api/events: un messaggio o un array di messaggi."""
        items = payload if isinstance(payload, list) else [payload]
        res = {"ok": True, "stored": 0, "duplicates": 0, "forwarded": 0, "skipped": 0}
        for m in items:
            if not isinstance(m, dict) or not m.get("type"):
                res["skipped"] += 1
                continue
            r = self._ingest(m, ip=ip)
            res["duplicates" if r["duplicate"] else "stored"] += 1
            res["forwarded"] += len(r["forwarded"])
        return res

    def _ingest(self, m, ip=None, simulated=False, to_node=None):
        rx = now_ms()
        node = str(m.get("node") or (f"ip:{ip}" if ip else "sconosciuto"))
        typ = str(m.get("type"))
        kind = m.get("kind") if isinstance(m.get("kind"), str) else None
        action = (m.get("action") or "new") if typ == "hazard" else None
        hid = str(m["id"]) if typ == "hazard" and m.get("id") is not None else None
        if typ == "scene":  # arriva ogni secondo: tengo solo l'ultima per nodo, fuori dal database
            with self.lock:
                self.scenes[node] = (rx, m)
                self._touch(node, rx, ip)
                self.db.commit()
            return {"duplicate": False, "forwarded": []}
        with self.lock:
            # stesso pericolo rispedito (il nodo ritenta se la risposta si perde): non lo conto due volte
            if hid and self.db.execute("SELECT 1 FROM messages WHERE hid=? AND node=? AND action=? LIMIT 1",
                                       (hid, node, action)).fetchone():
                if not simulated:
                    self._touch(node, rx, ip)
                self.db.commit()
                return {"duplicate": True, "forwarded": []}
            self.db.execute(
                "INSERT INTO messages(ts, node, type, kind, action, hid, simulated, json) VALUES (?,?,?,?,?,?,?,?)",
                (rx, node, typ, kind, action, hid, int(simulated), json.dumps(m, ensure_ascii=False)))
            if simulated:
                self.db.execute("INSERT OR IGNORE INTO nodes(node, first_seen) VALUES (?, ?)", (node, rx))
            else:
                self._touch(node, rx, ip, status=m if typ == "status" else None, stats=m if typ == "stats" else None)
            self.db.commit()
            forwarded = []
            if typ == "hazard":
                self._print_hazard(node, m, action, simulated)
                forwarded = self._forward(m, node, rx, hid, action, to_node, simulated)
            return {"duplicate": False, "forwarded": forwarded}

    def _touch(self, node, t, ip=None, poll=False, acked=None, status=None, stats=None):
        self.db.execute("INSERT OR IGNORE INTO nodes(node, first_seen) VALUES (?, ?)", (node, t))
        self.db.execute("UPDATE nodes SET last_seen=?, ip=COALESCE(?, ip) WHERE node=?", (t, ip, node))
        if poll:
            self.db.execute("UPDATE nodes SET last_poll=?, acked=? WHERE node=?", (t, acked or 0, node))
        if status is not None:
            self.db.execute("UPDATE nodes SET status=? WHERE node=?", (json.dumps(status, ensure_ascii=False), node))
        if stats is not None:
            self.db.execute("UPDATE nodes SET stats=? WHERE node=?", (json.dumps(stats, ensure_ascii=False), node))

    def _print_hazard(self, node, m, action, simulated):
        kind = m.get("kind")
        label = KIND_LABELS.get(kind, kind)
        sim = " [simulato]" if simulated else ""
        if action == "cancel":
            log(f"FINE     {node}: {kind} ({label}) terminato{sim}")
            return
        a = m.get("actor") if isinstance(m.get("actor"), dict) else {}
        extra = f" {a.get('class')}" if a.get("class") else ""
        if a.get("track") not in (None, 0):
            extra += f" traccia {a.get('track')}"
        extra += f" verso '{a.get('towards')}'" if a.get("towards") else " direzione ?"
        log(f"PERICOLO {node}: {kind} ({label}) sev={m.get('severity')}{extra}{sim}")

    # ---------------------------------------------------------- inoltro

    def _route(self, m, src):
        """Destinatari [(nodo, motivo)]: next_nodes, poi actor.towards in topologia, poi tutti i vicini."""
        topo = self.topology.get()
        nn = [n for n in (m.get("next_nodes") or []) if isinstance(n, str) and n and n != src]
        if nn:
            return [(n, "next_nodes del nodo") for n in dict.fromkeys(nn)]
        neighbors = (topo["nodes"].get(src) or {}).get("neighbors") or {}
        actor = m.get("actor") if isinstance(m.get("actor"), dict) else {}
        towards = actor.get("towards")
        if towards and towards in neighbors:
            if neighbors[towards] and neighbors[towards] != src:
                return [(neighbors[towards], f"va verso '{towards}'")]
            log(f"   nessun inoltro: da '{towards}' si esce dalla rete di incroci")
            return []
        if not topo.get("forward_unknown_direction", True):
            log("   nessun inoltro: direzione sconosciuta (forward_unknown_direction = false)")
            return []
        tutti = [d for d in dict.fromkeys(neighbors.values()) if d and d != src]
        if not tutti:
            log(f"   nessun inoltro: {src} non ha vicini in topology.json")
        motivo = f"via '{towards}' non in topologia: tutti i vicini" if towards else "direzione sconosciuta: tutti i vicini"
        return [(d, motivo) for d in tutti]

    def _server_times(self, m, rx):
        """ts/expires del pericolo; se l'orologio del nodo e' sbagliato li riporto all'ora del server."""
        ts, exp = m.get("ts"), m.get("expires")
        ts = ts if _is_num(ts) else rx
        if not _is_num(exp):
            exp = ts + self.kinds.get(m.get("kind"), {}).get("validity_s", 30) * 1000
        if abs(rx - ts) > CLOCK_TOLERANCE_MS:
            exp, ts = rx + (exp - ts), rx
        return int(ts), int(exp)

    def _forward(self, m, src, rx, hid, action, to_node, simulated):
        kind = m.get("kind")
        if action == "cancel":
            # la fine del pericolo va a chi aveva ricevuto l'inizio
            prev = [r[0] for r in self.db.execute(
                "SELECT DISTINCT node FROM inbox WHERE hid=? AND from_node=? AND action='new'", (hid, src))]
            if prev:
                dests = [(d, "fine del pericolo") for d in prev]
            elif to_node:
                dests = [(to_node, "scelto dalla dashboard")]
            elif m.get("propagate"):
                dests = self._route(m, src)
            else:
                return []
        elif to_node:
            dests = [(to_node, "scelto dalla dashboard")]
        elif m.get("propagate"):
            dests = self._route(m, src)
        else:
            return []

        ts, exp = self._server_times(m, rx)
        if action != "cancel" and exp < rx:
            log(f"   non inoltrato: {kind} di {src} e' gia' scaduto")
            return []
        topo_nodes = self.topology.get()["nodes"]
        actor = m.get("actor") if isinstance(m.get("actor"), dict) else {}
        out = []
        for dest, reason in dests:
            # da quale via del destinatario arrivera' il veicolo (la via che porta a src)
            nb = (topo_nodes.get(dest) or {}).get("neighbors") or {}
            arrives_from = next((via for via, n in nb.items() if n == src), None)
            rh = {"v": 1, "type": "remote_hazard", "node": dest, "id": hid, "kind": kind,
                  "category": m.get("category") or self.kinds.get(kind, {}).get("category", "risk"),
                  "severity": m.get("severity", 2), "action": action, "from_node": src,
                  "ts": ts, "expires": exp, "targets": m.get("targets") or [],
                  "towards": actor.get("towards"), "arrives_from": arrives_from, "reason": reason,
                  "original": m}
            if simulated:
                rh["simulated"] = True
            keep = max(exp, rx) + CANCEL_KEEP_MS if action == "cancel" else exp
            cur = self.db.execute(
                "INSERT INTO inbox(node, ts, exp, from_node, kind, action, hid, reason, simulated, json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (dest, rx, keep, src, kind, action, hid, reason, int(simulated), json.dumps(rh, ensure_ascii=False)))
            self.db.commit()
            out.append({"node": dest, "seq": cur.lastrowid, "reason": reason})
            cosa = f"fine {kind}" if action == "cancel" else kind
            log(f"   inoltro {cosa} da {src} a {dest} ({reason})")
        return out

    # ---------------------------------------------------------- casella dei nodi

    def inbox(self, node, after, ip=None):
        """GET /api/inbox/<node>?after=<seq>. Vale anche come heartbeat del nodo."""
        t = now_ms()
        with self.lock:
            top_all = self.db.execute("SELECT MAX(seq) FROM inbox").fetchone()[0] or 0
            # after=0: nodo appena acceso; after oltre il massimo: database del server nuovo.
            # In entrambi i casi niente messaggi gia' scaduti (non riaccendere LED di ieri).
            fresh = after <= 0 or after > top_all
            if fresh:
                rows = self.db.execute("SELECT seq, json FROM inbox WHERE node=? AND exp>=? ORDER BY seq LIMIT ?",
                                       (node, t, INBOX_MAX + 1)).fetchall()
            else:
                rows = self.db.execute("SELECT seq, json FROM inbox WHERE node=? AND seq>? ORDER BY seq LIMIT ?",
                                       (node, after, INBOX_MAX + 1)).fetchall()
            more = len(rows) > INBOX_MAX
            rows = rows[:INBOX_MAX]
            if more:
                last = rows[-1]["seq"]
            else:
                top_node = self.db.execute("SELECT MAX(seq) FROM inbox WHERE node=?", (node,)).fetchone()[0] or 0
                last = top_node if fresh else max(after, top_node)
            self._touch(node, t, ip, poll=True, acked=0 if fresh else after)
            self.db.commit()
        msgs = []
        for r in rows:
            m = json.loads(r["json"])
            m["seq"] = r["seq"]
            msgs.append(m)
        return {"node": node, "messages": msgs, "last": last, "more": more, "server_ts": t}

    # ---------------------------------------------------------- dashboard

    def nodes(self):
        t = now_ms()
        topo = self.topology.get()
        with self.lock:
            rows = {r["node"]: dict(r) for r in self.db.execute("SELECT * FROM nodes")}
            counts = {}
            for r in self.db.execute("SELECT node, type, COUNT(*) FROM messages WHERE simulated=0 GROUP BY node, type"):
                counts.setdefault(r[0], {})[r[1]] = r[2]
            inbox = {r[0]: {"total": r[1], "last_seq": r[2]}
                     for r in self.db.execute("SELECT node, COUNT(*), MAX(seq) FROM inbox GROUP BY node")}
            for nid, info in inbox.items():
                acked = (rows.get(nid) or {}).get("acked") or 0
                info["acked"] = acked
                info["pending"] = self.db.execute("SELECT COUNT(*) FROM inbox WHERE node=? AND seq>?",
                                                  (nid, acked)).fetchone()[0]
            recent = self.db.execute("SELECT seq, node, action, hid, exp, json FROM inbox WHERE ts>=? ORDER BY seq",
                                     (t - ACTIVE_WINDOW_MS,)).fetchall()
        # pericoli in arrivo ancora validi per ogni nodo (l'ultimo messaggio per id decide)
        ultimo = {}
        for r in recent:
            ultimo[(r["node"], r["hid"])] = r
        active = {}
        for (nid, _), r in ultimo.items():
            if r["action"] == "new" and r["exp"] >= t:
                m = json.loads(r["json"])
                a = {k: m.get(k) for k in ("id", "kind", "category", "severity", "from_node", "towards",
                                           "arrives_from", "expires", "ts", "simulated")}
                a["seq"] = r["seq"]
                active.setdefault(nid, []).append(a)
        ids = list(topo["nodes"]) + sorted(n for n in rows if n not in topo["nodes"])
        out = []
        for nid in ids:
            tn = topo["nodes"].get(nid) or {}
            r = rows.get(nid) or {}
            last_seen, last_poll = r.get("last_seen"), r.get("last_poll")
            out.append({
                "id": nid, "name": tn.get("name") or nid, "virtual": bool(tn.get("virtual")),
                "in_topology": nid in topo["nodes"], "neighbors": tn.get("neighbors") or {}, "pos": tn.get("pos"),
                "first_seen": r.get("first_seen"), "last_seen": last_seen, "ip": r.get("ip"),
                "online": bool(last_seen) and t - last_seen <= self.offline_ms,
                "polling": bool(last_poll) and t - last_poll <= self.offline_ms,
                "status": json.loads(r["status"]) if r.get("status") else None,
                "stats": json.loads(r["stats"]) if r.get("stats") else None,
                "counts": counts.get(nid, {}),
                "inbox": inbox.get(nid, {"total": 0, "last_seq": 0, "acked": r.get("acked") or 0, "pending": 0}),
                "active_remote": sorted(active.get(nid, []), key=lambda a: a["seq"]),
            })
        return {"nodes": out, "server_ts": t, "offline_after_s": self.offline_ms / 1000,
                "forward_unknown_direction": topo.get("forward_unknown_direction", True)}

    def events(self, after=0, limit=100, node=None, types=None, exclude=None):
        """GET /api/events: ultimi messaggi ricevuti (after=0) o quelli dopo il seq 'after'."""
        where, args = [], []
        if node:
            where.append("node=?")
            args.append(node)
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            args += types
        if exclude:
            where.append(f"type NOT IN ({','.join('?' * len(exclude))})")
            args += exclude
        cond = (" AND " + " AND ".join(where)) if where else ""
        with self.lock:
            top = self.db.execute("SELECT MAX(seq) FROM messages").fetchone()[0] or 0
            if after > top:  # database del server nuovo: si riparte dagli ultimi messaggi
                after = 0
            if after > 0:
                rows = self.db.execute(f"SELECT * FROM messages WHERE seq>?{cond} ORDER BY seq LIMIT ?",
                                       [after] + args + [limit]).fetchall()
            else:
                rows = self.db.execute(f"SELECT * FROM messages WHERE 1=1{cond} ORDER BY seq DESC LIMIT ?",
                                       args + [limit]).fetchall()[::-1]
            out = []
            for r in rows:
                item = {"seq": r["seq"], "rx": r["ts"], "node": r["node"], "type": r["type"], "kind": r["kind"],
                        "action": r["action"], "simulated": bool(r["simulated"]), "msg": json.loads(r["json"])}
                if r["type"] == "hazard" and r["hid"]:
                    item["forwarded_to"] = [x[0] for x in self.db.execute(
                        "SELECT node FROM inbox WHERE hid=? AND from_node=? AND action=? ORDER BY seq",
                        (r["hid"], r["node"], r["action"]))]
                out.append(item)
        # pagina incompleta: niente altro da leggere fino a 'top' (salta anche i tipi filtrati)
        last = out[-1]["seq"] if out else after
        if len(out) < limit:
            last = max(last, top)
        return {"messages": out, "last": last, "server_ts": now_ms()}

    def summary(self, since=None):
        """Contatori dall'avvio del server (o da 'since') e pericoli attivi in questo momento."""
        t = now_ms()
        since = since or self.started
        with self.lock:
            kinds, kinds_sim = {}, {}
            for r in self.db.execute("SELECT kind, simulated, COUNT(*) FROM messages WHERE type='hazard' "
                                     "AND action='new' AND ts>=? GROUP BY kind, simulated", (since,)):
                (kinds_sim if r[1] else kinds)[r[0]] = r[2]
            by_type = {r[0]: r[1] for r in self.db.execute(
                "SELECT type, COUNT(*) FROM messages WHERE ts>=? GROUP BY type", (since,))}
            fwd = self.db.execute("SELECT COUNT(*) FROM inbox WHERE action='new' AND ts>=?", (since,)).fetchone()[0]
            hz = self.db.execute("SELECT ts, node, hid, action, simulated, json FROM messages "
                                 "WHERE type='hazard' AND ts>=? ORDER BY seq", (t - ACTIVE_WINDOW_MS,)).fetchall()
            sent = {}
            for r in self.db.execute("SELECT hid, from_node, node FROM inbox WHERE action='new' AND ts>=?",
                                     (t - ACTIVE_WINDOW_MS,)):
                sent.setdefault((r[1], r[0]), []).append(r[2])
        ultimo = {}
        for r in hz:
            ultimo[(r["node"], r["hid"])] = r
        active = []
        for (nid, hid), r in ultimo.items():
            if r["action"] != "new":
                continue
            m = json.loads(r["json"])
            ts, exp = self._server_times(m, r["ts"])
            if exp < t:
                continue
            a = m.get("actor") if isinstance(m.get("actor"), dict) else {}
            active.append({"node": nid, "id": hid, "kind": m.get("kind"), "category": m.get("category"),
                           "severity": m.get("severity"), "simulated": bool(r["simulated"]), "rx": r["ts"],
                           "expires": exp, "expires_in_s": round((exp - t) / 1000), "towards": a.get("towards"),
                           "forwarded_to": sent.get((nid, hid), [])})
        active.sort(key=lambda a: a["rx"], reverse=True)
        return {"since": since, "server_ts": t, "counts": kinds, "counts_simulated": kinds_sim,
                "by_type": by_type, "forwards": fwd, "active": active}

    def forwards(self, after=0, limit=30):
        with self.lock:
            q = ("SELECT i.seq, i.ts, i.node, i.from_node, i.kind, i.action, i.reason, i.simulated, i.json, "
                 "n.acked, n.last_poll FROM inbox i LEFT JOIN nodes n ON n.node = i.node")
            if after > 0:
                rows = self.db.execute(q + " WHERE i.seq>? ORDER BY i.seq LIMIT ?", (after, limit)).fetchall()
            else:
                rows = self.db.execute(q + " ORDER BY i.seq DESC LIMIT ?", (limit,)).fetchall()[::-1]
        out = []
        for r in rows:
            m = json.loads(r["json"])
            out.append({"seq": r["seq"], "ts": r["ts"], "to_node": r["node"], "from_node": r["from_node"],
                        "kind": r["kind"], "action": r["action"], "reason": r["reason"],
                        "simulated": bool(r["simulated"]), "severity": m.get("severity"),
                        "category": m.get("category"), "arrives_from": m.get("arrives_from"),
                        "delivered": bool(r["acked"]) and r["seq"] <= r["acked"]})
        return {"forwards": out, "last": out[-1]["seq"] if out else after, "server_ts": now_ms()}

    def kinds_list(self):
        return {"kinds": [{"kind": k, "label": KIND_LABELS.get(k, k), **v} for k, v in self.kinds.items()]}

    # ---------------------------------------------------------- mappa (formato della dashboard del team)

    def traffic(self):
        """GET /api/traffic: un elemento per incrocio, nel formato della mappa e della pagina ciclista
        ({node_id, coordinates {lat, lon}, counts {pedestrians, cyclists, ambulances, vehicles}}) piu' stato,
        pericoli attivi e pericoli in arrivo. Ci sono anche i nodi simulati dal client.py."""
        t = now_ms()
        topo = self.topology.get()["nodes"]
        attivi = {}
        for a in self.summary()["active"]:
            attivi.setdefault(a["node"], []).append(a)
        out = []
        for n in self.nodes()["nodes"]:
            counts, window = self._counts(n["id"], n.get("stats"), t)
            out.append({
                "node_id": n["id"], "name": n["name"], "source": "virtual" if n["virtual"] else "board",
                "coordinates": _coords(topo.get(n["id"]) or {}), "counts": counts, "counts_window": window,
                "online": n["online"], "last_seen": n["last_seen"], "neighbors": n["neighbors"],
                "status": _status_brief(n.get("status")), "active_hazards": attivi.get(n["id"], []),
                "active_remote": n["active_remote"]})
        with self.lock:
            legacy = [dict(v) for v in self.legacy.values()]
        for v in legacy:
            rx = v.pop("_rx", 0)
            out.append({**v, "name": v["node_id"], "source": "client", "online": t - rx <= self.offline_ms,
                        "last_seen": rx, "counts_window": "live", "neighbors": {}, "status": None,
                        "active_hazards": [], "active_remote": []})
        return out

    def _counts(self, node, stats, t):
        """Oggetti visti adesso (ultima scena) o, se la scena e' vecchia, nell'ultimo periodo di statistiche."""
        base = {"pedestrians": 0, "cyclists": 0, "ambulances": 0, "vehicles": 0}
        with self.lock:
            sc = self.scenes.get(node)
        if sc and t - sc[0] <= SCENE_FRESH_MS:
            for o in sc[1].get("objects") or []:
                k = CLASS_TO_COUNT.get(o.get("class")) if isinstance(o, dict) else None
                if k:
                    base[k] += 1
            return base, "live"
        if isinstance(stats, dict):
            for cam in (stats.get("cameras") or {}).values():
                for cls, v in ((cam or {}).get("counts") or {}).items():
                    k = CLASS_TO_COUNT.get(cls)
                    if k and _is_num(v):
                        base[k] += int(v)
            return base, "last_period"
        return base, "none"

    def receive_traffic(self, data):
        """POST /api/traffic: formato del client.py (conteggi per nodo con coordinate)."""
        if not isinstance(data, dict):
            raise BadRequest("Invalid JSON or missing payload")
        node_id, counts = data.get("node_id"), data.get("counts")
        if not node_id or not isinstance(counts, dict):
            raise BadRequest("Missing required fields (node_id or counts)")
        with self.lock:
            rec = self.legacy.get(str(node_id)) or {"node_id": str(node_id), "coordinates": None, "counts": {}}
            if isinstance(data.get("coordinates"), dict):
                rec["coordinates"] = data["coordinates"]
            rec["counts"] = counts
            rec["_rx"] = now_ms()
            self.legacy[str(node_id)] = rec
            n = len(self.legacy)
        return {"status": "success", "message": "Data recorded successfully", "node_id": str(node_id),
                "total_active_nodes": n}

    # ---------------------------------------------------------- simulazione

    def simulate(self, body):
        """POST /api/simulate: pericolo visto da from_node, consegnato a to_node (o secondo la direzione)."""
        if not isinstance(body, dict):
            raise BadRequest("serve un oggetto JSON")
        if body.get("action") == "cancel":
            return self.cancel_simulated(body.get("id"))
        src = str(body.get("from_node") or "").strip()
        dst = str(body.get("to_node") or "").strip() or None
        kind = str(body.get("kind") or "bike_lane_invasion")
        towards = str(body.get("towards") or "").strip() or None
        if not src:
            raise BadRequest("manca from_node (l'incrocio che vede il pericolo)")
        if dst == src:
            raise BadRequest("from_node e to_node sono lo stesso incrocio")
        if kind not in self.kinds:
            raise BadRequest(f"tipo di pericolo sconosciuto: {kind}")
        try:
            sev = min(max(int(body.get("severity", 2)), 1), 3)
        except (TypeError, ValueError):
            raise BadRequest("severity deve essere 1, 2 o 3")
        neighbors = (self.topology.get()["nodes"].get(src) or {}).get("neighbors") or {}
        if dst and not towards:  # la via di src che porta a dst
            towards = next((via for via, n in neighbors.items() if n == dst), None)
        info = self.kinds[kind]
        t = now_ms()
        actor = {"track": 0, "class": actor_class(kind), "confidence": 1.0}
        if towards:
            actor["towards"] = towards
        m = {"v": 1, "type": "hazard", "action": "new", "id": f"{src}-sim-{t}", "kind": kind,
             "category": info["category"], "node": src, "camera": "sim", "ts": t,
             "expires": t + int(info["validity_s"] * 1000), "cause": info["cause"], "cause_txt": info["cause_txt"],
             "severity": sev, "targets": info["targets"], "propagate": True, "norma": info["norma"],
             "actor": actor, "simulated": True, "details": {"note": "simulato dalla dashboard"}}
        r = self._ingest(m, simulated=True, to_node=dst)
        res = {"ok": True, "hazard": m, "delivered_to": r["forwarded"]}
        if not r["forwarded"]:
            res["warning"] = "nessun destinatario: controlla la direzione o topology.json"
        return res

    def cancel_simulated(self, hid):
        if not hid:
            raise BadRequest("manca l'id del pericolo da terminare")
        with self.lock:
            row = self.db.execute("SELECT json, simulated FROM messages WHERE hid=? AND action='new' "
                                  "ORDER BY seq DESC LIMIT 1", (str(hid),)).fetchone()
        if row is None:
            raise BadRequest(f"pericolo {hid} non trovato")
        if not row["simulated"]:
            raise BadRequest("dalla dashboard si terminano solo i pericoli simulati")
        c = copy.deepcopy(json.loads(row["json"]))
        t = now_ms()
        c["action"] = "cancel"
        c["details"] = {**(c.get("details") or {}), "reason": "terminato dalla dashboard",
                        "duration_s": round((t - c.get("ts", t)) / 1000, 1)}
        c["ts"] = c["expires"] = t
        r = self._ingest(c, simulated=True)
        return {"ok": True, "hazard": c, "delivered_to": r["forwarded"]}


# ---------------------------------------------------------------------- HTTP

class Reply:
    """Risposta HTTP indipendente dal server web (la usano sia http.server sia Flask)."""
    __slots__ = ("code", "data", "ctype", "headers")

    def __init__(self, code, data=b"", ctype="application/json; charset=utf-8", headers=None):
        self.code, self.data, self.ctype, self.headers = code, data, ctype, headers or {}


def json_reply(code, obj):
    return Reply(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def common_headers(reply):
    """Intestazioni comuni a tutte le risposte (CORS aperto: la dashboard puo' stare altrove)."""
    return {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS", **reply.headers}


def limit_body(data):
    if not data:
        raise BadRequest("corpo della richiesta vuoto (serve JSON)")
    if len(data) > MAX_BODY:
        raise BadRequest(f"richiesta troppo grande ({len(data)} byte)")
    return data


def _parse_body(read_body):
    try:
        return json.loads(read_body().decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BadRequest(f"JSON non valido: {e}")


def _authorized(token, auth_header):
    if not token:
        return True
    h = auth_header or ""
    return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip(), token)


def _file(path, ctype):
    try:
        return Reply(200, path.read_bytes(), ctype)
    except OSError:
        return Reply(404, f"file non trovato: {path.name}".encode("utf-8"), "text/plain; charset=utf-8")


def _static(path):
    """/static/<file>: solo file dentro la cartella static (niente ../)."""
    name = unquote(path[len("/static/"):])
    target = (STATIC / name).resolve()
    if STATIC.resolve() not in target.parents or not target.is_file():
        return json_reply(404, {"error": f"non trovato: {path}"})
    return _file(target, CTYPES.get(target.suffix.lower(), "application/octet-stream"))


def _phone_page(node, raw_query):
    if "api" not in parse_qs(raw_query):  # la pagina legge l'API da ?api=
        return Reply(302, b"", "text/plain", {"Location": f"/app?node={quote(node)}&api=/phone/{quote(node)}"})
    return _file(PHONE_PAGE, "text/html; charset=utf-8")


def _phone_api(hub, path, q):
    """/phone/<nodo>/api/events?since=&limit= e /phone/<nodo>/api/status, nel formato dell'API del nodo:
    i pericoli visti da quel nodo + quelli che il server gli ha inoltrato dai vicini."""
    parti = path.split("/")  # ['', 'phone', nodo, 'api', risorsa]
    if len(parti) != 5 or parti[3] != "api":
        return json_reply(404, {"error": f"non trovato: {path}"})
    node, risorsa = unquote(parti[2]), parti[4]
    if risorsa == "status":
        return json_reply(200, {"node": node, "server_online": True, "via": "server"})
    if risorsa != "events":
        return json_reply(404, {"error": f"non trovato: {path}"})
    since = _int(q, "since", 0, 0)
    limit = _int(q, "limit", 200, 1, 1000)
    with hub.lock:
        locali = hub.db.execute("SELECT json FROM messages WHERE node=? AND type='hazard' ORDER BY seq DESC LIMIT ?",
                                (node, limit)).fetchall()
        remoti = hub.db.execute("SELECT json FROM inbox WHERE node=? ORDER BY seq DESC LIMIT ?",
                                (node, limit)).fetchall()
    out = []
    for (raw,) in list(locali) + list(remoti):
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if isinstance(m, dict) and (m.get("ts") or 0) >= since:
            out.append(m)
    out.sort(key=lambda m: m.get("ts") or 0)
    return json_reply(200, out[-limit:])


def dispatch(hub, token, method, raw_path, auth_header, read_body, ip=None):
    """Tutte le pagine e le API. read_body() restituisce i byte del corpo (solo se servono)."""
    try:
        u = urlparse(raw_path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if method == "OPTIONS":
            return Reply(204, b"", "text/plain")
        if method in ("GET", "HEAD"):
            # pagine e dati pubblici (sola lettura): dashboard, ciclista, telefono, mappa
            if path in PAGES:
                return _file(PAGES[path], "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return Reply(204, b"", "image/x-icon")
            if path.startswith("/static/"):
                return _static(path)
            if path == "/app":
                return _phone_page(q.get("node") or "incrocio-1", u.query)
            if path.startswith("/phone/"):
                return _phone_api(hub, path, q)
            if path == "/api/health":
                return json_reply(200, {"ok": True, "server_ts": now_ms()})
            if path == "/api/traffic":
                return json_reply(200, hub.traffic())
            if path.startswith("/api/traffic/"):
                nid = unquote(path[len("/api/traffic/"):])
                found = next((n for n in hub.traffic() if n["node_id"] == nid), None)
                if found is None:
                    return json_reply(404, {"error": f"Node '{nid}' not found or inactive"})
                return json_reply(200, found)
        if not path.startswith("/api/"):
            return json_reply(404, {"error": f"non trovato: {path}"})
        # il token protegge cio' che scrivono (POST) o leggono solo i dispositivi (casella dei pericoli);
        # le letture della dashboard qui sotto restano pubbliche, in sola lettura
        dispositivo = method not in ("GET", "HEAD") or path.startswith("/api/inbox/")
        if dispositivo and not _authorized(token, auth_header):
            return json_reply(401, {"error": "token mancante o sbagliato (Authorization: Bearer <token>)"})
        if method == "POST":
            if path == "/api/events":
                return json_reply(200, hub.receive(_parse_body(read_body), ip))
            if path == "/api/simulate":
                return json_reply(200, hub.simulate(_parse_body(read_body)))
            if path == "/api/traffic":  # formato del client.py di simulazione (conteggi per nodo)
                return json_reply(201, hub.receive_traffic(_parse_body(read_body)))
            return json_reply(405, {"error": f"metodo POST non previsto su {path}"})
        if method not in ("GET", "HEAD"):
            return json_reply(405, {"error": f"metodo {method} non previsto su {path}"})
        if path.startswith("/api/inbox/"):
            node = unquote(path[len("/api/inbox/"):])
            if not node:
                raise BadRequest("manca il nome del nodo")
            return json_reply(200, hub.inbox(node, _int(q, "after", 0, 0), ip))
        if path == "/api/nodes":
            return json_reply(200, hub.nodes())
        if path == "/api/events":
            return json_reply(200, hub.events(_int(q, "after", 0, 0), _int(q, "limit", 100, 1, 1000),
                                              q.get("node") or None, _list(q, "type"), _list(q, "exclude")))
        if path == "/api/summary":
            return json_reply(200, hub.summary(_int(q, "since", 0, 0)))
        if path == "/api/forwards":
            return json_reply(200, hub.forwards(_int(q, "after", 0, 0), _int(q, "limit", 30, 1, 500)))
        if path == "/api/topology":
            return json_reply(200, hub.topology.get())
        if path == "/api/kinds":
            return json_reply(200, hub.kinds_list())
        return json_reply(404, {"error": f"non trovato: {path}"})
    except BadRequest as e:
        return json_reply(400, {"error": str(e)})
    except Exception as e:
        log(f"[errore] {method} {raw_path}: {e!r}")
        return json_reply(500, {"error": repr(e)})


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartCrossing/1"
    hub = None
    token = ""

    def log_message(self, fmt, *args):
        pass  # niente riga per ogni richiesta: con il polling ogni secondo la console diventerebbe illeggibile

    def do_GET(self):
        self._go("GET")

    def do_HEAD(self):
        self._go("HEAD")

    def do_POST(self):
        self._go("POST")

    def do_OPTIONS(self):
        self._go("OPTIONS")

    def _go(self, method):
        r = dispatch(self.hub, self.token, method, self.path, self.headers.get("Authorization"),
                     self._read_body, self.client_address[0])
        try:
            self.send_response(r.code)
            self.send_header("Content-Type", r.ctype)
            self.send_header("Content-Length", str(len(r.data)))
            for k, v in common_headers(r).items():
                self.send_header(k, v)
            self.end_headers()
            if r.data and method != "HEAD":
                self.wfile.write(r.data)
        except (ConnectionError, TimeoutError, OSError):
            pass  # il client ha chiuso la connessione

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Content-Length non valido")
        if n > MAX_BODY:
            raise BadRequest(f"richiesta troppo grande ({n} byte)")
        return limit_body(self.rfile.read(n) if n > 0 else b"")


def _int(q, key, default, lo=None, hi=None):
    try:
        v = int(q.get(key, default))
    except (TypeError, ValueError):
        raise BadRequest(f"{key} deve essere un numero intero")
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _list(q, key):
    return [x.strip() for x in (q.get(key) or "").split(",") if x.strip()] or None


def make_server(hub, host="0.0.0.0", port=8000, token=""):
    """Server HTTP pronto (serve_forever da chiamare). port=0 -> porta libera (per i test)."""
    handler = type("SmartCrossingHandler", (Handler,), {"hub": hub, "token": token or ""})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        # su Windows SO_REUSEADDR permetterebbe due server sulla stessa porta senza errori
        allow_reuse_address = sys.platform != "win32"

    return Server((host, port), handler)


def lan_ips():
    """Indirizzi IP del PC nella rete locale (quelli da mettere in server.url della board)."""
    ips = []
    try:  # nessun pacchetto parte davvero: serve solo a sapere quale scheda di rete esce
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(ips) if not ip.startswith(("127.", "169.254.", "0."))]


def _resolve(given, default_name):
    """Percorso relativo: prima rispetto alla cartella corrente, poi alla cartella di server.py."""
    if not given:
        return HERE / default_name
    p = Path(given)
    if p.is_absolute() or p.exists():
        return p.resolve()
    return HERE / p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # la console di Windows non ha tutti i caratteri
    env_file = load_env()
    ap = argparse.ArgumentParser(description="Smart Crossing - server centrale: riceve i dati degli incroci, "
                                             "inoltra i pericoli ai vicini, dashboard web.")
    ap.add_argument("--port", type=int, default=8000, help="porta HTTP (default 8000)")
    ap.add_argument("--host", default="0.0.0.0", help="indirizzo di ascolto (default 0.0.0.0 = tutta la rete)")
    ap.add_argument("--topology", default=None, help="topologia degli incroci (default: topology.json accanto a server.py)")
    ap.add_argument("--db", default=None, help="database SQLite (default: server.db accanto a server.py)")
    ap.add_argument("--token", default=os.environ.get("SC_TOKEN", ""),
                    help="richiesto come 'Authorization: Bearer <token>' a nodi e client (default: SC_TOKEN del .env); "
                         "--token \"\" lo disattiva")
    ap.add_argument("--offline-after", type=float, default=15.0,
                    help="secondi senza notizie dopo cui un incrocio e' offline (default 15)")
    args = ap.parse_args(argv)

    topo_path, db_path = _resolve(args.topology, "topology.json"), _resolve(args.db, "server.db")
    topology = Topology(topo_path)
    topology.get()
    hub = Hub(db_path, topology, args.offline_after)
    try:
        httpd = make_server(hub, args.host, args.port, args.token)
    except OSError as e:
        print(f"Non riesco ad aprire la porta {args.port} ({e}). Un altro server.py e' gia' aperto? "
              f"Prova con --port {args.port + 1}")
        hub.close()
        return 1
    port = httpd.server_address[1]
    ips = lan_ips() if args.host in ("0.0.0.0", "") else [args.host]
    print()
    print("Smart Crossing - server centrale")
    print(f"  database:   {db_path}")
    print(f"  topologia:  {topo_path}")
    print(f"  .env:       {env_file or 'nessuno (copiate .env.example in .env)'}")
    print(f"  token:      {'richiesto ai dispositivi (Authorization: Bearer ...)' if args.token else 'nessuno'}")
    print()
    print(f"  Dashboard su questo PC:    http://localhost:{port}/")
    for ip in ips:
        print(f"  Dashboard da altri device: http://{ip}:{port}/")
    base = f"http://{ips[0]}:{port}" if ips else f"http://localhost:{port}"
    print(f"  Ciclista: {base}/cyclist   Telefono: {base}/app?node=incrocio-1   Console offline: {base}/console")
    if ips:
        print()
        print("  Nel config.json della board (se ci sono piu' indirizzi, quello della rete della board):")
        for ip in ips:
            print(f'    "server": {{"url": "http://{ip}:{port}"}}')
        if args.token:
            print("  e il token: copiate il .env accanto a config.json sulla board (oppure \"token\" in config.json).")
    else:
        print("  (nessun indirizzo di rete locale trovato: il PC e' collegato al Wi-Fi?)")
    print()
    print("  Se la board non si collega: stessa rete del PC e Python consentito nel Firewall di Windows.")
    print("  Ctrl+C per fermare.")
    print(flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nserver fermato")
    finally:
        httpd.server_close()
        hub.close()
    return 0


def __getattr__(name):
    """'from server import app' (vecchio file WSGI di PythonAnywhere) -> l'app Flask di flask_app.py.
    Flask si importa solo qui: server.py da solo resta con la sola libreria standard."""
    if name in ("app", "application"):
        from flask_app import app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    sys.exit(main())
