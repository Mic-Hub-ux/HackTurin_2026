"""Mappa di calore dei passaggi registrati da un nodo (tabella heatmap dell'archivio).

Uso:
    python heatmap.py replay_out\\smartcrossing.db                          # tutte le classi
    python heatmap.py replay_out\\smartcrossing.db --class vehicle --image foto.png --out heatmap.png

Se la camera non e' calibrata le celle sono in frazioni d'immagine: con --image la mappa viene
sovrapposta alla foto della camera. Se e' calibrata le celle sono in metri: griglia vista dall'alto.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from storage import Storage  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--camera")
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--image", help="foto della camera (per le celle in frazioni d'immagine)")
    ap.add_argument("--out", default="heatmap.png")
    args = ap.parse_args()
    st = Storage(args.db)
    try:
        righe = st.heatmap(camera=args.camera, cls=args.cls)
    finally:
        st.close()
    if not righe:
        sys.exit("nessun dato di heatmap nell'archivio (servono statistiche: lasciate girare il nodo almeno un periodo)")

    celle = defaultdict(int)
    for r in righe:
        celle[(int(r["cx"]), int(r["cy"]))] += int(r["count"])
    unita = righe[0].get("unit", "img")
    lato = float(righe[0].get("cell") or 0.02)
    xs, ys = [c[0] for c in celle], [c[1] for c in celle]

    if unita == "img":
        n = max(1, round(1 / lato))
        griglia = np.zeros((n, n), np.float32)
        for (cx, cy), v in celle.items():
            if 0 <= cx < n and 0 <= cy < n:
                griglia[cy, cx] += v
        base = cv2.imread(args.image) if args.image else None
        if base is None:
            base = np.full((720, 1280, 3), 40, np.uint8)
        h, w = base.shape[:2]
    else:  # metri: vista dall'alto, y verso l'alto
        x0, y0 = min(xs), min(ys)
        griglia = np.zeros((max(ys) - y0 + 1, max(xs) - x0 + 1), np.float32)
        for (cx, cy), v in celle.items():
            griglia[cy - y0, cx - x0] += v
        griglia = np.flipud(griglia)
        scala = max(1, int(800 / max(griglia.shape)))
        h, w = griglia.shape[0] * scala, griglia.shape[1] * scala
        base = np.full((h, w, 3), 40, np.uint8)

    norm = griglia / griglia.max() if griglia.max() > 0 else griglia
    norm = cv2.GaussianBlur(cv2.resize(norm, (w, h), interpolation=cv2.INTER_NEAREST), (0, 0), max(1, w // 150))
    norm = norm / norm.max() if norm.max() > 0 else norm
    colori = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
    alpha = (np.clip(norm * 1.5, 0, 0.75))[..., None]
    img = (base * (1 - alpha) + colori * alpha).astype(np.uint8)
    titolo = f"heatmap {args.cls or 'tutte le classi'} - {sum(celle.values())} passaggi - celle {lato:g} {'m' if unita == 'm' else 'img'}"
    cv2.putText(img, titolo, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.imwrite(args.out, img)
    print(f"salvata {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
