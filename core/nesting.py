"""
core/nesting.py
===============
2D bin-packing for sheet-metal nesting.

Given an order (list of rectangular products with quantities) and the inner
dimensions of a metal sheet, work out how many sheets are needed and how the
products lay out on each sheet. Pieces from different products may share a
sheet — leftover space on one sheet is reused for the next product.

The packer uses a guillotine-cut heuristic:
  - sort pieces by their longest side, descending
  - for each piece, try every existing sheet's free rectangles and pick the
    one with the smallest leftover area (Best-Area-Fit)
  - rotate 90° if that gives a fit when the natural orientation does not
  - on placement, split the chosen free rect into a right and bottom strip

This is a greedy heuristic, not optimal — but it is deterministic, fast, and
gives good results for typical sheet-metal orders.
"""

from dataclasses import dataclass, field


# ── Size string parsing ───────────────────────────────────────────────────────

def parse_size(size_str: str) -> list[tuple[int, int]]:
    """
    Convert a sheet-size label into concrete (width_mm, height_mm) tuples.

    Examples:
        "1000x2000"               -> [(1000, 2000)]
        "1250x2500/1500x3000"     -> [(1250, 2500), (1500, 3000)]
    """
    out: list[tuple[int, int]] = []
    for part in (size_str or "").split("/"):
        try:
            w_str, h_str = part.lower().split("x")
            out.append((int(w_str.strip()), int(h_str.strip())))
        except (ValueError, IndexError):
            continue
    return out


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

    def __post_init__(self):
        if not self.free_rects:
            self.free_rects = [(0, 0, self.w, self.h)]

    @property
    def used_area(self) -> int:
        return sum(p.w * p.h for p in self.placements)

    @property
    def utilization(self) -> float:
        total = self.w * self.h
        return self.used_area / total if total else 0.0


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
) -> tuple[list[Sheet], list[int]]:
    """
    Pack `pieces` onto sheets of size sheet_w × sheet_h.

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
        fits_natural = pw <= sheet_w and ph <= sheet_h
        fits_rotated = allow_rotation and ph <= sheet_w and pw <= sheet_h
        if not (fits_natural or fits_rotated):
            failed.append(orig_i)
            continue

        placed = False
        for sheet in sheets:
            if _try_place(sheet, pw, ph, product_idx, allow_rotation):
                placed = True
                break
        if not placed:
            new_sheet = Sheet(sheet_w, sheet_h)
            _try_place(new_sheet, pw, ph, product_idx, allow_rotation)
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


def summarise(
    sheet_w: int,
    sheet_h: int,
    sheets: list[Sheet],
    failed_count: int,
) -> dict:
    total_sheet_area = sheet_w * sheet_h * len(sheets)
    used_area = sum(s.used_area for s in sheets)
    return {
        "sheet_w":          sheet_w,
        "sheet_h":          sheet_h,
        "sheets_needed":    len(sheets),
        "failed_pieces":    failed_count,
        "utilization":      (used_area / total_sheet_area) if total_sheet_area else 0.0,
        "used_area_mm2":    used_area,
        "sheet_area_mm2":   total_sheet_area,
    }
