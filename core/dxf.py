"""The one DXF reader: ``read_dxf`` reads a file once, ``DxfFile.part(layers)``
builds the part that is shown, nested and priced — or the reasons it cannot be
priced (see "How a DXF file is read" in the README)."""

from __future__ import annotations

import io
import math
import re
import sys
from array import array
from collections import defaultdict
from dataclasses import dataclass, field

import ezdxf
from ezdxf import path, recover

from core.geometry import (
    area,
    bbox,
    bbox_wh,
    length,
    min_area_angle,
    point_in_polygon,
    representative_point,
    rotate_translate,
)

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

class _Piece:
    """One flattened entity, in millimetres. Its points are kept as one flat
    array of floats, not as a list of tuples: a big RFQ's drawings have
    millions of them, and tuples take about seven times the memory."""

    __slots__ = ("_xy", "closed", "layer", "solid")

    def __init__(self, points: list[Point], closed: bool, layer: str, solid: bool):
        self.points = points
        self.closed = closed
        self.layer = sys.intern(layer)
        self.solid = solid   # False for dashed / dotted (reference) linetypes

    @property
    def points(self) -> list[Point]:
        xy = self._xy
        return list(zip(xy[0::2], xy[1::2]))

    @points.setter
    def points(self, points: list[Point]) -> None:
        self._xy = array("d", [v for x, y in points for v in (x, y)])


@dataclass
class Contour:
    """One closed loop or open chain, in millimetres."""

    points: list[Point]
    closed: bool
    area_mm2: float = 0.0
    depth: int = 0       # how many larger closed contours contain this one

    @property
    def width_mm(self) -> float:
        return bbox_wh(self.points)[0]

    @property
    def height_mm(self) -> float:
        return bbox_wh(self.points)[1]


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
    dropped_more: int = 0      # shapes outside the part not kept (see _trim_dropped)
    # Why this part cannot be priced (empty = it can).
    problems: list[str] = field(default_factory=list)
    # For the work price: threads drawn with the ISO mark, and bend lines.
    threads: int = 0
    bends: int = 0

    @property
    def dropped_count(self) -> int:
        return len(self.dropped) + self.dropped_more

    @property
    def cut_length_mm(self) -> float:
        """What the laser cuts: the outline, every hole and any open cut."""
        if self.outline is None:
            return 0.0
        return (sum(length(c.points, closed=True) for c in (self.outline, *self.holes))
                + sum(length(line) for line in self.open_lines))


@dataclass
class DxfFile:
    """Everything read from one DXF file, grouped by layer."""

    name: str
    unit_label: str = ""
    # No unit anywhere: unit_label is only a guess, which the user must confirm
    # before the part is priced.
    unit_guessed: bool = False
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

        if self.problems and not self.pieces:
            return report  # the file couldn't be read: its own problem says why
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
            _trim_dropped(report)
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
        report.bends = len(_lines_inside(main, [p.points for p in reference
                                                if _is_bend_layer(p.layer)]))
        rings = [c for c in inner if c.closed]
        for c in inner:
            if not c.closed and _is_thread_mark(c.points, rings):
                report.threads += 1
                report.reference_lines.append(c.points)
            elif c.closed and c.depth >= 2:
                report.reference_lines.append(c.points)
            elif not c.closed:
                report.open_lines.append(c.points)

        report.problems += _part_problems(report)
        _trim_dropped(report)
        _lay_along_length(report)
        return report


_DROPPED_POINTS = 5000   # points kept of the shapes outside the part


def _trim_dropped(report: DxfReport) -> None:
    """Keep only the biggest shapes outside the part, up to ``_DROPPED_POINTS``
    points: they are only drawn grey in a small preview, and a drawing sheet's
    views and dimensions can be a million points (a big RFQ's memory). The
    rest are counted in ``dropped_more``."""
    if sum(map(len, report.dropped)) <= _DROPPED_POINTS:
        return
    kept, n = [], 0
    for line in sorted(report.dropped, key=lambda ln: -sum(bbox_wh(ln))):
        if n + len(line) > _DROPPED_POINTS:
            continue
        kept.append(line)
        n += len(line)
    report.dropped_more = len(report.dropped) - len(kept)
    report.dropped = kept


# A part within this many degrees of lying along its length is left as drawn,
# and a turn this close to a quarter turn is made an exact one.
_STRAIGHT_TOL_DEG = 0.5

# Exact quarter turns (counter-clockwise), free of float round-off.
_QUARTER_TURNS = {1: lambda x, y: (-y, x), 2: lambda x, y: (-x, -y), 3: lambda x, y: (y, -x)}


def _lay_along_length(report: DxfReport) -> None:
    """Turn the whole drawing so the part lies along its length, long side
    horizontal, as a designer lays it before nesting: a strip drawn diagonally
    is then 2936 × 446 mm, not 2120 × 2061, and a part drawn standing lies
    down. The card, the price, the nesting angle (0/180 = along the length)
    and Sparrow all use the turned part."""
    turn = min_area_angle(report.outline.points)
    off = turn % 180.0
    if min(off, 180.0 - off) <= _STRAIGHT_TOL_DEG:
        return
    quarters = round(turn / 90.0)
    if abs(turn - 90.0 * quarters) <= _STRAIGHT_TOL_DEG:
        exact = _QUARTER_TURNS[quarters % 4]

        def turned(points):
            return [exact(x, y) for x, y in points]
    else:
        def turned(points):
            return rotate_translate(points, turn, (0.0, 0.0))

    for c in (report.outline, *report.holes):
        c.points = turned(c.points)
    for lines in (report.reference_lines, report.open_lines, report.dropped):
        lines[:] = [turned(line) for line in lines]


# ── Reading ──────────────────────────────────────────────────────────────────

def read_dxf(data: bytes, name: str = "drawing.dxf") -> DxfFile:
    """Read one DXF file's bytes. Never raises: a bad file comes back with problems."""
    f = DxfFile(name=name)
    try:
        doc = _load(data)
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash the app
        f.problems.append(f"Tiedostoa ei voitu lukea DXF-muodossa ({exc}).")
        return f

    entities = list(_explode_blocks(doc.modelspace()))
    f.texts = [t for t in (_text_of(e) for e in entities
                           if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")) if t]

    unit_code = int(getattr(doc, "units", 0) or 0)
    if unit_code not in _UNITS:
        unit_code = _unit_from_texts(f.texts)
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
        if _iso_sheet([pt for p in f.pieces for pt in p.points]):
            f.unit_label = "mm"
        else:
            # Guess: mm, unless the header says the drawing is imperial.
            f.unit_guessed = True
            if doc.header.get("$MEASUREMENT", 1) == 0:
                factor, f.unit_label = _UNITS[1]
                for p in f.pieces:
                    p.points = [(x * factor, y * factor) for x, y in p.points]
            else:
                f.unit_label = "mm"
    return f


def _load(data: bytes):
    """The DXF document: read the usual way and checked (``audit``), which is
    several times faster than ezdxf's ``recover``; a file that can't be read
    so (broken structure, binary DXF) goes through ``recover``."""
    if not data.startswith(b"AutoCAD Binary DXF"):
        try:
            from ezdxf.lldxf.validator import dxf_info

            head = io.StringIO(data[:1 << 16].decode("cp1252", errors="ignore"), newline=None)
            text = data.decode(dxf_info(head).encoding, errors="surrogateescape")
            doc = ezdxf.read(io.StringIO(text, newline=None))   # CRLF files too
            doc.audit()
            return doc
        except Exception:  # noqa: BLE001 — recover reads what it can
            pass
    doc, _auditor = recover.read(io.BytesIO(data))
    return doc


_SLIT_TOL_MM = 0.01      # a slit's points are this close to the line between its ends
_SLIT_MIN_MM = 1.0


def is_slit(points: list[Point]) -> bool:
    """A single straight line inside the part, such as a louvre or a lanced
    slit in a flat pattern: cut as a line, not a broken hole outline (a gap
    in a hole leaves a bent chain: an arc or the rest of a rectangle)."""
    (x0, y0), (x1, y1) = points[0], points[-1]
    span = math.hypot(x1 - x0, y1 - y0)
    if span < _SLIT_MIN_MM:
        return False
    return all(abs((x - x0) * (y1 - y0) - (y - y0) * (x1 - x0)) / span <= _SLIT_TOL_MM
               for x, y in points)


def _part_problems(report: DxfReport) -> list[str]:
    """Why a built part cannot be priced — each reason in plain Finnish."""
    out = []
    rings = [report.outline, *report.holes]
    gaps = [line for line in report.open_lines if not is_slit(line)]
    if gaps:
        n = len(gaps)
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


_FASTENERS = re.compile(r"(?:MUTTER|RUUV|PULT|NIIT|HOLK|TAPP)\w*\D*?(\d+)\s*KPL",
                        re.IGNORECASE)


def fasteners_from_texts(texts: list[str]) -> int:
    """Fasteners the drawing's notes ask for (nuts, screws, bolts, rivets,
    bushes, studs), e.g. "M5-PURISTEMUTTERIT X4KPL" and "M4-PURISTEMUTTERIT
    5KPL" make 9."""
    return sum(int(n) for t in texts for n in _FASTENERS.findall(t))


def _is_bend_layer(name: str) -> bool:
    """A layer of bend lines (e.g. IV_BEND, IV_BEND_DOWN), not their extents."""
    name = name.lower()
    return (any(k in name for k in ("bend", "taiv", "taitto", "kantti"))
            and "extent" not in name)


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
    # A line shorter than the join tolerance (CAD exports leave 0.004 mm stubs
    # between outline segments) is a stub to chain through, not a loop.
    if max(bbox_wh(pts)) <= _JOIN_TOL_MM:
        closed = False
    if closed and (pts[0][0] != pts[-1][0] or pts[0][1] != pts[-1][1]):
        pts.append(pts[0])  # make the closing edge explicit
    return pts, closed


# ── Contour building ─────────────────────────────────────────────────────────

def _chain_open_segments(segments: list[list[Point]], tol: float) -> list[Contour]:
    """Chain open segments end-to-end into closed loops / open chains.

    Endpoints within `tol` mm are one vertex, and every end is moved onto it, so
    a line that overshoots its corner by a few hundredths leaves no spike. A
    chain whose two ends meet is a closed contour; otherwise it stays open.
    """
    # Vertices: an endpoint joins the nearest vertex within `tol` (searched in
    # the neighbouring grid cells too, so two ends on either side of a cell
    # border still meet), else starts a new one.
    vertices: list[Point] = []
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    def vertex(pt: Point) -> int:
        cx, cy = math.floor(pt[0] / tol), math.floor(pt[1] / tol)
        near = [(math.dist(vertices[v], pt), v)
                for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for v in grid.get((cx + dx, cy + dy), ())]
        near = [n for n in near if n[0] <= tol]
        if near:
            return min(near)[1]
        vertices.append(pt)
        grid[(cx, cy)].append(len(vertices) - 1)
        return len(vertices) - 1

    contours: list[Contour] = []
    segs: list[tuple[list[Point], int, int]] = []
    for pts in segments:
        a, b = vertex(pts[0]), vertex(pts[-1])
        pts = [vertices[a], *pts[1:-1], vertices[b]]
        if a == b:
            if max(bbox_wh(pts)) > tol:     # a loop on its own
                contours.append(Contour(points=pts, closed=True))
            continue                        # else a zero-length stub
        segs.append((pts, a, b))

    adj: dict[int, list[int]] = defaultdict(list)
    for i, (_, a, b) in enumerate(segs):
        adj[a].append(i)
        adj[b].append(i)

    used = [False] * len(segs)

    def next_unused(v: int) -> int | None:
        for j in adj.get(v, ()):
            if not used[j]:
                return j
        return None

    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        chain, head, tail = list(segs[start][0]), segs[start][1], segs[start][2]
        while (j := next_unused(tail)) is not None:     # extend the tail
            used[j] = True
            seg, a, b = segs[j]
            seg, tail = (seg, b) if a == tail else (seg[::-1], a)
            chain.extend(seg[1:])
        while (j := next_unused(head)) is not None:     # extend the head
            used[j] = True
            seg, a, b = segs[j]
            seg, head = (seg, a) if b == head else (seg[::-1], b)
            chain = seg[:-1] + chain
        closed = len(chain) >= 4 and head == tail
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
        # c within 1e-6 mm of the line ab counts as on it. The tolerance is a
        # distance, not a raw cross product: on a 2 m edge float noise of
        # 1e-12 mm would otherwise turn two collinear edges into a "crossing".
        if abs(v) <= 1e-6 * math.hypot(b[0] - a[0], b[1] - a[1]):
            return 0
        return 1 if v > 0 else -1

    # Ignore segments that share an endpoint — neighbours in a polygon always do.
    for a in (p1, p2):
        for b in (p3, p4):
            if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                return False
    # Properly: each segment's ends on strictly opposite sides of the other. A
    # point on the other line (orient 0) only touches it — a nearly straight
    # arc flattened into tiny collinear segments touches itself everywhere.
    return (orient(p3, p4, p1) * orient(p3, p4, p2) < 0
            and orient(p1, p2, p3) * orient(p1, p2, p4) < 0)


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
