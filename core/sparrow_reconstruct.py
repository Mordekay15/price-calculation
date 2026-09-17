"""
core/sparrow_reconstruct.py
===========================
Sparrow — reconstruct the final production DXF (Phase 9).

This is the manufacturing-critical step. Sparrow only nested *approximated*
polygons; the laser needs the **original** geometry. So for every placed item in
the solution we read:

  * source item id,
  * X / Y translation,
  * rotation,
  * sheet / strip number,

and re-apply that placement to the *original* DXF entities of that part — lines,
polylines, arcs, circles, holes, and every layer (engraving, cutting, …) — never
to the simplified Sparrow polygon.

Coordinate frame
----------------
jagua-rs centres each item on import (a ``pre_transform``) but exports each
placement already composed back onto the *original* input coordinates
(``int_to_ext_transformation`` = ``pre`` then ``int``; the SVG confirms it by
drawing each item at its original coordinates and placing it with
``translate(t) rotate(θ)``). A placed point is therefore::

    p' = R(θ) · p + t          # θ CCW degrees, t = translation, p in mm

Because our Sparrow polygons were emitted in the DXF's own millimetre
coordinates, the very same transform maps the original entities into place. Each
original entity is scaled to millimetres, then rotated, then translated — a
single rigid+uniform transform, so circles stay circles and arcs stay arcs.

Two output modes
----------------
* ``"original"`` (default) — copies the real entities with their layers. Use this
  for production.
* ``"polygon"`` — emits only the Sparrow outline (and hole) polygons. Acceptable
  for a quick MVP / visual test, **not** for a real laser (it loses arcs, exact
  geometry, layers, and possibly holes).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

import ezdxf
from ezdxf import recover
from ezdxf.bbox import extents
from ezdxf.math import Matrix44

from core.sparrow_input import ItemSource

# Tolerance (mm) for the "inside the sheet" check — accounts for the item
# separation / float noise in the packer's placement.
_BOUNDS_TOL_MM = 0.5


@dataclass
class Placement:
    """One placed copy of an item, as read from the Sparrow solution."""

    item_id: int
    rotation_deg: float
    translation: tuple[float, float]
    sheet: int


@dataclass
class ReconstructResult:
    ok: bool
    mode: str
    dxf_text: str = ""
    placed_count: int = 0
    requested_count: int = 0
    strip_width: float | None = None
    strip_height: float | None = None
    layers: list[str] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    out_of_bounds: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def dxf_bytes(self) -> bytes:
        return self.dxf_text.encode("utf-8")


# ── Reading the solution ─────────────────────────────────────────────────────

def read_placements(solution: dict) -> list[Placement]:
    """Pull every placed item (id, rotation, translation, sheet) from a solution."""
    sol = solution.get("solution", solution)
    layouts = _layouts(sol)
    placements: list[Placement] = []
    for sheet_idx, layout in enumerate(layouts):
        for pi in layout.get("placed_items", []):
            transf = pi.get("transformation", {})
            tx, ty = transf.get("translation", [0.0, 0.0])
            placements.append(Placement(
                item_id=int(pi["item_id"]),
                rotation_deg=float(transf.get("rotation", 0.0)),
                translation=(float(tx), float(ty)),
                sheet=sheet_idx,
            ))
    return placements


def _layouts(sol: dict) -> list[dict]:
    """Sparrow SPP has a single ``layout``; be tolerant of a ``layouts`` list too."""
    if isinstance(sol, dict):
        if isinstance(sol.get("layout"), dict):
            return [sol["layout"]]
        if isinstance(sol.get("layouts"), list):
            return [l for l in sol["layouts"] if isinstance(l, dict)]
    return []


def _strip_size(solution: dict) -> tuple[float | None, float | None]:
    sol = solution.get("solution", solution)
    width = sol.get("strip_width") if isinstance(sol, dict) else None
    height = solution.get("strip_height")
    return (
        float(width) if isinstance(width, (int, float)) else None,
        float(height) if isinstance(height, (int, float)) else None,
    )


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _rotate_translate(points, rotation_deg: float, t: tuple[float, float]):
    """Apply p' = R(θ)·p + t to a list of mm points."""
    th = math.radians(rotation_deg)
    c, s = math.cos(th), math.sin(th)
    tx, ty = t
    return [(c * x - s * y + tx, s * x + c * y + ty) for x, y in points]


def _placement_matrix(rotation_deg: float, t: tuple[float, float], scale: float) -> Matrix44:
    """Matrix that scales to mm, rotates θ CCW, then translates — in that order."""
    return Matrix44.chain(
        Matrix44.scale(scale, scale, 1.0),
        Matrix44.z_rotate(math.radians(rotation_deg)),
        Matrix44.translate(t[0], t[1], 0.0),
    )


def _bbox(points) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


# ── Original-entity handling ─────────────────────────────────────────────────

def _read_source_doc(data: bytes):
    doc, _auditor = recover.read(io.BytesIO(data))
    return doc


def _entity_centroid_mm(entity, factor: float) -> tuple[float, float] | None:
    """Bounding-box centre of one entity, in millimetres, or None if unknown."""
    try:
        bb = extents([entity], fast=True)
    except Exception:
        return None
    if bb is None or not bb.has_data:
        return None
    c = bb.center
    return c.x * factor, c.y * factor


def _assign_entities_to_parts(doc, report) -> dict[int, list]:
    """Group a source doc's entities by which part (report.parts index) they belong to.

    A single-part file sends every entity to part 0. For multi-part files each
    entity is assigned to the part whose outer bounding box (in mm) contains the
    entity's centroid; ties break to the smallest part, misses to the nearest.
    This keeps a part's outline, holes and engraving/cutting marks together.
    """
    msp = doc.modelspace()
    parts = report.parts
    if len(parts) <= 1:
        return {0: list(msp)}

    factor = report.mm_per_unit
    boxes = [(_bbox(p.points), _area(p.points)) for p in parts]
    groups: dict[int, list] = {i: [] for i in range(len(parts))}
    for e in msp:
        c = _entity_centroid_mm(e, factor)
        if c is None:
            continue
        idx = _best_part(c, boxes)
        groups[idx].append(e)
    return groups


def _best_part(c, boxes) -> int:
    contained = [
        (area, i) for i, (box, area) in enumerate(boxes) if _in_box(c, box)
    ]
    if contained:
        return min(contained)[1]  # smallest containing part
    # fall back to the nearest bbox centre
    def dist(i):
        x0, y0, x1, y1 = boxes[i][0]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return (c[0] - cx) ** 2 + (c[1] - cy) ** 2
    return min(range(len(boxes)), key=dist)


def _in_box(c, box, tol: float = 1.0) -> bool:
    x0, y0, x1, y1 = box
    return x0 - tol <= c[0] <= x1 + tol and y0 - tol <= c[1] <= y1 + tol


def _area(points) -> float:
    n = len(points)
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


# ── Main entry point ─────────────────────────────────────────────────────────

def reconstruct_dxf(
    solution: dict,
    sources: list[ItemSource],
    *,
    mode: str = "original",
) -> ReconstructResult:
    """Reconstruct the nested production DXF from a Sparrow solution.

    `sources[item_id]` must be the original part for that item (see
    ``core.sparrow_input.build_job``). `mode="original"` preserves the real
    entities and layers; `mode="polygon"` emits only the Sparrow outlines.
    """
    placements = read_placements(solution)
    strip_w, strip_h = _strip_size(solution)
    requested = sum(int(s.quantity) for s in sources)

    result = ReconstructResult(
        ok=False,
        mode=mode,
        placed_count=len(placements),
        requested_count=requested,
        strip_width=strip_w,
        strip_height=strip_h,
    )

    if not placements:
        result.warnings.append("Ratkaisussa ei ollut yhtään sijoitettua osaa.")
        return result

    out = ezdxf.new(setup=True)
    out.units = 4  # millimetres
    omsp = out.modelspace()

    if mode == "polygon":
        _emit_polygons(omsp, placements, sources, result)
    else:
        _emit_original(out, omsp, placements, sources, result)

    # bounds check against the strip
    if strip_w is not None and strip_h is not None:
        for tag, (x0, y0, x1, y1) in result_bboxes(result):
            if (x0 < -_BOUNDS_TOL_MM or y0 < -_BOUNDS_TOL_MM
                    or x1 > strip_w + _BOUNDS_TOL_MM or y1 > strip_h + _BOUNDS_TOL_MM):
                result.out_of_bounds.append(
                    f"{tag}: bbox [{x0:.1f},{y0:.1f}]–[{x1:.1f},{y1:.1f}] mm "
                    f"ylittää levyn {strip_w:.1f}×{strip_h:.1f} mm."
                )

    result.layers = sorted({e.dxf.layer for e in omsp})
    counts: dict[str, int] = {}
    for e in omsp:
        counts[e.dxftype()] = counts.get(e.dxftype(), 0) + 1
    result.entity_counts = counts

    stream = io.StringIO()
    out.write(stream)
    result.dxf_text = stream.getvalue()
    result.ok = not result.out_of_bounds
    return result


# per-placement bboxes are stashed on the result during emission
def result_bboxes(result: ReconstructResult):
    return getattr(result, "_bboxes", [])


def _emit_original(out, omsp, placements, sources, result) -> None:
    """Copy the original entities of each placed part into place, keeping layers."""
    docs: dict[int, object] = {}
    groups: dict[int, dict[int, list]] = {}
    bboxes: list[tuple[str, tuple[float, float, float, float]]] = []

    for p in placements:
        if p.item_id >= len(sources):
            result.warnings.append(f"item_id {p.item_id} puuttuu lähteistä — ohitettu.")
            continue
        src = sources[p.item_id]
        key = id(src.original_bytes)
        if key not in docs:
            try:
                docs[key] = _read_source_doc(src.original_bytes)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"{src.dxf_name}: alkuperäistä DXF:ää ei voitu lukea: {exc}")
                docs[key] = None
        doc = docs[key]
        if doc is None:
            continue
        if key not in groups:
            groups[key] = _assign_entities_to_parts(doc, src.report)
        entities = groups[key].get(src.part_index, [])

        # copy the source layer definitions we touch (colour / linetype)
        _ensure_layers(out, doc, entities)

        factor = src.report.mm_per_unit
        m = _placement_matrix(p.rotation_deg, p.translation, factor)
        for e in entities:
            try:
                e2 = e.copy()
                e2.transform(m)
                omsp.add_foreign_entity(e2, copy=False)
            except Exception as exc:  # noqa: BLE001 — skip an entity we can't move
                result.warnings.append(
                    f"{src.dxf_name}: {e.dxftype()} ei siirtynyt ({exc})."
                )

        # bbox for the bounds check, from the part outline (already mm)
        outer = sources[p.item_id].report.parts[src.part_index].points
        placed = _rotate_translate(outer, p.rotation_deg, p.translation)
        bboxes.append((f"{src.part_id}#s{p.sheet}", _bbox(placed)))

    result._bboxes = bboxes


def _emit_polygons(omsp, placements, sources, result) -> None:
    """MVP mode: emit the Sparrow outline (+ holes) as polylines only."""
    bboxes = []
    for p in placements:
        if p.item_id >= len(sources):
            continue
        src = sources[p.item_id]
        part = src.report.parts[src.part_index]
        outer = _rotate_translate(part.points, p.rotation_deg, p.translation)
        layer = f"PART_{src.part_id}"
        omsp.add_lwpolyline(
            [(x, y) for x, y in outer], close=True, dxfattribs={"layer": layer}
        )
        # holes on a dedicated layer so they are visibly present
        for hole in _holes_for(part, src.report):
            hpts = _rotate_translate(hole.points, p.rotation_deg, p.translation)
            omsp.add_lwpolyline(
                [(x, y) for x, y in hpts], close=True, dxfattribs={"layer": "HOLES"}
            )
        bboxes.append((f"{src.part_id}#s{p.sheet}", _bbox(outer)))
    result._bboxes = bboxes


def _holes_for(part, report):
    from core.dxf_inspect import _point_in_polygon, _representative_point
    out = []
    for h in report.holes:
        if _point_in_polygon(_representative_point(h.points), part.points):
            out.append(h)
    return out


def _ensure_layers(out_doc, src_doc, entities) -> None:
    """Create in the output doc any layer used by `entities`, copying properties."""
    src_layers = src_doc.layers
    for e in entities:
        name = getattr(e.dxf, "layer", "0") or "0"
        if name in out_doc.layers:
            continue
        attribs = {}
        if name in src_layers:
            sl = src_layers.get(name)
            for key in ("color", "linetype", "lineweight"):
                if sl.dxf.hasattr(key):
                    attribs[key] = sl.dxf.get(key)
        try:
            out_doc.layers.add(name, **attribs)
        except Exception:  # noqa: BLE001 — a bad linetype ref shouldn't stop us
            try:
                out_doc.layers.add(name)
            except Exception:
                pass
