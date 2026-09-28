"""
core/sparrow_input.py
=====================
DXF part → Sparrow item.

``part_from_report`` turns the one part of a clean DXF (``core/dxf.py``) into a
``SparrowPart``: the outline as a cleaned counter-clockwise ring, its holes as
clockwise rings, the allowed rotations, and the bend lines to draw on it.
``SparrowPart.shape_dict()`` is the jagua-rs ``shape`` object Sparrow nests
(core/sparrow_pack.py builds the instance). Sparrow places a part by its outer
boundary; the holes are kept for the drawing and the real part area.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.dxf import DxfReport
from core.geometry import signed_area

# Default rotations offered to the packer (degrees). Four quadrant orientations
# suit rectangular-ish sheet-metal parts; override per call when a part may only
# be placed one way (grain direction, finish) or may rotate freely.
DEFAULT_ORIENTATIONS: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)

# Points closer than this (mm) are treated as the same vertex when cleaning a
# ring. Guards against jagua-rs bailing on duplicate vertices, including f32
# round-off once the JSON is parsed by the Rust solver.
_MIN_VERTEX_GAP_MM = 1e-4


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

def part_from_report(
    report: DxfReport,
    quantity: int = 1,
    *,
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS,
) -> SparrowPart:
    """The SparrowPart for a priceable report's part, with its holes attached.

    Outer rings come out counter-clockwise and holes clockwise (standard
    convention; the solver re-derives winding, but this keeps the JSON tidy).
    """
    outline = report.outline
    return SparrowPart(
        part_id=_stem(report.name),
        quantity=int(quantity),
        outer=_as_ccw(outline.points),
        holes=[_as_cw(h.points) for h in report.holes],
        allowed_orientations=tuple(float(a) for a in allowed_orientations),
        dxf=report.name,
        width_mm=outline.width_mm,
        height_mm=outline.height_mm,
        construction=list(report.reference_lines),
    )


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".dxf"):
        base = base[:-4]
    return base or "part"
