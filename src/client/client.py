import os
import random
import requests
import multiprocessing as mp
import time
import json
from pathlib import Path
from queue import Empty


def load_env(start=Path(__file__).resolve().parent, levels=3):
    """Legge il primo .env trovato qui o nelle cartelle sopra (la radice della repo), senza
    sovrascrivere le variabili d'ambiente gia' impostate."""
    for d in [start, *list(start.parents)[:levels]]:
        p = d / ".env"
        if p.is_file():
            for riga in p.read_text(encoding="utf-8-sig").splitlines():
                riga = riga.strip()
                if riga and not riga.startswith("#") and "=" in riga:
                    k, v = riga.split("=", 1)
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    os.environ.setdefault(k.strip(), v)
            return p
    return None


load_env()

# Server di destinazione: per il server locale  SC_TRAFFIC_URL=http://localhost:8000/api/traffic
URL = os.environ.get("SC_TRAFFIC_URL") or "https://michubux.pythonanywhere.com/api/traffic"

# Segreto condiviso con il server (SC_TOKEN nel file .env, vedi .env.example)
API_SECRET_TOKEN = os.environ.get("SC_TOKEN", "")

DURATION = 60
INTERVAL = 1
TIMEOUT = 15

# 1. Localizza e carica dinamicamente il file topology.json
# Presuppone che la struttura sia: src/client/client.py e src/server/topology.json
TOPOLOGY_PATH = Path(__file__).resolve().parents[1] / "server" / "topology.json"

try:
    with open(TOPOLOGY_PATH, 'r', encoding='utf-8') as f:
        topology_data = json.load(f)
except FileNotFoundError:
    print(f"Errore: File '{TOPOLOGY_PATH}' non trovato.")
    print("Assicurati di lanciare lo script mantenendo intatta la struttura delle directory.")
    exit(1)

# 2. Crea la lista dei nodi usando la struttura del file JSON (chiavi "incrocio-1", ecc.)
LUOGHI_TORINO = []
for node_id, data in topology_data.get("nodes", {}).items():
    LUOGHI_TORINO.append({
        "node_id": node_id,
        "latitude": data.get("lat", 0.0),
        "longitude": data.get("lon", 0.0)
    })

NUM_NODES = len(LUOGHI_TORINO)


def client_worker(nodo_info, start_event, stop_event, result_queue):
    # Estrae i dati letti dal file JSON
    node_id = nodo_info["node_id"]
    latitude = nodo_info["latitude"]
    longitude = nodo_info["longitude"]

    # Header protetti con token segreto
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_SECRET_TOKEN}"
    }

    start_event.wait()

    with requests.Session() as session:
        while not stop_event.is_set():
            is_ambulance = 1 if random.random() < 0.03 else 0

            dati = {
                "node_id": node_id,
                "coordinates": {"lat": latitude, "lon": longitude},
                "counts": {
                    "pedestrians": random.randint(2, 25),
                    "cyclists": random.randint(1, 15),
                    "ambulances": is_ambulance,
                },
            }

            inizio = time.perf_counter()

            try:
                risposta = session.post(
                    URL,
                    json=dati,
                    headers=headers,
                    timeout=TIMEOUT
                )

                latenza = time.perf_counter() - inizio

                result_queue.put({
                    "node_id": node_id,
                    "status": risposta.status_code,
                    "latency": latenza,
                    "success": risposta.ok,
                    "error": None
                })

            except requests.RequestException as errore:
                latenza = time.perf_counter() - inizio
                result_queue.put({
                    "node_id": node_id,
                    "status": None,
                    "latency": latenza,
                    "success": False,
                    "error": str(errore)
                })

            attesa = max(0, INTERVAL - (time.perf_counter() - inizio))
            if stop_event.wait(attesa):
                break

def main():
    ctx = mp.get_context("spawn")
    start_event = ctx.Event()
    stop_event = ctx.Event()
    result_queue = ctx.Queue()

    processi = []
    # Passiamo direttamente il dizionario con le info del nodo al worker
    for nodo_info in LUOGHI_TORINO:
        p = ctx.Process(
            target=client_worker,
            args=(nodo_info, start_event, stop_event, result_queue)
        )
        p.start()
        processi.append(p)

    if not API_SECRET_TOKEN:
        print("ATTENZIONE: SC_TOKEN non impostato (manca il file .env?): il server rispondera' 401")
    print(f"\nAvvio test sicuro: {NUM_NODES} nodi, {DURATION}s -> {URL}\n")
    start_event.set()

    risultati = []
    try:
        fine = time.monotonic() + DURATION
        while time.monotonic() < fine:
            # la coda va svuotata mentre i processi lavorano: se si riempie, i processi non terminano
            try:
                risultati.append(result_queue.get(timeout=0.5))
            except Empty:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for p in processi:
            p.join(timeout=TIMEOUT + 2)
            if p.is_alive():
                p.terminate()
                p.join()

    while True:
        try:
            risultati.append(result_queue.get_nowait())
        except Empty:
            break

    ok = sum(1 for r in risultati if r["success"])
    print(f"\nTest completato: {ok}/{len(risultati)} richieste riuscite.")
    if any(r["status"] in (401, 403) for r in risultati):
        print("Token rifiutato: SC_TOKEN nel .env deve essere uguale a quello del server.")
    errori = [r["error"] for r in risultati if r["error"]]
    if errori:
        print(f"Errori di rete ({len(errori)}), il primo: {errori[0]}")

if __name__ == "__main__":
    main()