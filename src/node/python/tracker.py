"""Tracker multi-oggetto pensato per pochi FPS (2-5) su una CPU piccola.

- Associazione greedy per costo crescente: distanza dalla posizione PREVISTA (moto a
  velocita' costante) normalizzata sul "cancello" della classe, con un bonus per la
  sovrapposizione dei riquadri (IoU).
- Velocita' con regressione ai minimi quadrati sugli ultimi punti: molto piu' stabile della
  semplice differenza tra due frame quando il riquadro "trema".
- Le tracce perse restano vive per max_age_s: se l'oggetto riappare (occlusione breve)
  riprende lo STESSO ID.
- Lavora in metri se la camera e' calibrata, altrimenti in coordinate immagine 0..1. Con
  piu' camere calibrate nello stesso piano basta un solo tracker per tutte.
"""
import math
from collections import Counter, deque

from geometry import box_iou, distance

FAMIGLIE = {"person": "people", "cyclist": "people", "vehicle": "vehicles", "emergency": "vehicles"}

# Distanza massima di aggancio = base + velocita' massima plausibile * dt
LIMITI = {
    "world": {"base": 1.5, "person": 3.0, "cyclist": 12.0, "vehicle": 30.0, "emergency": 30.0},
    "image": {"base": 0.06, "person": 0.3, "cyclist": 0.8, "vehicle": 1.2, "emergency": 1.2},
}
MAX_DT = 3.0  # oltre questo intervallo la previsione non ha senso
SALTO = 0.6   # scarto dalla previsione (in frazioni del cancello) oltre il quale il moto riparte


class Track:
    # history: (t, punto immagine, punto in metri o None, altezza box, box xyxy, confidenza)
    def __init__(self, tid, label, t, img, world, size=None, box=None, conf=None, camera="cam0"):
        self.id = tid
        self.camera = camera
        self.labels = Counter({label: 1})
        self.history = deque(maxlen=120)
        self.history.append((t, img, world, size, box, conf))
        self.first_seen = self.last_seen = t
        self.hits = 1
        self.conf_sum = conf or 0.0
        self.break_t = t  # ultimo "salto": la velocita' si calcola solo con i punti successivi
        self.state = {}  # memoria delle regole (zone, eventi gia' segnalati, ...)

    @property
    def label(self):
        n_emerg = self.labels.get("emergency", 0)
        if n_emerg >= max(2, 0.3 * self.hits):
            return "emergency"  # un mezzo di emergenza riconosciuto spesso resta tale
        return self.labels.most_common(1)[0][0]

    @property
    def img(self):
        return self.history[-1][1]

    @property
    def world(self):
        return self.history[-1][2]

    @property
    def box(self):
        return self.history[-1][4]

    @property
    def size(self):
        return self.history[-1][3]

    @property
    def confidence(self):
        return self.conf_sum / self.hits if self.hits else 0.0

    @property
    def age(self):
        return self.last_seen - self.first_seen

    def point(self, space):
        return self.world if space == "world" else self.img

    def previous(self):
        return self.history[-2] if len(self.history) >= 2 else None

    def velocity(self, space, window=1.5):
        """(vx, vy) per secondo: retta dei minimi quadrati sugli ultimi 'window' secondi."""
        idx = 2 if space == "world" else 1
        t_now = self.history[-1][0]
        pts = [(h[0], h[idx]) for h in self.history
               if t_now - h[0] <= window and h[0] >= self.break_t and h[idx] is not None]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.25:
            return None
        tm = sum(p[0] for p in pts) / len(pts)
        xm = sum(p[1][0] for p in pts) / len(pts)
        ym = sum(p[1][1] for p in pts) / len(pts)
        den = sum((p[0] - tm) ** 2 for p in pts)
        if den <= 0:
            return None
        vx = sum((p[0] - tm) * (p[1][0] - xm) for p in pts) / den
        vy = sum((p[0] - tm) * (p[1][1] - ym) for p in pts) / den
        return (vx, vy)

    def speed(self, space, window=1.5):
        v = self.velocity(space, window)
        return None if v is None else math.hypot(*v)

    def scaled_speed(self, window=1.5):
        """Velocita' in "altezze del proprio riquadro al secondo": non dipende dalla distanza
        dalla camera (piu' e' vicino, piu' si muove in pixel ma piu' e' grande il riquadro)."""
        v = self.speed("image", window)
        t_now = self.history[-1][0]
        sizes = sorted(h[3] for h in self.history if h[3] and t_now - h[0] <= window)
        if v is None or not sizes:
            return None
        return v / sizes[len(sizes) // 2]

    def heading(self, space, window=1.5):
        """Direzione di marcia in gradi (0 = +y, 90 = +x), None se fermo o incerto."""
        v = self.velocity(space, window)
        if v is None or math.hypot(*v) < 1e-6:
            return None
        return math.degrees(math.atan2(v[0], v[1])) % 360

    def predict(self, space, t):
        p = self.point(space)
        v = self.velocity(space, window=1.0)
        if v is None or p is None:
            return p
        dt = min(max(t - self.last_seen, 0.0), MAX_DT)
        return (p[0] + v[0] * dt, p[1] + v[1] * dt)

    def path_length(self, space):
        idx = 2 if space == "world" else 1
        pts = [h[idx] for h in self.history if h[idx] is not None]
        return sum(distance(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


class Tracker:
    def __init__(self, space, camera_id="cam0", max_age_s=2.0, min_hits=2, max_tracks=300):
        self.space = space  # "world" (metri) oppure "image" (0..1)
        self.camera_id = camera_id
        self.max_age_s = max_age_s
        self.min_hits = min_hits
        self.max_tracks = max_tracks
        self.tracks = {}
        self.last_t = None
        self._next = 1

    def update(self, observations, t):
        """observations: lista di (label, punto_img, punto_metri, altezza_box[, box, conf]).

        Ritorna (tracce aggiornate e confermate, tracce chiuse perche' sparite e che erano
        state confermate). Il tempo non torna mai indietro (orologio che salta).
        """
        if self.last_t is not None and t <= self.last_t:
            t = self.last_t + 1e-3
        self.last_t = t

        obs = []
        for o in observations:
            label, img, world, size = o[0], o[1], o[2], o[3]
            box = o[4] if len(o) > 4 else None
            conf = o[5] if len(o) > 5 else None
            if label not in FAMIGLIE or (self.space == "world" and world is None):
                continue
            obs.append((label, img, world, size, box, conf))

        # prima chiudo le tracce sparite da troppo: un oggetto nuovo non deve "ereditarle"
        chiuse = [tr for tr in self.tracks.values() if t - tr.last_seen > self.max_age_s]
        for tr in chiuse:
            del self.tracks[tr.id]

        coppie = []
        lim = LIMITI[self.space]
        for oi, (label, img, world, size, box, conf) in enumerate(obs):
            p = world if self.space == "world" else img
            for tr in self.tracks.values():
                if FAMIGLIE[tr.label] != FAMIGLIE[label]:
                    continue
                dt = min(max(t - tr.last_seen, 1e-3), MAX_DT)
                gate = lim["base"] + lim.get(label, lim["vehicle"]) * dt
                d = distance(p, tr.predict(self.space, t))
                if d <= gate:
                    costo = d / gate - 0.3 * box_iou(box, tr.box)
                    coppie.append((costo, oi, tr.id, d / gate))
        coppie.sort()

        usate_obs, usate_tr, aggiornate = set(), set(), []
        for _, oi, tid, scarto in coppie:
            if oi in usate_obs or tid in usate_tr:
                continue
            usate_obs.add(oi)
            usate_tr.add(tid)
            label, img, world, size, box, conf = obs[oi]
            tr = self.tracks[tid]
            # Lontanissimo da dove il moto lo prevedeva (scambio di identita', taglio di scena):
            # il modello di moto riparte, cosi' non nascono velocita'/frenate/inversioni finte.
            if scarto > SALTO and tr.velocity(self.space, window=1.0) is not None:
                tr.break_t = t
            tr.history.append((t, img, world, size, box, conf))
            tr.labels[label] += 1
            tr.hits += 1
            tr.conf_sum += conf or 0.0
            tr.last_seen = t
            aggiornate.append(tr)

        for oi, (label, img, world, size, box, conf) in enumerate(obs):
            if oi not in usate_obs:
                tid = f"{self.camera_id}-{self._next}"
                self._next += 1
                self.tracks[tid] = Track(tid, label, t, img, world, size, box, conf, self.camera_id)

        if len(self.tracks) > self.max_tracks:  # protezione memoria: via le piu' vecchie non confermate
            extra = sorted(self.tracks.values(), key=lambda tr: (tr.hits >= self.min_hits, tr.last_seen))
            for tr in extra[: len(self.tracks) - self.max_tracks]:
                del self.tracks[tr.id]
        confermate = [tr for tr in aggiornate if tr.hits >= self.min_hits]
        return confermate, [tr for tr in chiuse if tr.hits >= self.min_hits]

    def active(self, t, recent_s=0.6):
        """Tracce confermate viste negli ultimi recent_s secondi."""
        return [tr for tr in self.tracks.values() if tr.hits >= self.min_hits and t - tr.last_seen <= recent_s]
