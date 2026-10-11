"""Stato del semaforo dell'incrocio.

mode "none":      il nodo non vede/conosce il semaforo (default): le regole sul rosso sono spente.
mode "simulated": ciclo fisso verde -> giallo -> rosso calcolato dall'orologio (per la demo).
mode "external":  lo stato arriva da fuori (es. dallo sketch MCU che comanda i LED) con set_state().
"""
import time


class TrafficLight:
    def __init__(self, cfg):
        self.mode = cfg.get("mode", "none")
        self.green_s = cfg.get("green_s", 12)
        self.yellow_s = cfg.get("yellow_s", 3)
        self.red_s = cfg.get("red_s", 12)
        self._t0 = cfg.get("start_time", time.time())
        # in modalita' esterna lo stato e' sconosciuto finche' l'MCU non lo comunica
        self._state = cfg.get("initial_state", "unknown")
        self._since = self._t0

    def set_state(self, state, t=None):
        """Per mode "external": "green" | "yellow" | "red"."""
        if state != self._state:
            self._state = state
            self._since = time.time() if t is None else t

    def state(self, t):
        """Ritorna (stato, secondi da quando e' in quello stato)."""
        if self.mode == "none":
            return "unknown", 0.0
        if self.mode == "external":
            return self._state, max(0.0, t - self._since)
        ciclo = self.green_s + self.yellow_s + self.red_s
        x = (t - self._t0) % ciclo
        if x < self.green_s:
            return "green", x
        if x < self.green_s + self.yellow_s:
            return "yellow", x - self.green_s
        return "red", x - self.green_s - self.yellow_s
