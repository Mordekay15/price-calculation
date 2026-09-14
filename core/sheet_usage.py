"""
core/sheet_usage.py
===================
Pure sheet-usage costing — no Streamlit, no I/O.

Given a group of products that share a material and thickness, this compares
every sheet size that carries a price: how many sheets the pieces need, the
utilisation, and the total cost after margin. It is the single source of truth
behind both the manual price calculator (view/calculator.py) and the DXF
nesting section (view/dxf_nesting.py).

A "product" here is any dict with at least:
    width  (mm, float)   height (mm, float)   qty (int)
plus a "_global_idx" the caller uses to colour/label it, and optionally
"_polylines" (a real DXF outline) for drawing. Only width/height/qty are used
for the costing itself, so manual rectangles and DXF parts cost identically.
"""

from __future__ import annotations

from core.calculator import (
    calculate,
    density_for_material,
    get_sizes_for_material,
    piece_weight_kg,
)
from core.nesting import expand_products, pack, parse_size, summarise


def _inflate(pieces, rankavali_mm: int):
    """Grow each piece by the cut gap so the packer leaves room between parts."""
    if rankavali_mm <= 0:
        return pieces
    return [
        (p_idx, c_idx, w + rankavali_mm, h + rankavali_mm)
        for p_idx, c_idx, w, h in pieces
    ]


def _effective_sheet(sw: int, sh: int, long_side_clamp_mm: int) -> tuple[int, int]:
    """Shrink the sheet's short side by the claw strip on the long edge."""
    if sw >= sh:
        return sw, max(0, sh - long_side_clamp_mm)
    return max(0, sw - long_side_clamp_mm), sh


def compute_options(
    lookup: dict,
    material: str,
    thickness: str,
    thickness_mm: float,
    products: list[dict],
    margin_pct: float = 0.0,
    long_side_clamp_mm: int = 0,
    rankavali_mm: int = 0,
) -> dict:
    """Compare every priced sheet size for one material+thickness group.

    Returns a dict:
        {
          "rows":       list of per-sheet-size result rows (see below),
          "n_pieces":   total pieces across the group,
          "pieces_kg":  total real piece weight (kg),
          "has_pieces": bool,
          "has_candidates": bool,   # any priced sheet size exists
        }

    Each row carries display-ready strings, underscore-prefixed raw values for
    the caller (sheets, totals, effective dims), and a "_breakdown" dict feeding
    the step-by-step price explanation. The caller picks the cheapest row and
    handles all rendering; this function never touches the UI.
    """
    pieces = expand_products(products)
    if not pieces:
        return {
            "rows": [],
            "n_pieces": 0,
            "pieces_kg": 0.0,
            "has_pieces": False,
            "has_candidates": False,
        }

    pieces = _inflate(pieces, rankavali_mm)
    n_pieces = len(pieces)

    candidates: list[tuple[str, int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))

    if not candidates:
        return {
            "rows": [],
            "n_pieces": n_pieces,
            "pieces_kg": 0.0,
            "has_pieces": True,
            "has_candidates": False,
        }

    pieces_kg = sum(
        piece_weight_kg(p["width"], p["height"], thickness_mm, material) * p["qty"]
        for p in products
    )

    rows = []
    for _size_label, sw, sh, price_per_tonne in candidates:
        eff_w, eff_h = _effective_sheet(sw, sh, long_side_clamp_mm)
        sheets, failed = pack(pieces, eff_w, eff_h, allow_rotation=True)
        summary = summarise(sw, sh, sheets, len(failed))
        has_failures = summary["failed_pieces"] > 0

        sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
        sheet_kg = sheet_weight_kg * summary["sheets_needed"]
        billable_kg = sheet_kg
        result = calculate(price_per_tonne, billable_kg / 1000, margin_pct=margin_pct)
        adjusted_ppt = result["after_margin"]
        total_eur = result["total"]
        cost_per_pc = (
            round(total_eur / n_pieces, 2)
            if (not has_failures and n_pieces)
            else ""
        )
        # Effective rate to apply against piece weight so the pieces-summary
        # totals add up to the sheet-usage total.
        bill_rate_ppt = (
            adjusted_ppt * (sheet_kg / pieces_kg) if pieces_kg else adjusted_ppt
        )
        rows.append({
            "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
            "Tarvittavat levyt": "" if has_failures else summary["sheets_needed"],
            "Käyttöaste":        "" if has_failures else f"{summary['utilization'] * 100:.1f} %",
            "Levyn kg":          "" if has_failures else round(sheet_kg, 2),
            "Laskutettava kg":   "" if has_failures else round(billable_kg, 2),
            "Yhteensä €":        "" if has_failures else round(total_eur, 2),
            "€/kpl":             cost_per_pc,
            "_total":         total_eur,
            "_ppt":           bill_rate_ppt,
            "_failed":        summary["failed_pieces"],
            "_utilization":   summary["utilization"],
            "_sheets":        sheets,
            "_eff_w":         eff_w,
            "_eff_h":         eff_h,
            "_sw":            sw,
            "_sh":            sh,
            "_breakdown": {
                "sw":              sw,
                "sh":              sh,
                "base_ppt":        price_per_tonne,
                "adjusted_ppt":    adjusted_ppt,
                "sheet_weight_kg": sheet_weight_kg,
                "sheets_needed":   summary["sheets_needed"],
                "sheet_kg":        sheet_kg,
                "pieces_kg":       pieces_kg,
                "billable_kg":     billable_kg,
                "total_eur":       total_eur,
                "cost_per_pc":     cost_per_pc,
            },
        })

    return {
        "rows": rows,
        "n_pieces": n_pieces,
        "pieces_kg": pieces_kg,
        "has_pieces": True,
        "has_candidates": True,
    }


def cheapest_index(rows: list[dict]) -> int | None:
    """Index of the cheapest fully-fitting row, tie-broken by utilisation."""
    valid = [i for i, r in enumerate(rows) if r["_failed"] == 0]
    if not valid:
        return None
    return min(valid, key=lambda i: (rows[i]["_total"], -rows[i]["_utilization"]))


def _fmt_m(mm: int) -> str:
    """Format a mm value as metres: 1000 -> '1.0', 1250 -> '1.25', 1500 -> '1.5'."""
    s = f"{mm / 1000:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s
