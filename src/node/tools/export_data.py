"""Esporta l'archivio di un nodo (smartcrossing.db) in CSV leggibili da Excel e in un JSON completo.

Uso:  python export_data.py replay_out\\smartcrossing.db  [--out cartella]

File prodotti nella cartella:
  eventi.csv        pericoli (locali e degli incroci vicini) e movimenti tra camere
  tracce.csv        un oggetto per riga: classe, da dove a dove, durata, velocita', strisce, eventi
  statistiche.csv   una riga per periodo e camera: conteggi, velocita' V85, occupazione zone
  stato.csv         salute del nodo: FPS, tempi, temperatura, avvisi
  tutto.json        tutti i messaggi originali
"""
import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from storage import Storage  # noqa: E402


def ora(ms):
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def scrivi(path, righe):
    if not righe:
        return 0
    campi = list(dict.fromkeys(k for r in righe for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: Excel legge gli accenti
        w = csv.DictWriter(f, fieldnames=campi, delimiter=";")
        w.writeheader()
        for r in righe:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                        for k, v in r.items()})
    return len(righe)


def eventi(st):
    out = []
    for e in st.query("events", limit=None):
        a = e.get("actor") or {}
        out.append({"ora": ora(e.get("ts")), "ts": e.get("ts"), "tipo": e.get("type"), "kind": e.get("kind"),
                    "azione": e.get("action"), "severita": e.get("severity"), "categoria": e.get("category"),
                    "nodo": e.get("node") or e.get("from_node"), "camera": e.get("camera"), "via": e.get("via"),
                    "traccia": a.get("track"), "classe": a.get("class") or e.get("class"), "da": a.get("from") or e.get("from"),
                    "verso": a.get("towards") or e.get("towards"), "incroci_successivi": e.get("next_nodes"),
                    "km_h": a.get("speed_kmh"), "norma": e.get("norma"), "dettagli": e.get("details")})
    return out


def tracce(st):
    out = []
    for tr in st.query("tracks", limit=None):
        v = tr.get("speed_kmh") or tr.get("speed_rel") or {}
        out.append({"inizio": ora(tr.get("ts_start")), "fine": ora(tr.get("ts_end")), "id": tr.get("id"),
                    "camera": tr.get("camera"), "classe": tr.get("class"), "durata_s": tr.get("duration_s"),
                    "da": tr.get("from"), "a": tr.get("to"), "verso": tr.get("towards"),
                    "percorso": tr.get("path_length"), "unita": tr.get("unit"),
                    "velocita_media": v.get("avg"), "velocita_max": v.get("max"), "velocita_p85": v.get("p85"),
                    "velocita_unita": "km/h" if tr.get("speed_kmh") else ("rel" if tr.get("speed_rel") else ""),
                    "strisce": (tr.get("crosswalk_crossing") or {}).get("direction"),
                    "eventi": ",".join(tr.get("events") or []), "zone_s": tr.get("zones"),
                    "confidenza": tr.get("confidence")})
    return out


def statistiche(st):
    out = []
    for s in st.query("stats", limit=None):
        for cam, c in (s.get("cameras") or {}).items():
            riga = {"ora": ora(s.get("ts")), "periodo_s": s.get("period_s"), "camera": cam,
                    "oggetti_max": c.get("active_max"), "frame": c.get("frames")}
            for cls, n in (c.get("counts") or {}).items():
                riga[f"n_{cls}"] = n
            for cls, v in (c.get("speeds") or {}).items():
                riga[f"v85_{cls}"] = v.get("p85")
                riga[f"vmedia_{cls}"] = v.get("mean")
            for z, info in (c.get("zones") or {}).items():
                riga[f"occupazione_{z}"] = info.get("occupancy_ratio")
            riga["movimenti"] = c.get("movements")
            riga["strisce"] = c.get("crosswalk_crossings")
            riga["pericoli"] = s.get("events")
            riga["pericoli_vicini"] = s.get("remote_events")
            out.append(riga)
    return out


def stato(st):
    out = []
    for s in st.query("status", limit=None):
        riga = {"ora": ora(s.get("ts")), "uptime_s": s.get("uptime_s")}
        for cam, c in (s.get("cameras") or {}).items():
            riga[f"fps_{cam}"] = c.get("fps")
            riga[f"ms_{cam}"] = c.get("proc_ms_avg")
        sistema = s.get("system") or {}
        riga.update({"temp_c": sistema.get("cpu_temp_c"), "ram_libera_mb": sistema.get("mem_available_mb"),
                     "disco_libero_mb": sistema.get("disk_free_mb"), "server_online": s.get("server_online"),
                     "coda_invio": s.get("outbox"), "avvisi": s.get("warnings")})
        out.append(riga)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--out", default="export")
    args = ap.parse_args()
    if not Path(args.db).exists():
        sys.exit(f"archivio {args.db} non trovato")
    st = Storage(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        for nome, fn in (("eventi", eventi), ("tracce", tracce), ("statistiche", statistiche), ("stato", stato)):
            n = scrivi(out / f"{nome}.csv", fn(st))
            print(f"{nome}.csv: {n} righe")
        tutto = {t: st.query(t, limit=None) for t in ("events", "tracks", "stats", "status")}
        tutto["heatmap"] = st.heatmap()
        (out / "tutto.json").write_text(json.dumps(tutto, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"tutto.json scritto. Cartella: {out.resolve()}")
    finally:
        st.close()


if __name__ == "__main__":
    main()
