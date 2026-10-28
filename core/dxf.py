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
    "SOLID", "TRACE",   # filled 2D shapes: a dimension's exploded arrowheads
    "IMAGE",            # a raster picture (a scanned view): nothing to cut
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

# A bend note on a drawing's flat pattern view, e.g. "UP 90° R 1.5" or
# "YLÖS 90°": it marks that view as the flat pattern among the sheet's views.
_BEND_NOTE_RE = re.compile(r"^\s*(?:up|down|yl[oö]s|alas)\b.*\d", re.IGNORECASE)
# A drawing's scale in its title block, e.g. "2:1" or "1:2".
_SCALE_TEXT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)\s*$")

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
    # A profile drawing's blank as the customer drew it, when its width was
    # replaced by the cross-section unfolded (0: not replaced).
    drawn_blank_mm: float = 0.0

    @property
    def dropped_count(self) -> int:
        return len(self.dropped) + self.dropped_more

    @property
    def net_area_mm2(self) -> float:
        """The part's real area: the outline less its holes."""
        if self.outline is None:
            return 0.0
        return self.outline.area_mm2 - sum(h.area_mm2 for h in self.holes)

    @property
    def cut_length_mm(self) -> float:
        """What the laser cuts: the outline, every hole and any open cut. A
        hole that is only a cut line (``is_sliver``) is cut once, not round."""
        if self.outline is None:
            return 0.0
        return (sum(length(c.points, closed=True) / (2 if is_sliver(c.points) else 1)
                    for c in (self.outline, *self.holes))
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
    # Where bend notes ("UP 90° R 1.5") are written, and the drawing's scale
    # (drawn size / real size, from a "2:1" in the title block): a drawing
    # sheet's flat pattern view is found by them (see ``_flat_view``).
    bend_notes: list[Point] = field(default_factory=list)
    scale: float = 1.0
    # Each linear dimension's measured points and its shown value / drawn
    # length: a view drawn at 1:10 on a 1:1 sheet has its dimensions at 10.
    dimensions: list[tuple[Point, Point, float, float]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)          # file-level
    unreadable: dict[str, dict[str, int]] = field(default_factory=dict)  # layer -> type -> n
    # The part from the default layers, once built (see ``part``).
    _default: DxfReport | None = field(default=None, repr=False, compare=False)

    def forget_lines(self) -> None:
        """Build the part from the default layers, then drop the drawing's
        lines: only the part is used after that (a big RFQ's drawings are
        millions of points). ``part`` with other layers can't be built after."""
        self.part()
        self.pieces = []

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
        """Build the main part from ``layers`` (default: ``suggested_layers``,
        built once and kept)."""
        if layers is None:
            if self._default is None:
                self._default = self._part(None)
            return self._default
        return self._part(layers)

    def _part(self, layers: set[str] | None) -> DxfReport:
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
        cut = _distinct([p for p in self.pieces if p.layer in chosen and p.solid])
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
        # On a drawing sheet (the largest outline is its frame), the part is
        # the view with the bend note: its flat pattern, drawn at the sheet's
        # scale.
        main = max(top, key=lambda c: c.width_mm * c.height_mm)
        view = _flat_view(main, contours, self.bend_notes)
        # Or a profile's drawing: its cross-section and its blank (the one
        # other view), the blank often drawn at a smaller scale.
        section = _profile_section(main, contours) if view is None else None
        if section is not None:
            view = _blank_view(main, contours, section)
            if view is None:
                # The blank drawn broken (a long strip shortened with break
                # lines) or not at all: it is the dimensioned length by the
                # unfolded width.
                blank = _blank_from_dimensions(section, self.dimensions)
                if blank is not None:
                    return _profile_report(report, blank, section, contours)
                section = None
        # Views drawn at the sheet's scale: a frame the size of a paper sheet
        # (a frame drawn at 1:1 round real-size views is the paper / scale).
        at_scale = view is not None and section is None and _paper_size(main)
        if view is not None:
            main = view
        report.outline = main
        notes = [n for n in self.bend_notes if point_in_polygon(n, main.points)]
        # A note on a bend at the part's edge is often written just outside it.
        others = [c.points for c in contours if c.closed and c is not main
                  and c.depth == main.depth]
        near = notes + [n for n in self.bend_notes if n not in notes
                        and _near_ring(n, main.points, _NOTE_NEAR_MM)
                        and not any(point_in_polygon(n, o) for o in others)]

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
        # A hole with a bend note written in it (a cut-out along the bend):
        # the bend line runs on from it, see below.
        boxes = [c for c in inner if c.closed and any(point_in_polygon(n, c.points)
                                                      for n in notes)]
        report.holes = [c for c in inner if c.closed and c.depth == main.depth + 1]
        report.reference_lines = _lines_inside(main, [p.points for p in reference])
        report.bends = len(_lines_inside(main, [p.points for p in reference
                                                if _is_bend_layer(p.layer)])) or len(near) \
            or _edge_to_edge(main, [p.points for p in _distinct(reference)
                                    if not p.solid and len(p.points) == 2])
        rings = [c for c in inner if c.closed]
        hole_rings = [c.points for c in report.holes]
        for c in inner:
            if not c.closed and _is_thread_mark(c.points, rings):
                report.threads += 1
                report.reference_lines.append(c.points)
            elif not c.closed and _is_drawing_line(c.points, main.points, hole_rings):
                report.reference_lines.append(c.points)
            elif c.closed and c.depth >= main.depth + 2:
                report.reference_lines.append(c.points)
            elif not c.closed:
                report.open_lines.append(c.points)

        # The bend line, where it runs on from the note's cut-out: drawn, not cut.
        on_box = [ln for ln in report.open_lines
                  if any(_on_ring(end, b.points) for b in boxes for end in (ln[0], ln[-1]))]
        report.open_lines = [ln for ln in report.open_lines if not any(ln is o for o in on_box)]
        report.reference_lines += on_box
        # The part's own dimensions tell its scale best: a view's scale can
        # differ from the title block's (M58581: drawn 7:5, sheet "1:7").
        k = _dimension_scale(main, self.dimensions, at_least=2) if section is None else 1.0
        if k != 1.0:
            _to_real_size(report, k)
        elif at_scale and self.scale != 1.0:
            _to_real_size(report, 1.0 / self.scale)
        if section is not None:
            k = _dimension_scale(main, self.dimensions)
            if k != 1.0:
                _to_real_size(report, k)
            report.bends = report.bends or _section_bends(section.points)
            _blank_from_section(report, section)
        report.problems += _part_problems(report)
        _trim_dropped(report)
        _lay_along_length(report)
        return report


def _flat_view(frame: Contour, contours: list[Contour], notes: list[Point]) -> Contour | None:
    """On a drawing sheet, the view of the flat pattern: the one outline
    straight inside the largest (the frame) with a bend note written in it.
    None when no outline, or more than one, has."""
    views = [c for c in contours if c.closed and c.depth == frame.depth + 1
             and any(point_in_polygon(n, c.points) for n in notes)]
    return views[0] if len(views) == 1 else None


_SECTION_MAX_T_MM = 6.0     # thicker than this is not a sheet-metal section


def _strip_thickness(c: Contour) -> float:
    """The width t of a closed strip of even width (a sheet-metal profile's
    cross-section): its sides run the strip's length L twice and its ends t
    twice, so perimeter = 2L + 2t and area = L·t."""
    n = len(c.points)
    half = sum(math.dist(c.points[i], c.points[(i + 1) % n]) for i in range(n)) / 2
    disc = half * half - 4 * abs(c.area_mm2)
    return (half - math.sqrt(disc)) / 2 if half and disc >= 0 else 0.0


def _is_section(c: Contour) -> bool:
    """A profile's cross-section: a strip of sheet thickness, bent, so it
    spans far more than its thickness both ways (a slot doesn't)."""
    t = _strip_thickness(c)
    return 0 < t <= _SECTION_MAX_T_MM and min(c.width_mm, c.height_mm) >= 10 * t


def _profile_section(frame: Contour, contours: list[Contour]) -> Contour | None:
    """On a drawing sheet of a bent profile, the profile's cross-section: the
    one view straight inside the frame drawn as a strip of sheet thickness."""
    found = [c for c in contours if c.closed and c.depth == frame.depth + 1 and _is_section(c)]
    return found[0] if len(found) == 1 else None


def _blank_view(frame: Contour, contours: list[Contour], section: Contour) -> Contour | None:
    """The profile's blank: the one other view straight inside the frame."""
    views = [c for c in contours if c.closed and c.depth == frame.depth + 1 and c is not section]
    return views[0] if len(views) == 1 else None


def _dimension_scale(view: Contour, dimensions: list[tuple[Point, Point, float, float]],
                     at_least: int = 1) -> float:
    """Real size / drawn size of a view, from the dimensions measured on its
    edges (2500 shown on a 250 line: 10); 1 when they don't agree, or fewer
    than ``at_least`` are measured on it."""
    found = [k for a, b, k, _value in dimensions
             if _near_ring(a, view.points, _JOIN_TOL_MM) and _near_ring(b, view.points, _JOIN_TOL_MM)]
    if len(found) < max(at_least, 1) or max(found) > min(found) * 1.01:
        return 1.0
    k = sum(found) / len(found)
    # Dimensions are shown rounded (479 for 47.86 at 1:10): the scale is a
    # round one, 10 or 1/2, when one is that close.
    for nice in (round(k), 1 / max(round(1 / k), 1)):
        if nice and abs(k / nice - 1) < 0.01:
            k = nice
            break
    return 1.0 if abs(k - 1.0) < 0.01 else k


def _blank_from_dimensions(section: Contour,
                           dimensions: list[tuple[Point, Point, float, float]]) -> Contour | None:
    """A profile's blank when it isn't drawn as one outline: as long as the
    longest dimension outside the cross-section, as wide as the section
    unfolds. None without such a dimension."""
    from core.step import DEFAULT_K_FACTOR

    x0, y0, x1, y1 = bbox(section.points)
    tol = _REFERENCE_BBOX_TOL_MM

    def on_section(p: Point) -> bool:
        return x0 - tol <= p[0] <= x1 + tol and y0 - tol <= p[1] <= y1 + tol

    width = _unfolded_width(section, DEFAULT_K_FACTOR)
    lengths = [v for a, b, _k, v in dimensions if not (on_section(a) or on_section(b))]
    if not lengths or width <= 0 or max(lengths) <= width:
        return None
    long = max(lengths)
    ring = [(0.0, 0.0), (long, 0.0), (long, width), (0.0, width)]
    return Contour(ring, True, area_mm2=long * width)


def _profile_report(report: DxfReport, blank: Contour, section: Contour,
                    contours: list[Contour]) -> DxfReport:
    """The part as a profile's plain blank; the drawing's lines are left out
    (they are at other scales, and the blank is drawn broken)."""
    report.outline = blank
    report.bends = _section_bends(section.points)
    report.problems += _part_problems(report)
    return report


def _section_bends(ring: list[Point]) -> int:
    """The bends of a profile from its cross-section: each bend turns its
    outline twice (outside and inside), each end turns it by 180°."""
    return len(_bend_turns(ring)) // 2


def _unfolded_width(section: Contour, k: float) -> float:
    """The width of a profile's blank, unfolded from its cross-section: the
    middle of the sheet (area / thickness) less what each bend gains there
    over the neutral axis, at r + K·t."""
    t = _strip_thickness(section)
    angle = sum(abs(a) for a in _bend_turns(section.points)) / 2   # each bend twice
    return abs(section.area_mm2) / t - math.radians(angle) * (0.5 - k) * t


_BLANK_DIFFERS = 0.005    # a drawn blank this much off the unfolded one is replaced


def _blank_from_section(report: DxfReport, section: Contour) -> None:
    """Give a rectangular blank the width unfolded from the cross-section:
    customers' blanks can be left from an older profile (557 drawn for 532.8)."""
    from core.step import DEFAULT_K_FACTOR

    blank = report.outline
    x0, y0, x1, y1 = bbox(blank.points)
    w, h = x1 - x0, y1 - y0
    if report.holes or abs(blank.area_mm2) < 0.99 * w * h:
        return    # not a plain rectangle: leave it as drawn
    width = _unfolded_width(section, DEFAULT_K_FACTOR)
    drawn = min(w, h)
    if width <= 0 or abs(width / drawn - 1) < _BLANK_DIFFERS:
        return
    if w >= h:
        y1 = y0 + width
    else:
        x1 = x0 + width
    blank.points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    blank.area_mm2 = (x1 - x0) * (y1 - y0)
    report.reference_lines = []    # the drawn blank's bend lines no longer fit
    report.drawn_blank_mm = drawn


def _bend_turns(ring: list[Point]) -> list[float]:
    """The turns of a cross-section's outline at its bends, in degrees."""
    ring = [q for i, q in enumerate(ring) if math.dist(q, ring[i - 1]) > 1e-6]
    t = _strip_thickness(Contour(ring, True, area_mm2=area(ring)))
    n = len(ring)
    turns = []   # (turn in degrees, length of the side after it)
    for i in range(n):
        a, b, c = ring[i - 1], ring[i], ring[(i + 1) % n]
        turn = math.degrees(math.atan2((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]),
                                       (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])))
        if abs(turn) > 1.0:
            turns.append((turn, math.dist(b, c)))
    # A corner: turns the same way along sides shorter than a few thicknesses
    # (an arc flattened to segments, a square end).
    corners: list[float] = []
    for i, (turn, _side) in enumerate(turns):
        if corners and turns[i - 1][1] < 5 * t and (turn > 0) == (corners[-1] > 0):
            corners[-1] += turn
        else:
            corners.append(turn)
    if len(corners) > 1 and turns[-1][1] < 5 * t and (corners[0] > 0) == (corners[-1] > 0):
        corners[0] += corners.pop()
    return [c for c in corners if 10.0 < abs(c) < 150.0]


def _distinct(pieces: list[_Piece]) -> list[_Piece]:
    """The pieces less any drawn twice (the same points either way round):
    a SolidWorks DWG can carry every line on two layers (SLD-0 and SLD-0_0),
    and a doubled line chains into a back-and-forth gap, not the cut-out."""
    seen, out = set(), []
    for p in pieces:
        key = tuple((round(x, 3), round(y, 3)) for x, y in p.points)
        if key in seen or key[::-1] in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _is_drawing_line(line: list[Point], outline: list[Point], holes: list[list[Point]]) -> bool:
    """An open line inside the part that can't be a cut: one into or across
    a hole (a centre mark), one from hole to hole (both ends at a hole), or a
    straight line from edge to edge (it would cut the part in two: a bend
    line drawn solid)."""
    if any(point_in_polygon(p, h) for p in line for h in holes):
        return True
    ends = (line[0], line[-1])
    if all(any(_near_ring(end, h, _AT_HOLE_MM) for h in holes) for end in ends):
        return True
    return is_slit(line) and all(_on_ring(end, outline) for end in ends)


_AT_HOLE_MM = 5.0   # a line ending this close to a hole ends at the hole
_NOTE_NEAR_MM = 25.0  # a bend note this close outside the part is its note


def _near_ring(p: Point, ring: list[Point], tol: float) -> bool:
    x0, y0, x1, y1 = bbox(ring)
    if not (x0 - tol <= p[0] <= x1 + tol and y0 - tol <= p[1] <= y1 + tol):
        return False
    return _on_ring(p, ring, tol)


def _paper_size(frame: Contour) -> bool:
    """A frame the size of an ISO paper sheet, or a little inside one."""
    w, h = sorted((frame.width_mm, frame.height_mm))
    return any(0.85 * b <= w <= b * 1.01 and 0.85 * a <= h <= a * 1.01
               for a, b in _ISO_SHEETS.values())


def _on_ring(p: Point, ring: list[Point], tol: float = _JOIN_TOL_MM) -> bool:
    n = len(ring)
    return any(_point_segment_mm(p, ring[i], ring[(i + 1) % n]) <= tol for i in range(n))


def _drawing_scale(texts: list[str]) -> float:
    """The drawing's scale (drawn / real) from its title block's "2:1", when
    the drawing gives one scale; else 1."""
    found = set()
    for text in texts:
        m = _SCALE_TEXT_RE.match(text)
        if m:
            a, b = (float(g.replace(",", ".")) for g in m.groups())
            if a > 0 and b > 0:
                found.add(a / b)
    return found.pop() if len(found) == 1 else 1.0


def _to_real_size(report: DxfReport, k: float) -> None:
    """Scale the part's geometry by ``k`` (a view drawn at 2:1 by 1/2)."""
    def scaled(points):
        return [(x * k, y * k) for x, y in points]

    for c in (report.outline, *report.holes):
        c.points = scaled(c.points)
        c.area_mm2 *= k * k
    for lines in (report.reference_lines, report.open_lines, report.dropped):
        lines[:] = [scaled(line) for line in lines]


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
    written = [(t, e) for t, e in ((_text_of(e), e) for e in entities
                                   if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB")) if t]
    f.texts = [t for t, _ in written]
    f.scale = _drawing_scale(f.texts)

    unit_code = int(getattr(doc, "units", 0) or 0)
    if unit_code not in _UNITS:
        unit_code = _unit_from_texts(f.texts)
    # Without a unit, read in drawing units; a standard sheet size below may
    # still show they are millimetres.
    factor, f.unit_label = _UNITS.get(unit_code, (1.0, "ei yksikköä"))

    for text, entity in written:
        at = getattr(entity.dxf, "insert", None)
        if at is not None and _BEND_NOTE_RE.match(text):
            f.bend_notes.append((at[0] * factor, at[1] * factor))
    f.dimensions = [d for d in (_dimension(e, factor) for e in entities
                                if e.dxftype() == "DIMENSION") if d]
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


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _dimension(entity, factor: float) -> tuple[Point, Point, float, float] | None:
    """A linear, aligned or ordinate dimension's measured points, its shown
    value / drawn length and its shown value."""
    try:
        if entity.dimtype in (0, 1):
            a, b = entity.dxf.defpoint2, entity.dxf.defpoint3
            drawn = entity.get_measurement() * factor
        elif entity.dimtype == 6:            # ordinate: from the origin, along x or y
            a, b = entity.dxf.defpoint, entity.dxf.defpoint2
            axis = 0 if entity.dxf.dimtype & 64 else 1
            drawn = abs(b[axis] - a[axis]) * factor
        else:
            return None
        block = entity.get_geometry_block()
        shown = [t for t in (_text_of(x) for x in block or ()
                             if x.dxftype() in ("TEXT", "MTEXT")) if t]
    except Exception:  # noqa: BLE001 — a broken dimension is only a drawing mark
        return None
    m = _NUMBER_RE.search(shown[0]) if len(shown) == 1 else None
    if not m or not drawn:
        return None
    value = float(m.group().replace(",", "."))
    if not value:
        return None
    return (a[0] * factor, a[1] * factor), (b[0] * factor, b[1] * factor), value / drawn, value


def _load(data: bytes):
    """The DXF document: read the usual way and checked (``audit``), which is
    several times faster than ezdxf's ``recover``; a file that can't be read
    so (broken structure, binary DXF) goes through ``recover``."""
    if not data.startswith(b"AutoCAD Binary DXF"):
        try:
            from ezdxf.lldxf.validator import dxf_info

            head = io.StringIO(data[:1 << 16].decode("cp1252", errors="ignore"), newline=None)
            info = dxf_info(head)
            # R2007 and later are always UTF-8, whatever the header's code page.
            encoding = "utf-8" if info.version >= "AC1021" else info.encoding
            text = data.decode(encoding, errors="surrogateescape")
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


_SLIVER_MM = 0.05      # a ring whose sides are this close is a cut line
_SLIVER_MAX_POINTS = 64


def is_sliver(points: list[Point]) -> bool:
    """A closed ring with next to no width: a cut line modelled as a hole,
    such as a V-shaped lance in a STEP model's flat (both sides of the cut
    0.003 mm apart), where the middle of every side lies on another side.
    It is cut as a line, and its sides may cross by round-off without the
    part being wrong."""
    n = len(points)
    if not 3 <= n <= _SLIVER_MAX_POINTS:
        return False
    segments = [(points[j], points[(j + 1) % n]) for j in range(n)]
    return all(any(_point_segment_mm(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), c, d)
                   <= _SLIVER_MM for j, (c, d) in enumerate(segments) if j != i)
               for i, (a, b) in enumerate(segments))


def _point_segment_mm(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = dx * dx + dy * dy
    s = 0.0 if span == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / span))
    return math.hypot(p[0] - a[0] - s * dx, p[1] - a[1] - s * dy)


def _part_problems(report: DxfReport) -> list[str]:
    """Why a built part cannot be priced — each reason in plain Finnish."""
    out = []
    rings = [report.outline, *report.holes]
    gaps = [line for line in report.open_lines if not is_slit(line)]
    if gaps:
        n = len(gaps)
        lines = "1 viiva osan sisällä ei" if n == 1 else f"{n} viivaa osan sisällä ei"
        out.append(f"{lines} sulkeudu ääriviivaksi — reiän ääriviivassa on aukko?")
    if any(len(c.points) <= _SELF_INTERSECT_EDGE_CAP and not is_sliver(c.points)
           and _self_intersects(c.points) for c in rings):
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


_EDGE_TOL_MM = 1.0   # a dashed line ending this close to the outline ends on it


def _edge_to_edge(main: Contour, lines: list[list[Point]]) -> int:
    """Dashed straight lines across the part, from edge to edge: its bend
    lines, when no bend layer or bend note says so (a hidden edge or centre
    line stops short of the edges)."""
    return sum(1 for a, b in lines
               if _near_ring(a, main.points, _EDGE_TOL_MM) and _near_ring(b, main.points, _EDGE_TOL_MM)
               and point_in_polygon(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), main.points)
               and math.dist(a, b) > 2 * _EDGE_TOL_MM)


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

# Blocks a CAD program draws as annotation, e.g. SolidWorks' centre marks
# (SW_CENTERMARKSYMBOL_3): a cross on each hole with lines joining the holes.
_ANNOTATION_BLOCK_RE = re.compile(r"CENT(?:ER|RE)_?MARK", re.IGNORECASE)


def _explode_blocks(entities):
    """Yield the entities with every block reference (INSERT) replaced by its
    content, recursively. Dimensions stay whole (they are annotation), and
    centre-mark blocks are left out."""
    for e in entities:
        if e.dxftype() == "INSERT":
            if _ANNOTATION_BLOCK_RE.search(e.dxf.name):
                continue
            yield from _explode_blocks(e.virtual_entities())
            yield from e.attribs
        else:
            yield e


_FASTENERS = re.compile(r"(?:MUTTER|RUUV|PULT|NIIT|HOLK|TAPP)\w*\D*?(\d+)\s*(?:KPL|PCS)",
                        re.IGNORECASE)


def fasteners_from_texts(texts: list[str]) -> int:
    """Fasteners the drawing's notes ask for (nuts, screws, bolts, rivets,
    bushes, studs), e.g. "M5-PURISTEMUTTERIT X4KPL" and "M4-PURISTEMUTTERIT
    5KPL" make 9."""
    return sum(n for _, n in fastener_lines(texts))


def fastener_lines(texts: list[str]) -> list[tuple[str, int]]:
    """The drawing's fastener notes one by one, ``(name, kpl)``: e.g.
    "M5-PURISTEMUTTERIT X4KPL" is ("M5-PURISTEMUTTERIT", 4)."""
    out = []
    for t in texts:
        for m in _FASTENERS.finditer(t):
            start = max(t.rfind(c, 0, m.start()) for c in " \t\n,;(") + 1
            name = re.sub(r"[\s:=,(-]*X?$", "", t[start:m.start(1)].strip(), flags=re.IGNORECASE)
            out.append((name.upper(), int(m.group(1))))
    return out


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
    return ltype not in _SOLID_LINETYPES and _dash_count(doc, ltype) != 0


def _dash_count(doc, name: str) -> int | None:
    """The number of dashes in a linetype's pattern: 0 for a solid line under
    another name (e.g. Finnish "Yhtenäinen"), None for an unknown linetype."""
    try:
        tags = doc.linetypes.get(name).pattern_tags.tags
    except Exception:  # noqa: BLE001 — missing or broken table entry
        return None
    return next((t.value for t in tags if t.code == 73), None)


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
