import random
import requests
import multiprocessing as mp
import time
import statistics
from queue import Empty

URL = "https://michubux.pythonanywhere.com/api/traffic"

NUM_NODES = 10
DURATION = 60
INTERVAL = 1
TIMEOUT = 15

# Coordinate GPS di luoghi reali a Torino
LUOGHI_TORINO = [
    {"name": "node_01", "latitude": 45.062237, "longitude": 7.672350},
    {"name": "node_02", "latitude": 45.061826,"longitude": 7.673455},
    {"name": "node_03", "latitude": 45.061596, "longitude": 7.674118},
    {"name": "node_04","latitude": 45.061340,"longitude": 7.674811},
]


def client_worker(node_number, start_event, stop_event, result_queue):
    node_id = f"node_{node_number:02d}"

    # Ogni nodo sceglie una posizione una sola volta
    luogo = random.choice(LUOGHI_TORINO)

    latitude = luogo["latitude"]
    longitude = luogo["longitude"]

    print(
        f"[{node_id}] Posizione assegnata: {luogo['name']} "
        f"({latitude}, {longitude})",
        flush=True
    )

    headers = {"Content-Type": "application/json"}

    start_event.wait()

    with requests.Session() as session:
        while not stop_event.is_set():

            dati = {
                "node_id": node_id,
                "latitude": latitude,
                "longitude": longitude,
                "counts": {
                    "pedestrians": random.randint(0, 20),
                    "cyclists": random.randint(0, 10),
                    "ambulances": random.randint(0, 2)
                }
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

                print(
                    f"[{node_id}] HTTP {risposta.status_code} "
                    f"- {latenza:.3f}s - "
                    f"GPS: {latitude}, {longitude}",
                    flush=True
                )

            except requests.RequestException as errore:
                latenza = time.perf_counter() - inizio

                result_queue.put({
                    "node_id": node_id,
                    "status": None,
                    "latency": latenza,
                    "success": False,
                    "error": str(errore)
                })

                print(f"[{node_id}] ERRORE: {errore}", flush=True)

            attesa = max(0, INTERVAL - (time.perf_counter() - inizio))

            if stop_event.wait(attesa):
                break


def percentile(valori, percentuale):
    if not valori:
        return 0
    valori = sorted(valori)
    indice = round((len(valori) - 1) * percentuale / 100)
    return valori[indice]


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

    print(
        f"\nAvvio test: {NUM_NODES} nodi, "
        f"{DURATION}s, {INTERVAL}s per richiesta/nodo\n"
    )

    inizio_test = time.perf_counter()
    start_event.set()

    try:
        time.sleep(DURATION)
    except KeyboardInterrupt:
        print("\nInterruzione del test...")
    finally:
        stop_event.set()

        for p in processi:
            p.join(timeout=TIMEOUT + 2)

        for p in processi:
            if p.is_alive():
                p.terminate()
                p.join()

    durata = time.perf_counter() - inizio_test
    risultati = []

    while True:
        try:
            risultati.append(result_queue.get_nowait())
        except Empty:
            break

    result_queue.close()

    successi = [r for r in risultati if r["success"]]
    errori = [r for r in risultati if not r["success"]]
    latenze = [r["latency"] for r in risultati]

    print("\n========== RISULTATI ==========")
    print(f"Durata:             {durata:.2f}s")
    print(f"Nodi:               {NUM_NODES}")
    print(f"Richieste totali:   {len(risultati)}")
    print(f"Successi HTTP 2xx:  {len(successi)}")
    print(f"Errori:             {len(errori)}")

    if durata > 0:
        print(f"Richieste/s:        {len(risultati) / durata:.2f}")

    if latenze:
        print(f"Latenza media:      {statistics.mean(latenze):.3f}s")
        print(f"Latenza mediana:    {statistics.median(latenze):.3f}s")
        print(f"Latenza p95:        {percentile(latenze, 95):.3f}s")
        print(f"Latenza massima:    {max(latenze):.3f}s")

    print("\nRichieste per nodo:")
    for numero in range(1, NUM_NODES + 1):
        node_id = f"node_{numero:02d}"
        righe = [r for r in risultati if r["node_id"] == node_id]
        ok = sum(1 for r in righe if r["success"])
        print(f"{node_id}: {ok}/{len(righe)} riuscite")


if __name__ == "__main__":
    main()
