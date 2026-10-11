"""Batteria di test completa del nodo Smart Crossing (sul PC, senza board e senza camera).

Prova: tutte le regole (caso positivo e negativo), i riepiloghi delle tracce, la robustezza,
la configurazione, il nodo intero (archivio, statistiche, stato, LED), il server sul PC
(inoltro per direzione, simulazione dalla dashboard), il peer-to-peer tra due incroci senza
server, l'API locale con la pagina per il telefono e la fusione tra due camere.

Uso:  python test_suite.py          (dalla cartella node/tools; esce con codice 1 se qualcosa fallisce)
"""
import copy
import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[0] / "python"))
sys.path.insert(0, str(QUI.parents[1] / "server"))
sys.path.insert(0, str(QUI))

import simulate as sim  # noqa: E402  (generatori di detection finte)
from config import load_config, prepare, public_config  # noqa: E402
from situation import SituationEngine  # noqa: E402

RISULTATI = []
FPS = sim.FPS


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {info}"))


def test(fn):
    """Decoratore: un test che solleva un'eccezione conta come fallito, gli altri continuano."""
    def wrapper():
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=4)}")
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------- scena di prova

CFG = copy.deepcopy(sim.CFG)
CFG["node_id"] = "incrocio-1"
CFG["zones"].update({
    "sidewalk": [[0.88, 0.0], [1.0, 0.0], [1.0, 1.0], [0.88, 1.0]],
    "waiting_area": [[0.88, 0.5], [1.0, 0.5], [1.0, 0.6], [0.88, 0.6]],
    "box": [[0.1, 0.9], [0.78, 0.9], [0.78, 1.0], [0.1, 1.0]],
})
CFG["neighbors"] = {"via": "incrocio-2", "incrocio": "incrocio-3"}
CFG_IMG = copy.deepcopy(CFG)
CFG_IMG["calibration"] = {"image_points": None, "world_points_m": None}


def detections(gen, tau):
    det = {}
    for label, x1, y1, x2, y2 in gen(tau):
        det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
    return det


def run(gen, cfg=None, luce=None, durata=16.0, t0=1000.0, engine=None):
    msgs = []
    eng = engine or SituationEngine(copy.deepcopy(cfg or CFG), on_event=msgs.append)
    if engine is not None:
        eng.on_event = msgs.append
    eng.light.set_state(luce or "unknown", t0 - 5)
    for k in range(int(durata * FPS)):
        t = t0 + k / FPS
        eng.update(detections(gen, t - t0), t)
    return eng, msgs


def kinds(msgs, action=None):
    return {m["kind"] for m in msgs if m["type"] == "hazard" and (action is None or m["action"] == action)}


def tracks(msgs, classe=None):
    return [m for m in msgs if m["type"] == "track" and (classe is None or m["class"] == classe)]


def contiene(nome, msgs, attesi=(), vietati=()):
    k = kinds(msgs)
    check(nome, set(attesi) <= k and not (set(vietati) & k),
          f"attesi {sorted(attesi)}, vietati {sorted(vietati)}, trovati {sorted(k)}")


DRITTA = sim.DRITTA


# ---------------------------------------------------------------------- 1. regole

@test
def test_scenari_base():
    check("simulate.py: 15 scenari base", sim.main() == 0)


@test
def test_u_turn():
    def inversione(t):  # 20 km/h, rallenta e gira su un semicerchio di 1,7 m (~10 km/h), riparte
        if t < 2:
            return 6.4, 25 - 5.56 * t
        if t < 4:
            th = math.pi * (t - 2) / 2
            return 4.7 + 1.7 * math.cos(th), 13.88 - 1.7 * math.sin(th)
        return 3.0, 13.88 + 5.56 * (t - 4)
    _, m = run(sim.auto(inversione, 6.5))
    contiene("u_turn (inversione a U + linea continua)", m, {"u_turn", "solid_line_crossing"}, {"wrong_way"})
    _, m = run(sim.auto(DRITTA(8.33), 3.6))
    contiene("u_turn: auto dritta non e' un'inversione", m, vietati={"u_turn"})
    # due auto in versi opposti che si incrociano vicine: uno scambio di tracce NON e' un'inversione
    gen = sim.insieme(sim.auto(lambda t: (5.6, 25 - 8.33 * t), 3.0), sim.auto(lambda t: (4.4, 0 + 8.33 * t), 3.0))
    _, m = run(gen)
    contiene("u_turn: due auto che si incrociano non sono un'inversione", m, vietati={"u_turn"})
    salto = lambda t: (6.4, 25 - 8.33 * t) if t < 1.5 else (6.4, 5 + 8.33 * (t - 1.5))  # noqa: E731
    _, m = run(sim.auto(salto, 3.0))
    contiene("u_turn: salto improvviso (taglio di scena) non e' un'inversione", m, vietati={"u_turn"})


@test
def test_collisione_e_mancato_investimento():
    gen = sim.insieme(sim.pedone(lambda t: (9.5 - 1.4 * t, 15), 6.0), sim.auto(DRITTA(8.33), 3.6))
    _, m = run(gen)
    contiene("collision_risk previsto prima del near_miss", m, {"collision_risk", "near_miss"})
    rischio = [x for x in m if x.get("kind") == "collision_risk"]
    check("collision_risk ha ttc e distanza minima", rischio and "ttc_s" in rischio[0]["details"]
          and rischio[0]["details"]["min_distance"] <= 0.8, rischio[:1])
    gen = sim.insieme(sim.pedone(lambda t: (1.5, 20), 6.0), sim.auto(DRITTA(8.33), 3.6))
    _, m = run(gen)
    contiene("pedone lontano (5 m): nessun rischio", m, vietati={"collision_risk", "near_miss"})


@test
def test_sorpasso_ciclista():
    gen = sim.insieme(sim.ciclista(lambda t: (7.3, 20 - 4.17 * t), 4.5), sim.auto(lambda t: (6.3, 30 - 11.1 * t), 2.6))
    _, m = run(gen)
    contiene("close_pass: sorpasso a 1 m dal ciclista", m, {"close_pass"})
    ev = [x for x in m if x.get("kind") == "close_pass"]
    check("close_pass misura la distanza laterale", ev and ev[0]["details"]["lateral_m"] < 1.5, ev[:1])
    gen = sim.insieme(sim.ciclista(lambda t: (7.3, 20 - 4.17 * t), 4.5), sim.auto(lambda t: (3.3, 30 - 11.1 * t), 2.6))
    _, m = run(gen)
    contiene("sorpasso a 4 m: nessun close_pass", m, vietati={"close_pass"})


@test
def test_coda():
    def fermi(t):
        if t > 25:
            return []
        return [sim.box("car", x, y, 0.08, 0.06) for x, y in ((6.4, 20), (6.4, 15), (3.0, 18))]
    _, m = run(fermi, durata=45)
    check("congestion: nasce con 3 auto ferme", "congestion" in kinds(m, "new"), sorted(kinds(m)))
    check("congestion: si chiude (cancel) quando la coda sparisce", "congestion" in kinds(m, "cancel"), sorted(kinds(m)))
    nuovi = [x for x in m if x.get("kind") == "congestion" and x["action"] == "new"]
    canc = [x for x in m if x.get("kind") == "congestion" and x["action"] == "cancel"]
    check("congestion: cancel con lo stesso id del new", nuovi and canc and nuovi[0]["id"] == canc[0]["id"])


@test
def test_pedoni():
    _, m = run(sim.pedone(lambda t: (1.5 + 0.5 * t, 10.5), 10))
    contiene("slow_pedestrian: 0,5 m/s sulle strisce", m, {"slow_pedestrian"}, {"pedestrian_outside_crosswalk"})
    _, m = run(sim.pedone(lambda t: (1.5 + 1.2 * t, 10.5), 6))
    contiene("pedone a 1,2 m/s: non e' lento", m, vietati={"slow_pedestrian"})
    _, m = run(sim.pedone(lambda t: (9.4, 13.5), 25), durata=30)
    check("pedestrian_waiting: new dopo 20 s e cancel quando se ne va",
          "pedestrian_waiting" in kinds(m, "new") and "pedestrian_waiting" in kinds(m, "cancel"), sorted(kinds(m)))
    folla = sim.insieme(*[sim.pedone((lambda i: lambda t: (1.5 + i + 1.2 * t, 10.5))(i), 4) for i in range(6)])
    _, m = run(folla)
    contiene("crowd_on_crosswalk: 6 pedoni sulle strisce", m, {"crowd_on_crosswalk"})
    _, m = run(sim.ciclista(lambda t: (9.4, 20 - 3 * t), 5))
    contiene("cyclist_on_sidewalk", m, {"cyclist_on_sidewalk"})
    _, m = run(sim.ciclista(lambda t: (8.3, 20 - 3 * t), 5))
    contiene("ciclista sulla ciclabile: nessun evento", m, vietati={"cyclist_on_sidewalk", "wrong_way"})


@test
def test_movimento_vietato():
    cfg = copy.deepcopy(CFG)
    cfg["forbidden_movements"] = [["via", "incrocio"]]
    _, m = run(sim.auto(DRITTA(8.33), 3.6), cfg=cfg)
    contiene("forbidden_movement via->incrocio", m, {"forbidden_movement"})
    _, m = run(sim.auto(DRITTA(8.33), 3.6))
    contiene("senza divieti: nessun forbidden_movement", m, vietati={"forbidden_movement"})


@test
def test_semaforo():
    _, m = run(sim.pedone(lambda t: (9.5 - 1.2 * t, 10.5), 7), luce="green")
    contiene("pedestrian_red_crossing (verde per le auto)", m, {"pedestrian_red_crossing"})
    _, m = run(sim.pedone(lambda t: (9.5 - 1.2 * t, 10.5), 7), luce="red")
    contiene("pedone col verde suo: nessun evento", m, vietati={"pedestrian_red_crossing"})
    _, m = run(sim.auto(DRITTA(8.33), 3.6), luce="red")
    contiene("red_light_violation col rosso noto", m, {"red_light_violation"})
    _, m = run(sim.auto(DRITTA(8.33), 3.6))
    contiene("semaforo sconosciuto: nessuna regola sul rosso", m,
             vietati={"red_light_violation", "late_yellow", "pedestrian_red_crossing"})


@test
def test_cicli_di_vita():
    def sosta(t):
        if t < 2:
            return 6.4, 16 - 2.78 * t
        if t < 10:
            return 6.4, 10.44
        return 6.4, 10.44 - 3 * (t - 10)
    _, m = run(sim.auto(sosta, 13))
    check("illegal_stop: new e poi cancel quando riparte",
          "illegal_stop" in kinds(m, "new") and "illegal_stop" in kinds(m, "cancel"), sorted(kinds(m)))
    _, m = run(sim.auto(DRITTA(8.33), 3.6, label="ambulance"))
    check("emergency_vehicle: new e cancel a fine traccia",
          "emergency_vehicle" in kinds(m, "new") and "emergency_vehicle" in kinds(m, "cancel"), sorted(kinds(m)))
    canc = [x for x in m if x.get("action") == "cancel"]
    check("il cancel ha durata e motivo", canc and "duration_s" in canc[0]["details"] and canc[0]["details"]["reason"])


@test
def test_regole_senza_calibrazione():
    def traffico(t):
        for i in range(9):  # 9 auto normali a 4 m/s, una ogni 10 s
            tau = t - i * 10
            if 0 <= tau <= 7:
                return [sim.box("car", 6.4, 29 - 4 * tau, 0.08, 0.06)]
        tau = t - 90  # poi una a 11 m/s (2,75 volte)
        if 0 <= tau <= 2.5:
            return [sim.box("car", 6.4, 29 - 11 * tau, 0.08, 0.06)]
        return []
    eng, m = run(traffico, cfg=CFG_IMG, durata=95)
    veloci = [x for x in m if x.get("kind") == "speeding"]
    check("immagine: speeding relativo solo per l'auto veloce", len(veloci) == 1 and veloci[0]["details"].get("relative"),
          [(x["actor"]["track"], x["details"]) for x in veloci])
    check("immagine: spazio 'image'", eng.space == "image")
    gen = sim.insieme(sim.pedone(lambda t: (6.0, 18), 6), sim.auto(DRITTA(8.33), 3.6))
    _, m = run(gen, cfg=CFG_IMG)
    contiene("immagine: near_miss in frazioni d'immagine", m, {"near_miss"})
    # pedone alto 1,7 m in questa scala (0,17 d'immagine): la velocita' si misura in "altezze al secondo"
    alto = lambda path, dur: lambda tau: [sim.box("person", *path(tau), 0.02, 0.17)] if 0 <= tau <= dur else []  # noqa: E731
    _, m = run(alto(lambda t: (1.5 + 0.5 * t, 10.5), 10), cfg=CFG_IMG)
    contiene("immagine: slow_pedestrian in altezze/s", m, {"slow_pedestrian"})
    _, m = run(alto(lambda t: (1.5 + 1.3 * t, 10.5), 6), cfg=CFG_IMG)
    contiene("immagine: pedone normale non e' lento", m, vietati={"slow_pedestrian"})


# ---------------------------------------------------------------------- 2. riepiloghi

@test
def test_riepiloghi():
    _, m = run(sim.pedone(lambda t: (1.5 + 1.2 * t, 10.5), 6.5))
    p = tracks(m, "person")
    check("attraversamento strisce A->B", p and p[0].get("crosswalk_crossing", {}).get("direction") == "A->B", p[:1])
    _, m = run(sim.pedone(lambda t: (8.5 - 1.2 * t, 10.5), 6.5))
    p = tracks(m, "person")
    check("attraversamento strisce B->A", p and p[0].get("crosswalk_crossing", {}).get("direction") == "B->A")
    _, m = run(sim.auto(DRITTA(8.33), 3.6))
    v = tracks(m, "vehicle")
    ok = v and v[0]["from"] == "via" and v[0]["to"] == "incrocio" and v[0]["towards"] == "incrocio" \
        and v[0]["zones"].get("crosswalk", 0) > 0 and abs(v[0]["speed_kmh"]["avg"] - 30) < 3 \
        and v[0]["unit"] == "m" and v[0]["events"] == []
    check("riepilogo veicolo: vie, direzione, zone, velocita'", ok, v[:1])
    _, m = run(sim.pedone(lambda t: (1.5 + 0.08 * t, 20.5), 60), durata=65)
    p = tracks(m, "person")
    check("traiettoria limitata a 50 punti", p and len(p[0]["trajectory"]) <= 50, len(p[0]["trajectory"]) if p else None)


# ---------------------------------------------------------------------- 3. robustezza

@test
def test_robustezza():
    msgs = []
    eng = SituationEngine(copy.deepcopy(CFG), on_event=msgs.append)
    strani = [None, {}, {"car": "x"}, {"car": [{"bounding_box_xyxy": [1, 2]}]}, {"car": [{}]},
              {"car": [{"confidence": "alta", "bounding_box_xyxy": [1, 2, 3, 4]}]},
              {"car": [{"confidence": 0.9, "bounding_box_xyxy": [float("nan"), 1, 2, 3]}]},
              {"car": [{"confidence": 0.9, "bounding_box_xyxy": [-500, -400, -100, -50]}]},
              {"giraffa": [{"confidence": 0.9, "bounding_box_xyxy": [10, 10, 50, 50]}]}, "testo", [1, 2]]
    for i, d in enumerate(strani):
        eng.update(d, 100 + i)
    check("detection malformate: nessuna eccezione", True)
    eng.update({}, 200)
    eng.update({}, 150)  # orologio che torna indietro
    check("orologio all'indietro: il tempo del tracker non scende", eng.tracker.last_t >= 200)
    tante = {"car": [{"confidence": 0.9, "bounding_box_xyxy": ((i % 25) * 25, (i // 25) * 24, (i % 25) * 25 + 20,
                                                               (i // 25) * 24 + 20)} for i in range(500)]}
    t0 = time.perf_counter()
    for k in range(3):
        eng.update(tante, 300 + k / FPS)
    dt = time.perf_counter() - t0
    check("500 detection per frame: tracce limitate e tempi accettabili",
          len(eng.tracker.tracks) <= eng.tracker.max_tracks and dt < 30, f"{len(eng.tracker.tracks)} tracce, {dt:.1f} s")

    gen = sim.insieme(sim.pedone(lambda t: (6.0, 18), 6), sim.auto(DRITTA(22.2), 1.35))
    eng = SituationEngine(copy.deepcopy(CFG), on_event=lambda m: None)
    eng._vru_rules = lambda *a: 1 / 0  # una regola che va in errore...
    _, m = run(gen, engine=eng)
    contiene("regola in errore: le altre continuano (speeding)", m, {"speeding"}, {"near_miss"})
    check("regola in errore: contata in rule_errors", eng.rule_errors.get("vru", 0) > 0, dict(eng.rule_errors))

    cfg = copy.deepcopy(CFG)
    cfg["rules"] = {"speeding": {"enabled": False}}
    _, m = run(sim.auto(DRITTA(22.2), 1.35), cfg=cfg)
    contiene("regola disattivata da config", m, vietati={"speeding"})


@test
def test_configurazione():
    def avvisi(raw):
        return " | ".join(prepare(raw)[1])
    check("config: poligono con 2 punti", "bike_lane" in avvisi({"zones": {"bike_lane": [[0, 0], [1, 1]]}}))
    check("config: coordinate in pixel", "pixel" in avvisi({"zones": {"crosswalk": [[100, 200], [300, 200], [300, 400]]}}))
    check("config: regola sconosciuta", "pippo" in avvisi({"rules": {"pippo": {"enabled": False}}}))
    check("config: soglia sconosciuta", "pluto" in avvisi({"thresholds": {"pluto": 1}}))
    check("config: calibrazione con punti allineati",
          "calibrazione" in avvisi({"calibration": {"image_points": [[0, 0], [0.5, 0.5], [1, 1], [0.2, 0.2]],
                                                    "world_points_m": [[0, 0], [1, 1], [2, 2], [3, 3]]}}))
    with tempfile.TemporaryDirectory() as d:
        rotto = Path(d) / "rotto.json"
        rotto.write_text('{"node_id": "x",', encoding="utf-8")
        cfg, w = load_config(rotto)
        check("config: JSON non valido -> default + avviso", "JSON non valido" in " ".join(w) and cfg["node_id"])
        bom = Path(d) / "bom.json"
        bom.write_text(json.dumps({"node_id": "con-bom"}), encoding="utf-8-sig")
        cfg, w = load_config(bom)
        check("config: file con BOM (Notepad)", cfg["node_id"] == "con-bom")
    pub = public_config(prepare({"server": {"token": "segreto"}, "p2p": {"key": "k"},
                                 "publish": {"token": "t"}})[0])
    check("config pubblica senza segreti", "segreto" not in json.dumps(pub) and pub["p2p"]["key"] == "***")


# ---------------------------------------------------------------------- 4-7. nodo, server, p2p, api

class FakeBridge:
    def __init__(self):
        self.calls = []

    def call(self, name, *args, **kw):
        self.calls.append((name, args))


def modes(packed):
    return [(packed >> (2 * i)) & 3 for i in range(4)]


def porta_libera():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url, data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def avvia_server(tmp):
    import server as srv
    hub = srv.Hub(Path(tmp) / "server.db", srv.Topology(QUI.parents[1] / "server" / "topology.json"))
    httpd = srv.make_server(hub, "127.0.0.1", 0)
    httpd.hub = hub
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def node_cfg(node_id, tmp_url=None, **extra):
    raw = copy.deepcopy(CFG)
    raw["node_id"] = node_id
    raw["data"] = {"stats_interval_s": 5, "status_interval_s": 2}
    if tmp_url:
        raw["server"] = {"url": tmp_url, "poll_s": 0.5}
    raw.update(extra)
    cfg, _ = prepare(raw)
    return cfg


def guida(node, gen, durata, t0, cam="cam0", bridge=None, registro=None):
    """Fa girare il nodo come sulla board: process() a ogni frame, tick() ogni secondo."""
    ultimo_tick = t0
    for k in range(int(durata * FPS)):
        t = t0 + k / FPS
        node.process(detections(gen, t - t0), cam, t)
        if t - ultimo_tick >= 1.0:
            ultimo_tick = t
            node.tick(t)
            if bridge is not None and registro is not None and bridge.calls:
                registro.append(modes(bridge.calls[-1][1][0]))


@test
def test_nodo_e_server():
    from node import Node
    with tempfile.TemporaryDirectory() as tmp:
        httpd, url = avvia_server(tmp)
        bridge = FakeBridge()
        node = Node(node_cfg("incrocio-1", url), base_dir=tmp, bridge=bridge, start_threads=False)
        try:
            t0 = time.time()
            led = []
            guida(node, sim.auto(sim.zigzag, 7.2), 16, t0, bridge=bridge, registro=led)
            st = node.storage
            ev = [e for e in st.query("events", limit=None) if e.get("type") == "hazard"]
            check("nodo: pericolo salvato nell'archivio", any(e["kind"] == "bike_lane_invasion" for e in ev),
                  [e["kind"] for e in ev])
            check("nodo: riepilogo traccia salvato", st.query("tracks", limit=None))
            tutte = st.query("stats", limit=None)
            check("nodo: statistiche periodiche", len(tutte) >= 2, len(tutte))
            cams = [(s.get("cameras") or {}).get("cam0", {}) for s in tutte]
            check("statistiche: conteggi, movimenti, velocita' V85, heatmap",
                  sum(c.get("counts", {}).get("vehicle", 0) for c in cams) >= 1
                  and any(c.get("movements") for c in cams) and any(c.get("speeds") for c in cams)
                  and any(c.get("heatmap") for c in cams), cams)
            check("archivio: heatmap accumulata", st.heatmap(camera="cam0"))
            check("nodo: stato di salute", (st.latest("status") or {}).get("cameras"))
            check("LED rosso (pericolo) acceso durante l'invasione", any(m[2] > 0 for m in led), led)
            check("LED giallo e verde spenti: c'era solo un'auto", not any(m[0] or m[3] for m in led), led)
            check("pericolo con incrocio successivo (next_nodes)", any(e.get("next_nodes") == ["incrocio-3"] for e in ev),
                  [e.get("next_nodes") for e in ev])

            for _ in range(20):  # svuota la coda verso il server
                if node.publisher.flush_once() == 0 and node.publisher.backlog() == 0:
                    break
            check("invio: coda verso il server vuota", node.publisher.backlog() == 0, node.publisher.backlog())
            ricevuti = http_json(f"{url}/api/events?limit=500")["messages"]
            check("server: ha ricevuto i pericoli", any(m.get("kind") == "bike_lane_invasion" for m in ricevuti))
            check("server: ha ricevuto tracce, statistiche e stato",
                  {"track", "stats", "status"} <= {m.get("type") for m in ricevuti}, {m.get("type") for m in ricevuti})
            in3 = http_json(f"{url}/api/inbox/incrocio-3?after=0")["messages"]
            in2 = http_json(f"{url}/api/inbox/incrocio-2?after=0")["messages"]
            check("server: inoltro SOLO verso l'incrocio dove va il veicolo (incrocio-3)",
                  any(m.get("kind") == "bike_lane_invasion" for m in in3)
                  and not any(m.get("kind") == "bike_lane_invasion" for m in in2),
                  (len(in3), len(in2)))

            http_json(f"{url}/api/simulate", {"from_node": "incrocio-2", "to_node": "incrocio-1",
                                              "kind": "bike_lane_invasion", "severity": 3})
            n = node.remote.poll_once()
            m = modes(bridge.calls[-1][1][0])  # senza tick: il LED cambia appena arriva il pericolo
            check("dashboard -> server -> board: LED rosso subito, senza aspettare il tick", n >= 1 and m[2] > 0, (n, m))
            node.tick(time.time())
            remoti = [e for e in st.query("events", limit=None) if e.get("type") == "remote_hazard"]
            check("pericolo remoto archiviato", remoti)

            doppio = dict(remoti[0])
            check("stesso pericolo dal p2p: gestito una volta sola", node._on_remote(doppio, via="p2p") is False)

            now = int(time.time() * 1000)
            node._on_server({"type": "remote_hazard", "id": "test-emerg", "kind": "emergency_vehicle",
                             "category": "emergency", "severity": 3, "action": "new", "from_node": "incrocio-2",
                             "ts": now, "expires": now + 60000})
            node.tick(time.time())
            check("ambulanza segnalata da un vicino: LED blu lampeggio veloce", modes(bridge.calls[-1][1][0])[1] == 3)
            node._on_server({"type": "remote_hazard", "id": "test-emerg", "kind": "emergency_vehicle",
                             "category": "emergency", "severity": 3, "action": "cancel", "from_node": "incrocio-2",
                             "ts": now, "expires": now})
            node.tick(time.time())
            check("cancel dell'ambulanza: LED blu spento", modes(bridge.calls[-1][1][0])[1] == 0)
            nodi = http_json(f"{url}/api/nodes")
            check("server: elenco nodi", "incrocio-1" in json.dumps(nodi))
            with urllib.request.urlopen(url + "/", timeout=5) as r:
                check("server: dashboard servita", b"<html" in r.read(2000).lower())
            with urllib.request.urlopen(url + "/app?node=incrocio-1", timeout=5) as r:
                check("server: pagina telefono su /app", b"<html" in r.read(3000).lower())
            for pagina in ("/cyclist", "/console", "/static/style.css"):
                with urllib.request.urlopen(url + pagina, timeout=5) as r:
                    check(f"server: pagina {pagina}", r.status == 200 and len(r.read()) > 100)
            traffico = http_json(f"{url}/api/traffic")
            n1 = next((n for n in traffico if n["node_id"] == "incrocio-1"), {})
            check("api/traffic: incrocio-1 con coordinate, conteggi e stato",
                  n1.get("coordinates") and set(n1.get("counts", {})) >= {"pedestrians", "cyclists", "ambulances", "vehicles"}
                  and n1.get("status") and n1.get("source") == "board", n1)
            req = urllib.request.Request(f"{url}/api/traffic", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"node_id": "node_07", "coordinates": {"lat": 45.06, "lon": 7.67},
                                                          "counts": {"pedestrians": 5, "cyclists": 2, "ambulances": 0}}).encode())
            with urllib.request.urlopen(req, timeout=5) as r:
                check("api/traffic POST del client.py (201)", r.status == 201)
            n7 = next((n for n in http_json(f"{url}/api/traffic") if n["node_id"] == "node_07"), {})
            check("api/traffic: nodo del client.py sulla mappa", n7.get("source") == "client" and n7["counts"]["pedestrians"] == 5)
            tel = http_json(f"{url}/phone/incrocio-1/api/events?since=0")
            check("server: API telefono = pericoli del nodo + quelli dei vicini",
                  any(m.get("type") == "hazard" and m.get("kind") == "bike_lane_invasion" for m in tel)
                  and any(m.get("type") == "remote_hazard" for m in tel), [(m.get("type"), m.get("kind")) for m in tel])
        finally:
            node.close()
            httpd.shutdown()
            httpd.server_close()
            httpd.hub.close()


@test
def test_led_tempo_reale():
    """Giallo = persona, verde = ciclista, blu = emergenza: accesi al frame stesso, senza tick."""
    from node import Node
    with tempfile.TemporaryDirectory() as tmp:
        bridge = FakeBridge()
        node = Node(node_cfg("incrocio-1"), base_dir=tmp, bridge=bridge, start_threads=False)
        try:
            def frame(dets, t):
                det = {}
                for label, x1, y1, x2, y2 in dets:
                    det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
                node.process(det, "cam0", t)
                return modes(bridge.calls[-1][1][0]) if bridge.calls else None

            t0 = time.time()
            persona = [sim.box("person", 3.0, 15.0, 0.02, 0.07)]
            m = [frame(persona, t0 + k * 0.1) for k in range(3)]
            check("LED giallo acceso appena la camera vede una persona (2-3 frame)", m[-1] and m[-1][0] == 1, m)
            m = frame([], t0 + 1.5)
            check("LED giallo spento quando la persona sparisce", m and m[0] == 0, m)
            bici = sim.ciclista(lambda tau: (8.3, 15.0), 5)
            m = [frame(bici(0), t0 + 10 + k * 0.1) for k in range(3)]
            check("LED verde acceso con un ciclista", m[-1] and m[-1][3] == 1, m)
            amb = [sim.box("ambulance", 5.0, 15.0, 0.08, 0.06)]
            m = [frame(amb, t0 + 20 + k * 0.1) for k in range(3)]
            check("LED blu lampeggio veloce con un mezzo di emergenza", m[-1] and m[-1][1] == 3, m)
        finally:
            node.close()


@test
def test_led_pericolo_si_spegne():
    """Pericolo visto qui: spento appena l'oggetto sparisce. Pericolo di un vicino: resta acceso."""
    from leds import LedController
    bridge = FakeBridge()
    leds = LedController({"roles": ["person", "emergency", "hazard", "cyclist"]}, bridge=bridge, start_thread=False)
    t = time.time()
    now = int(t * 1000)
    leds.on_hazard({"id": "h1", "kind": "bike_lane_invasion", "severity": 3, "ts": now, "expires": now + 30000,
                    "actor": {"track": "cam0-9", "class": "vehicle"}}, True, t)
    leds.set_presence({"vehicle"}, t, ["cam0-9"])
    leds.push(t)
    check("LED rosso acceso mentre l'auto e' nella ciclabile", modes(bridge.calls[-1][1][0])[2] > 0)
    leds.set_presence({"vehicle"}, t + 0.3, ["cam0-9"])
    leds.push(t + 0.3)
    check("LED rosso ancora acceso finche' l'auto si vede", modes(bridge.calls[-1][1][0])[2] > 0)
    leds.push(t + 1.2)
    check("LED rosso spento appena l'auto sparisce (non dopo 10 s)", modes(bridge.calls[-1][1][0])[2] == 0)
    leds.on_hazard({"id": "r1", "kind": "bike_lane_invasion", "severity": 2, "ts": now, "expires": now + 30000,
                    "from_node": "incrocio-2"}, False, t + 1.2)
    leds.push(t + 5)
    check("pericolo di un vicino: LED rosso resta acceso anche senza oggetti in vista",
          modes(bridge.calls[-1][1][0])[2] > 0)
    leds.close()


@test
def test_inoltro_percorsi():
    """Inoltro sulla topologia vera: lungo la direzione (2 o 3 incroci), ai vicini, sul percorso piu' breve."""
    import server as srv
    with tempfile.TemporaryDirectory() as tmp:
        hub = srv.Hub(Path(tmp) / "server.db", srv.Topology(QUI.parents[1] / "server" / "topology.json"))
        try:
            def a_chi(**body):
                return [d["node"] for d in hub.simulate(body)["delivered_to"]]
            check("percorso piu' breve 4 -> 1: anche gli incroci in mezzo",
                  a_chi(from_node="incrocio-4", to_node="incrocio-1") == ["incrocio-3", "incrocio-2", "incrocio-1"])
            check("direzione nota: 2 incroci lungo la strada",
                  a_chi(from_node="incrocio-1", towards="sud-ovest") == ["incrocio-2", "incrocio-3"])
            check("ambulanza con direzione nota: 3 incroci lungo la strada",
                  a_chi(from_node="incrocio-1", towards="sud-ovest", kind="emergency_vehicle")
                  == ["incrocio-2", "incrocio-3", "incrocio-4"])
            check("direzione come punto cardinale (nord)",
                  a_chi(from_node="incrocio-3", towards="nord")[:1] == ["incrocio-2"])
            check("direzione sconosciuta: solo gli incroci vicini",
                  sorted(a_chi(from_node="incrocio-6")) == ["incrocio-1", "incrocio-5", "incrocio-7"])
        finally:
            hub.close()


@test
def test_token_e_env():
    """Token dei dispositivi (SC_TOKEN): scritture protette, letture della dashboard pubbliche; .env -> nodo."""
    import server as srv
    with tempfile.TemporaryDirectory() as tmp:
        hub = srv.Hub(Path(tmp) / "server.db", srv.Topology(QUI.parents[1] / "server" / "topology.json"))
        httpd = srv.make_server(hub, "127.0.0.1", 0, token="prova-token")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"

        def stato(path, data=None, token=None):
            h = {"Content-Type": "application/json"}
            if token:
                h["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url + path, data=json.dumps(data).encode() if data is not None else None,
                                         headers=h)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        traffico = {"node_id": "node_01", "counts": {"pedestrians": 1, "cyclists": 0, "ambulances": 0}}
        try:
            check("token: POST /api/traffic senza token -> 401", stato("/api/traffic", traffico) == 401)
            check("token: POST /api/traffic con token sbagliato -> 401", stato("/api/traffic", traffico, "no") == 401)
            check("token: POST /api/traffic con il token giusto -> 201",
                  stato("/api/traffic", traffico, "prova-token") == 201)
            check("token: casella dei pericoli del nodo protetta",
                  stato("/api/inbox/incrocio-1") == 401 and stato("/api/inbox/incrocio-1", token="prova-token") == 200)
            check("token: Simula pericolo protetto",
                  stato("/api/simulate", {"from_node": "incrocio-2", "to_node": "incrocio-1"}) == 401)
            pubbliche = ("/", "/cyclist", "/api/traffic", "/api/summary", "/api/nodes", "/api/events", "/api/kinds")
            check("token: pagine e letture della dashboard pubbliche",
                  all(stato(p) == 200 for p in pubbliche), {p: stato(p) for p in pubbliche})
        finally:
            httpd.shutdown()
            httpd.server_close()
            hub.close()

        app = Path(tmp) / "app"
        app.mkdir()
        (app / ".env").write_text('# commento\nSC_TOKEN="dal-env"\nexport SC_P2P_KEY=chiave-env\n', encoding="utf-8")
        (app / "config.json").write_text(json.dumps({"server": {"url": "http://x"}, "p2p": {"key": "dal-config"}}),
                                         encoding="utf-8")
        salvate = {k: os.environ.pop(k, None) for k in ("SC_TOKEN", "SC_P2P_KEY")}
        try:
            cfg, _ = load_config(app / "config.json")
            check(".env: token del server letto dal .env", cfg["server"]["token"] == "dal-env", cfg["server"])
            check(".env: un valore scritto in config.json vince sul .env", cfg["p2p"]["key"] == "dal-config")
        finally:
            for k, v in salvate.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


@test
def test_peer_to_peer():
    from node import Node
    with tempfile.TemporaryDirectory() as tmp:
        pa, pb = porta_libera(), porta_libera()
        cfg_a = node_cfg("incrocio-1", p2p={"enabled": True, "port": pa, "bind_port": pa,
                                            "peers": [f"127.0.0.1:{pb}"], "key": "chiave"})
        cfg_b = node_cfg("incrocio-3", p2p={"enabled": True, "port": pb, "bind_port": pb,
                                            "peers": [f"127.0.0.1:{pa}"], "key": "chiave"})
        cfg_b["neighbors"] = {"ovest": "incrocio-1"}
        bridge_b = FakeBridge()
        a = Node(cfg_a, base_dir=Path(tmp) / "a", bridge=FakeBridge(), start_threads=False)
        b = Node(cfg_b, base_dir=Path(tmp) / "b", bridge=bridge_b, start_threads=False)
        try:
            check("p2p: UDP attivo su entrambi", a.peer.state()["udp"] and b.peer.state()["udp"])
            t0 = time.time()
            for k in range(int(10 * FPS)):  # A vede l'auto che invade la ciclabile e va verso incrocio-3
                t = t0 + k / FPS
                a.process(detections(sim.auto(sim.zigzag, 7.2), t - t0), "cam0", t)
                b.peer.poll_once(0.05)
            b.tick(time.time())
            remoti = [e for e in b.storage.query("events", limit=None) if e.get("type") == "remote_hazard"]
            check("p2p senza server: incrocio-3 riceve il pericolo di incrocio-1",
                  any(e.get("kind") == "bike_lane_invasion" and e.get("via") == "p2p" for e in remoti),
                  [(e.get("kind"), e.get("via")) for e in remoti])
            check("p2p: LED rosso acceso su incrocio-3", bridge_b.calls and modes(bridge_b.calls[-1][1][0])[2] > 0)
            a.peer.send_heartbeat()
            b.peer.poll_once(0.3)
            check("p2p: heartbeat -> vicino online", b.peer.online and "incrocio-1" in b.peer.peers())
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:  # beacon di un braccialetto
                s.sendto(json.dumps({"v": 1, "t": "vru", "d": "polso-42", "p": "cyclist", "ts": 1, "h": 0}).encode(),
                         ("127.0.0.1", pb))
            b.peer.poll_once(0.3)
            check("p2p: braccialetto ciclista rilevato", "polso-42" in b.peer.vrus())
        finally:
            a.close()
            b.close()


@test
def test_api_e_pagina_telefono():
    from node import Node
    with tempfile.TemporaryDirectory() as tmp:
        cfg = node_cfg("incrocio-1", api={"enabled": True, "host": "127.0.0.1", "port": 0},
                       server={"url": "", "token": "segreto"})
        node = Node(cfg, base_dir=tmp, bridge=FakeBridge(), start_threads=False)
        try:
            node.api.start()
            base = f"http://127.0.0.1:{node.api.port}"
            guida(node, sim.auto(sim.zigzag, 7.2), 10, time.time())
            check("api: health", http_json(base + "/api/health").get("ok") is True)
            check("api: eventi", any(e.get("kind") == "bike_lane_invasion" for e in http_json(base + "/api/events")))
            check("api: tracce", http_json(base + "/api/tracks"))
            check("api: config senza token", "segreto" not in json.dumps(http_json(base + "/api/config")))
            with urllib.request.urlopen(base + "/app", timeout=5) as r:
                check("api: pagina telefono su /app", b"<html" in r.read(3000).lower())
        finally:
            node.close()


@test
def test_fusione_due_camere():
    from node import Node
    cam = {k: copy.deepcopy(CFG[k]) for k in ("calibration", "zones", "lines", "lanes", "approaches")}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = node_cfg("incrocio-1", cameras={"cam0": copy.deepcopy(cam), "cam1": copy.deepcopy(cam)})
        node = Node(cfg, base_dir=tmp, bridge=FakeBridge(), start_threads=False)
        try:
            t0 = time.time()
            guida(node, sim.auto(DRITTA(8.33), 3.6), 3.75, t0, cam="cam0")   # entra al centro dalla cam0...
            t1 = t0 + 3.75
            esce = sim.auto(lambda t: (3.0, 1 + 8.33 * t), 3.3)               # ...ed esce verso la via nella cam1
            guida(node, esce, 10, t1, cam="cam1")
            for k in range(8):
                node.tick(t1 + 10 + k)
            mov = [e for e in node.storage.query("events", limit=None) if e.get("type") == "movement"]
            check("fusione: movimento da cam0 a cam1", mov and mov[0]["from"].startswith("cam0:")
                  and mov[0]["to"].startswith("cam1:"), mov[:1])
        finally:
            node.close()


def main():
    for fn in (test_scenari_base, test_u_turn, test_collisione_e_mancato_investimento, test_sorpasso_ciclista,
               test_coda, test_pedoni, test_movimento_vietato, test_semaforo, test_cicli_di_vita,
               test_regole_senza_calibrazione, test_riepiloghi, test_robustezza, test_configurazione,
               test_nodo_e_server, test_led_tempo_reale, test_led_pericolo_si_spegne,
               test_inoltro_percorsi, test_token_e_env, test_peer_to_peer, test_api_e_pagina_telefono,
               test_fusione_due_camere):
        print(f"\n== {fn.__name__}")
        fn()
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
