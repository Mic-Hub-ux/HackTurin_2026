import random
import requests
import multiprocessing as mp
import time
import statistics
from queue import Empty

URL = "https://michubux.pythonanywhere.com/api/traffic"
API_SECRET_TOKEN = "efesto-secure-iot-key-2026"

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

    print(f"\nAvvio test sicuro: {NUM_NODES} nodi, {DURATION}s\n")
    start_event.set()

    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for p in processi:
            p.join(timeout=TIMEOUT + 2)
            if p.is_alive():
                p.terminate()
                p.join()

    print("\nTest completato.")

if __name__ == "__main__":
    main()