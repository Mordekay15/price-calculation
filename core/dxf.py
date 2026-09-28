"""
core/dxf.py
===========
The one DXF reader: turns an uploaded file into the part that is shown on the
card, nested by Sparrow and priced — or explains why it cannot be priced.

Two stages:

``read_dxf(data, name) -> DxfFile``
    Reads the file once. Blocks (INSERT) are exploded, curves flattened to
    polylines in millimetres, and every piece of geometry is kept with its CAD
    layer. The unit comes from the $INSUNITS header; failing that from a
    ``Un="mm"`` style text label; failing that, a drawing whose extents are
    exactly an ISO A0–A4 sheet is taken to be in millimetres.

``DxfFile.part(layers) -> DxfReport``
    Builds the part from the chosen layers. By default that is every layer
    whose name does not look like drawing furniture (frame, title, dimension,
    text, bend, info, …) — see ``suggested_layers``; the card lets the user
    change the choice.

    * Pieces that only together form a loop (e.g. four LINEs) are chained.
    * The main part is the largest closed outline; closed outlines directly
      inside it are its holes. Anything outside it (detail views, sketches,
      stray lines) is dropped.
    * Reference lines are drawn, never cut: lines inside the part from the
      other layers (bend lines, centre marks, dashed lines), outlines inside a
      hole (countersinks / threads drawn as concentric circles) and the ISO
      thread symbol (a thin ¾-circle around a hole).

A report with ``problems`` cannot be priced; each problem is a plain-Finnish
reason (no unit, no closed outline, an open line inside the part, a
self-crossing outline, …).
"""

from __future__ import annotations

import io
import re
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

# How far (mm) a reference line may stick out of the part's bounding box and
# still be drawn with the part (bend lines often end exactly on the outline).
_REFERENCE_BBOX_TOL_MM = 0.5

# Entity types that carry cut geometry.
_GEOMETRY_TYPES = {"LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE", "SPLINE", "ELLIPSE"}

# Entity types that are annotation, never a cut — ignored without complaint.
_ANNOTATION_TYPES = {
    "TEXT", "MTEXT", "ATTDEF", "ATTRIB", "DIMENSION", "ARC_DIMENSION",
    "LARGE_RADIAL_DIMENSION", "LEADER", "MLEADER", "MULTILEADER", "TOLERANCE",
    "HATCH", "POINT", "VIEWPORT", "WIPEOUT",
}

# Layer-name fragments that mark a layer as drawing furniture or reference
# geometry rather than the cut part — left out by default. Matched
# case-insensitively as substrings (so "IV_BEND_DOWN" and "IV_ARC_CENTERS" are
# left out, while "IV_OUTER_PROFILE" and "CONTOURS" stay).
_NON_CUT_LAYER_KEYWORDS = (
    # drawing furniture
    "frame", "border", "title", "format", "info", "bom", "table", "note",
    "text", "label", "balloon", "symbol", "detail", "section", "defpoint",
    # dimensions and annotation
    "dim", "annot", "leader", "gdt", "tol", "hatch", "mark", "weld", "thread",
    "cosm",
    # reference / construction geometry
    "bend", "tangent", "arc_center", "arccenter", "centers", "centermark",
    "center_mark", "centerline", "centreline", "construction", "reference",
    "axis", "axes", "csys", "sketch", "draft", "hidden",
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

# A unit written in the drawing's text, e.g.  Un="mm"  or  Units=inch.
_UNIT_TEXT_RE = re.compile(
    r'\b(?:un|unit|units|yksikk[oö])\s*=\s*"?([a-z]+|")', re.IGNORECASE)
_TEXT_UNIT_TO_CODE: dict[str, int] = {
    "mm": 4, "millimeter": 4, "millimetre": 4, "millimeters": 4, "millimetres": 4,
    "cm": 5, "centimeter": 5, "centimetre": 5,
    "m": 6, "meter": 6, "metre": 6,
    "in": 1, "inch": 1, "inches": 1, '"': 1,
}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class _Piece:
    """One flattened entity, in millimetres."""

    points: list[Point]
    closed: bool
    layer: str
    solid: bool          # False for dashed / dotted (reference) linetypes


@dataclass
class Contour:
    """One closed loop or open chain, in millimetres."""

    points: list[Point]
    closed: bool
    area_mm2: float = 0.0
    depth: int = 0       # how many larger closed contours contain this one

    @property
    def width_mm(self) -> float:
        x0, _, x1, _ = bbox(self.points)
        return x1 - x0

    @property
    def height_mm(self) -> float:
        _, y0, _, y1 = bbox(self.points)
        return y1 - y0


@dataclass
class DxfReport:
    """The part built from one file and a choice of layers."""

    name: str
    unit_label: str
    outline: Contour | None = None
    holes: list[Contour] = field(default_factory=list)
    # Lines inside the part from the other layers (bend lines, centre marks):
    # drawn on the card and the layout, never cut.
    reference_lines: list[list[Point]] = field(default_factory=list)
    open_lines: list[list[Point]] = field(default_factory=list)   # inside the part
    dropped: list[list[Point]] = field(default_factory=list)      # outside the part
    # Why this part cannot be priced (empty = it can).
    problems: list[str] = field(default_factory=list)


@dataclass
class DxfFile:
    """Everything read from one DXF file, grouped by layer."""

    name: str
    unit_label: str = ""
    unit_note: str = ""          # how the unit was found, when not from the header
    pieces: list[_Piece] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)          # file-level
    unreadable: dict[str, dict[str, int]] = field(default_factory=dict)  # layer -> type -> n

    def available_layers(self) -> list[str]:
        """Layers that carry geometry, sorted."""
        return sorted({p.layer for p in self.pieces} | set(self.unreadable))

    def suggested_layers(self) -> list[str]:
        """The layers cut by default: all but frame / text / bend / … layers.

        Falls back to every layer when the names can't tell them apart.
        """
        avail = self.available_layers()
        return [n for n in avail if not _is_non_cut_layer(n)] or avail

    def layer_sizes(self) -> dict[str, tuple[int, float, float]]:
        """Per-layer (piece count, bbox width mm, bbox height mm)."""
        by_layer: dict[str, list[_Piece]] = defaultdict(list)
        for p in self.pieces:
            by_layer[p.layer].append(p)
        out = {}
        for name, pieces in by_layer.items():
            x0, y0, x1, y1 = bbox([pt for p in pieces for pt in p.points])
            out[name] = (len(pieces), x1 - x0, y1 - y0)
        return out

    def part(self, layers: set[str] | None = None) -> DxfReport:
        """Build the main part from ``layers`` (default: ``suggested_layers``)."""
        chosen = set(self.suggested_layers() if layers is None else layers)
        report = DxfReport(name=self.name, unit_label=self.unit_label,
                           problems=list(self.problems))

        unreadable: dict[str, int] = defaultdict(int)
        for layer in chosen:
            for etype, n in self.unreadable.get(layer, {}).items():
                unreadable[etype] += n
        if unreadable:
            listed = ", ".join(f"{t} ×{n}" for t, n in sorted(unreadable.items()))
            report.problems.append(
                f"Valituilla tasoilla on elementtejä, joita ei vielä osata lukea: {listed}."
            )

        cut = [p for p in self.pieces if p.layer in chosen and p.solid]
        reference = [p for p in self.pieces if not (p.layer in chosen and p.solid)]
        contours = [Contour(p.points, True) for p in cut if p.closed]
        contours += _chain_open_segments([p.points for p in cut if not p.closed], _JOIN_TOL_MM)
        for c in contours:
            c.area_mm2 = area(c.points)
        _mark_depths(contours)

        top = [c for c in contours if c.closed and c.depth == 0 and len(c.points) >= 3]
        if not top:
            report.problems.append("Valituilta tasoilta ei löytynyt suljettua ääriviivaa.")
            report.dropped = [c.points for c in contours]
            return report

        # The main part is the largest outline; everything inside it belongs to
        # it, everything outside is dropped (detail views, sketches, stray lines).
        main = max(top, key=lambda c: c.width_mm * c.height_mm)
        report.outline = main

        inner = []
        for c in contours:
            if c is main:
                continue
            pt = representative_point(c.points) if c.closed else c.points[len(c.points) // 2]
            if point_in_polygon(pt, main.points):
                inner.append(c)
            else:
                report.dropped.append(c.points)
        # Holes are the outlines directly inside the part. An outline inside a
        # hole (a countersink or thread drawn as concentric circles) and a thin
        # ¾-circle around a hole (the ISO thread symbol) are drawing marks of
        # that hole: drawn, not cut. Any other open line is a real gap.
        report.holes = [c for c in inner if c.closed and c.depth == 1]
        report.reference_lines = _lines_inside(main, [p.points for p in reference])
        rings = [c for c in inner if c.closed]
        for c in inner:
            if c.closed and c.depth >= 2 or not c.closed and _is_thread_mark(c.points, rings):
                report.reference_lines.append(c.points)
            elif not c.closed:
                report.open_lines.append(c.points)

        report.problems += _part_problems(report)
        return report


# ── Reading ──────────────────────────────────────────────────────────────────

def read_dxf(data: bytes, name: str = "drawing.dxf") -> DxfFile:
    """Read one DXF file's bytes. Never raises: a bad file comes back with problems."""
    f = DxfFile(name=name)
    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash the app
        f.problems.append(f"Tiedostoa ei voitu lukea DXF-muodossa ({exc}).")
        return f

    entities = list(_explode_blocks(doc.modelspace()))
    f.texts = [t for t in (_text_of(e) for e in entities
                           if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")) if t]

    unit_code = int(getattr(doc, "units", 0) or 0)
    if unit_code not in _UNITS:
        unit_code = _unit_from_texts(f.texts)
        if unit_code is not None:
            f.unit_note = "luettu piirustuksen tekstistä"
    # Without a unit, read in drawing units; a standard sheet size below may
    # still show they are millimetres.
    factor, f.unit_label = _UNITS.get(unit_code, (1.0, "ei yksikköä"))

    for entity in entities:
        etype = entity.dxftype()
        if etype in _ANNOTATION_TYPES:
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        if etype not in _GEOMETRY_TYPES:
            counts = f.unreadable.setdefault(layer, {})
            counts[etype] = counts.get(etype, 0) + 1
            continue
        extracted = _entity_polyline(entity, factor)
        if extracted is not None:
            pts, closed = extracted
            f.pieces.append(_Piece(pts, closed, layer, not _is_dashed(entity, doc)))

    if unit_code is None:
        sheet = _iso_sheet([pt for p in f.pieces for pt in p.points])
        if sheet:
            f.unit_label, f.unit_note = "mm", f"päätelty piirustusarkin koosta ({sheet})"
        else:
            f.problems.append(
                "Piirustuksesta puuttuu mittayksikkö ($INSUNITS tai Un=-teksti), "
                "eikä piirustusarkin koosta voi päätellä sitä, joten mittoja ei voi "
                "tulkita varmasti. Tallenna DXF uudelleen yksikkö (mm) asetettuna."
            )
    return f


def _part_problems(report: DxfReport) -> list[str]:
    """Why a built part cannot be priced — each reason in plain Finnish."""
    out = []
    rings = [report.outline, *report.holes]
    if report.open_lines:
        n = len(report.open_lines)
        lines = "1 viiva osan sisällä ei" if n == 1 else f"{n} viivaa osan sisällä ei"
        out.append(f"{lines} sulkeudu ääriviivaksi — reiän ääriviivassa on aukko?")
    if any(len(c.points) <= _SELF_INTERSECT_EDGE_CAP and _self_intersects(c.points)
           for c in rings):
        out.append("Ääriviiva leikkaa itseään.")
    if report.outline.area_mm2 < 1e-6:
        out.append("Ääriviivan pinta-ala on nolla (viallinen geometria).")
    return out


# ISO 216 drawing sheets (mm), long × short side.
_ISO_SHEETS = {"A0": (1189, 841), "A1": (841, 594), "A2": (594, 420),
               "A3": (420, 297), "A4": (297, 210)}
_ISO_SHEET_TOL = 2.0


def _iso_sheet(points: list[Point]) -> str | None:
    """The ISO sheet whose size the drawing's extents match (in mm), if any."""
    if not points:
        return None
    x0, y0, x1, y1 = bbox(points)
    long_side, short_side = max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0)
    for name, (a, b) in _ISO_SHEETS.items():
        if abs(long_side - a) <= _ISO_SHEET_TOL and abs(short_side - b) <= _ISO_SHEET_TOL:
            return name
    return None


def _is_thread_mark(line: list[Point], rings: list[Contour]) -> bool:
    """Is this open line the ISO thread symbol — an arc centred on a hole ring
    and a little larger than it (e.g. a ¾-circle of Ø4 around an M4 tap hole)?"""
    circle = _circle_through(line[0], line[len(line) // 2], line[-1])
    if circle is None:
        return False
    (cx, cy), r = circle
    if any(abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - r) > 0.02 * r for x, y in line):
        return False                                   # not a circular arc
    for ring in rings:
        hx0, hy0, hx1, hy1 = bbox(ring.points)
        hr = (hx1 - hx0) / 2
        if (abs((hx0 + hx1) / 2 - cx) <= 0.05 * r and abs((hy0 + hy1) / 2 - cy) <= 0.05 * r
                and 0.98 * hr <= r <= 1.5 * hr):
            return True
    return False


def _circle_through(a: Point, b: Point, c: Point) -> tuple[Point, float] | None:
    """Centre and radius of the circle through three points (None if collinear)."""
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    if abs(d) < 1e-12:
        return None
    sa, sb, sc = a[0] ** 2 + a[1] ** 2, b[0] ** 2 + b[1] ** 2, c[0] ** 2 + c[1] ** 2
    cx = (sa * (b[1] - c[1]) + sb * (c[1] - a[1]) + sc * (a[1] - b[1])) / d
    cy = (sa * (c[0] - b[0]) + sb * (a[0] - c[0]) + sc * (b[0] - a[0])) / d
    return (cx, cy), ((a[0] - cx) ** 2 + (a[1] - cy) ** 2) ** 0.5


def _lines_inside(main: Contour, lines: list[list[Point]]) -> list[list[Point]]:
    """The lines within the part's bounding box whose centre is inside it."""
    x0, y0, x1, y1 = bbox(main.points)
    tol = _REFERENCE_BBOX_TOL_MM
    out = []
    for line in lines:
        if any(x < x0 - tol or x > x1 + tol or y < y0 - tol or y > y1 + tol
               for x, y in line):
            continue
        cx = sum(x for x, _ in line) / len(line)
        cy = sum(y for _, y in line) / len(line)
        if point_in_polygon((cx, cy), main.points):
            out.append(line)
    return out


# ── Entity helpers ───────────────────────────────────────────────────────────

def _explode_blocks(entities):
    """Yield the entities with every block reference (INSERT) replaced by its
    content, recursively. Dimensions stay whole (they are annotation)."""
    for e in entities:
        if e.dxftype() == "INSERT":
            yield from _explode_blocks(e.virtual_entities())
            yield from e.attribs
        else:
            yield e


def _is_non_cut_layer(name: str) -> bool:
    low = (name or "").lower()
    return any(kw in low for kw in _NON_CUT_LAYER_KEYWORDS)


def _is_dashed(entity, doc) -> bool:
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
        text = entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.text
    except Exception:  # noqa: BLE001
        return ""
    return (text or "").strip()


def _unit_from_texts(texts: list[str]) -> int | None:
    for text in texts:
        m = _UNIT_TEXT_RE.search(text)
        if m and m.group(1).lower() in _TEXT_UNIT_TO_CODE:
            return _TEXT_UNIT_TO_CODE[m.group(1).lower()]
    return None


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
    """Set ``depth``: how many larger closed contours contain each closed one."""
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
