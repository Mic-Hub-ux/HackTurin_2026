"""Lo stesso server di server.py, ma come applicazione Flask/WSGI (es. PythonAnywhere).

Tutta la logica (pagine, API, inoltro dei pericoli) e' in server.dispatch(): qui c'e' solo
l'adattatore. In locale basta server.py (solo libreria standard, funziona senza internet).

PythonAnywhere: nel file WSGI della web app
    import sys
    sys.path.insert(0, "/home/<utente>/HackTurin_2026/src/server")
    from flask_app import app as application      (va bene anche il vecchio "from server import app")
Configurazione nel file .env della repo (vedi .env.example): SC_TOKEN (token dei dispositivi),
facoltative SC_DB (database) e SC_TOPOLOGY (topologia).

Prova in locale:  pip install -r requirements.txt  ->  python flask_app.py  (porta 5000)
"""
import os

from flask import Flask, Response, request

import server as core

core.load_env()
hub = core.Hub(os.environ.get("SC_DB") or str(core.HERE / "server.db"),
               core.Topology(os.environ.get("SC_TOPOLOGY") or str(core.HERE / "topology.json")),
               wal=False)  # i dischi di rete degli hosting non supportano bene il WAL di SQLite
TOKEN = os.environ.get("SC_TOKEN", "")

app = Flask(__name__, static_folder=None)  # anche /static passa da dispatch()

METODI = ["GET", "HEAD", "POST", "OPTIONS"]


@app.route("/", defaults={"path": ""}, methods=METODI)
@app.route("/<path:path>", methods=METODI)
def tutto(path):
    qs = request.query_string.decode("utf-8", "replace")
    raw = request.path + (f"?{qs}" if qs else "")
    r = core.dispatch(hub, TOKEN, request.method, raw, request.headers.get("Authorization"),
                      lambda: core.limit_body(request.get_data(cache=False)), request.remote_addr)
    resp = Response(r.data, status=r.code, content_type=r.ctype)
    for k, v in core.common_headers(r).items():
        resp.headers[k] = v
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
