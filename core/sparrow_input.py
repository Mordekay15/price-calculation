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

from core.dxf_inspect import (
    Contour,
    InspectionReport,
    _point_in_polygon,
    _representative_point,
    inspect_dxf,
)

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

    def to_item(self, item_id: int) -> dict:
        """The full jagua-rs item entry (with our metadata fields)."""
        return {
            "id": item_id,
            "demand": int(self.quantity),
            "dxf": self.dxf,
            "part_id": self.part_id,
            "allowed_orientations": [float(a) for a in self.allowed_orientations],
            "shape": self.shape_dict(),
        }


# ── Ring cleaning / winding ──────────────────────────────────────────────────

def _signed_area(points: list[Point]) -> float:
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


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
    if _signed_area(pts) < 0:
        pts = pts[::-1]
    return pts


def _as_cw(points: list[Point]) -> list[Point]:
    pts = _clean_ring(points)
    if _signed_area(pts) > 0:
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
        ))
    return parts


def _holes_of(part: Contour, report: InspectionReport) -> list[Contour]:
    """Holes that sit directly inside `part` (one nesting level in)."""
    result = []
    for h in report.holes:
        if h.depth != part.depth + 1:
            continue
        if _point_in_polygon(_representative_point(h.points), part.points):
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


# ── Job: instance + the originals needed to reconstruct the final DXF ─────────

@dataclass
class ItemSource:
    """Links a Sparrow item id back to the original DXF part it came from.

    Phase 9 reconstruction needs the *original* entities, so every item carries
    the source file's bytes plus which part inside that file it is (an index into
    ``report.parts``). The Sparrow polygon is never the source of truth here.
    """

    item_id: int
    part_id: str
    dxf_name: str
    original_bytes: bytes
    report: InspectionReport
    part_index: int
    quantity: int


def build_job(
    inputs: list[tuple[bytes, str, int]],
    *,
    strip_height: float,
    name: str = "stremet_instance",
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS,
) -> tuple[dict, list["ItemSource"]]:
    """Build a Sparrow instance *and* the per-item source records together.

    `inputs` is a list of ``(dxf_bytes, name, quantity)``. The returned instance
    and source list share the same 0-based item ordering, so a placed item's
    ``item_id`` in the solution indexes straight into the sources — which is what
    the reconstruction relies on.
    """
    all_parts: list[SparrowPart] = []
    sources: list[ItemSource] = []
    for data, dxf_name, qty in inputs:
        report = inspect_dxf(data, dxf_name)
        parts = parts_from_report(
            report, qty, allowed_orientations=allowed_orientations
        )
        for k, part in enumerate(parts):
            sources.append(ItemSource(
                item_id=len(all_parts),
                part_id=part.part_id,
                dxf_name=dxf_name,
                original_bytes=data,
                report=report,
                part_index=k,
                quantity=int(qty),
            ))
            all_parts.append(part)
    instance = build_instance(all_parts, name=name, strip_height=strip_height)
    return instance, sources


# ── Assembling the instance ──────────────────────────────────────────────────

def build_instance(
    parts: list[SparrowPart],
    *,
    name: str = "stremet_instance",
    strip_height: float,
) -> dict:
    """Assemble a Sparrow strip-packing instance dict from parts.

    `strip_height` is the fixed dimension of the strip (e.g. the sheet width) in
    millimetres; the packer minimises the strip length. Items get unique 0-based
    ids in the order given.
    """
    items = [part.to_item(i) for i, part in enumerate(parts)]
    return {
        "name": name,
        "strip_height": float(strip_height),
        "items": items,
    }


def validate_instance(instance: dict) -> list[str]:
    """Check an instance against the jagua-rs schema rules. Returns problems.

    Empty list == structurally valid for Sparrow. This mirrors the Rust
    deserialisation / import rules (unique ids, ≥3-vertex simple rings, no
    duplicate vertices, part fits the strip) so we can be confident before ever
    invoking the solver.
    """
    problems: list[str] = []
    if not isinstance(instance.get("name"), str):
        problems.append("`name` must be a string.")
    strip_h = instance.get("strip_height")
    if not isinstance(strip_h, (int, float)) or strip_h <= 0:
        problems.append("`strip_height` must be a positive number.")
    items = instance.get("items")
    if not isinstance(items, list) or not items:
        problems.append("`items` must be a non-empty list.")
        return problems

    ids = set()
    for idx, item in enumerate(items):
        where = f"item[{idx}]"
        iid = item.get("id")
        if not isinstance(iid, int):
            problems.append(f"{where}: `id` must be an integer.")
        elif iid in ids:
            problems.append(f"{where}: duplicate id {iid}.")
        else:
            ids.add(iid)
        demand = item.get("demand")
        if not isinstance(demand, int) or demand < 1:
            problems.append(f"{where}: `demand` must be an integer ≥ 1.")

        shape = item.get("shape", {})
        rings = _shape_rings(shape)
        if rings is None:
            problems.append(f"{where}: unsupported/malformed shape.")
            continue
        outer = rings[0]
        problems += _ring_problems(outer, f"{where} outer")
        for hi, hole in enumerate(rings[1]):
            problems += _ring_problems(hole, f"{where} hole[{hi}]")

        # part must fit the strip in at least one allowed orientation
        if outer and isinstance(strip_h, (int, float)) and strip_h > 0:
            w, h = _bbox_wh(outer)
            oris = item.get("allowed_orientations") or [0.0]
            # a 90°-type rotation swaps w/h, so the fitting dimension is the
            # part height at 0°/180° and the part width at 90°/270°
            candidates = [w if _swaps(a) else h for a in oris]
            if min(candidates) > strip_h + 1e-6:
                problems.append(
                    f"{where}: part ({w:.1f}×{h:.1f} mm) does not fit strip "
                    f"height {strip_h:.1f} mm in any allowed orientation."
                )
    return problems


def _swaps(angle: float) -> bool:
    """True if the rotation swaps width/height (an odd multiple of 90°)."""
    return round(angle / 90.0) % 2 == 1


def _shape_rings(shape: dict):
    """Return (outer_points, [hole_points, ...]) or None if unsupported."""
    t = shape.get("type")
    data = shape.get("data")
    if t == "simple_polygon" and isinstance(data, list):
        return [tuple(p) for p in data], []
    if t == "polygon" and isinstance(data, dict):
        outer = [tuple(p) for p in data.get("outer", [])]
        inner = [[tuple(p) for p in h] for h in data.get("inner", [])]
        return outer, inner
    if t == "rectangle" and isinstance(data, dict):
        x, y = data["x_min"], data["y_min"]
        w, h = data["width"], data["height"]
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], []
    return None


def _ring_problems(points, where: str) -> list[str]:
    out: list[str] = []
    cleaned = _clean_ring([tuple(p) for p in points])
    if len(cleaned) < 3:
        out.append(f"{where}: fewer than 3 distinct vertices.")
        return out
    # non-consecutive exact duplicates would make jagua-rs bail
    seen = set()
    for p in cleaned:
        key = (round(p[0], 6), round(p[1], 6))
        if key in seen:
            out.append(f"{where}: duplicate vertex {p}.")
            break
        seen.add(key)
    return out


def _bbox_wh(points) -> tuple[float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return max(xs) - min(xs), max(ys) - min(ys)
