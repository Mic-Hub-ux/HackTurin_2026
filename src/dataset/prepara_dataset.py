"""Prepara il dataset per Edge Impulse.

Classi finali (4):
    person     -> pedoni (adulti e bambini insieme)
    cyclist    -> UNA box che contiene persona + bicicletta
    vehicle    -> auto, furgoni, bus, camion, moto (con il motociclista)
    emergency  -> ambulanze, polizia, pompieri

Sorgenti:
    Open Images V7 (CC BY 4.0 / CC BY 2.0): persone, ciclisti (persona sopra una bici), ambulanze.
        Le auto di Open Images non vengono scaricate apposta (sono vecchie/straniere), ma quelle
        che compaiono nelle foto di persone vengono etichettate "vehicle": se restassero senza
        box il modello imparerebbe che un'auto e' "sfondo".
    roboflow/<nome>/<split>/   export Roboflow in formato COCO (mezzi di emergenza)
    video/*.mp4                 video vostri (es. un incrocio di Torino): un frame al secondo
                                finisce in foto_nostre/strada/ ed e' etichettato in automatico
    foto_nostre/strada/         foto/screenshot di strade, auto moderne, persone, ciclisti
    foto_nostre/emergency/      foto di mezzi di emergenza (il veicolo piu' grande = emergency)

Uso:
    python prepara_dataset.py            # unisce tutto ed esporta in dataset_oi/data
    python prepara_dataset.py --leggero  # versione piu' piccola (se Edge Impulse supera il tempo)
    python prepara_dataset.py --guarda   # in piu' apre il browser per controllare le box
"""
import sys
from pathlib import Path

import fiftyone as fo
import fiftyone.zoo as foz
from fiftyone import ViewField as F

SPLITS_LEGGERI = ["validation", "test"]
EXPORT_DIR = "dataset_oi"
ROBOFLOW_DIR = Path("roboflow")
VIDEO_DIR = Path("video")
STRADA_DIR = Path("foto_nostre") / "strada"
EMERGENZA_DIR = Path("foto_nostre") / "emergency"
NOME_FINALE = "smartcrossing_final"
CLASSI_FINALI = ["person", "cyclist", "vehicle", "emergency"]
ESTENSIONI = {".jpg", ".jpeg", ".png", ".webp"}
FRAME_OGNI_S = 1.0          # un frame al secondo dai video
MAX_SOLO_PERSONE = 1500     # con --leggero: massimo di immagini che contengono solo persone
MODELLO_ETICHETTE = "yolov8m.pt"  # usato solo per etichettare le foto vostre

# Etichette "temporanee": bicycle e motorcycle servono solo per costruire cyclist/vehicle.
MAPPA_OI = {
    "Person": "person", "Man": "person", "Woman": "person", "Boy": "person", "Girl": "person",
    "Bicycle": "bicycle",
    "Motorcycle": "motorcycle",
    "Car": "vehicle", "Bus": "vehicle", "Truck": "vehicle", "Van": "vehicle", "Taxi": "vehicle",
    "Ambulance": "emergency",
}
SINONIMI_ROBOFLOW = {
    "ambulance": "emergency",
    "police": "emergency", "police car": "emergency", "policecar": "emergency",
    "police vehicle": "emergency",
    "fire truck": "emergency", "firetruck": "emergency", "fire engine": "emergency",
    "fireengine": "emergency", "fire": "emergency",
    "emergency": "emergency", "emergency vehicle": "emergency",
    "car": "vehicle", "truck": "vehicle", "bus": "vehicle", "van": "vehicle",
    "cyclist": "cyclist", "bicyclist": "cyclist", "biker": "cyclist", "rider": "cyclist",
    "person": "person", "pedestrian": "person",
}
COCO_YOLO = {0: "person", 1: "bicycle", 2: "vehicle", 3: "motorcycle", 5: "vehicle", 7: "vehicle"}

# (nome dataset, classi Open Images, immagini massime per split, split)
GRUPPI_OI = [
    ("oi_child", ["Boy", "Girl"], 1500, SPLITS_LEGGERI),
    ("oi_adult", ["Man", "Woman"], 1500, SPLITS_LEGGERI),
    ("oi_bicycle", ["Bicycle"], 1500, SPLITS_LEGGERI),
    ("oi_ambulance", ["Ambulance"], None, SPLITS_LEGGERI),
]
if "oi_ambulance_train" in fo.list_datasets():
    GRUPPI_OI.append(("oi_ambulance_train", ["Ambulance"], None, ["train"]))


# --------------------------------------------------------------------------- box

def area(b):
    return b[2] * b[3]


def intersezione(a, b):
    iw = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    return iw * ih


def iou(a, b):
    inter = intersezione(a, b)
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def unione(a, b):
    x1, y1 = min(a[0], b[0]), min(a[1], b[1])
    x2, y2 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return [x1, y1, x2 - x1, y2 - y1]


def sopra(persona, mezzo):
    """La persona sta in sella al mezzo? (box relative [x, y, w, h], y verso il basso)"""
    if area(persona) == 0:
        return 0.0
    cx = persona[0] + persona[2] / 2
    piedi = persona[1] + persona[3]
    if not (mezzo[0] <= cx <= mezzo[0] + mezzo[2]):
        return 0.0
    if piedi < mezzo[1] + 0.3 * mezzo[3]:  # i piedi devono arrivare sul mezzo
        return 0.0
    return intersezione(persona, mezzo) / area(persona)


def sistema_box(dets):
    """dets: lista di (label, [x, y, w, h]) con label temporanee -> label finali.

    - toglie i doppioni (in Open Images la stessa persona e' spesso sia "Person" che "Man")
    - persona sopra una bici  -> UNA box "cyclist"
    - persona sopra una moto  -> UNA box "vehicle"
    - bici e moto senza nessuno sopra: le bici spariscono, le moto diventano "vehicle"
    - box "vehicle" che coincide con un mezzo di emergenza -> tolta
    """
    tenute = []
    for label, bb in sorted(dets, key=lambda d: -area(d[1])):
        if any(l == label and iou(bb, b) > 0.7 for l, b in tenute):
            continue
        tenute.append((label, bb))

    persone = [bb for l, bb in tenute if l == "person"]
    usate = set()
    finali = []
    for label, bb in tenute:
        if label not in ("bicycle", "motorcycle"):
            continue
        punteggi = [(sopra(p, bb), i) for i, p in enumerate(persone) if i not in usate]
        migliore = max(punteggi, default=(0.0, None))
        if migliore[0] >= 0.25:
            usate.add(migliore[1])
            nuova = "cyclist" if label == "bicycle" else "vehicle"
            finali.append((nuova, unione(persone[migliore[1]], bb)))
        elif label == "motorcycle":
            finali.append(("vehicle", bb))
    finali += [("person", p) for i, p in enumerate(persone) if i not in usate]
    finali += [(l, bb) for l, bb in tenute if l in ("vehicle", "emergency", "cyclist")]

    emergenze = [bb for l, bb in finali if l == "emergency"]
    return [
        (l, bb) for l, bb in finali
        if not (l == "vehicle" and any(iou(bb, e) > 0.5 for e in emergenze))
    ]


def a_detections(dets):
    return fo.Detections(detections=[fo.Detection(label=l, bounding_box=bb) for l, bb in dets])


# --------------------------------------------------------------------------- sorgenti

def carica_oi(nome, classi, max_samples, splits):
    if nome in fo.list_datasets():
        print(f"[{nome}] gia' scaricato, lo riuso")
        return fo.load_dataset(nome)
    print(f"[{nome}] scarico {classi} da {splits} ...")
    ds = foz.load_zoo_dataset(
        "open-images-v7", splits=splits, label_types=["detections"], classes=classi,
        max_samples=max_samples, shuffle=True, seed=42, dataset_name=nome,
    )
    ds.persistent = True
    return ds


def open_images():
    tutto = fo.Dataset("smartcrossing_oi", overwrite=True)
    for nome, classi, max_samples, splits in GRUPPI_OI:
        tutto.merge_samples(carica_oi(nome, classi, max_samples, splits))
    view = tutto.exists("ground_truth").filter_labels(
        "ground_truth",
        F("label").is_in(list(MAPPA_OI))
        & (F("IsGroupOf") != True)      # box che coprono un gruppo intero
        & (F("IsDepiction") != True)    # disegni, statue, poster
        & (F("bounding_box")[2] * F("bounding_box")[3] > 0.001),  # box minuscole
        only_matches=True,
    )
    return view.map_labels("ground_truth", MAPPA_OI)


def normalizza(nome):
    return nome.lower().replace("_", " ").replace("-", " ").strip()


def roboflow():
    viste = []
    for json_path in sorted(ROBOFLOW_DIR.glob("*/*/_annotations.coco.json")):
        split_dir = json_path.parent
        nome = f"rf_{split_dir.parent.name}_{split_dir.name}"
        print(f"[{nome}] carico {split_dir}")
        ds = fo.Dataset.from_dir(
            dataset_type=fo.types.COCODetectionDataset,
            data_path=str(split_dir),
            labels_path=str(json_path),
            label_types="detections",   # senza questo FiftyOne crea "ground_truth_detections"
            label_field="ground_truth",
            name=nome,
            overwrite=True,
        )
        originali = ds.distinct("ground_truth.detections.label")
        mappa = {c: SINONIMI_ROBOFLOW[normalizza(c)] for c in originali if normalizza(c) in SINONIMI_ROBOFLOW}
        ignorate = sorted(set(originali) - set(mappa))
        print(f"   classi usate: {mappa}")
        if ignorate:
            print(f"   classi ignorate: {ignorate}")
        view = ds.filter_labels("ground_truth", F("label").is_in(list(mappa)), only_matches=True)
        viste.append(view.map_labels("ground_truth", mappa))
    return viste


def estrai_frame():
    """Un frame ogni FRAME_OGNI_S secondi da ogni video in video/ -> foto_nostre/strada/."""
    video = [p for p in sorted(VIDEO_DIR.glob("*")) if p.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv"}]
    if not video:
        return
    import cv2

    STRADA_DIR.mkdir(parents=True, exist_ok=True)
    for path in video:
        if (STRADA_DIR / f"{path.stem}_00000.jpg").exists():
            print(f"[video] {path.name}: frame gia' estratti")
            continue
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        passo = max(1, round(fps * FRAME_OGNI_S))
        i = salvati = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % passo == 0:
                if frame.shape[1] > 1280:  # il 4K e' inutile: il modello lavora a 320x320
                    frame = cv2.resize(frame, (1280, round(frame.shape[0] * 1280 / frame.shape[1])))
                cv2.imwrite(str(STRADA_DIR / f"{path.stem}_{salvati:05d}.jpg"), frame)
                salvati += 1
            i += 1
        cap.release()
        print(f"[video] {path.name}: {salvati} frame estratti")


def foto_nostre():
    """Etichetta in automatico foto_nostre/strada e foto_nostre/emergency con YOLO (COCO)."""
    cartelle = [(STRADA_DIR, False), (EMERGENZA_DIR, True)]
    lavori = [(p, emerg) for d, emerg in cartelle if d.exists()
              for p in sorted(d.glob("*")) if p.suffix.lower() in ESTENSIONI]
    if not lavori:
        return None
    from ultralytics import YOLO  # usato solo per etichettare

    print(f"[foto_nostre] etichetto {len(lavori)} immagini ...")
    model = YOLO(MODELLO_ETICHETTE)
    samples, scartate = [], []
    for path, emergenza in lavori:
        boxes = model(str(path), verbose=False, conf=0.35)[0].boxes
        dets = []
        for (cx, cy, w, h), cls in zip(boxes.xywhn.tolist(), boxes.cls.tolist()):
            label = COCO_YOLO.get(int(cls))
            if label:
                dets.append((label, [cx - w / 2, cy - h / 2, w, h]))
        if emergenza:
            veicoli = [i for i, (l, _) in enumerate(dets) if l == "vehicle"]
            if not veicoli:
                scartate.append(path.name)
                continue
            piu_grande = max(veicoli, key=lambda i: area(dets[i][1]))
            dets[piu_grande] = ("emergency", dets[piu_grande][1])
        dets = sistema_box(dets)
        if not dets:
            scartate.append(path.name)
            continue
        samples.append(fo.Sample(filepath=str(path.resolve()), ground_truth=a_detections(dets)))
    ds = fo.Dataset("foto_nostre", overwrite=True)
    ds.add_samples(samples)
    print(f"   etichettate: {len(samples)}")
    if scartate:
        print(f"   senza oggetti utili (escluse): {len(scartate)} -> {scartate[:10]}")
    return ds


# --------------------------------------------------------------------------- main

def main():
    estrai_frame()
    if NOME_FINALE in fo.list_datasets():
        fo.delete_dataset(NOME_FINALE)
    finale = open_images().clone(NOME_FINALE)

    print("[open images] costruisco ciclisti e tolgo i doppioni ...")
    for sample in finale.iter_samples(autosave=True, progress=True):
        dets = [(d.label, list(d.bounding_box)) for d in sample.ground_truth.detections]
        sample.ground_truth = a_detections(sistema_box(dets))
    finale.delete_samples(finale.match(F("ground_truth.detections").length() == 0))

    for view in roboflow():
        finale.merge_samples(view)
    nostre = foto_nostre()
    if nostre is not None:
        finale.merge_samples(nostre)

    if "--leggero" in sys.argv:  # meno foto con sole persone: upload e training piu' veloci
        solo_persone = finale.match(F("ground_truth.detections.label").filter(F() != "person").length() == 0)
        troppe = len(solo_persone) - MAX_SOLO_PERSONE
        if troppe > 0:
            finale.delete_samples(solo_persone.take(troppe, seed=42))
            print(f"--leggero: tolte {troppe} immagini con sole persone")

    conteggi = finale.count_values("ground_truth.detections.label")
    print("Immagini totali:", len(finale))
    print("Box per classe:", conteggi)
    for classe in CLASSI_FINALI:
        if conteggi.get(classe, 0) < 300:
            print(f"ATTENZIONE: poche box per '{classe}' ({conteggi.get(classe, 0)}): "
                  "aggiungi un video o foto vostre")

    finale.export(
        export_dir=EXPORT_DIR,
        dataset_type=fo.types.COCODetectionDataset,
        label_field="ground_truth",
        data_path="data",
        labels_path="data/_annotations.coco.json",  # immagini e annotazioni nella stessa cartella
        classes=CLASSI_FINALI,
        overwrite=True,
    )
    print(f"Fatto: carica la cartella {EXPORT_DIR}\\data su Edge Impulse")

    if "--guarda" in sys.argv:
        session = fo.launch_app(finale)
        session.wait()


if __name__ == "__main__":
    main()
