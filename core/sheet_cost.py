"""Sheet-usage costing, pure (no Streamlit): for one group of pieces that share
a material and thickness, price every sheet size. The nesting is passed in as
``pack(sheet_w, sheet_h) -> list[Packing]`` (``rect_nesting`` or ``sparrow``):
one Packing per plan worth pricing (see ``core.programs``)."""

from __future__ import annotations

from dataclasses import dataclass

from core.pricing import get_sizes_for_material, parse_thickness_mm, weight_kg


@dataclass(frozen=True)
class EdgeGaps:
    """Strips along a sheet's edges where no part is placed, in mm.

    The sheet lies long side horizontal: ``top`` and ``bottom`` are its long
    edges, ``left`` and ``right`` its short ones.
    """

    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0


@dataclass
class Packing:
    """How one sheet size was packed, as reported by a packer.

    ``sheets`` holds one entry per distinct layout (a program), each with the
    ``count`` of sheets cut from it. ``draw_w × draw_h`` is the sheet laid long
    side horizontal. Parts may use the ``eff_w × eff_h`` area whose top-left
    corner is at ``(x0, y0)`` (see ``usable_area``); placements are relative to
    that corner. ``failed`` counts pieces that did not fit.
    """

    sheets: list
    sheets_needed: int
    eff_w: int
    eff_h: int
    draw_w: int
    draw_h: int
    failed: int = 0
    reason: str = ""
    x0: int = 0
    y0: int = 0

    @property
    def programs(self) -> int:
        """Distinct sheet layouts: the programs the designer makes."""
        return len(self.sheets)


@dataclass
class SheetOption:
    """One priced sheet size. When ``failed`` > 0 the pieces did not fit and
    only the size, prices, packing and reason are set."""

    sw: int
    sh: int
    base_ppt: float                 # list price, €/tn
    adjusted_ppt: float             # with the margin, €/tn
    packing: Packing
    sheet_weight_kg: float = 0.0    # one sheet
    sheets_needed: int = 0
    total_eur: float = 0.0
    bill_rate_ppt: float = 0.0      # €/tn against piece weight (see compute_options)
    utilization: float = 0.0
    failed: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0

    @property
    def sheet_kg(self) -> float:
        return self.sheet_weight_kg * self.sheets_needed

    @property
    def programs(self) -> int:
        return self.packing.programs


@dataclass
class GroupCost:
    options: list[SheetOption]
    n_pieces: int
    pieces_kg: float                # real weight of every piece in the group


def parse_size(size_str: str) -> list[tuple[int, int]]:
    """Convert a sheet-size label into concrete (width_mm, height_mm) tuples.

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


def usable_area(sw: int, sh: int, gaps: EdgeGaps) -> tuple[int, int, int, int]:
    """The sheet laid long side horizontal, less its edge gaps: ``(x0, y0, w,
    h)`` of the area parts may use, measured from the top-left corner."""
    long_side, short_side = max(sw, sh), min(sw, sh)
    w = max(0, long_side - gaps.left - gaps.right)
    h = max(0, short_side - gaps.top - gaps.bottom)
    return gaps.left, gaps.top, w, h


def utilization(part_area_mm2: float, sheet_w: float, sheet_h: float, n_sheets: int = 1) -> float:
    """Share of the bought sheet area that ends up as parts (Käyttöaste).

    ``part_area_mm2`` is the real part area — without the cut gap, and with the
    holes removed for DXF parts. The edge gaps count as sheet area, because
    they are paid for. Every utilisation figure in the app uses this one formula.
    """
    total = sheet_w * sheet_h * n_sheets
    return part_area_mm2 / total if total else 0.0


def too_big(parts, sheet_w: float, sheet_h: float, tol: float = 0.0) -> str:
    """Why parts don't fit a ``sheet_w`` × ``sheet_h`` usable area, as the
    sheet-size table shows it: ``ITM-072558 (3059 mm) ei mahdu``. ``parts`` are
    ``(name, w, h)``; a part fits as is or turned a quarter. Names the length
    when that is what's too long, else both sides. "" when every part fits."""
    long_cap, short_cap = max(sheet_w, sheet_h) + tol, min(sheet_w, sheet_h) + tol
    out = []
    for name, w, h in parts:
        long_side, short_side = max(w, h), min(w, h)
        if long_side > long_cap:
            out.append(f"{name} ({long_side:.0f} mm)")
        elif short_side > short_cap:
            out.append(f"{name} ({long_side:.0f} × {short_side:.0f} mm)")
    return ", ".join(out) + " ei mahdu" if out else ""


def fmt_m(mm: int) -> str:
    """Format a mm value as metres: 1000 -> '1.0', 1250 -> '1.25', 1500 -> '1.5'."""
    s = f"{mm / 1000:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def compute_options(
    lookup: dict,
    material: str,
    thickness: str,
    thickness_mm: float,
    *,
    n_pieces: int,
    part_area_mm2: float,
    pack,
    margin_pct: float = 0.0,
    on_progress=None,
) -> GroupCost | None:
    """Price every sheet size of one material + thickness; None when there are
    no pieces or no priced size.

    ``part_area_mm2`` is the total real part area of the group (see
    ``utilization``); the piece weight the sheet cost is spread over is derived
    from it, as ``piece_costs`` does, so the two totals reconcile.
    ``on_progress``, if given, is called as ``on_progress("size", index=i,
    count=n, w=sw, h=sh)`` before each sheet size is packed. A size packed
    with more than one plan gives one option per plan.
    """
    candidates: list[tuple[int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((w_mm, h_mm, price))
    if n_pieces <= 0 or not candidates:
        return None

    pieces_kg = weight_kg(part_area_mm2, thickness_mm, material)
    options = []
    for index, (sw, sh, price_per_tonne) in enumerate(candidates):
        if on_progress is not None:
            on_progress("size", index=index, count=len(candidates), w=sw, h=sh)
        for packing in pack(sw, sh):
            option = SheetOption(sw, sh, price_per_tonne,
                                 price_per_tonne * (1 + margin_pct / 100), packing,
                                 failed=packing.failed, reason=packing.reason)
            if option.ok:
                option.sheet_weight_kg = weight_kg(sw * sh, thickness_mm, material)
                option.sheets_needed = packing.sheets_needed
                option.utilization = utilization(part_area_mm2, sw, sh, packing.sheets_needed)
                option.total_eur = option.adjusted_ppt * (option.sheet_kg / 1000)
                # Effective rate against piece weight, so the pieces summary adds
                # up to the sheet total.
                option.bill_rate_ppt = (option.adjusted_ppt * (option.sheet_kg / pieces_kg)
                                        if pieces_kg else option.adjusted_ppt)
            options.append(option)
    return GroupCost(options, n_pieces, pieces_kg)


def cheapest_index(options: list[SheetOption]) -> int | None:
    """Index of the cheapest fully-fitting option; on a tie the one with fewer
    programs, then the better utilisation."""
    valid = [i for i, o in enumerate(options) if o.ok]
    if not valid:
        return None
    return min(valid, key=lambda i: (options[i].total_eur, options[i].programs,
                                     -options[i].utilization))


# ── Products: grouping and per-piece cost ─────────────────────────────────────

def is_ready(prod: dict) -> bool:
    """A product can be priced once it has a material, thickness and size."""
    return bool(prod["material"] and prod["thickness"]
                and prod["width"] > 0 and prod["height"] > 0)


def group_products(products: list[dict], nest_mode: str) -> dict[tuple, list[dict]]:
    """Group ready products by ``(material, thickness)``.

    In "separate" mode the product id joins the key, so each product is nested
    on its own sheets. Each grouped product gets ``_global_idx`` — its position
    in ``products`` — which picks its colour and ``#N`` label in the layout.
    """
    groups: dict[tuple, list[dict]] = {}
    for i, prod in enumerate(products):
        if not is_ready(prod):
            continue
        key = (prod["material"], prod["thickness"])
        if nest_mode == "separate":
            key += (prod["id"],)
        groups.setdefault(key, []).append({**prod, "_global_idx": i})
    return groups


@dataclass
class PieceCost:
    """Weight and cost of one product's pieces. ``ppt`` is the €/tn the pieces
    are billed at (a group's ``bill_rate_ppt``), None while unpriced."""

    index: int                      # position in the products list
    product: dict
    kg: float                       # one piece
    ppt: float | None

    @property
    def batch_kg(self) -> float:
        return self.kg * self.product["qty"]

    @property
    def eur(self) -> float | None:
        return self.kg * (self.ppt / 1000) if self.ppt else None

    @property
    def batch_eur(self) -> float | None:
        return self.batch_kg * (self.ppt / 1000) if self.ppt else None


def piece_costs(
    products: list[dict],
    prices: dict[str, float],
    areas_mm2: dict[str, float] | None = None,
) -> list[PieceCost]:
    """Per-product piece weight and cost, skipping products without a size or
    thickness. ``prices`` maps a product id to its €/tn; ``areas_mm2`` its real
    area (holes removed) — rectangles without an entry use width × height."""
    areas_mm2 = areas_mm2 or {}
    out = []
    for i, prod in enumerate(products):
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        thickness_mm = parse_thickness_mm(prod["thickness"]) if prod["thickness"] else None
        if thickness_mm is None:
            continue
        area = areas_mm2.get(prod["id"], prod["width"] * prod["height"])
        out.append(PieceCost(i, prod, weight_kg(area, thickness_mm, prod["material"]),
                             prices.get(prod["id"])))
    return out
