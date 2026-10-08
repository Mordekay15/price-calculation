"""Bounding-box nesting for the manual tab: a greedy guillotine packer
(longest side first, best-area fit, 90° rotation) and ``rect_options``.
Identical sheets are merged into one layout with a ``count``, as Sparrow's are."""

import math
from dataclasses import dataclass, field, replace

from core.cutting_stock import MixedSizes, best_plan, candidate_sheets
from core.programs import choose_plans, kit_plan, sheets_used
from core.sheet_cost import (
    EdgeGaps,
    GroupCost,
    Packing,
    compute_options,
    product_label,
    too_big,
    usable_area,
)


# ── Packing ───────────────────────────────────────────────────────────────────

@dataclass
class Placement:
    x: int
    y: int
    w: int
    h: int
    product_idx: int   # index into the original products list
    rotated: bool


@dataclass
class Sheet:
    w: int
    h: int
    placements: list[Placement] = field(default_factory=list)
    free_rects: list[tuple[int, int, int, int]] = field(default_factory=list)
    count: int = 1      # identical sheets cut from this layout

    def __post_init__(self):
        if not self.free_rects:
            self.free_rects = [(0, 0, self.w, self.h)]


def _try_place(sheet: Sheet, rw: int, rh: int, product_idx: int,
               allow_rotation: bool) -> bool:
    orientations = [(rw, rh, False)]
    if allow_rotation and rw != rh:
        orientations.append((rh, rw, True))

    best_i = -1
    best_orient: tuple[int, int, bool] | None = None
    best_score: int | None = None

    for i, (fx, fy, fw, fh) in enumerate(sheet.free_rects):
        for ow, oh, rot in orientations:
            if ow <= fw and oh <= fh:
                leftover = fw * fh - ow * oh
                if best_score is None or leftover < best_score:
                    best_score = leftover
                    best_i = i
                    best_orient = (ow, oh, rot)

    if best_i < 0 or best_orient is None:
        return False

    fx, fy, fw, fh = sheet.free_rects.pop(best_i)
    ow, oh, rot = best_orient
    sheet.placements.append(Placement(fx, fy, ow, oh, product_idx, rot))

    # Guillotine split: keep a vertical strip to the right of the placed piece
    # and a horizontal strip below it (full width of the original free rect).
    right = (fx + ow, fy, fw - ow, oh)
    bottom = (fx, fy + oh, fw, fh - oh)
    if right[2] > 0 and right[3] > 0:
        sheet.free_rects.append(right)
    if bottom[2] > 0 and bottom[3] > 0:
        sheet.free_rects.append(bottom)
    return True


def pack(
    pieces: list[tuple[int, int, int, int]],
    sheet_w: int,
    sheet_h: int,
    allow_rotation: bool = True,
    fixed: frozenset[int] = frozenset(),
) -> tuple[list[Sheet], list[int]]:
    """
    Pack `pieces` onto sheets of size sheet_w × sheet_h. Products in ``fixed``
    are never turned.

    `pieces` is a list of (product_idx, copy_idx, width_mm, height_mm). The
    copy_idx is unused by the algorithm but lets callers map placements back
    to a specific physical piece if they need to.

    Returns (sheets, failed_indices) — `failed_indices` lists positions in
    the input `pieces` list that could not fit on any sheet (piece larger
    than the sheet in both orientations).
    """
    indexed = list(enumerate(pieces))
    indexed.sort(key=lambda item: -max(item[1][2], item[1][3]))

    sheets: list[Sheet] = []
    failed: list[int] = []

    for orig_i, (product_idx, _copy_idx, pw, ph) in indexed:
        turns = allow_rotation and product_idx not in fixed
        fits_natural = pw <= sheet_w and ph <= sheet_h
        fits_rotated = turns and ph <= sheet_w and pw <= sheet_h
        if not (fits_natural or fits_rotated):
            failed.append(orig_i)
            continue

        placed = False
        for sheet in sheets:
            if _try_place(sheet, pw, ph, product_idx, turns):
                placed = True
                break
        if not placed:
            new_sheet = Sheet(sheet_w, sheet_h)
            _try_place(new_sheet, pw, ph, product_idx, turns)
            sheets.append(new_sheet)

    return sheets, failed


# ── High-level summary ────────────────────────────────────────────────────────

def expand_products(products: list[dict]) -> list[tuple[int, int, int, int]]:
    """
    Turn the calculator's product list (width, height, qty per entry) into
    the (product_idx, copy_idx, w, h) tuples that pack() expects.

    Entries with width == 0 or height == 0 are skipped.
    """
    pieces: list[tuple[int, int, int, int]] = []
    for p_idx, prod in enumerate(products):
        w = int(round(prod.get("width", 0) or 0))
        h = int(round(prod.get("height", 0) or 0))
        q = int(prod.get("qty", 0) or 0)
        if w <= 0 or h <= 0 or q <= 0:
            continue
        for c in range(q):
            pieces.append((p_idx, c, w, h))
    return pieces


def merge_identical(sheets: list[Sheet]) -> list[Sheet]:
    """One sheet per distinct layout, its ``count`` the sheets that repeat it,
    in order of first appearance."""
    merged: dict[tuple, Sheet] = {}
    for sheet in sheets:
        key = tuple(sorted((p.x, p.y, p.w, p.h, p.product_idx) for p in sheet.placements))
        if key in merged:
            merged[key].count += sheet.count
        else:
            merged[key] = sheet
    return list(merged.values())


def _turned(sheet: Sheet) -> Sheet:
    """The sheet's layout mirrored across its diagonal: a standing sheet laid
    down. Every piece keeps its place relative to the others, so the layout
    stays valid."""
    return Sheet(sheet.h, sheet.w, [
        Placement(p.y, p.x, p.h, p.w, p.product_idx, not p.rotated)
        for p in sheet.placements
    ], [(y, x, h, w) for x, y, w, h in sheet.free_rects], sheet.count)


def _one_sheet(sheets: list[Sheet], failed: int) -> Sheet | None:
    """The layout, when every piece went on a single sheet."""
    return sheets[0] if not failed and sheets_used(sheets) == 1 else None


def _all_fit(sheets: list[Sheet], failed: int) -> list[Sheet] | None:
    return None if failed else sheets


def counts_on(placements, n: int, index=lambda p: p.product_idx) -> tuple[int, ...]:
    """How many of each of the ``n`` products a sheet holds."""
    counts = [0] * n
    for p in placements:
        counts[index(p)] += 1
    return tuple(counts)


def keep_counts(placements: list, counts, index=lambda p: p.product_idx) -> list:
    """The placements less any beyond ``counts`` of each product (a sheet with
    surplus pieces taken off)."""
    left = list(counts)
    kept = []
    for p in placements:
        if left[index(p)] > 0:
            left[index(p)] -= 1
            kept.append(p)
    return kept


def box_candidates(sizes: list[tuple[float, float]], quantities: list[int], seeds: list,
                   sheet_w: float, sheet_h: float, gap: float, *, fixed=frozenset(),
                   index=lambda p: p.product_idx) -> dict | None:
    """``core.cutting_stock.candidate_sheets`` on the parts' boxes: each box
    grown by the cut gap (as ``rect_options`` packs), packed on the
    ``sheet_w`` × ``sheet_h`` sheet (laid down: ``sheet_w`` along x) standing
    on its side, as ``rect_options`` packs, and turned back.

    ``seeds`` are layouts known to fit, with ``placements`` and their part
    index read by ``index``. Returns ``{counts: layout}`` or None; a layout is
    a seed or a lying ``Sheet``, and a plan's layout may hold surplus pieces
    (see ``keep_counts``). A lying sheet's piece is ``rotated`` when it is
    turned a quarter from its ``sizes`` box."""
    n = len(sizes)

    def fits(counts):
        # Boxes round up and the sheet down: a real shape never comes closer
        # than the gap. Each box goes to the standing sheet already turned, so
        # an unturned piece there lies as given once the sheet lies down (and
        # a part in ``fixed`` keeps its angle).
        pieces = [(i, c, math.ceil(h + gap - 1e-9), math.ceil(w + gap - 1e-9))
                  for i, (w, h) in enumerate(sizes) for c in range(counts[i])]
        sheets, failed = pack(pieces, math.floor(sheet_h + gap + 1e-9),
                              math.floor(sheet_w + gap + 1e-9), fixed=fixed)
        if failed or len(sheets) != 1:
            return None
        return Sheet(sheets[0].h, sheets[0].w, [
            Placement(p.y, p.x, p.h, p.w, p.product_idx, p.rotated)
            for p in sheets[0].placements])

    return candidate_sheets(quantities, sizes, fits,
                            [(counts_on(s.placements, n, index), s) for s in seeds])


def fewest_sheets(quantities: list[int], pool: dict | None, areas: list[float] | None = None,
                  sheet_area: float | None = None) -> list | None:
    """The plan with the fewest sheets of one size from its candidate sheets:
    ``[(repeats, counts, layout)]``, or None. With the parts' real ``areas``
    and the ``sheet_area``, well-filled programs are kept first (see
    ``core.cutting_stock.GOOD_FILL``)."""
    plan = best_plan(quantities, [(1.0, pool, sheet_area)], areas=areas) if pool else None
    return None if plan is None else [(r, p, layout) for _, r, p, layout in plan]


# ── Costing with this packer ──────────────────────────────────────────────────

def rect_options(
    lookup: dict,
    material: str,
    thickness: str,
    thickness_mm: float,
    products: list[dict],
    margin_pct: float = 0.0,
    edges: EdgeGaps = EdgeGaps(),
    rankavali_mm: int = 0,
) -> GroupCost | None:
    """``core.sheet_cost.compute_options`` with the bounding-box packer.

    Each piece is grown by the cut gap (rankaväli) on its right and bottom
    side, so the packer leaves that gap between parts. The usable area is
    grown by the same amount, so the last piece's gap may overhang the edge:
    parts may touch the sheet edge. The edge gaps shrink the usable area.
    Each size is priced with the fewest sheets and, when worth showing, with a
    repeatable program (see ``core.programs``); one more option may cut the
    parts from several sizes (see ``core.cutting_stock.MixedSizes``).
    """
    quantities = [int(p["qty"]) for p in products]
    part_area_mm2 = sum(p["width"] * p["height"] * p["qty"] for p in products)
    boxes = [(int(round(p["width"])), int(round(p["height"]))) for p in products]
    areas = [p["width"] * p["height"] for p in products]
    mixed = MixedSizes(quantities, areas)

    def pieces_for(qty: list[int]) -> list[tuple[int, int, int, int]]:
        counted = [{**p, "qty": q} for p, q in zip(products, qty)]
        return [(p_idx, c_idx, w + rankavali_mm, h + rankavali_mm)
                for p_idx, c_idx, w, h in expand_products(counted)]

    def pack_fn(sw: int, sh: int) -> list[Packing]:
        # Laid long side horizontal, like every sheet in the app. The packer
        # still fills the sheet standing on its short side, as it always has
        # (its greedy order packs differently the other way round), and the
        # finished layouts are turned to lie down.
        x0, y0, eff_w, eff_h = usable_area(sw, sh, edges)

        def pack_sheets(qty: list[int]) -> tuple[list[Sheet], int]:
            sheets, failed = pack(pieces_for(qty), eff_h + rankavali_mm,
                                  eff_w + rankavali_mm, allow_rotation=True)
            return merge_identical(sheets), len(failed)

        def packing(sheets: list[Sheet], failed: int = 0) -> Packing:
            return Packing(
                sheets=[_turned(sheet) for sheet in sheets], sheets_needed=sheets_used(sheets),
                eff_w=eff_w, eff_h=eff_h, draw_w=max(sw, sh), draw_h=min(sw, sh),
                failed=failed, x0=x0, y0=y0,
            )

        greedy, failed = pack_sheets(quantities)
        # Candidate sheets (see ``core.cutting_stock``), also on a size too
        # small for some parts: it may still take the others in a mixed plan.
        pool = box_candidates(boxes, quantities, [] if failed else [_turned(s) for s in greedy],
                              eff_w, eff_h, rankavali_mm)
        mixed.keep(pool, lambda plan: (packing(standing(plan)), area_of(plan)), sw * sh)
        if failed:
            reason = too_big([(product_label(p), p["width"], p["height"]) for p in products],
                             eff_w, eff_h)
            return [replace(packing(greedy, failed), reason=reason)]
        kit = kit_plan(quantities, greedy, lambda kit: _one_sheet(*pack_sheets(kit)),
                       lambda rest: _all_fit(*pack_sheets(rest)))
        fewest = fewest_sheets(quantities, pool, areas, sw * sh)
        return [packing(sheets) for sheets in
                choose_plans(greedy, fewest and standing(fewest), kit)]

    def area_of(plan) -> float:
        return sum(r * n * products[i]["width"] * products[i]["height"]
                   for r, counts, _ in plan for i, n in enumerate(counts))

    def standing(plan) -> list[Sheet]:
        """A cutting-stock plan's lying layouts as standing sheets like the
        greedy ones (``packing`` turns them)."""
        return [_turned(replace(layout, placements=keep_counts(layout.placements, counts),
                                count=r))
                for r, counts, layout in plan]

    return compute_options(
        lookup, material, thickness, thickness_mm,
        n_pieces=sum(quantities), part_area_mm2=part_area_mm2, pack=pack_fn,
        margin_pct=margin_pct, mix=mixed.plan,
    )
