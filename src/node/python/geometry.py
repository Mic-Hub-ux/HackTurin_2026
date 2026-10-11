"""Geometria 2D senza dipendenze: poligoni, segmenti, omografia immagine -> metri."""
import math


def point_in_polygon(p, poly):
    """Ray casting: True se il punto p=(x, y) sta dentro il poligono [(x, y), ...]."""
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(p1, p2, q1, q2):
    """True se il segmento p1-p2 attraversa il segmento q1-q2."""
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def crosses_polyline(p1, p2, polyline):
    """True se il movimento p1 -> p2 attraversa una linea spezzata [(x, y), ...]."""
    return any(segments_intersect(p1, p2, polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1))


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polygon_centroid(poly):
    """Media dei vertici (basta per decidere "verso quale zona" si sta andando)."""
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def angle_diff_deg(a, b):
    """Differenza minima tra due angoli in gradi (0..180)."""
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def box_iou(a, b):
    """IoU tra due box (x1, y1, x2, y2)."""
    if a is None or b is None:
        return 0.0
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def main_axis(poly):
    """Direzione (dx, dy) del lato piu' lungo del poligono: l'asse "lungo" delle strisce."""
    best, axis = -1.0, (1.0, 0.0)
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        d = distance(a, b)
        if d > best:
            best, axis = d, (b[0] - a[0], b[1] - a[1])
    n = math.hypot(*axis) or 1.0
    return (axis[0] / n, axis[1] / n)


def _solve(m, v):
    """Eliminazione di Gauss con pivot parziale (sistema n x n)."""
    n = len(v)
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("punti di calibrazione degeneri (3 allineati?)")
        a[col], a[piv] = a[piv], a[col]
        for r in range(n):
            if r != col:
                f = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= f * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


class Homography:
    """Trasforma punti del piano stradale dall'immagine (0..1) ai metri, con 4 punti noti."""

    def __init__(self, image_points, world_points):
        if len(image_points) != 4 or len(world_points) != 4:
            raise ValueError("servono esattamente 4 punti immagine e 4 punti in metri")
        m, v = [], []
        for (x, y), (u, w) in zip(image_points, world_points):
            m.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); v.append(u)
            m.append([0, 0, 0, x, y, 1, -w * x, -w * y]); v.append(w)
        self.h = _solve(m, v) + [1.0]

    def apply(self, p):
        h = self.h
        x, y = p
        d = h[6] * x + h[7] * y + h[8]
        return ((h[0] * x + h[1] * y + h[2]) / d, (h[3] * x + h[4] * y + h[5]) / d)
