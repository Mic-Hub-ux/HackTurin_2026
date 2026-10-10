import random
import requests
import multiprocessing as mp
import time
import statistics
from queue import Empty

URL = "https://michubux.pythonanywhere.com/api/traffic"

NUM_NODES = 12
DURATION = 60
INTERVAL = 1
TIMEOUT = 15

# Griglia rettangolare 4x3 di 12 incroci reali a Torino
LUOGHI_TORINO = [
    # Riga 1
    {"name": "node_01", "latitude": 45.0625, "longitude": 7.6720},
    {"name": "node_02", "latitude": 45.0625, "longitude": 7.6735},
    {"name": "node_03", "latitude": 45.0625, "longitude": 7.6750},
    # Riga 2
    {"name": "node_04", "latitude": 45.0613, "longitude": 7.6720},
    {"name": "node_05", "latitude": 45.0613, "longitude": 7.6735},
    {"name": "node_06", "latitude": 45.0613, "longitude": 7.6750},
    # Riga 3
    {"name": "node_07", "latitude": 45.0601, "longitude": 7.6720},
    {"name": "node_08", "latitude": 45.0601, "longitude": 7.6735},
    {"name": "node_09", "latitude": 45.0601, "longitude": 7.6750},
    # Riga 4
    {"name": "node_10", "latitude": 45.0589, "longitude": 7.6720},
    {"name": "node_11", "latitude": 45.0589, "longitude": 7.6735},
    {"name": "node_12", "latitude": 45.0589, "longitude": 7.6750},
]


def client_worker(node_number, start_event, stop_event, result_queue):
    node_id = f"node_{node_number:02d}"

    # Assegnazione fissa e deterministica alla griglia 4x3
    luogo = LUOGHI_TORINO[node_number - 1]

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

            # Ambulanze più rare (circa 3% di probabilità)
            is_ambulance = 1 if random.random() < 0.03 else 0

            dati = {
                "node_id": node_id,
                "coordinates": {
                    "lat": latitude,
                    "lon": longitude,
                },
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

                print(
                    f"[{node_id}] HTTP {risposta.status_code} "
                    f"- {latenza:.3f}s - "
                    f"Amb: {is_ambulance}",
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
        f"\nAvvio test griglia: {NUM_NODES} nodi (4x3), "
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