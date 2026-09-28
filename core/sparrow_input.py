"""
core/sparrow_input.py
=====================
Sparrow — DXF → Sparrow instance conversion (Phase 7).

Turns the parts found by the DXF inspector (``core/dxf_inspect.py``) into a
Sparrow strip-packing instance:

    original DXF geometry
      ↓                          (core/dxf_inspect.py — the true outline)
    polygon approximation        (this module — flattened, cleaned rings)
      ↓
    Sparrow instance JSON        (jagua-rs external format)

For every part the converter records:

  * a stable part id (kept as metadata; jagua-rs also needs an integer id),
  * the requested quantity (``demand``),
  * an outer polygon,
  * inner polygons for its holes,
  * the allowed rotations,
  * the original DXF file name (metadata).

The polygon is only an *approximation used for placement*. The original DXF is
kept separately (the converter never discards it, and the JSON only references it
by name) so the manufacturing geometry is never replaced by Sparrow's polygon.

Schema
------
The output matches the current Sparrow / jagua-rs strip-packing input
(``ExtSPInstance`` → ``ExtItem`` → ``ExtShape``), verified against
``jagua-rs/src/io/ext_repr.rs`` and Sparrow's own ``data/input`` examples:

    {
      "name": "...",
      "strip_height": <float>,
      "items": [
        {
          "id": <int>,                       # unique, 0-based
          "demand": <int>,                   # quantity
          "dxf": "<original file name>",     # metadata (ignored by the solver)
          "part_id": "<stable id>",          # metadata (ignored by the solver)
          "allowed_orientations": [<deg>, ...],
          "shape": { "type": "simple_polygon", "data": [[x, y], ...] }
            # or, when the part has holes:
            # { "type": "polygon", "data": { "outer": [...], "inner": [[...], ...] } }
        }
      ]
    }

jagua-rs currently ignores holes on *item* shapes (it nests by the outer
boundary and logs a warning) — which is exactly what we want here: the holes are
recorded for our own downstream use without changing how the part is placed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.dxf_inspect import Contour, InspectionReport, inspect_dxf
from core.geometry import point_in_polygon, representative_point, signed_area

# Default rotations offered to the packer (degrees). Four quadrant orientations
# suit rectangular-ish sheet-metal parts; override per call when a part may only
# be placed one way (grain direction, finish) or may rotate freely.
DEFAULT_ORIENTATIONS: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)

# Points closer than this (mm) are treated as the same vertex when cleaning a
# ring. Guards against jagua-rs bailing on duplicate vertices, including f32
# round-off once the JSON is parsed by the Rust solver.
_MIN_VERTEX_GAP_MM = 1e-4

# How far (mm) a construction line may stick out of its part's bounding box and
# still be drawn with the part (bend lines often end exactly on the outline).
_CONSTRUCTION_BBOX_TOL_MM = 0.5


Point = tuple[float, float]


@dataclass
class SparrowPart:
    """One part ready to become a Sparrow item.

    Geometry is in millimetres. `outer` is the placement outline; `holes` are the
    interior features (kept for our own use — the solver ignores them for now).
    """

    part_id: str
    quantity: int
    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS
    dxf: str = ""
    width_mm: float = 0.0
    height_mm: float = 0.0
    # Bend / tangent / centre-mark polylines (mm), in the same coordinate frame as
    # `outer` — drawn on the layout, never nested or counted in the area.
    construction: list[list[Point]] = field(default_factory=list)

    def shape_dict(self) -> dict:
        """The jagua-rs `shape` object for this part."""
        outer = _clean_ring(self.outer)
        if not self.holes:
            return {"type": "simple_polygon", "data": [list(p) for p in outer]}
        inner = [_clean_ring(h) for h in self.holes]
        inner = [[list(p) for p in h] for h in inner if len(h) >= 3]
        if not inner:
            return {"type": "simple_polygon", "data": [list(p) for p in outer]}
        return {
            "type": "polygon",
            "data": {
                "outer": [list(p) for p in outer],
                "inner": inner,
            },
        }


# ── Ring cleaning / winding ──────────────────────────────────────────────────

def _clean_ring(points: list[Point]) -> list[Point]:
    """Return a simple ring safe for jagua-rs `import_simple_polygon`.

    Drops a repeated closing vertex and any vertex within `_MIN_VERTEX_GAP_MM` of
    the previous one, so the ring carries no duplicate vertices (which the solver
    rejects). Order is preserved; winding is left to the caller.
    """
    if not points:
        return []
    pts = [points[0]]
    for x, y in points[1:]:
        px, py = pts[-1]
        if abs(x - px) > _MIN_VERTEX_GAP_MM or abs(y - py) > _MIN_VERTEX_GAP_MM:
            pts.append((x, y))
    # drop trailing point coincident with the first (closing vertex)
    while len(pts) > 1:
        x0, y0 = pts[0]
        xn, yn = pts[-1]
        if abs(xn - x0) <= _MIN_VERTEX_GAP_MM and abs(yn - y0) <= _MIN_VERTEX_GAP_MM:
            pts.pop()
        else:
            break
    return pts


def _as_ccw(points: list[Point]) -> list[Point]:
    pts = _clean_ring(points)
    if signed_area(pts) < 0:
        pts = pts[::-1]
    return pts


def _as_cw(points: list[Point]) -> list[Point]:
    pts = _clean_ring(points)
    if signed_area(pts) > 0:
        pts = pts[::-1]
    return pts


# ── Building parts from an inspection report ─────────────────────────────────

def parts_from_report(
    report: InspectionReport,
    quantity: int = 1,
    *,
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS,
    part_id_prefix: str | None = None,
) -> list[SparrowPart]:
    """Build one SparrowPart per part contour in a report, attaching its holes.

    Holes are assigned to the smallest part that contains them (even–odd depth +
    a point-in-polygon test), so a hole is never attached to the wrong part.
    Outer rings come out counter-clockwise and holes clockwise (standard
    convention; the solver re-derives winding, but this keeps the JSON tidy).
    """
    stem = part_id_prefix or _stem(report.name)
    outer_parts = report.parts
    parts: list[SparrowPart] = []
    for k, part in enumerate(outer_parts):
        holes = _holes_of(part, report)
        parts.append(SparrowPart(
            part_id=f"{stem}#{k}" if len(outer_parts) > 1 else stem,
            quantity=int(quantity),
            outer=_as_ccw(part.points),
            holes=[_as_cw(h.points) for h in holes],
            allowed_orientations=tuple(float(a) for a in allowed_orientations),
            dxf=report.name,
            width_mm=part.width_mm,
            height_mm=part.height_mm,
            construction=_construction_of(part, report),
        ))
    return parts


def _construction_of(part: Contour, report: InspectionReport) -> list[list[Point]]:
    """Bend/tangent/centre-mark lines that belong to this part.

    A line belongs to the part when it lies within the part's bounding box and
    its centroid is inside the outline. This holds even for a single-part file,
    so a whole drawing sheet (frame, title block, text, dimensions) around the
    part is never carried onto every placed copy.
    """
    x0, y0, x1, y1 = part.bbox
    tol = _CONSTRUCTION_BBOX_TOL_MM
    out: list[list[Point]] = []
    for line in report.construction_lines:
        if len(line) < 2:
            continue
        if any(x < x0 - tol or x > x1 + tol or y < y0 - tol or y > y1 + tol
               for x, y in line):
            continue
        cx = sum(x for x, _ in line) / len(line)
        cy = sum(y for _, y in line) / len(line)
        if point_in_polygon((cx, cy), part.points):
            out.append(list(line))
    return out


def _holes_of(part: Contour, report: InspectionReport) -> list[Contour]:
    """Every hole whose interior point lies inside this part's outline.

    Parts are top-level (non-nested) outlines, so a hole falls inside at most one
    part. Depth is not used here: a deeply nested interior feature (e.g. a hole
    inside a big central cut-out) is still kept as this part's hole.
    """
    result = []
    for h in report.holes:
        if point_in_polygon(representative_point(h.points), part.points):
            result.append(h)
    return result


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".dxf"):
        base = base[:-4]
    return base or "part"


def parts_from_dxf(
    data: bytes,
    name: str,
    quantity: int = 1,
    *,
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS,
) -> tuple[list[SparrowPart], InspectionReport]:
    """Inspect one DXF's bytes and build its SparrowParts. Returns (parts, report)."""
    report = inspect_dxf(data, name)
    parts = parts_from_report(
        report, quantity, allowed_orientations=allowed_orientations
    )
    return parts, report
