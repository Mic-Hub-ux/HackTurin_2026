import os
import random
import requests
import multiprocessing as mp
import time
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

NUM_NODES = 12
DURATION = 60
INTERVAL = 1
TIMEOUT = 15

LUOGHI_TORINO = [
    {"name": "node_01", "latitude": 45.0625, "longitude": 7.6720},
    {"name": "node_02", "latitude": 45.0625, "longitude": 7.6735},
    {"name": "node_03", "latitude": 45.0625, "longitude": 7.6750},
    {"name": "node_04", "latitude": 45.0613, "longitude": 7.6720},
    {"name": "node_05", "latitude": 45.0613, "longitude": 7.6735},
    {"name": "node_06", "latitude": 45.0613, "longitude": 7.6750},
    {"name": "node_07", "latitude": 45.0601, "longitude": 7.6720},
    {"name": "node_08", "latitude": 45.0601, "longitude": 7.6735},
    {"name": "node_09", "latitude": 45.0601, "longitude": 7.6750},
    {"name": "node_10", "latitude": 45.0589, "longitude": 7.6720},
    {"name": "node_11", "latitude": 45.0589, "longitude": 7.6735},
    {"name": "node_12", "latitude": 45.0589, "longitude": 7.6750},
]

def client_worker(node_number, start_event, stop_event, result_queue):
    node_id = f"node_{node_number:02d}"
    luogo = LUOGHI_TORINO[node_number - 1]

    latitude = luogo["latitude"]
    longitude = luogo["longitude"]

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
    for numero in range(1, NUM_NODES + 1):
        p = ctx.Process(
            target=client_worker,
            args=(numero, start_event, stop_event, result_queue)
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