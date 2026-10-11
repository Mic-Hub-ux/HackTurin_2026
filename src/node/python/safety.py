"""Misure di sicurezza "surrogate" usate in ingegneria del traffico.

cpa():       punto di massimo avvicinamento (Closest Point of Approach) tra due oggetti
             a velocita' costante: tra quanto tempo e a che distanza si incrociano.
ttc():       Time To Collision: tra quanto i due oggetti arrivano a meno di 'radius'.
lateral():   distanza laterale/longitudinale di un oggetto rispetto alla direzione di un altro
             (serve per il sorpasso troppo vicino a un ciclista).
"""
import math


def cpa(p1, v1, p2, v2):
    """Ritorna (t_cpa >= 0, distanza minima). Unita' coerenti con quelle in ingresso."""
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    wx, wy = v2[0] - v1[0], v2[1] - v1[1]
    w2 = wx * wx + wy * wy
    t = 0.0 if w2 < 1e-12 else max(0.0, -(rx * wx + ry * wy) / w2)
    return t, math.hypot(rx + wx * t, ry + wy * t)


def ttc(p1, v1, p2, v2, radius):
    """Secondi prima che la distanza scenda sotto 'radius' (None se non succede)."""
    rx, ry = p2[0] - p1[0], p2[1] - p1[1]
    wx, wy = v2[0] - v1[0], v2[1] - v1[1]
    a = wx * wx + wy * wy
    b = 2 * (rx * wx + ry * wy)
    c = rx * rx + ry * ry - radius * radius
    if c <= 0:
        return 0.0
    if a < 1e-12:
        return None
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    t = (-b - math.sqrt(disc)) / (2 * a)
    return t if t >= 0 else None


def lateral(p_ref, v_ref, p_other):
    """(longitudinale, laterale) di p_other nel sistema "avanti/lato" di p_ref che va a v_ref."""
    n = math.hypot(*v_ref)
    if n < 1e-9:
        return None
    ux, uy = v_ref[0] / n, v_ref[1] / n
    dx, dy = p_other[0] - p_ref[0], p_other[1] - p_ref[1]
    return dx * ux + dy * uy, -dx * uy + dy * ux
