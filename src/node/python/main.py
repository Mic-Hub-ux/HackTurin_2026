"""Smart Crossing - nodo incrocio (Arduino UNO Q, App Lab).

camera -> Video Object Detection (modello Edge Impulse) -> Node (regole, dati, LED, server)
"""
import threading
import time
from pathlib import Path

from arduino.app_bricks.video_objectdetection import VideoObjectDetection
from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

from config import load_config
from node import Node

BASE = Path(__file__).resolve().parent
cfg, avvisi = load_config(BASE / "config.json")
for a in avvisi:
    print("[config]", a)

ui = WebUI()
node = Node(cfg, ui=ui, base_dir=BASE, bridge=Bridge)
detector = VideoObjectDetection(confidence=cfg.get("confidence", 0.45), debounce_sec=0.0)


def on_detections(detections):
    node.process(detections, "cam0")


def watchdog():
    """Una volta al secondo: chiude le tracce sparite, manda stato e statistiche, aggiorna i LED."""
    while True:
        time.sleep(1.0)
        try:
            node.tick()
        except Exception as e:  # il watchdog non deve morire mai
            print("[watchdog]", repr(e))


detector.on_detect_all(on_detections)
threading.Thread(target=watchdog, daemon=True).start()
App.run()
