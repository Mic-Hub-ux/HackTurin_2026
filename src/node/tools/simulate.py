"""Prova il motore di situation awareness sul PC, senza board e senza camera.

Genera detection finte (in pixel, come il brick, con etichette COCO come il modello di
default) per uno scenario per ogni regola e controlla che escano ESATTAMENTE gli eventi
attesi: ne' meno (regola che non scatta) ne' di piu' (falso allarme).

Uso:  python simulate.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from situation import SituationEngine  # noqa: E402

W, H, FPS = 640, 480, 4

# Strada vista dall'alto: immagine (0..1) -> metri. x: 0..10 m (sinistra-destra), y: 0..30 m
# (0 = vicino alla camera). Corsia destra = verso la camera, corsia sinistra = verso il fondo.
CFG = {
    "node_id": "sim", "camera_id": "cam0", "frame_size": [W, H], "debug": False,
    "calibration": {"image_points": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "world_points_m": [[0, 30], [10, 30], [10, 0], [0, 0]]},
    "zones": {
        "bike_lane": [[0.78, 0.0], [0.88, 0.0], [0.88, 1.0], [0.78, 1.0]],
        "crosswalk": [[0.10, 0.60], [0.90, 0.60], [0.90, 0.70], [0.10, 0.70]],
        "roadway": [[0.10, 0.0], [0.78, 0.0], [0.78, 1.0], [0.10, 1.0]],
    },
    "lines": {
        "linea_continua": {"type": "solid", "points": [[0.5, 0.0], [0.5, 1.0]]},
        "linea_arresto": {"type": "stop", "points": [[0.5, 0.75], [0.78, 0.75]]},
    },
    "lanes": {
        "corsia_dx": {"polygon": [[0.5, 0], [0.78, 0], [0.78, 1], [0.5, 1]], "direction": [[0.64, 0.0], [0.64, 1.0]]},
        "corsia_sx": {"polygon": [[0.1, 0], [0.5, 0], [0.5, 1], [0.1, 1]], "direction": [[0.3, 1.0], [0.3, 0.0]]},
    },
    "approaches": {"via": [[0, 0], [1, 0], [1, 0.2], [0, 0.2]], "incrocio": [[0, 0.85], [1, 0.85], [1, 1], [0, 1]]},
    "traffic_light": {"mode": "external"},
}


def img(wx, wy):
    return wx / 10, 1 - wy / 30


def box(label, wx, wy, w, h):
    x, y = img(wx, wy)
    return label, (x - w / 2) * W, (y - h) * H, (x + w / 2) * W, y * H


def auto(path, dur, label="car"):
    return lambda tau: [box(label, *path(tau), 0.08, 0.06)] if 0 <= tau <= dur else []


def pedone(path, dur):
    return lambda tau: [box("person", *path(tau), 0.02, 0.07)] if 0 <= tau <= dur else []


def ciclista(path, dur):
    def f(tau):
        if not 0 <= tau <= dur:
            return []
        x, y = img(*path(tau))
        return [("person", (x - 0.012) * W, (y - 0.09) * H, (x + 0.012) * W, (y - 0.02) * H),
                ("bicycle", (x - 0.02) * W, (y - 0.04) * H, (x + 0.02) * W, y * H)]
    return f


def insieme(*gen):
    return lambda tau: [d for g in gen for d in g(tau)]


def zigzag(tau):  # entra nella ciclabile, esce, rientra
    wx = 8.3 if 2.5 <= tau < 3.8 or 5.0 <= tau < 6.4 else 6.4
    return wx, 30 - 4.17 * tau


def frenata(tau):  # 50 -> 10 km/h in 2 s, poi prosegue piano
    if tau <= 2:
        return 6.4, 30 - (13.9 * tau - 2.775 * tau * tau)
    return 6.4, 30 - 16.7 - 2.8 * (tau - 2)


DRITTA = lambda v: (lambda tau: (6.4, 30 - v * tau))  # noqa: E731  corsia destra, verso la camera

SCENARI = [  # (nome, detection, semaforo, eventi attesi)
    ("auto normale, nessun pericolo", auto(DRITTA(8.33), 3.6), None, set()),
    ("auto a 80 km/h", auto(DRITTA(22.2), 1.35), None, {"speeding"}),
    ("auto che taglia la linea continua", auto(lambda t: (6.4 - 1.8 * t / 3.6, 30 - 8.33 * t), 3.6),
     None, {"solid_line_crossing"}),
    ("auto contromano", auto(lambda t: (3.0, 30 - 8.33 * t), 3.6), None, {"wrong_way"}),
    ("auto che accelera 10 -> 45 km/h", auto(lambda t: (6.4, 30 - (2.8 * t + 1.617 * t * t)), 3.0),
     None, {"sudden_acceleration"}),
    ("frenata brusca 50 -> 10 km/h", auto(frenata, 4.0), None, {"harsh_braking"}),
    ("auto che invade 2 volte la ciclabile", auto(zigzag, 7.2), None, {"bike_lane_invasion"}),
    ("terza invasione -> incrocio a rischio", auto(lambda t: (8.3 if 2.5 <= t < 3.8 else 6.4, 30 - 4.17 * t), 5.0),
     None, {"bike_lane_invasion", "bike_lane_hotspot"}),
    ("ciclista sulla ciclabile", ciclista(lambda t: (8.3, 21 - 5 * t), 4.2), None, set()),
    ("auto non si ferma col pedone sulle strisce",
     insieme(pedone(lambda t: (1.5 + 1.2 * t, 10.5), 6.25), auto(DRITTA(8.33), 3.6)), None, {"pedestrian_not_yielded"}),
    ("pedone fuori dalle strisce", pedone(lambda t: (3.0 + 0.2 * t, 20), 5.0), None, {"pedestrian_outside_crosswalk"}),
    ("auto sfiora un pedone", insieme(pedone(lambda t: (5.0 + 0.25 * t, 18), 6.0), auto(DRITTA(8.33), 3.6)),
     None, {"near_miss", "pedestrian_outside_crosswalk"}),
    ("auto ferma sulle strisce", auto(lambda t: (6.4, 16 - 2.78 * min(t, 2.0)), 10.0), None, {"illegal_stop"}),
    ("auto col rosso (solo se il semaforo e' noto)", auto(DRITTA(8.33), 3.6), "red", {"red_light_violation"}),
    ("ambulanza", auto(DRITTA(8.33), 3.6, label="ambulance"), "red", {"emergency_vehicle"}),
]


def main():
    eventi = []
    engine = SituationEngine(CFG, on_event=eventi.append)
    ok = True
    for i, (nome, gen, luce, attesi) in enumerate(SCENARI):
        t0 = 100 + i * 20
        engine.light.set_state(luce or "unknown", t0 - 5)
        prima = len(eventi)
        for k in range(int(16 * FPS)):
            t = t0 + k / FPS
            det = {}
            for label, x1, y1, x2, y2 in gen(t - t0):
                det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
            engine.update(det, t)
        nuovi = eventi[prima:]
        hazard = [e for e in nuovi if e["type"] == "hazard"]
        flussi = [f"{e['class']} {e['from']}->{e['to']}" for e in nuovi if e["type"] == "track"]
        trovati = {e["kind"] for e in hazard}
        esito = "OK " if trovati == attesi else "ERR"
        ok &= trovati == attesi
        print(f"[{esito}] {nome}: attesi={sorted(attesi)} trovati={sorted(trovati)} transiti={flussi}")
        for e in hazard:
            print(f"        {e['kind']} sev={e['severity']} ({e['norma']}) {e.get('actor', {})} {e.get('details', '')}")
    print("\nTUTTO OK" if ok else "\nQUALCOSA NON VA")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
