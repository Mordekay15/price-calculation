"""
core/sheet_cost.py
==================
Sheet-usage costing — pure, no Streamlit, no I/O.

For one group of pieces that share a material and thickness, compare every
sheet size that carries a price: how many sheets the pieces need, the
utilisation, and the total cost after margin.

The nesting itself is injected as a ``pack(sheet_w, sheet_h) -> Packing``
callable, so the same costing serves both packers:

  * ``core.rect_nesting.rect_options``   — bounding-box packer (manual calculator)
  * ``core.sparrow.sparrow_options``       — Sparrow shape nesting (DXF tab)
"""

from __future__ import annotations

from dataclasses import dataclass

from core.pricing import get_sizes_for_material, weight_kg


@dataclass
class Packing:
    """How one sheet size was packed, as reported by a packer.

    ``eff_w × eff_h`` is the usable area after the clamp strip and
    ``draw_w × draw_h`` the sheet as laid out (a packer may turn it).
    ``failed`` counts pieces that did not fit; ``alt`` optionally carries the
    other sheet orientation's layout for display.
    """

    sheets: list
    sheets_needed: int
    eff_w: int
    eff_h: int
    draw_w: int
    draw_h: int
    failed: int = 0
    reason: str = ""
    alt: dict | None = None


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


def effective_sheet(sw: int, sh: int, long_side_clamp_mm: int) -> tuple[int, int]:
    """Shrink the sheet's short side by the claw strip on the long edge."""
    if sw >= sh:
        return sw, max(0, sh - long_side_clamp_mm)
    return max(0, sw - long_side_clamp_mm), sh


def utilization(part_area_mm2: float, sheet_w: float, sheet_h: float, n_sheets: int = 1) -> float:
    """Share of the bought sheet area that ends up as parts (Käyttöaste).

    ``part_area_mm2`` is the real part area — without the cut gap, and with the
    holes removed for DXF parts. The clamp strip counts as sheet area, because
    it is paid for. Every utilisation figure in the app uses this one formula.
    """
    total = sheet_w * sheet_h * n_sheets
    return part_area_mm2 / total if total else 0.0


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
) -> dict:
    """Compare every priced sheet size for one material+thickness group.

    ``part_area_mm2`` is the total real part area of the group (see
    ``utilization``); the piece weight the sheet cost is spread over is derived
    from it, as the per-piece summary does, so the two totals reconcile.
    ``on_progress``, if given, is called as ``on_progress("size", index=i,
    count=n, w=sw, h=sh)`` before each sheet size is packed.

    Returns ``{"rows", "n_pieces", "pieces_kg", "has_pieces", "has_candidates"}``.
    Each row carries display-ready strings, underscore-prefixed raw values
    (``_total``, ``_ppt``, ``_failed``, ``_sheets``, ``_sw``/``_sh``, …) and a
    ``_breakdown`` dict feeding the step-by-step price explanation.
    """
    if n_pieces <= 0:
        return {"rows": [], "n_pieces": 0, "pieces_kg": 0.0,
                "has_pieces": False, "has_candidates": False}

    candidates: list[tuple[int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((w_mm, h_mm, price))

    if not candidates:
        return {"rows": [], "n_pieces": n_pieces, "pieces_kg": 0.0,
                "has_pieces": True, "has_candidates": False}

    pieces_kg = weight_kg(part_area_mm2, thickness_mm, material)

    rows = []
    for index, (sw, sh, price_per_tonne) in enumerate(candidates):
        if on_progress is not None:
            on_progress("size", index=index, count=len(candidates), w=sw, h=sh)
        packing = pack(sw, sh)
        adjusted_ppt = price_per_tonne * (1 + margin_pct / 100)
        if packing.failed:
            rows.append(_failed_row(sw, sh, adjusted_ppt, packing))
            continue

        sheets_needed = packing.sheets_needed
        sheet_weight_kg = weight_kg(sw * sh, thickness_mm, material)
        util = utilization(part_area_mm2, sw, sh, sheets_needed)
        sheet_kg = sheet_weight_kg * sheets_needed
        billable_kg = sheet_kg
        total_eur = adjusted_ppt * (billable_kg / 1000)
        cost_per_pc = round(total_eur / n_pieces, 2)
        # Effective rate to apply against piece weight so the pieces-summary
        # totals add up to the sheet-usage total.
        bill_rate_ppt = (
            adjusted_ppt * (sheet_kg / pieces_kg) if pieces_kg else adjusted_ppt
        )

        rows.append({
            "Levykoko":          f"{fmt_m(sw)} × {fmt_m(sh)} m",
            "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
            "Tarvittavat levyt": sheets_needed,
            "Käyttöaste":        f"{util * 100:.1f} %",
            "Levyn kg":          round(sheet_kg, 2),
            "Laskutettava kg":   round(billable_kg, 2),
            "Yhteensä €":        round(total_eur, 2),
            "€/kpl":             cost_per_pc,
            "_total":         total_eur,
            "_ppt":           bill_rate_ppt,
            "_failed":        0,
            "_utilization":   util,
            "_sheets":        packing.sheets,
            "_eff_w":         packing.eff_w,
            "_eff_h":         packing.eff_h,
            "_sw":            packing.draw_w,
            "_sh":            packing.draw_h,
            "_alt":           packing.alt,
            "_breakdown": {
                "sw":              sw,
                "sh":              sh,
                "base_ppt":        price_per_tonne,
                "adjusted_ppt":    adjusted_ppt,
                "sheet_weight_kg": sheet_weight_kg,
                "sheets_needed":   sheets_needed,
                "sheet_kg":        sheet_kg,
                "pieces_kg":       pieces_kg,
                "billable_kg":     billable_kg,
                "total_eur":       total_eur,
                "cost_per_pc":     cost_per_pc,
            },
        })

    return {"rows": rows, "n_pieces": n_pieces, "pieces_kg": pieces_kg,
            "has_pieces": True, "has_candidates": True}


def _failed_row(sw: int, sh: int, adjusted_ppt: float, packing: Packing) -> dict:
    """A blanked row for a sheet size the pieces don't fit (kept for display)."""
    return {
        "Levykoko":          f"{fmt_m(sw)} × {fmt_m(sh)} m",
        "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
        "Tarvittavat levyt": "",
        "Käyttöaste":        "",
        "Levyn kg":          "",
        "Laskutettava kg":   "",
        "Yhteensä €":        "",
        "€/kpl":             "",
        "_total":       float("inf"),
        "_ppt":         None,
        "_failed":      packing.failed,
        "_utilization": 0.0,
        "_sheets":      [],
        "_reason":      packing.reason,
        "_sw":          sw,
        "_sh":          sh,
    }


def cheapest_index(rows: list[dict]) -> int | None:
    """Index of the cheapest fully-fitting row, tie-broken by utilisation."""
    valid = [i for i, r in enumerate(rows) if r["_failed"] == 0]
    if not valid:
        return None
    return min(valid, key=lambda i: (rows[i]["_total"], -rows[i]["_utilization"]))
