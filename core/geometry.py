"""Small 2-D polygon helpers. A point is an ``(x, y)`` tuple in mm; a ring is
a list of points, closed implicitly."""

from __future__ import annotations

import math

Point = tuple[float, float]


def signed_area(points: list[Point]) -> float:
    """Shoelace area: positive for counter-clockwise rings, negative for clockwise."""
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def area(points: list[Point]) -> float:
    """Absolute area of a ring (mm²)."""
    return abs(signed_area(points))


def net_area(outer: list[Point], holes: list[list[Point]]) -> float:
    """Area of an outline minus its holes (mm²) — the real cut area of a part."""
    return area(outer) - sum(area(h) for h in holes)


def length(points, closed: bool = False) -> float:
    """Length of a polyline in its units; ``closed`` adds the closing edge."""
    pts = list(points) + ([points[0]] if closed and points else [])
    return sum(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
               for (x0, y0), (x1, y1) in zip(pts, pts[1:]))


def bbox(points) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of a point list."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_wh(points) -> tuple[float, float]:
    """Width and height of a point list's bounding box."""
    minx, miny, maxx, maxy = bbox(points)
    return maxx - minx, maxy - miny


def rotate_translate(points, rotation_deg: float, t: tuple[float, float]) -> list[Point]:
    """Apply p' = R(θ)·p + t to every point."""
    th = math.radians(rotation_deg)
    c, s = math.cos(th), math.sin(th)
    tx, ty = t
    return [(x * c - y * s + tx, x * s + y * c + ty) for x, y in points]


def convex_hull(points) -> list[Point]:
    """Counter-clockwise convex hull (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def half(seq):
        out: list[Point] = []
        for p in seq:
            while len(out) >= 2 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                     - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out

    lower, upper = half(pts), half(reversed(pts))
    return lower[:-1] + upper[:-1]


def min_area_angle(points) -> float:
    """The turn (degrees, counter-clockwise) that lays a shape along its length:
    its smallest bounding rectangle straight, long side horizontal. That
    rectangle always has a side on a convex-hull edge, so only those are tried."""
    hull = convex_hull(points)
    best = (math.inf, 0.0)
    for (x1, y1), (x2, y2) in zip(hull, hull[1:] + hull[:1]):
        turn = -math.degrees(math.atan2(y2 - y1, x2 - x1))
        w, h = bbox_wh(rotate_translate(hull, turn, (0.0, 0.0)))
        if w * h < best[0]:
            best = (w * h, turn if w >= h else turn + 90.0)
    return best[1]


def point_in_polygon(pt: Point, poly: list[Point]) -> bool:
    """Ray-casting point-in-polygon test (even–odd rule)."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi) if yj != yi else xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def representative_point(poly: list[Point]) -> Point:
    """A point that is inside the polygon (centroid, nudged if it lands outside)."""
    n = len(poly)
    cx = sum(x for x, _ in poly) / n
    cy = sum(y for _, y in poly) / n
    if point_in_polygon((cx, cy), poly):
        return cx, cy
    # Concave shape: scan a horizontal line through the centroid for an inside x.
    xs = sorted({x for x, _ in poly})
    for a, b in zip(xs, xs[1:]):
        mid = (a + b) / 2
        if point_in_polygon((mid, cy), poly):
            return mid, cy
    return cx, cy


# ── Self-crossing rings ───────────────────────────────────────────────────────

def first_crossing(points: list[Point], eps: float = 1e-9
                   ) -> tuple[int, int, Point] | None:
    """The first two non-neighbouring edges of a closed ring that cross or
    touch, as ``(i, j, point)`` with i < j (edge k runs from point k to point
    k + 1), or None for a simple ring. The ring must not repeat its first
    point at the end. Edges are bucketed on a grid, so long rings stay quick."""
    n = len(points)
    if n < 4:
        return None
    edges = [(points[k], points[(k + 1) % n]) for k in range(n)]
    x0, y0, x1, y1 = bbox(points)
    cell = max(x1 - x0, y1 - y0, eps) / max(1, int(math.sqrt(n)) * 2)
    grid: dict[tuple[int, int], list[int]] = {}
    for k, (a, b) in enumerate(edges):
        for gx in range(int((min(a[0], b[0]) - x0) / cell), int((max(a[0], b[0]) - x0) / cell) + 1):
            for gy in range(int((min(a[1], b[1]) - y0) / cell),
                            int((max(a[1], b[1]) - y0) / cell) + 1):
                grid.setdefault((gx, gy), []).append(k)
    best = None
    for ks in grid.values():
        for u in range(len(ks)):
            for v in range(u + 1, len(ks)):
                i, j = sorted((ks[u], ks[v]))
                if j == i + 1 or (i == 0 and j == n - 1) or (best and (i, j) >= best[:2]):
                    continue
                p = _segment_hit(*edges[i], *edges[j], eps)
                if p is not None:
                    best = (i, j, p)
    return best


def _segment_hit(p1, p2, p3, p4, eps):
    """Where segments p1p2 and p3p4 meet (cross or touch), or None."""
    d1 = (p2[0] - p1[0], p2[1] - p1[1])
    d2 = (p4[0] - p3[0], p4[1] - p3[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    w = (p3[0] - p1[0], p3[1] - p1[1])
    scale = max(abs(d1[0]) + abs(d1[1]), abs(d2[0]) + abs(d2[1]), eps)
    if abs(den) <= eps * scale * scale:
        # Parallel: they meet only if collinear and overlapping.
        if abs(w[0] * d1[1] - w[1] * d1[0]) > eps * scale * scale:
            return None
        dd = d1[0] * d1[0] + d1[1] * d1[1]
        if dd <= eps:
            return None
        for q in (p3, p4):
            t = ((q[0] - p1[0]) * d1[0] + (q[1] - p1[1]) * d1[1]) / dd
            if -eps <= t <= 1 + eps:
                return q
        t = ((p1[0] - p3[0]) * d2[0] + (p1[1] - p3[1]) * d2[1]) / max(
            d2[0] * d2[0] + d2[1] * d2[1], eps)
        return p1 if -eps <= t <= 1 + eps else None
    t = (w[0] * d2[1] - w[1] * d2[0]) / den
    s = (w[0] * d1[1] - w[1] * d1[0]) / den
    if -eps <= t <= 1 + eps and -eps <= s <= 1 + eps:
        return (p1[0] + t * d1[0], p1[1] + t * d1[1])
    return None


def untangle(points: list[Point], max_splits: int = 200) -> list[Point]:
    """A closed ring made simple: wherever two of its edges cross or touch it
    is split there into two loops, and the larger one is kept (the other is a
    spike, a doubled line or a small twist from the drawing). A simple ring
    comes back unchanged."""
    ring = _dedupe(points)
    for _ in range(max_splits):
        hit = first_crossing(ring)
        if hit is None:
            return ring
        i, j, p = hit
        inner = [p, *ring[i + 1:j + 1]]
        outer = [*ring[j + 1:], *ring[:i + 1], p]
        loops = [_dedupe(loop) for loop in (inner, outer)]
        loops = [loop for loop in loops if len(loop) >= 3]
        if not loops:
            return ring
        ring = max(loops, key=lambda loop: abs(signed_area(loop)))
    return ring


def _dedupe(ring: list[Point], gap: float = 1e-6) -> list[Point]:
    out: list[Point] = []
    for q in ring:
        if not out or abs(q[0] - out[-1][0]) > gap or abs(q[1] - out[-1][1]) > gap:
            out.append(q)
    while len(out) > 1 and abs(out[0][0] - out[-1][0]) <= gap and abs(out[0][1] - out[-1][1]) <= gap:
        out.pop()
    return out
