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
