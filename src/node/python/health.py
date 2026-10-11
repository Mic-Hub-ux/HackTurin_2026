"""Stato di salute del nodo: la camera manda frame? il modello e' lento? la board scalda?

Ogni data.status_interval_s secondi produce un messaggio "status" (per il server vale anche
come battito cardiaco). Per ogni camera: FPS, tempo di elaborazione e detection medie su una
finestra mobile di 30 s, eta' dell'ultimo frame. Poi errori contati per punto del codice,
metriche di sistema lette da /sys e /proc (su Windows, o dove mancano, restano null) e una
lista di avvisi leggibili, stampati in console quando compaiono e quando rientrano.
"""
import glob
import math
import os
import shutil
import threading
from collections import deque

WINDOW_S = 30.0       # finestra mobile per FPS e tempi
NO_FRAME_S = 30.0     # nessun frame da piu' di N s -> avviso
WARMUP_S = 10.0       # prima di N s dal primo frame l'FPS non e' ancora significativo
MIN_FPS = 1.0
MAX_TEMP_C = 80.0
MAX_OUTBOX = 1000
MIN_DISK_MB = 200
MAX_PRINTED = 3       # errori stampati per punto del codice, poi solo contati
MAX_WHERE = 200
MAX_CAMERAS = 16
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # cartella dell'app (dove sta il database)


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _short(err):
    try:
        return repr(err)[:200]
    except Exception:
        return type(err).__name__


# ---------------------------------------------------------------------- metriche di sistema
# Ogni lettura e' isolata: un file mancante o strano da' None, mai un'eccezione.

def cpu_temp_c(pattern="/sys/class/thermal/thermal_zone*/temp"):
    """Temperatura piu' alta tra le zone termiche (millesimi di grado nei file di Linux)."""
    temps = []
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                v = float(f.read().strip())
        except (OSError, ValueError):
            continue
        c = v / 1000.0 if abs(v) > 1000 else v
        if -40.0 < c < 150.0:  # scarta sensori finti o non collegati
            temps.append(c)
    return round(max(temps), 1) if temps else None


def meminfo_mb(path="/proc/meminfo"):
    """{"MemAvailable": MB, "MemTotal": MB} da /proc/meminfo; {} se non leggibile."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("MemAvailable", "MemTotal"):
                    out[name] = round(int(rest.split()[0]) / 1024.0, 1)  # il file e' in kB
    except (OSError, ValueError, IndexError):
        pass
    return out


def proc_rss_mb(path="/proc/self/status"):
    """Memoria usata da questo processo (VmRSS): utile per accorgersi di una perdita di memoria."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def load_avg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):  # Windows non ha getloadavg
        return None


def disk_free_mb(path=BASE_DIR):
    try:
        return round(shutil.disk_usage(path).free / 1048576.0, 1)
    except (OSError, ValueError):
        return None


def read_system(path=BASE_DIR):
    mem = meminfo_mb()
    return {"cpu_temp_c": cpu_temp_c(), "load_avg": load_avg(),
            "mem_available_mb": mem.get("MemAvailable"), "mem_total_mb": mem.get("MemTotal"),
            "disk_free_mb": disk_free_mb(path), "proc_rss_mb": proc_rss_mb()}


# ---------------------------------------------------------------------- salute del nodo

class _Cam:
    __slots__ = ("frames", "first_t", "last_t", "window")

    def __init__(self):
        self.frames = 0
        self.first_t = self.last_t = None
        self.window = deque(maxlen=4000)  # (t, proc_ms, n_detections); 30 s anche a 100+ FPS


class Health:
    def __init__(self, cfg, node_id):
        data = cfg.get("data") or {}
        self.node = node_id
        try:
            self.interval = float(data.get("status_interval_s", 10))  # <= 0: niente status
        except (TypeError, ValueError):
            self.interval = 10.0
        self.server_url = str((cfg.get("server") or {}).get("url") or "").strip()
        self.cams = {str(c): _Cam() for c in (cfg.get("_cameras") or {})}
        self.errors = {}
        self.last_errors = {}
        self._lock = threading.RLock()
        self._t0 = None           # primo istante visto: base dell'uptime
        self._last_status = None
        self._extra = {}
        self._system = None
        self._attivi = {}         # avvisi in corso: chiave -> testo (per stampare solo i cambi)

    # ------------------------------------------------------------------ ingresso

    def on_frame(self, t, camera_id, proc_ms, n_detections):
        if not _num(t):  # un istante non valido bloccherebbe la finestra mobile
            return
        try:
            with self._lock:
                if self._t0 is None:
                    self._t0 = t
                cam = self.cams.get(str(camera_id))
                if cam is None:
                    if len(self.cams) >= MAX_CAMERAS:
                        return
                    cam = self.cams[str(camera_id)] = _Cam()
                cam.frames += 1
                if cam.first_t is None:
                    cam.first_t = t
                cam.last_t = t
                cam.window.append((t, proc_ms if _num(proc_ms) else None,
                                   n_detections if _num(n_detections) else None))
                self._prune(cam, t)
        except Exception as e:
            self.on_error("health.on_frame", e)

    def on_error(self, where, err):
        """Conta l'errore; stampa solo i primi MAX_PRINTED per ogni punto del codice."""
        try:
            where = str(where)[:80] or "?"
            testo = _short(err)
            with self._lock:
                if where not in self.errors and len(self.errors) >= MAX_WHERE:
                    where = "altro"
                n = self.errors[where] = self.errors.get(where, 0) + 1
                self.last_errors[where] = testo
            if n <= MAX_PRINTED:
                coda = " (i successivi non vengono stampati)" if n == MAX_PRINTED else ""
                print(f"[errore] {where}: {testo}{coda}")
        except Exception:
            pass  # chi segnala gli errori non deve crearne

    # ------------------------------------------------------------------ uscita

    def maybe_status(self, t, extra=None):
        """Messaggio status ogni status_interval_s (il primo subito), altrimenti None."""
        if self.interval <= 0 or not _num(t):
            return None
        try:
            with self._lock:
                if self._t0 is None:
                    self._t0 = t
                if self._last_status is not None and 0 <= t - self._last_status < self.interval:
                    return None
                self._last_status = t
                self._extra = dict(extra or {})
                self._system = read_system()
                avvisi = self._warnings(t)
                self._log_changes(avvisi)
                msg = {"v": 1, "type": "status", "node": self.node, "ts": int(t * 1000),
                       "uptime_s": round(max(0.0, t - self._t0), 1),
                       "cameras": {c: self._cam_status(cam, t) for c, cam in self.cams.items()},
                       "errors": dict(self.errors), "system": dict(self._system),
                       "warnings": [testo for _, testo in avvisi]}
                if self.last_errors:
                    msg["last_errors"] = dict(self.last_errors)
                for k, v in self._extra.items():  # le chiavi di extra non coprono quelle del messaggio
                    msg.setdefault(k, v)
                return msg
        except Exception as e:
            self.on_error("health.maybe_status", e)
            return None

    def warnings(self, t):
        """Avvisi leggibili in questo momento (usa l'ultimo extra ricevuto da maybe_status)."""
        if not _num(t):
            return []
        try:
            with self._lock:
                if self._system is None:
                    self._system = read_system()
                return [testo for _, testo in self._warnings(t)]
        except Exception as e:
            self.on_error("health.warnings", e)
            return []

    # ------------------------------------------------------------------ calcoli

    def _prune(self, cam, t):
        w = cam.window
        while w and w[0][0] <= t - WINDOW_S:
            w.popleft()

    def _fps(self, cam, t):
        n = len(cam.window)
        if n == 0 or cam.first_t is None:
            return 0.0
        trascorso = t - cam.first_t
        if trascorso >= WINDOW_S:
            return n / WINDOW_S
        # appena partito: frame contati sul tempo davvero trascorso (n-1 intervalli)
        return (n - 1) / trascorso if n >= 2 and trascorso > 0 else 0.0

    def _cam_status(self, cam, t):
        self._prune(cam, t)
        proc = [x[1] for x in cam.window if x[1] is not None]
        det = [x[2] for x in cam.window if x[2] is not None]
        return {"frames": cam.frames, "fps": round(self._fps(cam, t), 2),
                "proc_ms_avg": round(sum(proc) / len(proc), 1) if proc else None,
                "proc_ms_max": round(max(proc), 1) if proc else None,
                "detections_avg": round(sum(det) / len(det), 2) if det else None,
                "last_frame_age_s": round(max(0.0, t - cam.last_t), 1) if cam.last_t is not None else None}

    def _warnings(self, t):
        """[(chiave stabile, testo)]: la chiave serve a stampare un avviso solo quando compare."""
        out = []
        for cam_id, cam in self.cams.items():
            self._prune(cam, t)
            if cam.last_t is None:
                if self._t0 is not None and t - self._t0 > NO_FRAME_S:
                    out.append((f"frame:{cam_id}", f"{cam_id}: nessun frame dall'avvio ({int(t - self._t0)} s)"))
                continue
            eta = t - cam.last_t
            if eta > NO_FRAME_S:
                out.append((f"frame:{cam_id}", f"{cam_id}: nessun frame da {int(eta)} s"))
            elif t - cam.first_t >= WARMUP_S:
                fps = self._fps(cam, t)
                if fps < MIN_FPS:
                    out.append((f"fps:{cam_id}", f"{cam_id}: FPS basso ({fps:.1f})"))
        sistema = self._system or {}
        temp = sistema.get("cpu_temp_c")
        if _num(temp) and temp > MAX_TEMP_C:
            out.append(("temp", f"temperatura CPU alta ({temp:.0f} C)"))
        disco = sistema.get("disk_free_mb")
        if _num(disco) and disco < MIN_DISK_MB:
            out.append(("disk", f"disco quasi pieno ({disco:.0f} MB liberi)"))
        extra = self._extra or {}
        coda = extra.get("outbox")
        if _num(coda) and coda > MAX_OUTBOX:
            out.append(("outbox", f"coda di invio lunga ({int(coda)} messaggi)"))
        if self.server_url and extra.get("server_online") is False:
            out.append(("server", "server non raggiungibile"))
        regole = extra.get("rule_errors")
        if regole:
            nomi = sorted(str(k) for k in regole)
            altre = f" e altre {len(nomi) - 5}" if len(nomi) > 5 else ""
            out.append(("rules", f"errori nelle regole: {', '.join(nomi[:5])}{altre}"))
        return out

    def _log_changes(self, avvisi):
        nuovi = dict(avvisi)
        for k, testo in nuovi.items():
            if k not in self._attivi:
                print(f"[salute] AVVISO: {testo}")
        for k, testo in self._attivi.items():
            if k not in nuovi:
                print(f"[salute] rientrato: {testo}")
        self._attivi = nuovi
