"""API HTTP locale del nodo (solo se config api.enabled).

Serve a guardare il nodo dalla rete locale anche senza il server: stato, scena attuale,
eventi e tracce salvati nell'archivio SQLite, statistiche e heatmap. Solo lettura (GET),
risposte JSON con CORS aperto, cosi' una pagina web qualsiasi puo' interrogarla.

  GET /api/health                              il nodo e' vivo?
  GET /api/status                              stato, statistiche, scena, LED, config
  GET /api/scene                               oggetti visti adesso
  GET /api/events?since=<ms>&kind=&limit=      pericoli (locali e remoti) e movimenti
  GET /api/tracks?since=<ms>&limit=            riepiloghi delle tracce chiuse
  GET /api/stats?since=<ms>&limit=             statistiche periodiche
  GET /api/heatmap?camera=&class=              conteggi per cella
  GET /api/config                              configurazione senza segreti
  GET /app                                     pagina per il telefono: avvisi + vibrazione

Su App Lab la porta va dichiarata in app.yaml (ports: [8100]).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config import public_config

ENDPOINTS = ["/api/health", "/api/status", "/api/scene", "/api/events", "/api/tracks",
             "/api/stats", "/api/heatmap", "/api/config", "/app"]
APP_PAGE = Path(__file__).resolve().parent / "web" / "app.html"
LIMIT_DEFAULT, LIMIT_MAX = 100, 1000


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # le richieste in corso non bloccano l'uscita del programma
    # Su Windows SO_REUSEADDR permette di "rubare" una porta gia' in uso: lo tengo solo su Linux
    allow_reuse_address = sys.platform != "win32"


class BadRequest(Exception):
    """Parametro della richiesta non valido -> risposta 400."""


class LocalAPI:
    def __init__(self, cfg_api, node):
        cfg_api = cfg_api or {}
        self.node = node
        self.host = cfg_api.get("host", "0.0.0.0")
        try:
            self.port = int(cfg_api.get("port", 8100))  # 0 = porta libera qualsiasi (per i test)
        except (TypeError, ValueError):
            print(f"[api] porta non valida {cfg_api.get('port')!r} -> uso 8100")
            self.port = 8100
        self._server = None
        self._thread = None
        self._errori = 0

    # ------------------------------------------------------------------ ciclo di vita

    def start(self):
        """Avvia il server in un thread daemon. Se la porta e' occupata il nodo continua senza API."""
        if self._server is not None:
            return
        try:
            srv = _Server((self.host, self.port), _Handler)
        except Exception as e:  # porta occupata o fuori range, indirizzo sbagliato
            print(f"[api] non riesco ad aprire {self.host}:{self.port} ({e}) -> API locale disattivata")
            return
        srv.api = self
        self._server = srv
        self.port = srv.server_address[1]
        self._thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="api-locale", daemon=True)
        self._thread.start()
        print(f"[api] in ascolto su http://{self.host}:{self.port}/api/status")

    def stop(self):
        srv, self._server = self._server, None
        if srv is None:
            return
        try:
            srv.shutdown()      # aspetta la fine del ciclo serve_forever
            srv.server_close()  # libera la porta
        except Exception as e:
            print("[api] errore in chiusura:", repr(e))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    close = stop  # Node.close() chiama close() su ogni componente

    @property
    def running(self):
        return self._server is not None

    # ------------------------------------------------------------------ risposte

    def handle(self, path, query):
        """(codice HTTP, oggetto JSON) per una GET. Gli errori diventano 400/404/500."""
        path = path.rstrip("/") or "/"
        node = self.node
        try:
            # parametri vuoti (kind=) = nessun filtro
            q = {k: v[0] for k, v in parse_qs(query).items() if v and v[0].strip()}
            if path == "/api/health":
                return 200, {"ok": True, "node": getattr(node, "node_id", None), "ts": int(time.time() * 1000),
                             "uptime_s": round(time.time() - getattr(node, "started", time.time()), 1),
                             "storage": getattr(node, "storage", None) is not None}
            if path == "/api/status":
                return 200, self._status()
            if path == "/api/scene":
                return 200, {"node": getattr(node, "node_id", None), "ts": int(time.time() * 1000),
                             "objects": list(getattr(node, "latest_scene", None) or [])}
            if path == "/api/events":
                return 200, self._query("events", q, kind=q.get("kind"))
            if path in ("/api/tracks", "/api/stats"):
                return 200, self._query(path.rsplit("/", 1)[1], q)
            if path == "/api/heatmap":
                st = getattr(node, "storage", None)
                return 200, (st.heatmap(camera=q.get("camera"), cls=q.get("class")) or []) if st else []
            if path == "/api/config":
                return 200, public_config(node.cfg)
        except BadRequest as e:
            return 400, {"error": str(e)}
        except Exception as e:  # archivio o nodo in errore: rispondo 500 e il server resta su
            self._errori += 1
            if self._errori <= 5:
                print(f"[api] errore su {path}: {e!r}")
            return 500, {"error": "errore interno", "detail": repr(e), "path": path}
        return 404, {"error": "risorsa non trovata", "path": path, "endpoints": ENDPOINTS}

    def _status(self):
        snap = dict(self.node.status_snapshot() or {})
        st = getattr(self.node, "storage", None)
        if st is not None:  # appena riavviato: uso l'ultimo stato salvato nell'archivio
            for key, table in (("status", "status"), ("stats", "stats")):
                if snap.get(key) is None:
                    try:
                        snap[key] = st.latest(table)
                    except Exception as e:
                        print(f"[api] latest({table}) non riuscito: {e!r}")
        return snap

    def _query(self, table, q, kind=None):
        since = _int_param(q, "since", 0)
        limit = min(max(_int_param(q, "limit", LIMIT_DEFAULT), 1), LIMIT_MAX)
        st = getattr(self.node, "storage", None)
        if st is None:  # nodo senza archivio: nessuno storico
            return []
        return st.query(table, since_ms=since, limit=limit, kind=kind) or []


def _int_param(q, name, default):
    raw = q.get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))  # accetta anche "1.7e12"
    except ValueError:
        raise BadRequest(f"parametro '{name}' non valido: {raw!r} (serve un numero)") from None


class _Handler(BaseHTTPRequestHandler):
    server_version = "SmartCrossingNode/1"
    timeout = 10  # un client lento non tiene occupato un thread per sempre

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path.rstrip("/") in ("", "/app"):  # pagina per il telefono (avvisi + vibrazione)
            return self._send_page()
        code, obj = self.server.api.handle(parts.path, parts.query)
        self._send_json(code, obj)

    def _send_page(self):
        try:
            body = APP_PAGE.read_bytes()
            code, ctype = 200, "text/html; charset=utf-8"
        except OSError:
            body, code, ctype = b"pagina web/app.html mancante", 404, "text/plain; charset=utf-8"
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):  # preflight CORS dei browser
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._send_json(405, {"error": "API di sola lettura: usa GET"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, code, obj):
        body = None
        for _ in range(3):  # il nodo puo' modificare un dict mentre lo serializzo: riprovo
            try:
                body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
                break
            except RuntimeError:
                time.sleep(0.01)
            except (TypeError, ValueError) as e:
                code, obj = 500, {"error": "risposta non serializzabile", "detail": repr(e)}
        if body is None:
            code, body = 500, b'{"error": "dati in aggiornamento, riprova"}'
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, OSError):  # il client ha chiuso la connessione: niente da fare
            pass

    def log_message(self, format, *args):  # niente log per ogni richiesta (FPS e CPU della board)
        pass
