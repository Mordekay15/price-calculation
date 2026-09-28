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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from core.calculator import piece_weight_kg
from core.geometry import bbox, bbox_wh, net_area, rotate_translate
from core.sheet_cost import Packing, compute_options, effective_sheet

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
        w, h = bbox_wh(p.outer)
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
            net_area(pl.outer, pl.holes) for pl in best
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
        outer_t = rotate_translate(part.outer, rot, trans)
        minx, miny, maxx, maxy = bbox(outer_t)
        if minx < -_TOL or miny < -_TOL or maxx > sheet_w + _TOL or maxy > sheet_h + _TOL:
            continue  # spills past this sheet
        if left[orig_i] <= 0:
            continue
        holes_t = [rotate_translate(h, rot, trans) for h in getattr(part, "holes", [])]
        constr_t = [rotate_translate(c, rot, trans) for c in getattr(part, "construction", [])]
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


# ── fit check ────────────────────────────────────────────────────────────────

def _fits(w: float, h: float, sw: float, sh: float, orients) -> bool:
    """True if a w×h part fits an sw×sh sheet in some allowed orientation."""
    can_swap = orients is None or any(round(float(a) / 90.0) % 2 == 1 for a in orients)
    if w <= sw + _TOL and h <= sh + _TOL:
        return True
    if can_swap and h <= sw + _TOL and w <= sh + _TOL:
        return True
    return False


# ── Costing with Sparrow ─────────────────────────────────────────────────────

def sparrow_options(
    lookup: dict,
    material: str,
    thickness: str,
    thickness_mm: float,
    parts: list,
    *,
    run_fn,
    margin_pct: float = 0.0,
    long_side_clamp_mm: int = 0,
    rankavali_mm: int = 0,
    seed: int = 0,
    time_limit_sec: int = 8,
    pieces_kg_override: float | None = None,
    on_progress=None,
) -> dict:
    """``core.sheet_cost.compute_options`` with Sparrow shape nesting.

    ``parts`` are ``SparrowPart``-like objects. ``pieces_kg_override`` sets the
    total piece weight the sheet cost is spread over — pass the net-area weight
    the per-part summary uses so the two totals reconcile exactly.

    ``on_progress``, if given, receives ``("size", index=, count=, w=, h=)``
    before each sheet size and ``("sheet", w=, h=, placed=, total=)`` each time
    a sheet layout is settled in one orientation.
    """
    n_pieces = sum(int(getattr(p, "quantity", 1)) for p in parts)
    if pieces_kg_override is not None:
        pieces_kg = float(pieces_kg_override)
    else:
        pieces_kg = sum(
            piece_weight_kg(p.width_mm, p.height_mm, thickness_mm, material)
            * int(getattr(p, "quantity", 1))
            for p in parts
        )
    separation = float(rankavali_mm) if rankavali_mm else None

    def pack_fn(sw: int, sh: int) -> Packing:
        # Try the sheet both ways round (portrait / landscape) and keep the
        # tighter fit — the same as rotating the whole nest 90°, so an elongated
        # part is not forced to run along the wrong sheet axis.
        best, alt = _pack_best_orientation(
            parts, sw, sh, long_side_clamp_mm,
            run_fn=run_fn, seed=seed, time_limit_sec=time_limit_sec,
            separation=separation, on_progress=on_progress,
        )
        pack, eff_w, eff_h, draw_w, draw_h = best
        if not pack.ok:
            return Packing(sheets=[], sheets_needed=0, utilization=0.0,
                           eff_w=eff_w, eff_h=eff_h, draw_w=sw, draw_h=sh,
                           failed=1, reason=pack.reason)
        return Packing(
            sheets=pack.sheets,
            sheets_needed=pack.sheets_needed,
            utilization=_utilization(pack, eff_w, eff_h),
            eff_w=eff_w, eff_h=eff_h, draw_w=draw_w, draw_h=draw_h,
            alt=_alt_layout(alt),
        )

    return compute_options(
        lookup, material, thickness, thickness_mm,
        n_pieces=n_pieces if parts else 0, pieces_kg=pieces_kg, pack=pack_fn,
        margin_pct=margin_pct, on_progress=on_progress,
    )


def _utilization(pack: PackResult, eff_w: int, eff_h: int) -> float:
    cap = pack.sheets_needed * eff_w * eff_h
    return (pack.used_area / cap) if cap else 0.0


def _pack_best_orientation(
    parts, sw, sh, clamp, *, run_fn, seed, time_limit_sec, separation,
    on_progress=None,
):
    """Pack the sheet in both orientations; return ``(best, alt)``.

    Each attempt is ``(pack, eff_w, eff_h, draw_w, draw_h)`` where
    ``draw_w × draw_h`` is that orientation's sheet layout and ``eff_w × eff_h``
    its usable area after the clamp. ``best`` is the tighter fit (fewest sheets,
    tie-broken by utilisation) and drives the price; ``alt`` is the *other*
    orientation's real re-nest (or None when the sheet is square, the other
    orientation didn't fit, or it was skipped) — used for the "turn the sheet"
    view. When neither orientation fits, ``(first_attempt, None)`` is returned
    so the caller can surface the failure.

    The turned sheet is skipped when every part may turn a quarter: then its
    layout is just the first one rotated 90°, so re-nesting would only double
    the Sparrow time. Otherwise both orientations nest at the same time —
    Sparrow's time limit is wall-clock, so the pair takes about as long as one.
    """
    def _try(cw, ch):
        ew, eh = effective_sheet(cw, ch, clamp)
        on_sheet = None
        if on_progress is not None:
            on_sheet = lambda placed, total: on_progress(  # noqa: E731
                "sheet", w=cw, h=ch, placed=placed, total=total)
        pack = greedy_fixed_sheets(
            parts, ew, eh, run_fn=run_fn, seed=seed,
            time_limit_sec=time_limit_sec, separation=separation,
            on_sheet=on_sheet,
        )
        return pack, ew, eh, cw, ch

    # Sparrow's fixed strip height is the sheet's short side; the strip runs
    # along the long side (listed first, so it also wins a tie).
    long_side, short_side = max(sw, sh), min(sw, sh)
    if sw == sh or _quarter_turn_free(parts):
        options = [_try(long_side, short_side)]
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            options = list(pool.map(
                lambda d: _try(*d),
                [(long_side, short_side), (short_side, long_side)],
            ))

    ok = [o for o in options if o[0].ok]
    if not ok:
        return options[0], None
    best = min(ok, key=lambda o: (o[0].sheets_needed, -_utilization(o[0], o[1], o[2])))
    alt = next((o for o in ok if o is not best), None)
    return best, alt


def _quarter_turn_free(parts) -> bool:
    """True if every part's allowed rotations are closed under +90°.

    Then any layout turned 90° is still a valid layout (on the turned sheet).
    ``None`` / empty orientations mean free rotation.
    """
    for p in parts:
        orients = getattr(p, "allowed_orientations", None)
        if not orients:
            continue
        angles = {round(float(a)) % 360 for a in orients}
        if any((a + 90) % 360 not in angles for a in angles):
            return False
    return True


def _alt_layout(alt) -> dict | None:
    """Pack the alternate-orientation attempt into a small display dict."""
    if alt is None:
        return None
    pack, ew, eh, dw, dh = alt
    return {
        "_sheets": pack.sheets,
        "_eff_w": ew,
        "_eff_h": eh,
        "_sw": dw,
        "_sh": dh,
        "sheets_needed": pack.sheets_needed,
        "utilization": _utilization(pack, ew, eh),
    }
