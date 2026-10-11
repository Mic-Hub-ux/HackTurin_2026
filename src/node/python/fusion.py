"""Fusione tra camere: lo stesso oggetto passa dall'inquadratura di una camera a un'altra.

Con piu' camere sullo stesso incrocio ognuna vede un tratto di strada e chiama
center_approach (default "incrocio") la zona verso il centro. Una traccia A della camera X
che esce verso il centro, seguita poco dopo da una traccia B della camera Y che entra dal
centro, e' con buona probabilita' lo stesso oggetto: unendole si ottiene il movimento
completo attraverso l'incrocio, es. "cam0:via" -> "cam1:corso".

I riepiloghi arrivano solo quando una traccia si chiude, quindi A arriva prima di B:
A resta in attesa (fino a max_gap_s + 30 s, perche' B arriva solo a fine traccia) e ogni
B si collega all'A compatibile con il gap minimo. Ogni traccia fa da origine al massimo una
volta e da destinazione al massimo una volta.
"""
import time

# Si collegano solo oggetti della stessa famiglia: un pedone non diventa un'auto
FAMIGLIA = {"person": "persone", "cyclist": "persone", "bicycle": "persone",
            "vehicle": "veicoli", "emergency": "veicoli", "motorcycle": "veicoli"}
# Se le due camere danno classi diverse (es. person/cyclist) riporto la piu' specifica
PRIORITA = ["emergency", "cyclist", "motorcycle", "vehicle", "bicycle", "person"]
MARGINE_S = 30.0       # attesa extra: B viene riassunta solo quando esce di scena
MAX_IN_ATTESA = 500    # limite di memoria se nessuna traccia viene mai collegata


def _ms(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _famiglia(cls):
    return FAMIGLIA.get(cls, cls)


def _classe(a, b):
    ca, cb = a.get("class"), b.get("class")
    if ca == cb:
        return ca
    return min((ca, cb), key=lambda c: PRIORITA.index(c) if c in PRIORITA else len(PRIORITA))


class CameraFusion:
    def __init__(self, cfg, node_id):
        f = (cfg or {}).get("fusion") or {}
        self.node = node_id
        self.center = f.get("center_approach", "incrocio")
        self.max_gap_s = float(f.get("max_gap_s", 4.0))
        # campi visivi sovrapposti: B puo' iniziare poco prima che A finisca
        self.max_overlap_s = float(f.get("max_overlap_s", 1.0))
        self.pending = {}  # (camera, id) -> riepilogo di una traccia uscita verso il centro
        self.linked = 0

    def on_track(self, summary):
        """Riepilogo di una traccia chiusa -> [] oppure [messaggio movement]. Mai eccezioni."""
        try:
            return self._on_track(summary)
        except Exception as e:  # un riepilogo strano non deve fermare il nodo
            print("[fusione] riepilogo ignorato:", repr(e))
            return []

    def _on_track(self, s):
        if not isinstance(s, dict) or s.get("type", "track") != "track":
            return []
        now = _ms(s.get("ts")) or _ms(s.get("ts_end")) or int(time.time() * 1000)
        self._expire(now)
        out = []
        if s.get("from") == self.center:  # entrata dal centro: cerco da dove veniva
            key, gap = self._best_origin(s)
            if key is not None:
                out.append(self._movement(self.pending.pop(key), s, gap, now))
        if s.get("to") == self.center and _ms(s.get("ts_end")) is not None:
            self.pending[(s.get("camera"), s.get("id"))] = s
            while len(self.pending) > MAX_IN_ATTESA:
                self.pending.pop(next(iter(self.pending)))  # la piu' vecchia
        return out

    def _expire(self, now):
        limite = (self.max_gap_s + MARGINE_S) * 1000
        for key in [k for k, a in self.pending.items() if now - a["ts_end"] > limite]:
            del self.pending[key]

    def _best_origin(self, b):
        """Traccia in attesa compatibile con b con il gap piu' piccolo: (chiave, gap_s) o (None, None)."""
        start, fam = _ms(b.get("ts_start")), _famiglia(b.get("class"))
        if start is None or fam is None:
            return None, None
        best, best_gap = None, None
        for key, a in self.pending.items():
            if a.get("camera") == b.get("camera") or _famiglia(a.get("class")) != fam:
                continue
            gap = (start - a["ts_end"]) / 1000.0
            if not -self.max_overlap_s <= gap <= self.max_gap_s:
                continue
            if best is None or abs(gap) < abs(best_gap):
                best, best_gap = key, gap
        return best, best_gap

    def _movement(self, a, b, gap, now):
        self.linked += 1
        ca, cb = a.get("camera"), b.get("camera")
        msg = {
            "v": 1, "type": "movement", "kind": "movement",
            "id": f"{self.node}-mov-{a.get('id')}-{b.get('id')}-{now}",
            "node": self.node, "ts": now, "class": _classe(a, b),
            "from": f"{ca}:{a.get('from') or '?'}", "to": f"{cb}:{b.get('to') or '?'}",
            "tracks": [a.get("id"), b.get("id")], "cameras": [ca, cb], "gap_s": round(gap, 2),
            "ts_start": a.get("ts_start"), "ts_end": b.get("ts_end"),
        }
        if _ms(a.get("ts_start")) is not None and _ms(b.get("ts_end")) is not None:
            msg["duration_s"] = round((b["ts_end"] - a["ts_start"]) / 1000.0, 1)
        if b.get("towards"):
            msg["towards"] = b["towards"]  # verso quale incrocio vicino sta andando
        return msg
