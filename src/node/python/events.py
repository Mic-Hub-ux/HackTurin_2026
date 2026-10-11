"""Catalogo degli eventi di pericolo e costruzione dei messaggi.

Semantica C-ITS (ETSI TS 102 894-2): causeCode/cause_txt come nei messaggi DENM.
Ogni evento ha un ciclo di vita: "new" quando nasce e, per le situazioni che durano
(auto ferma sulle strisce, coda, mezzo di emergenza), "cancel" quando finisce.
Il campo "norma" e' un riferimento indicativo al Codice della Strada.
"""
import copy

TUTTI = ["person", "cyclist", "vehicle"]

# kind -> causeCode, nome ETSI, a chi interessa, inoltro ai vicini, validita' s, norma, categoria
# categoria: emergency | violation | risk | info (usata anche per i LED)
KINDS = {
    "emergency_vehicle": (95, "emergencyVehicleApproaching", TUTTI, True, 60, "CdS art. 177", "emergency"),
    "speeding": (99, "dangerousSituation", TUTTI, True, 30, "CdS art. 141-142", "violation"),
    "sudden_acceleration": (99, "dangerousSituation", TUTTI, True, 20, "CdS art. 141", "risk"),
    "harsh_braking": (99, "dangerousSituation", TUTTI, False, 15, "CdS art. 141", "risk"),
    "solid_line_crossing": (99, "dangerousSituation", TUTTI, True, 30, "CdS art. 146", "violation"),
    "wrong_way": (14, "wrongWayDriving", TUTTI, True, 60, "CdS art. 143", "violation"),
    "u_turn": (99, "dangerousSituation", TUTTI, True, 30, "CdS art. 154", "violation"),
    "forbidden_movement": (99, "dangerousSituation", TUTTI, True, 30, "CdS art. 146", "violation"),
    "bike_lane_invasion": (99, "dangerousSituation", ["cyclist"], True, 30, "CdS art. 146", "violation"),
    "bike_lane_hotspot": (99, "dangerousSituation", ["cyclist"], True, 600, "CdS art. 146", "info"),
    "illegal_stop": (94, "stationaryVehicle", ["person", "cyclist"], False, 120, "CdS art. 158", "violation"),
    "congestion": (1, "trafficCondition", TUTTI, True, 120, "-", "info"),
    "pedestrian_not_yielded": (97, "collisionRisk", ["person"], False, 15, "CdS art. 191", "violation"),
    "near_miss": (97, "collisionRisk", ["person", "cyclist"], True, 20, "CdS art. 141, 191", "risk"),
    "collision_risk": (97, "collisionRisk", TUTTI, False, 10, "CdS art. 141", "risk"),
    "close_pass": (97, "collisionRisk", ["cyclist"], True, 20, "CdS art. 148", "violation"),
    "pedestrian_outside_crosswalk": (12, "humanPresenceOnTheRoad", ["vehicle"], False, 15, "CdS art. 190", "risk"),
    "slow_pedestrian": (12, "humanPresenceOnTheRoad", ["vehicle"], False, 15, "CdS art. 191", "risk"),
    "pedestrian_waiting": (12, "humanPresenceOnTheRoad", ["vehicle"], False, 60, "CdS art. 191", "info"),
    "crowd_on_crosswalk": (12, "humanPresenceOnTheRoad", ["vehicle"], False, 30, "-", "info"),
    "cyclist_on_sidewalk": (99, "dangerousSituation", ["person"], False, 20, "CdS art. 182", "violation"),
    "red_light_violation": (98, "signalViolation", ["person", "cyclist"], True, 30, "CdS art. 146", "violation"),
    "late_yellow": (98, "signalViolation", ["person", "cyclist"], False, 15, "CdS art. 41, 146", "risk"),
    "pedestrian_red_crossing": (98, "signalViolation", ["vehicle"], False, 15, "CdS art. 190", "risk"),
}
STATION_TYPE = {"person": 1, "cyclist": 2, "vehicle": 5, "emergency": 10}  # ETSI StationType


class EventFactory:
    def __init__(self, node_id, camera_id):
        self.node = node_id
        self.camera = camera_id
        self._seq = 0

    def hazard(self, kind, t, severity, actor=None, extra=None):
        cause, cause_txt, targets, propagate, validity, norma, categoria = KINDS[kind]
        self._seq += 1
        ev = {
            "v": 1, "type": "hazard", "action": "new",
            "id": f"{self.node}-{self.camera}-{self._seq:06d}", "kind": kind, "category": categoria,
            "node": self.node, "camera": self.camera,
            "ts": int(t * 1000), "expires": int((t + validity) * 1000),
            "cause": cause, "cause_txt": cause_txt, "severity": int(severity),
            "targets": list(targets), "propagate": propagate, "norma": norma,
        }
        if actor:
            ev["actor"] = actor
        if extra:
            ev.update(extra)
        return ev

    @staticmethod
    def cancel(ev, t, reason=""):
        """Stesso id dell'evento originale, action "cancel": la situazione e' finita."""
        c = copy.deepcopy(ev)
        c["action"] = "cancel"
        c["ts"] = c["expires"] = int(t * 1000)
        c["details"] = {**(ev.get("details") or {}), "reason": reason,
                        "duration_s": round(t - ev["ts"] / 1000, 1)}
        return c
