"""Fa girare il NODO COMPLETO su un video (o una cartella di immagini) sul PC e mostra cosa capisce.

Usa YOLO pre-addestrato su COCO al posto del modello della board, ma tutto il resto e' identico
a quello che gira sull'UNO Q: regole, tracce, archivio SQLite, statistiche, stato, LED (simulati
nel pannello in alto a destra) e, con --server, invio al server sul PC e pericoli degli incroci
vicini. Senza board potete quindi fare la demo completa con la dashboard del server.

Uso:
    python replay.py video.mp4                          # finestra con zone, tracce, eventi, LED
    python replay.py video.mp4 --server http://127.0.0.1:8000   # manda tutto al server (dashboard)
    python replay.py video.mp4 --fps 3                  # velocita' della board (default 3)
    python replay.py video.mp4 --snapshot foto.png      # salva il primo frame (per zone_editor.py)
    python replay.py video.mp4 --save risultato.mp4     # salva il video annotato (per la demo)
    python replay.py video.mp4 --no-show                # senza finestra
    python replay.py video.mp4 --config config_auto.json --out cartella_dati
Tasti: q = esci, spazio = pausa
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[0] / "python"))
from config import load_config  # noqa: E402
from node import Node  # noqa: E402

COLORI = {"person": (255, 160, 0), "cyclist": (255, 0, 255), "vehicle": (0, 200, 0), "emergency": (0, 140, 255)}
# YOLO pre-addestrato: quello del dataset se c'e', altrimenti ultralytics lo scarica da solo
MODELLO = next((str(p) for p in (QUI.parents[1] / "dataset" / "yolov8s.pt",
                                 QUI.parents[3] / "dataset" / "yolov8s.pt") if p.exists()), "yolov8s.pt")
MODI = {0: "spento", 1: "acceso", 2: "lento", 3: "veloce"}


class BridgeFinto:
    """Al posto del Bridge verso l'MCU: ricorda l'ultimo comando dei LED per disegnarlo."""

    def __init__(self):
        self.packed = 0

    def call(self, name, packed, ttl_ms, **kw):
        self.packed = int(packed)


def frames(sorgente, fps_board):
    """Restituisce (t in secondi, frame) al ritmo della board."""
    p = Path(sorgente)
    if p.is_dir():
        for i, f in enumerate(sorted(x for x in p.iterdir() if x.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})):
            frame = cv2.imread(str(f))
            if frame is not None:
                yield i / fps_board, frame
        return
    cap = cv2.VideoCapture(str(p))
    fps_video = cap.get(cv2.CAP_PROP_FPS) or 30
    passo = max(1, round(fps_video / fps_board))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % passo == 0:
            yield i / fps_video, frame
        i += 1
    cap.release()


def disegna_config(img, cam):
    h, w = img.shape[:2]
    px = lambda pts: np.array([(int(x * w), int(y * h)) for x, y in pts])  # noqa: E731
    for nome, poly in (cam.get("zones") or {}).items():
        cv2.polylines(img, [px(poly)], True, (255, 0, 255), 2)
        cv2.putText(img, nome, tuple(px(poly)[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
    for lane in (cam.get("lanes") or {}).values():
        cv2.polylines(img, [px(lane["polygon"])], True, (200, 200, 0), 1)
        a, b = px(lane["direction"])
        cv2.arrowedLine(img, tuple(a), tuple(b), (200, 200, 0), 2, tipLength=0.05)
    for line in (cam.get("lines") or {}).values():
        cv2.polylines(img, [px(line["points"])], False, (0, 0, 255), 2)
    for nome, poly in (cam.get("approaches") or {}).items():
        cv2.polylines(img, [px(poly)], True, (0, 255, 0), 1)
        cv2.putText(img, nome, tuple(px(poly)[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)


def disegna_led(img, packed, ruoli, t):
    w = img.shape[1]
    for i in range(4):
        modo = (packed >> (2 * i)) & 3
        acceso = modo == 1 or (modo == 2 and int(t * 2) % 2 == 0) or (modo == 3 and int(t * 8) % 2 == 0)
        c = (w - 150, 30 + 30 * i)
        cv2.circle(img, c, 10, (0, 0, 255) if acceso else (60, 60, 60), -1)
        ruolo = ruoli[i] if i < len(ruoli) else "?"
        cv2.putText(img, f"{ruolo}: {MODI[modo]}", (c[0] + 16, c[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sorgente")
    ap.add_argument("--config", default=str(QUI.parents[0] / "python" / "config.json"))
    ap.add_argument("--model", default=MODELLO)
    ap.add_argument("--fps", type=float, default=3.0)
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--server", help="indirizzo del server sul PC, es. http://127.0.0.1:8000")
    ap.add_argument("--out", default=str(QUI / "replay_out"), help="cartella per archivio e log")
    ap.add_argument("--snapshot")
    ap.add_argument("--save")
    ap.add_argument("--no-show", action="store_true")
    args = ap.parse_args()

    if args.snapshot:
        _, frame = next(frames(args.sorgente, args.fps))
        cv2.imwrite(args.snapshot, frame)
        print(f"salvato {args.snapshot}: usalo con zone_editor.py")
        return

    cfg, avvisi = load_config(args.config)
    for a in avvisi:
        print("[config]", a)
    cfg["api"]["enabled"] = False
    if args.server:
        cfg["server"]["url"] = args.server
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / cfg["data"]["db_file"]).unlink(missing_ok=True)  # ogni replay parte da un archivio pulito
    bridge = BridgeFinto()
    node = Node(cfg, base_dir=out, bridge=bridge, start_threads=False)
    cam_id = next(iter(node.engines))
    engine = node.engines[cam_id]
    cam = cfg["_cameras"][cam_id]
    ruoli = cfg["leds"]["roles"]
    model = YOLO(args.model)
    writer, banner, ultimo_tick = None, [], None
    t_start = time.time()  # il video viene "ambientato" adesso, cosi' scadenze e server tornano

    try:
        for tv, frame in frames(args.sorgente, args.fps):
            t = t_start + tv
            h, w = frame.shape[:2]
            engine.frame_w, engine.frame_h = w, h
            res = model(frame, verbose=False, conf=args.conf)[0]
            det = {}
            for (x1, y1, x2, y2), c, s in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist()):
                det.setdefault(model.names[int(c)], []).append({"confidence": s, "bounding_box_xyxy": (x1, y1, x2, y2)})
            for e in node.process(det, cam_id, t):
                if e["type"] == "hazard":
                    stato = "TERMINATO" if e["action"] == "cancel" else f"sev={e['severity']}"
                    banner.append((tv, f"{tv:6.1f}s  {e['kind']}  {stato}  {e.get('actor', {}).get('track', '')}"))
            if ultimo_tick is None or t - ultimo_tick >= 1.0:  # come il watchdog della board
                ultimo_tick = t
                node.tick(t)
                if args.server:
                    node.publisher.flush_once()
                    node.remote.poll_once()

            disegna_config(frame, cam)
            for tr in engine.tracker.tracks.values():
                if t - tr.last_seen > 0.01:
                    continue
                x, y = int(tr.img[0] * w), int(tr.img[1] * h)
                col = COLORI.get(tr.label, (255, 255, 255))
                cv2.circle(frame, (x, y), 6, col, -1)
                pts = np.array([(int(p[1][0] * w), int(p[1][1] * h)) for p in tr.history])
                cv2.polylines(frame, [pts], False, col, 2)
                info = f"{tr.label} {tr.id}"
                if tr.state.get("speeds"):
                    info += f" {tr.state['speeds'][-1][1]:.0f}km/h"
                elif tr.scaled_speed() is not None:
                    info += f" v{tr.scaled_speed():.1f}"
                if tr.state.get("towards"):
                    info += f" ->{tr.state['towards']}"
                cv2.putText(frame, info, (x + 8, y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 2)
            banner = [(tb, s) for tb, s in banner if tv - tb < 4]
            for i, (_, s) in enumerate(banner[-5:]):
                cv2.putText(frame, s, (10, 30 + 28 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            disegna_led(frame, bridge.packed, ruoli, tv)

            if args.save:
                if writer is None:
                    writer = cv2.VideoWriter(args.save, cv2.VideoWriter_fourcc(*"mp4v"), args.fps, (w, h))
                writer.write(frame)
            if not args.no_show:
                cv2.imshow("smart crossing - replay", frame)
                tasto = cv2.waitKey(int(1000 / args.fps)) & 0xFF
                if tasto == ord("q"):
                    break
                if tasto == ord(" "):
                    cv2.waitKey(0)
        for k in range(1, 4):  # chiude le tracce ancora aperte
            node.tick(t_start + tv + 2.5 * k)
        # chiude l'ultimo periodo di statistiche anche se il video e' piu' corto del periodo
        node.tick(t_start + tv + 8 + cfg["data"]["stats_interval_s"])
        if args.server:
            for _ in range(50):
                if node.publisher.flush_once() == 0:
                    break
    finally:
        if writer is not None:
            writer.release()
        cv2.destroyAllWindows()
    try:
        riepilogo(node, out, cfg)
    finally:
        node.close()


def riepilogo(node, out, cfg):
    st = node.storage
    if st is None:
        return
    pericoli = Counter(e["kind"] for e in st.query("events", limit=None) if e.get("type") == "hazard" and e.get("action") == "new")
    tracce = st.query("tracks", limit=None)
    print("\n=== riepilogo ===")
    print("pericoli:", dict(pericoli) or "nessuno")
    print("tracce per classe:", dict(Counter(tr["class"] for tr in tracce)))
    print("movimenti:", dict(Counter(f"{tr['class']} {tr['from']}->{tr['to']}" for tr in tracce if tr.get("from"))))
    print("attraversamenti strisce:", dict(Counter(tr["crosswalk_crossing"]["direction"] for tr in tracce
                                                    if tr.get("crosswalk_crossing"))))
    print(f"archivio: {out / cfg['data']['db_file']}  (esporta con export_data.py, mappa con heatmap.py)")


if __name__ == "__main__":
    main()
