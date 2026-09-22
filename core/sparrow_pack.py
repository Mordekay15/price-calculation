"""
core/sparrow_pack.py
====================
Greedy fixed-sheet nesting on top of the Sparrow strip packer.

Sparrow minimises the length of an open strip; it does not pack into a fixed
W×H sheet. This module bridges that gap for the "which sheet size is cheapest"
analysis: it fills one fixed sheet at a time.

For a given sheet (usable ``sheet_w`` × ``sheet_h`` mm) and a set of parts with
demands, each round:

  1. build a Sparrow instance for the *remaining* demand with the strip height
     fixed to the sheet's short side (``sheet_h``),
  2. run Sparrow (via an injected ``run_fn`` — this module never launches the
     binary itself),
  3. keep every placed part that lands fully inside the first ``sheet_w`` of
     length — that is this sheet's contents (no part straddles the cut),
  4. subtract those from the remaining demand and start the next sheet.

Rounds repeat until every part is placed or a part cannot fit the sheet at all.
The result is a list of ``PackedSheet`` with real placements (rotation +
translation) and true-area utilisation, ready to price and draw.

The module is deliberately free of ezdxf / Streamlit: parts are passed in as
objects exposing ``shape_dict()``, ``outer``, ``holes``, ``allowed_orientations``
and ``part_id`` (``core.sparrow_input.SparrowPart`` fits), and the solver is an
injected callable, so the greedy logic is unit-testable without the binary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]

# Tolerance (mm) for the "inside the sheet" test — absorbs solver float round-off
# and the tiny overhang a separation gap can introduce.
_TOL = 0.5


@dataclass
class Placed:
    """One part placed on a sheet, with its transformed geometry (mm)."""

    part_index: int                 # index into the parts list passed in
    part_id: str
    rotation_deg: float
    translation: tuple[float, float]
    outer: list[Point]              # outer ring after rotate+translate
    holes: list[list[Point]] = field(default_factory=list)
    construction: list[list[Point]] = field(default_factory=list)  # bend/tangent lines


@dataclass
class PackedSheet:
    """One fixed sheet with the parts nested onto it."""

    placements: list[Placed]
    sheet_w: float
    sheet_h: float
    used_area: float                # summed true part area (holes subtracted)

    @property
    def utilization(self) -> float:
        total = self.sheet_w * self.sheet_h
        return (self.used_area / total) if total else 0.0


@dataclass
class PackResult:
    """Outcome of packing one sheet size."""

    ok: bool
    sheets: list[PackedSheet] = field(default_factory=list)
    reason: str = ""

    @property
    def sheets_needed(self) -> int:
        return len(self.sheets)


def greedy_fixed_sheets(
    parts: list,
    sheet_w: float,
    sheet_h: float,
    *,
    run_fn,
    seed: int = 0,
    time_limit_sec: int = 8,
    separation: float | None = None,
    max_sheets: int = 400,
) -> PackResult:
    """Nest ``parts`` into fixed ``sheet_w`` × ``sheet_h`` sheets with Sparrow.

    ``parts`` items must expose ``shape_dict()``, ``outer`` (list of points),
    ``holes``, ``allowed_orientations`` and ``part_id``, plus a ``quantity``
    (used only as the initial demand). ``run_fn(instance, *, seed,
    time_limit_sec, separation)`` runs the solver and returns an object with
    ``ok`` (bool) and ``solution`` (dict) — typically wrapping
    ``core.sparrow_runner.run_sparrow``.

    Returns a ``PackResult``: ``ok`` with the per-sheet layout, or not-ok with a
    reason (a part too large for the sheet, the solver failed, or the sheet
    count blew past ``max_sheets``).
    """
    if sheet_w <= 0 or sheet_h <= 0:
        return PackResult(ok=False, reason="virheellinen levykoko")

    remaining = [int(getattr(p, "quantity", 1)) for p in parts]

    # Feasibility: every demanded part must fit the sheet in some orientation.
    for i, p in enumerate(parts):
        if remaining[i] <= 0:
            continue
        w, h = _bbox_wh(p.outer)
        if not _fits(w, h, sheet_w, sheet_h, getattr(p, "allowed_orientations", None)):
            return PackResult(
                ok=False,
                reason=f"osa {getattr(p, 'part_id', i)} ei mahdu levylle "
                       f"({w:.0f}×{h:.0f} mm)",
            )

    sheets: list[PackedSheet] = []
    while any(q > 0 for q in remaining):
        if len(sheets) >= max_sheets:
            return PackResult(ok=False, sheets=sheets, reason="liian monta levyä")

        active = [i for i, q in enumerate(remaining) if q > 0]
        instance = _build_instance(parts, remaining, active, sheet_h)
        res = run_fn(
            instance, seed=seed, time_limit_sec=time_limit_sec, separation=separation
        )
        if not getattr(res, "ok", False) or getattr(res, "solution", None) is None:
            return PackResult(
                ok=False, sheets=sheets,
                reason=getattr(res, "message", "") or "Sparrow-ajo epäonnistui",
            )

        kept: list[Placed] = []
        used_area = 0.0
        for local_id, rot, trans in _read_placements(res.solution):
            if local_id < 0 or local_id >= len(active):
                continue
            orig_i = active[local_id]
            part = parts[orig_i]
            outer_t = _rot(part.outer, rot, trans)
            minx, miny, maxx, maxy = _bbox(outer_t)
            if minx < -_TOL or miny < -_TOL or maxx > sheet_w + _TOL or maxy > sheet_h + _TOL:
                continue  # spills past this sheet — leave it for the next round
            if remaining[orig_i] <= 0:
                continue  # already satisfied this part's demand on this sheet
            holes_t = [_rot(h, rot, trans) for h in getattr(part, "holes", [])]
            constr_t = [_rot(c, rot, trans) for c in getattr(part, "construction", [])]
            kept.append(Placed(orig_i, part.part_id, rot, trans, outer_t, holes_t, constr_t))
            remaining[orig_i] -= 1
            used_area += _poly_area(outer_t) - sum(_poly_area(h) for h in holes_t)

        if not kept:
            # Nothing fit within the sheet although each part fits in isolation —
            # usually the separation gap makes even one part overflow.
            return PackResult(
                ok=False, sheets=sheets,
                reason="osat eivät mahtuneet levylle annetulla rankavälillä",
            )
        sheets.append(PackedSheet(kept, sheet_w, sheet_h, used_area))

    return PackResult(ok=True, sheets=sheets)


# ── instance building ────────────────────────────────────────────────────────

def _build_instance(parts: list, remaining: list[int], active: list[int],
                    strip_height: float) -> dict:
    """A jagua-rs strip instance for the currently-remaining demand.

    Items are 0-based in ``active`` order, so a solution's ``item_id`` indexes
    straight back into ``active``.
    """
    items = []
    for local_id, orig_i in enumerate(active):
        part = parts[orig_i]
        item = {
            "id": local_id,
            "demand": int(remaining[orig_i]),
            "part_id": getattr(part, "part_id", str(orig_i)),
            "allowed_orientations": [
                float(a) for a in (getattr(part, "allowed_orientations", None) or ())
            ],
            "shape": part.shape_dict(),
        }
        if not item["allowed_orientations"]:
            item.pop("allowed_orientations")
        items.append(item)
    return {"name": "pack", "strip_height": float(strip_height), "items": items}


# ── solution parsing (self-contained copy of the reconstruct reader) ──────────

def _read_placements(solution: dict):
    """Yield (item_id, rotation_deg, (tx, ty)) for every placed item."""
    sol = solution.get("solution", solution) if isinstance(solution, dict) else {}
    layouts = []
    if isinstance(sol, dict):
        if isinstance(sol.get("layout"), dict):
            layouts = [sol["layout"]]
        elif isinstance(sol.get("layouts"), list):
            layouts = [l for l in sol["layouts"] if isinstance(l, dict)]
    out = []
    for layout in layouts:
        for pi in layout.get("placed_items", []):
            transf = pi.get("transformation", {}) or {}
            tx, ty = transf.get("translation", [0.0, 0.0])
            out.append((
                int(pi["item_id"]),
                float(transf.get("rotation", 0.0)),
                (float(tx), float(ty)),
            ))
    return out


# ── geometry helpers ─────────────────────────────────────────────────────────

def _rot(points, rotation_deg: float, t: tuple[float, float]):
    """Apply p' = R(θ)·p + t to a list of mm points."""
    th = math.radians(rotation_deg)
    c, s = math.cos(th), math.sin(th)
    tx, ty = t
    return [(x * c - y * s + tx, x * s + y * c + ty) for x, y in points]


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_wh(points) -> tuple[float, float]:
    minx, miny, maxx, maxy = _bbox(points)
    return maxx - minx, maxy - miny


def _poly_area(points) -> float:
    """Absolute shoelace area of a ring."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _fits(w: float, h: float, sw: float, sh: float, orients) -> bool:
    """True if a w×h part fits an sw×sh sheet in some allowed orientation."""
    can_swap = orients is None or any(round(float(a) / 90.0) % 2 == 1 for a in orients)
    if w <= sw + _TOL and h <= sh + _TOL:
        return True
    if can_swap and h <= sw + _TOL and w <= sh + _TOL:
        return True
    return False
