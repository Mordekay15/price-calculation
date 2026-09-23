"""
core/sparrow_pack.py
====================
Greedy fixed-sheet nesting on top of the Sparrow strip packer.

Sparrow minimises the length of an open strip; it does not pack into a fixed
W×H sheet. This module bridges that gap for the "which sheet size is cheapest"
analysis: it fills one fixed sheet at a time.

For a given sheet (usable ``sheet_w`` × ``sheet_h`` mm) and a set of parts with
demands, each round:

  1. search for the most parts one sheet holds. A probe asks Sparrow (via an
     injected ``run_fn`` — this module never launches the binary itself) to
     nest ``n`` parts in a strip as high as the sheet (``sheet_h``) and keeps
     the parts that land fully inside the first ``sheet_w`` of length (no part
     straddles the cut). The first probe nests the whole remaining order: its
     first sheet is a layout known to work, but often a loose one — Sparrow
     spreads its effort over the long strip (400 parts: 18 on the sheet where
     26 fit). The strip's length tells how densely Sparrow packed overall
     (parts × ``sheet_w`` / strip length per sheet), so the next probe asks for
     that many, focused on a single sheet: if they fit, it keeps stepping up
     (+1, +2, +4 …); after the first miss it bisects down to the best count,
  2. repeat that sheet layout for as many further sheets as the remaining
     demand still covers (100 parts at 25 per sheet → one layout ×4) —
     re-running Sparrow for an identical demand would only reproduce the sheet,
  3. subtract those from the remaining demand and start the next sheet; only a
     smaller leftover (a different part mix) triggers a new search.

Rounds repeat until every part is placed or a part cannot fit the sheet at all.
The result is a list of distinct ``PackedSheet`` layouts, each with a
``count`` of identical copies, with real placements (rotation +
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
    """One fixed sheet layout with the parts nested onto it.

    ``count`` is how many identical sheets use this layout; ``used_area`` is
    for a single sheet.
    """

    placements: list[Placed]
    sheet_w: float
    sheet_h: float
    used_area: float                # summed true part area (holes subtracted)
    count: int = 1

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
        return sum(s.count for s in self.sheets)

    @property
    def used_area(self) -> float:
        """Summed true part area over every physical sheet."""
        return sum(s.used_area * s.count for s in self.sheets)


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
    on_sheet=None,
) -> PackResult:
    """Nest ``parts`` into fixed ``sheet_w`` × ``sheet_h`` sheets with Sparrow.

    ``parts`` items must expose ``shape_dict()``, ``outer`` (list of points),
    ``holes``, ``allowed_orientations`` and ``part_id``, plus a ``quantity``
    (used only as the initial demand). ``run_fn(instance, *, seed,
    time_limit_sec, separation)`` runs the solver and returns an object with
    ``ok`` (bool) and ``solution`` (dict) — typically wrapping
    ``core.sparrow_runner.run_sparrow``.

    ``on_sheet(placed, total)``, if given, is called after each sheet layout is
    settled with the running count of placed parts — for a progress display.

    Returns a ``PackResult``: ``ok`` with the distinct sheet layouts (each with
    its repeat ``count``), or not-ok with a
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

    n_total = sum(remaining)
    sheets: list[PackedSheet] = []
    while any(q > 0 for q in remaining):
        if sum(s.count for s in sheets) >= max_sheets:
            return PackResult(ok=False, sheets=sheets, reason="liian monta levyä")

        def probe(n: int):
            return _probe(
                parts, _mix(remaining, n), sheet_w, sheet_h, run_fn=run_fn,
                seed=seed, time_limit_sec=time_limit_sec, separation=separation,
            )

        total = sum(remaining)
        err, best, strip_len = probe(total)
        if err is not None:
            return PackResult(ok=False, sheets=sheets, reason=err)
        if not best:
            # Nothing fit within the sheet although each part fits in isolation —
            # usually the separation gap makes even one part overflow.
            return PackResult(
                ok=False, sheets=sheets,
                reason="osat eivät mahtuneet levylle annetulla rankavälillä",
            )

        # lo: most parts seen on one sheet; hi: smallest probe that didn't fit
        # entirely (None until one misses). Try the strip-density estimate,
        # step up from there, then bisect.
        lo, hi, step = len(best), None, 1
        guess = int(total * sheet_w / strip_len) if strip_len else 0
        while lo < total and (hi is None or hi - lo > 1):
            stepping = False
            if guess > lo:
                n, guess = min(total, guess), 0
            elif hi is None:
                n, stepping = min(total, lo + step), True
            else:
                n = (lo + hi) // 2
            err, kept, _ = probe(n)
            if err is not None:
                break  # keep the best sheet found so far
            if len(kept) > lo:
                lo, best = len(kept), kept
            if len(kept) < n:
                hi = n
            elif stepping:
                step *= 2

        per_sheet: dict[int, int] = {}
        for pl in best:
            per_sheet[pl.part_index] = per_sheet.get(pl.part_index, 0) + 1
        # Reuse this layout while the remaining demand still covers its part
        # mix — Sparrow would only reproduce the same sheet.
        count = min(remaining[i] // n for i, n in per_sheet.items())
        for i, n in per_sheet.items():
            remaining[i] -= count * n
        used_area = sum(
            _poly_area(pl.outer) - sum(_poly_area(h) for h in pl.holes) for pl in best
        )
        sheets.append(PackedSheet(best, sheet_w, sheet_h, used_area, count))
        if on_sheet is not None:
            on_sheet(n_total - sum(remaining), n_total)

    return PackResult(ok=True, sheets=sheets)


def _mix(remaining: list[int], n: int) -> dict[int, int]:
    """Pick ``n`` parts from the remaining demand, in proportion to it.

    Returns ``{part_index: count}``. A proportional mix keeps a sheet's layout
    repeatable across the rest of the order.
    """
    total = sum(remaining)
    if n >= total:
        return {i: q for i, q in enumerate(remaining) if q > 0}
    raw = {i: q * n / total for i, q in enumerate(remaining) if q > 0}
    mix = {i: int(r) for i, r in raw.items()}
    short = n - sum(mix.values())
    for i in sorted(raw, key=lambda i: raw[i] - mix[i], reverse=True)[:short]:
        mix[i] += 1
    return {i: c for i, c in mix.items() if c > 0}


def _probe(parts: list, demand: dict[int, int], sheet_w: float, sheet_h: float,
           *, run_fn, seed, time_limit_sec, separation):
    """Nest ``demand`` with Sparrow; return ``(error, placed_on_sheet, strip_len)``.

    ``error`` is None on success; the list holds the parts that land fully
    inside the first ``sheet_w`` of the strip; ``strip_len`` is the length of
    the whole nested strip (None if the solver doesn't report it).
    """
    active = list(demand)
    instance = _build_instance(parts, demand, sheet_h)
    res = run_fn(
        instance, seed=seed, time_limit_sec=time_limit_sec, separation=separation
    )
    if not getattr(res, "ok", False) or getattr(res, "solution", None) is None:
        return getattr(res, "message", "") or "Sparrow-ajo epäonnistui", [], None

    left = dict(demand)
    kept: list[Placed] = []
    for local_id, rot, trans in _read_placements(res.solution):
        if local_id < 0 or local_id >= len(active):
            continue
        orig_i = active[local_id]
        part = parts[orig_i]
        outer_t = _rot(part.outer, rot, trans)
        minx, miny, maxx, maxy = _bbox(outer_t)
        if minx < -_TOL or miny < -_TOL or maxx > sheet_w + _TOL or maxy > sheet_h + _TOL:
            continue  # spills past this sheet
        if left[orig_i] <= 0:
            continue
        holes_t = [_rot(h, rot, trans) for h in getattr(part, "holes", [])]
        constr_t = [_rot(c, rot, trans) for c in getattr(part, "construction", [])]
        kept.append(Placed(orig_i, part.part_id, rot, trans, outer_t, holes_t, constr_t))
        left[orig_i] -= 1
    return None, kept, getattr(res, "strip_width", None)


# ── instance building ────────────────────────────────────────────────────────

def _build_instance(parts: list, demand: dict[int, int], strip_height: float) -> dict:
    """A jagua-rs strip instance for ``demand`` (``{part_index: count}``).

    Items are 0-based in ``demand`` order, so a solution's ``item_id`` indexes
    straight back into ``list(demand)``.
    """
    items = []
    for local_id, (orig_i, n) in enumerate(demand.items()):
        part = parts[orig_i]
        item = {
            "id": local_id,
            "demand": int(n),
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
