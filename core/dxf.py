"""
core/dxf.py
===========
The one DXF reader: turns an uploaded file into a measured part, or explains
why it cannot be priced.

Both the part card and the Sparrow nesting use this result, so what the card
shows is exactly what gets nested and priced.

How a file is read
------------------
* Units come from the $INSUNITS header; everything is converted to millimetres.
* Lines, arcs, circles, polylines, splines and ellipses are flattened to
  polylines. Pieces that only together form a loop (e.g. four LINEs) are
  chained end-to-end; a chain that returns to its start is a closed outline.
* A closed outline inside another is a hole. The outermost one is the part.
* Geometry on bend / centre-mark / annotation layers, and dashed or dotted
  geometry, is never a cut: it is kept aside (bend lines are drawn on the
  layout) and never becomes an outline. Text and dimensions are ignored.

When a file is "messy"
----------------------
A file is only priced when it holds exactly one clean part. Otherwise
``DxfReport.problems`` lists, in plain Finnish, why it cannot be priced yet:
no unit, unreadable content (e.g. blocks), no outline, several outlines,
an outline inside a hole, open lines, or a self-crossing / zero-area outline.
"""

from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass, field

from ezdxf import path, recover

from core.geometry import area, bbox, point_in_polygon, representative_point

Point = tuple[float, float]

# Curve flattening tolerance in *drawing units* (arcs/circles/bulges are
# approximated by segments no further than this from the true curve).
_FLATTENING_DISTANCE = 0.2

# Endpoint-join tolerance in millimetres. Two segment ends closer than this are
# treated as the same vertex when chaining open segments into a loop, and a chain
# whose ends meet within this distance is considered closed.
_JOIN_TOL_MM = 0.05

# Entity types that carry cut geometry.
_GEOMETRY_TYPES = {"LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE"}

# Entity types that are annotation, never a cut — ignored without complaint.
_ANNOTATION_TYPES = {
    "TEXT", "MTEXT", "ATTDEF", "ATTRIB", "DIMENSION", "ARC_DIMENSION",
    "LARGE_RADIAL_DIMENSION", "LEADER", "MLEADER", "MULTILEADER", "TOLERANCE",
    "HATCH", "POINT", "VIEWPORT", "WIPEOUT",
}

# Layer-name fragments that mark a layer as *not a cut* — bend lines, tangent
# lines, centre marks, dimensions, text, frames. Matched case-insensitively as
# substrings (so "IV_BEND_DOWN", "IV_TANGENT", "IV_ARC_CENTERS" are excluded,
# while "IV_OUTER_PROFILE" / "IV_INTERIOR_PROFILES" / "CONTOURS" stay).
_CONSTRUCTION_LAYER_KEYWORDS = (
    "bend", "tangent", "arc_center", "arccenter", "centers", "centermark",
    "center_mark", "centerline", "centreline", "construction", "reference",
    "dim", "dimension", "text", "note", "annot", "format", "frame", "border",
    "title", "hatch", "symbol", "axis", "axes", "sketch", "weld", "mark",
    "label", "leader", "hidden",
)

# Linetypes that are drawn solid. Anything else (DOT, DASHED, HIDDEN, CENTER,
# PHANTOM, …) marks reference / hidden / centre geometry, which is never a cut.
_SOLID_LINETYPES = ("", "CONTINUOUS", "BYBLOCK")

# Self-intersection is an O(n²) check; skip it above this many edges so a
# densely flattened contour can never stall the reader.
_SELF_INTERSECT_EDGE_CAP = 3000

# DXF $INSUNITS header code -> (millimetres per unit, label).
# https://ezdxf.readthedocs.io/en/stable/concepts/units.html
_UNITS: dict[int, tuple[float, str]] = {
    1: (25.4, "tuuma"),
    2: (304.8, "jalka"),
    4: (1.0, "mm"),
    5: (10.0, "cm"),
    6: (1000.0, "m"),
    8: (0.0000254, "µin"),
    9: (0.0254, "mil"),
    10: (914.4, "jaardi"),
    13: (1e-6, "nm"),
    14: (100.0, "dm"),
}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Contour:
    """One closed loop or open chain, in millimetres."""

    points: list[Point]
    closed: bool
    area_mm2: float = 0.0
    self_intersects: bool = False
    depth: int = 0              # how many larger closed contours contain this one

    @property
    def is_hole(self) -> bool:
        return self.closed and self.depth >= 1

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return bbox(self.points)

    @property
    def width_mm(self) -> float:
        x0, _, x1, _ = self.bbox
        return x1 - x0

    @property
    def height_mm(self) -> float:
        _, y0, _, y1 = self.bbox
        return y1 - y0


@dataclass
class DxfReport:
    """Everything read from one DXF file."""

    name: str
    unit_label: str = ""
    contours: list[Contour] = field(default_factory=list)
    # Bend / tangent / centre-mark polylines (mm): drawn, never cut.
    construction_lines: list[list[Point]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    # Why this file cannot be priced (empty = clean, exactly one part).
    problems: list[str] = field(default_factory=list)

    @property
    def parts(self) -> list[Contour]:
        """Closed contours that are not holes — the real cut parts."""
        return [c for c in self.contours if c.closed and not c.is_hole]

    @property
    def holes(self) -> list[Contour]:
        return [c for c in self.contours if c.is_hole]

    @property
    def part(self) -> Contour | None:
        """The one part of a clean file."""
        return None if self.problems else self.parts[0]


# ── Public entry point ───────────────────────────────────────────────────────

def read_dxf(data: bytes, name: str = "drawing.dxf") -> DxfReport:
    """Read one DXF file's bytes. Never raises: a bad file comes back with problems."""
    report = DxfReport(name=name)
    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash the app
        report.problems.append(f"Tiedostoa ei voitu lukea DXF-muodossa ({exc}).")
        return report

    unit_code = int(getattr(doc, "units", 0) or 0)
    factor, report.unit_label = _UNITS.get(unit_code, (1.0, f"koodi {unit_code}"))

    closed_direct: list[Contour] = []
    open_segments: list[list[Point]] = []
    unreadable: dict[str, int] = defaultdict(int)

    for entity in doc.modelspace():
        etype = entity.dxftype()
        if etype in ("TEXT", "MTEXT"):
            text = _text_of(entity)
            if text:
                report.texts.append(text)
            continue
        if etype in _ANNOTATION_TYPES:
            continue
        # Dotted / dashed / hidden geometry is reference, not a cut.
        if _is_non_cut_linetype(entity, doc):
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if etype not in _GEOMETRY_TYPES:
            if not _is_construction_layer(layer):
                unreadable[etype] += 1
            continue
        extracted = _entity_polyline(entity, factor)
        if extracted is None:
            continue
        pts, closed = extracted
        if _is_construction_layer(layer):
            report.construction_lines.append(pts)
        elif closed:
            closed_direct.append(Contour(points=pts, closed=True))
        else:
            open_segments.append(pts)

    report.contours = closed_direct + _chain_open_segments(open_segments, _JOIN_TOL_MM)
    for c in report.contours:
        c.area_mm2 = area(c.points)
        if c.closed and len(c.points) <= _SELF_INTERSECT_EDGE_CAP:
            c.self_intersects = _self_intersects(c.points)
    _mark_depths(report.contours)

    report.problems = _problems(report, unit_code, unreadable)
    return report


def _problems(report: DxfReport, unit_code: int, unreadable: dict[str, int]) -> list[str]:
    """Why a file cannot be priced — each reason in plain Finnish."""
    out = []
    if unit_code not in _UNITS:
        out.append(
            "Piirustuksesta puuttuu mittayksikkö ($INSUNITS), joten mittoja ei "
            "voi tulkita varmasti. Tallenna DXF uudelleen yksikkö (mm) asetettuna."
        )
    if unreadable:
        listed = ", ".join(f"{t} ×{n}" for t, n in sorted(unreadable.items()))
        out.append(
            f"Tiedostossa on elementtejä, joita ei vielä osata lukea: {listed}. "
            "(INSERT = lohko; pura lohkot CAD-ohjelmassa ennen tallennusta.)"
        )
    parts = report.parts
    if not parts:
        out.append("Tiedostosta ei löytynyt yhtään suljettua ääriviivaa.")
    elif len(parts) > 1:
        out.append(
            f"Tiedostossa on {len(parts)} erillistä ääriviivaa — esim. kehys, "
            "lisäkuva tai useampi osa. Yhdessä tiedostossa saa olla vain yksi osa."
        )
    if any(c.closed and c.depth >= 2 for c in report.contours):
        out.append(
            "Reiän sisällä on toinen ääriviiva — todennäköisesti kehys osan "
            "ympärillä tai osa piirretty toisen osan sisään."
        )
    n_open = sum(1 for c in report.contours if not c.closed)
    if n_open:
        lines = "1 viiva ei" if n_open == 1 else f"{n_open} viivaa ei"
        out.append(
            f"{lines} sulkeudu ääriviivaksi — ääriviivassa on aukko tai "
            "piirustuksessa on irrallisia viivoja."
        )
    if any(c.self_intersects for c in report.contours):
        out.append("Ääriviiva leikkaa itseään.")
    if any(c.closed and c.area_mm2 < 1e-6 for c in report.contours):
        out.append("Ääriviivan pinta-ala on nolla (viallinen geometria).")
    return out


# ── Entity helpers ───────────────────────────────────────────────────────────

def _is_construction_layer(name: str) -> bool:
    """True for bend / tangent / centre-mark / annotation layers (never cuts)."""
    low = (name or "").lower()
    return any(kw in low for kw in _CONSTRUCTION_LAYER_KEYWORDS)


def _is_non_cut_linetype(entity, doc) -> bool:
    """True when the entity's effective linetype is not solid (e.g. dotted)."""
    ltype = (getattr(entity.dxf, "linetype", "") or "BYLAYER").upper()
    if ltype == "BYLAYER":
        layer_name = getattr(entity.dxf, "layer", "0") or "0"
        try:
            ltype = (doc.layers.get(layer_name).dxf.linetype or "").upper()
        except Exception:  # noqa: BLE001 — missing layer table entry: assume solid
            return False
    return ltype not in _SOLID_LINETYPES


def _text_of(entity) -> str:
    try:
        text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.plain_text()
    except Exception:  # noqa: BLE001
        return ""
    return (text or "").strip()


def _entity_polyline(entity, factor: float) -> tuple[list[Point], bool] | None:
    """Flatten one geometry entity to a mm polyline + a closed flag."""
    try:
        p = path.make_path(entity)
    except (TypeError, ValueError):
        return None
    pts = [(float(v.x) * factor, float(v.y) * factor) for v in p.flattening(_FLATTENING_DISTANCE)]
    if len(pts) < 2:
        return None
    closed = p.start.isclose(p.end, abs_tol=_JOIN_TOL_MM / max(factor, 1e-9))
    if closed and (pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]):
        pts.append(pts[0])  # make the closing edge explicit
    return pts, closed


# ── Contour building ─────────────────────────────────────────────────────────

def _chain_open_segments(segments: list[list[Point]], tol: float) -> list[Contour]:
    """Chain open segments end-to-end into closed loops / open chains.

    Endpoints within `tol` mm are treated as the same vertex. A chain whose two
    ends meet is a closed contour; otherwise it stays open.
    """
    def key(pt: Point) -> tuple[int, int]:
        return (round(pt[0] / tol), round(pt[1] / tol))

    adj: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, pts in enumerate(segments):
        adj[key(pts[0])].append(i)
        adj[key(pts[-1])].append(i)

    used = [False] * len(segments)

    def next_unused(node: tuple[int, int]) -> int | None:
        for j in adj.get(node, ()):
            if not used[j]:
                return j
        return None

    contours: list[Contour] = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        chain = list(segments[start])
        while (j := next_unused(key(chain[-1]))) is not None:   # extend the tail
            used[j] = True
            seg = segments[j]
            seg = seg if key(seg[0]) == key(chain[-1]) else seg[::-1]
            chain.extend(seg[1:])
        while (j := next_unused(key(chain[0]))) is not None:    # extend the head
            used[j] = True
            seg = segments[j]
            seg = seg if key(seg[-1]) == key(chain[0]) else seg[::-1]
            chain = seg[:-1] + chain
        closed = len(chain) >= 4 and key(chain[0]) == key(chain[-1])
        contours.append(Contour(points=chain, closed=closed))
    return contours


def _mark_depths(contours: list[Contour]) -> None:
    """Set ``depth``: how many larger closed contours contain each closed one.

    A part is a top-level outline (depth 0); anything inside a part is a hole.
    Something inside a hole (depth ≥ 2) is reported as a problem.
    """
    closed = [c for c in contours if c.closed and len(c.points) >= 3]
    reps = {id(c): representative_point(c.points) for c in closed}
    for c in closed:
        c.depth = sum(
            1 for other in closed
            if other is not c and other.area_mm2 > c.area_mm2
            and point_in_polygon(reps[id(c)], other.points)
        )


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """True if segment p1p2 properly crosses p3p4 (shared endpoints ignored)."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(v) < 1e-9:
            return 0
        return 1 if v > 0 else -1

    # Ignore segments that share an endpoint — neighbours in a polygon always do.
    for a in (p1, p2):
        for b in (p3, p4):
            if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                return False
    return (orient(p3, p4, p1) != orient(p3, p4, p2)
            and orient(p1, p2, p3) != orient(p1, p2, p4))


def _self_intersects(points: list[Point]) -> bool:
    """Does the closed polygon cross itself? Non-adjacent edges only."""
    n = len(points)
    if n < 4:
        return False
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    m = len(edges)
    for i in range(m):
        a1, a2 = edges[i]
        # start at i+2 to skip the adjacent edge; the shared-endpoint guard in
        # _segments_cross handles the wrap-around neighbour (edge m-1 vs edge 0).
        for j in range(i + 2, m):
            if i == 0 and j == m - 1:
                continue
            if _segments_cross(a1, a2, *edges[j]):
                return True
    return False
