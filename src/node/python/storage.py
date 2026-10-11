"""Archivio locale SQLite del nodo: eventi, tracce, statistiche, stato, heatmap e coda di invio.

Un solo file (smartcrossing.db) in modalita' WAL: il nodo scrive mentre l'API locale o gli
strumenti di export leggono, senza bloccarsi a vicenda. Ogni messaggio e' salvato come JSON
integrale (query() restituisce i dict originali) piu' poche colonne indicizzate per filtrare.
La tabella outbox e' la coda verso il server: sopravvive ai riavvii, cosi' quello che non e'
partito con la rete giu' parte quando la rete torna.

I metodi usati a runtime non sollevano mai eccezioni: scrivono un avviso e restituiscono un
valore vuoto (solo il costruttore puo' fallire, e il nodo allora va avanti senza archivio).
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

OUTBOX_MAX = 50000  # righe massime in coda di invio: oltre, si scartano le piu' vecchie

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    rid INTEGER PRIMARY KEY, id TEXT, ts INTEGER, type TEXT, kind TEXT,
    action TEXT, severity INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_id ON events(id);

CREATE TABLE IF NOT EXISTS tracks (
    rid INTEGER PRIMARY KEY, id TEXT, ts INTEGER, ts_start INTEGER, ts_end INTEGER,
    camera TEXT, class TEXT, from_ap TEXT, to_ap TEXT, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tracks_ts ON tracks(ts);
CREATE INDEX IF NOT EXISTS tracks_class_ts ON tracks(class, ts);

CREATE TABLE IF NOT EXISTS stats (rid INTEGER PRIMARY KEY, ts INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS stats_ts ON stats(ts);

CREATE TABLE IF NOT EXISTS status (rid INTEGER PRIMARY KEY, ts INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS status_ts ON status(ts);

CREATE TABLE IF NOT EXISTS messages (rid INTEGER PRIMARY KEY, ts INTEGER, type TEXT, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS messages_type_ts ON messages(type, ts);

CREATE TABLE IF NOT EXISTS heatmap (
    camera TEXT NOT NULL, class TEXT NOT NULL, cx INTEGER NOT NULL, cy INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, cell REAL, unit TEXT, ts INTEGER,
    PRIMARY KEY (camera, class, cx, cy));

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, type TEXT, json TEXT NOT NULL);
"""

EVENT_TYPES = ("hazard", "remote_hazard", "movement")
# tabella -> colonna usata dal filtro "kind" di query()
TABLES = {"events": "kind", "tracks": "class", "stats": None, "status": None, "messages": "type"}


def _dumps(msg):
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Storage:
    def __init__(self, path, retention_days=7, outbox_max=OUTBOX_MAX):
        self.path = None if str(path) == ":memory:" else Path(path)
        self.retention_days = retention_days
        self.outbox_max = outbox_max
        self._lock = threading.Lock()
        self._errors = {}
        self._dropped = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError as e:
            if self.path is None or not self.path.exists():
                raise
            # file rovinato (es. corrente staccata a meta' scrittura): lo metto da parte e riparto
            bad = self.path.with_name(f"{self.path.name}.corrotto-{int(time.time())}")
            print(f"[archivio] {self.path.name} illeggibile ({e}): rinominato in {bad.name}, ne creo uno nuovo")
            os.replace(self.path, bad)
            for suffix in ("-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    os.replace(p, Path(str(bad) + suffix))
            self._conn = self._open()
        self._outbox_n = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if self._outbox_n:
            print(f"[archivio] {self._outbox_n} messaggi ancora da inviare dalla sessione precedente")

    def _open(self):
        conn = sqlite3.connect(":memory:" if self.path is None else str(self.path),
                               check_same_thread=False, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")    # lettori (API, export) e scrittore insieme
            conn.execute("PRAGMA synchronous=NORMAL")  # sicuro con WAL e consuma meno la flash
            conn.executescript(SCHEMA)
            return conn
        except Exception:
            conn.close()
            raise

    # ------------------------------------------------------------------ utilita'

    def _err(self, where, e):
        n = self._errors[where] = self._errors.get(where, 0) + 1
        if n <= 3 or n % 100 == 0:  # non riempio la console se l'errore si ripete
            print(f"[archivio] errore in {where} (x{n}): {e!r}")

    def _db(self, where, fn, default=None):
        """Esegue fn(conn) sotto lock in una transazione; in caso di errore log + default."""
        with self._lock:
            if self._conn is None:  # archivio chiuso
                return default
            try:
                with self._conn:  # commit se va tutto bene, rollback se no
                    return fn(self._conn)
            except Exception as e:
                self._err(where, e)
                return default

    # ------------------------------------------------------------------ scrittura

    def save(self, msg):
        """Salva un messaggio nella tabella giusta in base a msg["type"]. Ritorna True se salvato."""
        if not isinstance(msg, dict):
            self._err("save", TypeError(f"messaggio non valido: {type(msg).__name__}"))
            return False
        tipo = msg.get("type")
        ts = _int(msg.get("ts")) or int(time.time() * 1000)

        def op(c):
            js = _dumps(msg)
            if tipo in EVENT_TYPES:
                c.execute("INSERT INTO events(id, ts, type, kind, action, severity, json) VALUES (?,?,?,?,?,?,?)",
                          (msg.get("id"), ts, tipo, msg.get("kind") or tipo, msg.get("action"),
                           _int(msg.get("severity")), js))
            elif tipo == "track":
                c.execute("INSERT INTO tracks(id, ts, ts_start, ts_end, camera, class, from_ap, to_ap, json)"
                          " VALUES (?,?,?,?,?,?,?,?,?)",
                          (msg.get("id"), ts, _int(msg.get("ts_start")), _int(msg.get("ts_end"), ts),
                           msg.get("camera"), msg.get("class"), msg.get("from"), msg.get("to"), js))
            elif tipo == "stats":
                c.execute("INSERT INTO stats(ts, json) VALUES (?,?)", (ts, js))
                self._heatmap_add(c, msg, ts)
            elif tipo == "status":
                c.execute("INSERT INTO status(ts, json) VALUES (?,?)", (ts, js))
            else:  # scene (se richiesta) e tipi futuri
                c.execute("INSERT INTO messages(ts, type, json) VALUES (?,?,?)", (ts, str(tipo), js))
            return True

        return self._db("save", op, False)

    def _heatmap_add(self, c, msg, ts):
        """Somma i conteggi della heatmap del messaggio stats a quelli gia' salvati."""
        for cam, data in (msg.get("cameras") or {}).items():
            if not isinstance(data, dict) or not isinstance(data.get("heatmap"), dict):
                continue
            cell, unit = data.get("heatmap_cell"), data.get("heatmap_unit")
            old = c.execute("SELECT cell, unit FROM heatmap WHERE camera=? LIMIT 1", (cam,)).fetchone()
            if old and (old[1] != unit or (old[0] is not None and cell is not None
                                          and abs(float(old[0]) - float(cell)) > 1e-9)):
                # celle di dimensione diversa (es. camera appena calibrata): non si possono sommare
                c.execute("DELETE FROM heatmap WHERE camera=?", (cam,))
                print(f"[archivio] heatmap di {cam} azzerata: cella cambiata ({old[0]} {old[1]} -> {cell} {unit})")
            rows = []
            for cls, cells in data["heatmap"].items():
                for key, n in (cells or {}).items():
                    try:
                        cx, cy = (int(float(v)) for v in str(key).split(","))
                        rows.append((cam, cls, cx, cy, int(n), cell, unit, ts))
                    except (TypeError, ValueError):
                        continue  # chiave cella malformata: la salto
            c.executemany(
                "INSERT INTO heatmap(camera, class, cx, cy, count, cell, unit, ts) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(camera, class, cx, cy) DO UPDATE SET count = count + excluded.count,"
                " cell = excluded.cell, unit = excluded.unit, ts = excluded.ts", rows)

    # ------------------------------------------------------------------ lettura

    def query(self, table, since_ms=0, limit=100, kind=None, msg_type=None):
        """Gli ultimi `limit` messaggi con ts >= since_ms, in ordine di ts crescente.

        kind filtra per kind (events), per classe (tracks) o per tipo (messages);
        msg_type filtra il tipo nella tabella events (hazard / remote_hazard / movement).
        limit None o <= 0 = tutti.
        """
        if table not in TABLES:
            self._err("query", ValueError(f"tabella sconosciuta: {table!r}"))
            return []
        where, args = ["ts >= ?"], [_int(since_ms, 0)]
        if kind is not None and TABLES[table]:
            where.append(f"{TABLES[table]} = ?")
            args.append(kind)
        if msg_type is not None and table == "events":
            where.append("type = ?")
            args.append(msg_type)
        n = -1 if limit is None else _int(limit, 100)
        args.append(n if n > 0 else -1)  # LIMIT -1 = nessun limite
        sql = (f"SELECT json FROM (SELECT rid, ts, json FROM {table} WHERE {' AND '.join(where)}"
               f" ORDER BY ts DESC, rid DESC LIMIT ?) ORDER BY ts, rid")
        rows = self._db("query", lambda c: c.execute(sql, args).fetchall(), [])
        return [m for m in (self._loads(r[0]) for r in rows) if m is not None]

    def latest(self, table):
        """L'ultimo messaggio di una tabella (es. lo stato piu' recente) o None."""
        if table not in TABLES:
            return None
        row = self._db("latest", lambda c: c.execute(
            f"SELECT json FROM {table} ORDER BY ts DESC, rid DESC LIMIT 1").fetchone())
        return self._loads(row[0]) if row else None

    def heatmap(self, camera=None, cls=None):
        """Conteggi cumulativi per cella: [{"camera","class","cx","cy","count","cell","unit"}]."""
        where, args = [], []
        if camera is not None:
            where.append("camera = ?")
            args.append(camera)
        if cls is not None:
            where.append("class = ?")
            args.append(cls)
        sql = ("SELECT camera, class, cx, cy, count, cell, unit FROM heatmap"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY camera, class, cy, cx")
        rows = self._db("heatmap", lambda c: c.execute(sql, args).fetchall(), [])
        return [{"camera": r[0], "class": r[1], "cx": r[2], "cy": r[3], "count": r[4],
                 "cell": r[5], "unit": r[6]} for r in rows]

    def _loads(self, text):
        try:
            return json.loads(text)
        except (TypeError, ValueError) as e:
            self._err("json", e)
            return None

    # ------------------------------------------------------------------ coda di invio

    def outbox_put(self, msg):
        """Mette un messaggio in coda verso il server. Ritorna True se accodato."""
        def op(c):
            c.execute("INSERT INTO outbox(ts, type, json) VALUES (?,?,?)",
                      (_int(msg.get("ts")) or int(time.time() * 1000), msg.get("type"), _dumps(msg)))
            n, tolti = self._outbox_n + 1, 0
            if n > self.outbox_max:  # coda piena (server giu' da molto): via i piu' vecchi
                tolti = max(c.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)",
                                      (n - self.outbox_max,)).rowcount, 0)
            self._outbox_n = n - tolti  # aggiornato solo se la transazione non e' fallita prima
            if tolti:
                self._dropped += tolti
                if self._dropped == tolti or self._dropped % 1000 < tolti:
                    print(f"[archivio] coda di invio piena ({self.outbox_max}): scartati {self._dropped} "
                          f"messaggi piu' vecchi (restano nell'archivio locale)")
            return True

        if not isinstance(msg, dict):
            return False
        return self._db("outbox_put", op, False)

    def outbox_take(self, n):
        """I messaggi piu' vecchi in coda come [(id, msg)], senza toglierli (vedi outbox_ack)."""
        rows = self._db("outbox_take", lambda c: c.execute(
            "SELECT id, json FROM outbox ORDER BY id LIMIT ?", (max(1, _int(n, 1)),)).fetchall(), [])
        out, bad = [], []
        for rid, text in rows:
            m = self._loads(text)
            if m is None:
                bad.append(rid)  # riga illeggibile: non la mando, ma non deve bloccare la coda
            else:
                out.append((rid, m))
        if bad:
            self.outbox_ack(bad)
        return out

    def outbox_ack(self, ids):
        """Toglie dalla coda i messaggi arrivati al server."""
        ids = [i for i in (ids or []) if isinstance(i, int)]

        def op(c):
            tolti = 0
            for k in range(0, len(ids), 500):  # limite di parametri di SQLite
                part = ids[k:k + 500]
                cur = c.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(part))})", part)
                tolti += max(cur.rowcount, 0)
            self._outbox_n = max(0, self._outbox_n - tolti)
            return tolti

        return self._db("outbox_ack", op, 0) if ids else 0

    def outbox_size(self):
        return self._outbox_n

    # ------------------------------------------------------------------ manutenzione

    def size_bytes(self):
        """Spazio occupato su disco (database + file WAL)."""
        try:
            if self.path is None:
                row = self._db("size", lambda c: c.execute(
                    "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()").fetchone())
                return int(row[0]) if row else 0
            return sum(os.path.getsize(p) for p in (str(self.path), f"{self.path}-wal", f"{self.path}-shm")
                       if os.path.exists(p))
        except Exception as e:
            self._err("size_bytes", e)
            return 0

    def cleanup(self, now_s):
        """Cancella le righe piu' vecchie di retention_days (la coda di invio e la heatmap no)."""
        try:
            days = float(self.retention_days or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:  # 0 = tieni tutto
            return 0
        cutoff = int((now_s - days * 86400) * 1000)

        def op(c):
            return sum(max(c.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,)).rowcount, 0) for t in TABLES)

        tolti = self._db("cleanup", op, 0)
        if tolti:
            print(f"[archivio] pulizia: cancellate {tolti} righe piu' vecchie di {days:g} giorni")
            self._checkpoint()
        return tolti

    def _checkpoint(self):
        """Riporta nel file principale il contenuto del WAL e lo azzera (il WAL non cresce all'infinito)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as e:
                    self._err("checkpoint", e)

    def close(self):
        self._checkpoint()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as e:
                    self._err("close", e)
                self._conn = None
