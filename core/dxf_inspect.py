"""
core/dxf_inspect.py
===================
Sparrow — DXF inspection tool (Phase 6).

The first Sparrow application only *reads* a DXF file and reports what it finds.
It does not nest, price, or draw anything yet — it is a diagnostic pass whose job
is to prove that we understand the file before we build anything on top of it.

For one uploaded DXF it reports:

  * Units (from $INSUNITS) and the millimetres-per-unit scale.
  * Entity types and how many of each are present.
  * Layers and how much geometry sits on each.
  * Closed contours (part outlines, holes) and open contours (construction lines).
  * The bounding box, and the width / height in millimetres.
  * Possible holes (closed contours nested inside another closed contour).
  * Invalid / self-intersecting geometry.

Only the five most common entity types carry geometry for now:

    LWPOLYLINE, POLYLINE, LINE, ARC, CIRCLE

Everything else is still *counted* (so the report is honest about what the file
contains) but is not turned into a contour. Support for more entity types is
added deliberately, one at a time, once a real file needs it.

Design notes
------------
* Every entity is converted to a flat polyline with ``ezdxf.path`` — this handles
  bulges in polylines and the arc/circle curves uniformly, and tells us whether
  the resulting path is closed.
* A part outline is often several LINE / ARC entities that only *together* form a
  closed loop, so open segments are chained end-to-end before a contour is judged
  open or closed. A chain that returns to its start is a closed contour; a chain
  that dangles is an open contour (a construction line is the classic example and
  must never be counted as a part).
* Holes are found with the even–odd rule: a closed contour contained by an odd
  number of other closed contours is a hole; an even number (zero included) is a
  part. This keeps islands-inside-holes correct without special cases.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import ezdxf
from ezdxf import path, recover

# Curve flattening tolerance in *drawing units* (arcs/circles/bulges are
# approximated by segments no further than this from the true curve).
_FLATTENING_DISTANCE = 0.2

# Endpoint-join tolerance in millimetres. Two segment ends closer than this are
# treated as the same vertex when chaining open segments into a loop, and a chain
# whose ends meet within this distance is considered closed.
_JOIN_TOL_MM = 0.05

# Entity types that carry geometry in this first version. Anything else is
# counted but not turned into a contour.
SUPPORTED_TYPES = ("LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE")

# Layer-name fragments that mark a layer as *not a cut* — bend lines, tangent
# lines, centre marks, dimensions, text, frames. Their geometry is still counted
# in the report, but it is kept out of the part/hole contours so a bend or
# tangent loop is never subtracted from the part area. Matched case-insensitively
# as substrings (so "IV_BEND_DOWN", "IV_TANGENT", "IV_ARC_CENTERS" are all
# excluded, while "IV_OUTER_PROFILE" / "IV_INTERIOR_PROFILES" / "CONTOURS" stay).
_CONSTRUCTION_LAYER_KEYWORDS = (
    "bend", "tangent", "arc_center", "arccenter", "centers", "centermark",
    "center_mark", "centerline", "centreline", "construction", "reference",
    "dim", "dimension", "text", "note", "annot", "format", "frame", "border",
    "title", "hatch", "symbol", "axis", "axes", "sketch", "weld", "mark",
    "label", "leader", "hidden",
)


def _is_construction_layer(name: str) -> bool:
    """True for bend / tangent / centre-mark / annotation layers (never cuts)."""
    low = (name or "").lower()
    return any(kw in low for kw in _CONSTRUCTION_LAYER_KEYWORDS)

# Self-intersection is an O(n²) check; skip (and warn) above this many edges so a
# densely flattened contour can never stall the inspection.
_SELF_INTERSECT_EDGE_CAP = 3000

# DXF $INSUNITS header code -> millimetres-per-unit.
# https://ezdxf.readthedocs.io/en/stable/concepts/units.html
_UNITS_TO_MM: dict[int, float] = {
    1: 25.4,     # inches
    2: 304.8,    # feet
    4: 1.0,      # millimetres
    5: 10.0,     # centimetres
    6: 1000.0,   # metres
    8: 0.0000254,  # microinches
    9: 0.0254,   # mils
    10: 914.4,   # yards
    13: 1e-6,    # nanometres
    14: 100.0,   # decimetres
}

_UNIT_LABELS: dict[int, str] = {
    0: "unitless",
    1: "inch",
    2: "feet",
    4: "mm",
    5: "cm",
    6: "m",
    8: "µin",
    9: "mil",
    10: "yard",
    13: "nm",
    14: "dm",
}


Point = tuple[float, float]


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Contour:
    """One closed loop or open chain, measured in millimetres."""

    points: list[Point]
    closed: bool
    source: str                 # "entity" or "chained"
    entity_types: list[str]     # entity type(s) the contour came from
    layers: list[str]           # layer(s) the contour's geometry sits on
    area_mm2: float = 0.0
    self_intersects: bool = False
    is_hole: bool = False
    depth: int = 0              # how many closed contours contain this one

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def width_mm(self) -> float:
        x0, _, x1, _ = self.bbox
        return x1 - x0

    @property
    def height_mm(self) -> float:
        _, y0, _, y1 = self.bbox
        return y1 - y0


@dataclass
class InspectionReport:
    """Everything the inspector found in one DXF file."""

    name: str
    unit_code: int
    unit_label: str
    mm_per_unit: float
    unit_assumed: bool                       # True when $INSUNITS was missing
    entity_counts: dict[str, int] = field(default_factory=dict)
    layer_counts: dict[str, int] = field(default_factory=dict)
    contours: list[Contour] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    # ── Derived views ────────────────────────────────────────────────────────
    @property
    def closed_contours(self) -> list[Contour]:
        return [c for c in self.contours if c.closed]

    @property
    def open_contours(self) -> list[Contour]:
        return [c for c in self.contours if not c.closed]

    @property
    def parts(self) -> list[Contour]:
        """Closed contours that are not holes — the real cut parts."""
        return [c for c in self.closed_contours if not c.is_hole]

    @property
    def holes(self) -> list[Contour]:
        return [c for c in self.closed_contours if c.is_hole]

    @property
    def bbox_mm(self) -> tuple[float, float, float, float] | None:
        """Bounding box over the parts (holes and construction lines excluded)."""
        boxes = [c.bbox for c in self.parts]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    @property
    def width_mm(self) -> float:
        box = self.bbox_mm
        return 0.0 if box is None else box[2] - box[0]

    @property
    def height_mm(self) -> float:
        box = self.bbox_mm
        return 0.0 if box is None else box[3] - box[1]

    @property
    def unsupported_types(self) -> dict[str, int]:
        """Entity types present in the file that this version does not yet read."""
        return {
            t: n for t, n in self.entity_counts.items()
            if t not in SUPPORTED_TYPES
        }


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _mm_per_unit(unit_code: int) -> float:
    return _UNITS_TO_MM.get(unit_code, 1.0)


def _shoelace_area(points: list[Point]) -> float:
    """Absolute polygon area (mm²) via the shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _point_in_polygon(pt: Point, poly: list[Point]) -> bool:
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


def _representative_point(poly: list[Point]) -> Point:
    """A point that is inside the polygon (centroid, nudged if it lands outside)."""
    n = len(poly)
    cx = sum(x for x, _ in poly) / n
    cy = sum(y for _, y in poly) / n
    if _point_in_polygon((cx, cy), poly):
        return cx, cy
    # Concave shape: scan a horizontal line through the centroid for an inside x.
    xs = sorted({x for x, _ in poly})
    for a, b in zip(xs, xs[1:]):
        mid = (a + b) / 2
        if _point_in_polygon((mid, cy), poly):
            return mid, cy
    return cx, cy


def _segments_cross(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """True if open segment p1p2 properly crosses p3p4 (shared endpoints ignored)."""
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

    d1 = orient(p3, p4, p1)
    d2 = orient(p3, p4, p2)
    d3 = orient(p1, p2, p3)
    d4 = orient(p1, p2, p4)
    return d1 != d2 and d3 != d4


def _self_intersects(points: list[Point]) -> bool:
    """Does the closed polygon cross itself? Non-adjacent edges only."""
    n = len(points)
    if n < 4:
        return False
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    m = len(edges)
    if m > _SELF_INTERSECT_EDGE_CAP:
        return False  # caller warns; too big to check exhaustively
    for i in range(m):
        a1, a2 = edges[i]
        # start at i+2 to skip the adjacent edge; the shared-endpoint guard in
        # _segments_cross handles the wrap-around neighbour (edge m-1 vs edge 0).
        for j in range(i + 2, m):
            if i == 0 and j == m - 1:
                continue
            b1, b2 = edges[j]
            if _segments_cross(a1, a2, b1, b2):
                return True
    return False


# ── Extraction ───────────────────────────────────────────────────────────────

def _entity_polyline(entity, factor: float) -> tuple[list[Point], bool] | None:
    """Flatten one supported entity to a mm polyline + a closed flag.

    Returns None when the entity has no usable geometry.
    """
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


def _chain_open_segments(
    segments: list[tuple[list[Point], str, str]], tol: float
) -> list[Contour]:
    """Chain open segments end-to-end into closed loops / open chains.

    Each input is (points, entity_type, layer). Endpoints within `tol` mm are
    treated as the same vertex. A resulting chain whose two ends meet is a closed
    contour; otherwise it stays open.
    """
    from collections import defaultdict

    def key(pt: Point) -> tuple[int, int]:
        return (round(pt[0] / tol), round(pt[1] / tol))

    adj: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (pts, _t, _l) in enumerate(segments):
        adj[key(pts[0])].append(i)
        adj[key(pts[-1])].append(i)

    used = [False] * len(segments)
    contours: list[Contour] = []

    def next_unused(node: tuple[int, int]) -> int | None:
        for j in adj.get(node, ()):
            if not used[j]:
                return j
        return None

    for start in range(len(segments)):
        if used[start]:
            continue
        pts0, t0, l0 = segments[start]
        used[start] = True
        chain = list(pts0)
        types = [t0]
        layers = [l0]

        # extend forward from the tail
        while True:
            j = next_unused(key(chain[-1]))
            if j is None:
                break
            used[j] = True
            seg, t, l = segments[j]
            seg = seg if key(seg[0]) == key(chain[-1]) else seg[::-1]
            chain.extend(seg[1:])
            types.append(t)
            layers.append(l)

        # extend backward from the head
        while True:
            j = next_unused(key(chain[0]))
            if j is None:
                break
            used[j] = True
            seg, t, l = segments[j]
            seg = seg if key(seg[-1]) == key(chain[0]) else seg[::-1]
            chain = seg[:-1] + chain
            types.append(t)
            layers.append(l)

        closed = len(chain) >= 4 and key(chain[0]) == key(chain[-1])
        contours.append(Contour(
            points=chain,
            closed=closed,
            source="chained" if len(types) > 1 else "entity",
            entity_types=sorted(set(types)),
            layers=sorted(set(layers)),
        ))
    return contours


def _mark_holes(contours: list[Contour]) -> None:
    """Set depth / is_hole on closed contours.

    ``depth`` counts how many larger closed contours contain this one. A part is a
    top-level outline (``depth == 0``); anything nested inside a part is a hole,
    whatever its own depth. We deliberately do *not* use the even–odd rule: a real
    sheet-metal part keeps all its interior features (small holes, a big central
    hole, slots) as holes, rather than re-promoting deeply nested loops — often
    just construction/tangent geometry — back into separate parts.
    """
    closed = [c for c in contours if c.closed and len(c.points) >= 3]
    reps = {id(c): _representative_point(c.points) for c in closed}
    for c in closed:
        depth = 0
        for other in closed:
            if other is c:
                continue
            if other.area_mm2 <= c.area_mm2:
                continue
            if _point_in_polygon(reps[id(c)], other.points):
                depth += 1
        c.depth = depth
        c.is_hole = depth >= 1


# ── Public entry point ───────────────────────────────────────────────────────

def _read_doc(data: bytes) -> tuple[object, list[str]]:
    """Open DXF bytes tolerantly (ASCII or binary), repairing what it can."""
    doc, auditor = recover.read(io.BytesIO(data))
    warnings: list[str] = []
    if auditor.has_errors:
        warnings.append(
            f"DXF had {len(auditor.errors)} structural error(s), repaired automatically."
        )
    return doc, warnings


def inspect_dxf(data: bytes, name: str = "drawing.dxf") -> InspectionReport:
    """Read one DXF file's bytes and report what it contains.

    Never raises for bad content: a read failure comes back as a report carrying a
    warning, so a UI on top of this stays alive on any upload.
    """
    try:
        doc, warnings = _read_doc(data)
    except (ezdxf.DXFError, IOError, ValueError, IndexError) as exc:
        return _failed_report(name, f"Could not read DXF: {exc}")
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash the app
        return _failed_report(name, f"Unexpected error reading DXF: {exc}")

    msp = doc.modelspace()

    unit_code = int(getattr(doc, "units", 0) or 0)
    unit_assumed = unit_code == 0
    factor = _mm_per_unit(unit_code)
    if unit_assumed:
        warnings.append(
            "No unit information ($INSUNITS) in the drawing — sizes are assumed "
            "to be millimetres. Check the dimensions below."
        )

    report = InspectionReport(
        name=name,
        unit_code=unit_code,
        unit_label=_UNIT_LABELS.get(unit_code, f"code {unit_code}"),
        mm_per_unit=factor,
        unit_assumed=unit_assumed,
        warnings=warnings,
    )

    # 1) Count every entity by type and by layer; collect supported geometry.
    closed_direct: list[Contour] = []
    open_segments: list[tuple[list[Point], str, str]] = []

    for entity in msp:
        etype = entity.dxftype()
        report.entity_counts[etype] = report.entity_counts.get(etype, 0) + 1
        layer = getattr(entity.dxf, "layer", "0") or "0"
        report.layer_counts[layer] = report.layer_counts.get(layer, 0) + 1

        if etype not in SUPPORTED_TYPES:
            continue
        # Bend / tangent / centre-mark / annotation geometry is counted above but
        # must not become a part or hole contour.
        if _is_construction_layer(layer):
            continue

        extracted = _entity_polyline(entity, factor)
        if extracted is None:
            continue
        pts, closed = extracted
        if closed:
            closed_direct.append(Contour(
                points=pts, closed=True, source="entity",
                entity_types=[etype], layers=[layer],
            ))
        else:
            open_segments.append((pts, etype, layer))

    # 2) Chain the open segments into loops / leftover open chains.
    chained = _chain_open_segments(open_segments, _JOIN_TOL_MM) if open_segments else []

    report.contours = closed_direct + chained

    # 3) Measure area, self-intersection, and hole nesting.
    for c in report.contours:
        c.area_mm2 = _shoelace_area(c.points)
        if c.closed:
            if len(c.points) > _SELF_INTERSECT_EDGE_CAP:
                report.warnings.append(
                    f"A contour has {len(c.points)} points — skipped the "
                    "self-intersection check (too dense)."
                )
            else:
                c.self_intersects = _self_intersects(c.points)
    _mark_holes(report.contours)

    # 4) Surface findings.
    bad = [c for c in report.closed_contours if c.self_intersects]
    if bad:
        report.invalid.append(
            f"{len(bad)} closed contour(s) are self-intersecting."
        )
    tiny = [c for c in report.parts if c.area_mm2 < 1e-6]
    if tiny:
        report.invalid.append(
            f"{len(tiny)} closed contour(s) have (near-)zero area — degenerate geometry."
        )
    if report.unsupported_types:
        listed = ", ".join(f"{t}×{n}" for t, n in sorted(report.unsupported_types.items()))
        report.warnings.append(
            f"Entity types not read in this version (counted only): {listed}."
        )
    if not report.parts:
        report.warnings.append(
            "No closed part contour found. If the drawing has only open lines, "
            "they are construction lines, not parts."
        )

    return report


def _failed_report(name: str, message: str) -> InspectionReport:
    return InspectionReport(
        name=name,
        unit_code=0,
        unit_label=_UNIT_LABELS[0],
        mm_per_unit=1.0,
        unit_assumed=True,
        warnings=[message],
    )


# ── Plain-text summary (handy for the CLI / debugging) ───────────────────────

def format_report(report: InspectionReport) -> str:
    """Render a report as readable plain text."""
    lines: list[str] = []
    lines.append(f"DXF inspection — {report.name}")
    lines.append("=" * (17 + len(report.name)))
    unit = f"{report.unit_label} ({report.mm_per_unit:g} mm/unit)"
    if report.unit_assumed:
        unit += "  [assumed — no $INSUNITS]"
    lines.append(f"Units:            {unit}")

    box = report.bbox_mm
    if box is not None:
        lines.append(
            f"Bounding box:     x {box[0]:.3f}..{box[2]:.3f} mm, "
            f"y {box[1]:.3f}..{box[3]:.3f} mm"
        )
        lines.append(f"Size (W × H):     {report.width_mm:.3f} × {report.height_mm:.3f} mm")
    else:
        lines.append("Bounding box:     — (no parts found)")

    lines.append(f"Parts (closed):   {len(report.parts)}")
    lines.append(f"Holes:            {len(report.holes)}")
    lines.append(f"Open contours:    {len(report.open_contours)} (construction lines etc.)")

    lines.append("")
    lines.append("Entity types:")
    for t, n in sorted(report.entity_counts.items()):
        mark = "" if t in SUPPORTED_TYPES else "   (not read yet)"
        lines.append(f"  {t:<14} {n}{mark}")

    lines.append("")
    lines.append("Layers:")
    for layer, n in sorted(report.layer_counts.items()):
        lines.append(f"  {layer:<20} {n} entit{'y' if n == 1 else 'ies'}")

    if report.parts:
        lines.append("")
        lines.append("Parts detail:")
        for i, c in enumerate(report.parts, 1):
            n_holes = sum(
                1 for h in report.holes
                if h.depth == c.depth + 1
                and _point_in_polygon(_representative_point(h.points), c.points)
            )
            lines.append(
                f"  #{i}: {c.width_mm:.3f} × {c.height_mm:.3f} mm, "
                f"area {c.area_mm2:.3f} mm², "
                f"{n_holes} hole(s) "
                f"[{'+'.join(c.entity_types)}]"
            )

    if report.invalid:
        lines.append("")
        lines.append("Invalid geometry:")
        for msg in report.invalid:
            lines.append(f"  ! {msg}")

    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for msg in report.warnings:
            lines.append(f"  - {msg}")

    return "\n".join(lines)
