"""
core/sparrow_sheet_cost.py
==========================
Sheet-usage costing with the Sparrow nesting backend.

This is the Sparrow-powered twin of ``core/sheet_usage.compute_options``: for one
material + thickness group it compares every priced sheet size, but instead of
the bounding-box packer it uses ``core.sparrow_pack.greedy_fixed_sheets`` (real
shape nesting, one fixed sheet at a time). The output rows have the same shape as
``compute_options`` — display strings, underscore-prefixed raw values, and a
``_breakdown`` dict — so the view can reuse ``cheapest_index`` and the price
breakdown, and only the layout drawing differs (Sparrow placements vs. rects).

Pure and Streamlit-free. The Sparrow solver is an injected ``run_fn`` (so this is
unit-testable without the binary); the caller supplies parts already extracted
from the DXF files (``core.sparrow_input.SparrowPart``), which carry the outer
polygon, holes, demand, allowed rotations and the mm bounding box.
"""

from __future__ import annotations

from core.calculator import (
    calculate,
    density_for_material,
    get_sizes_for_material,
    piece_weight_kg,
)
from core.nesting import parse_size
from core.sheet_usage import _effective_sheet, _fmt_m
from core.sparrow_pack import greedy_fixed_sheets


def compute_options_sparrow(
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
) -> dict:
    """Compare every priced sheet size for one group, nesting with Sparrow.

    ``parts`` are ``SparrowPart``-like objects (``outer``, ``holes``,
    ``quantity``, ``allowed_orientations``, ``part_id``, ``width_mm``,
    ``height_mm``, ``shape_dict()``). Returns the same dict shape as
    ``core.sheet_usage.compute_options``: ``rows`` (each with ``_sheets`` set to
    the ``PackedSheet`` list), ``n_pieces``, ``pieces_kg``, ``has_pieces``,
    ``has_candidates``.
    """
    n_pieces = sum(int(getattr(p, "quantity", 1)) for p in parts)
    if not parts or n_pieces <= 0:
        return {"rows": [], "n_pieces": 0, "pieces_kg": 0.0,
                "has_pieces": False, "has_candidates": False}

    candidates: list[tuple[str, int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))

    if not candidates:
        return {"rows": [], "n_pieces": n_pieces, "pieces_kg": 0.0,
                "has_pieces": True, "has_candidates": False}

    pieces_kg = sum(
        piece_weight_kg(p.width_mm, p.height_mm, thickness_mm, material)
        * int(getattr(p, "quantity", 1))
        for p in parts
    )
    separation = float(rankavali_mm) if rankavali_mm else None

    rows = []
    for _size_label, sw, sh, price_per_tonne in candidates:
        # Try the sheet both ways round (portrait / landscape) and keep the
        # tighter fit — the same as rotating the whole nest 90°, so an elongated
        # part is not forced to run along the wrong sheet axis.
        pack, eff_w, eff_h, draw_w, draw_h = _pack_best_orientation(
            parts, sw, sh, long_side_clamp_mm,
            run_fn=run_fn, seed=seed, time_limit_sec=time_limit_sec,
            separation=separation,
        )
        if not pack.ok:
            rows.append(_failed_row(sw, sh, pack.reason))
            continue

        sheets_needed = pack.sheets_needed
        used_area = sum(s.used_area for s in pack.sheets)
        capacity = sheets_needed * eff_w * eff_h
        utilization = (used_area / capacity) if capacity else 0.0

        sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
        sheet_kg = sheet_weight_kg * sheets_needed
        billable_kg = sheet_kg
        result = calculate(price_per_tonne, billable_kg / 1000, margin_pct=margin_pct)
        adjusted_ppt = result["after_margin"]
        total_eur = result["total"]
        cost_per_pc = round(total_eur / n_pieces, 2) if n_pieces else ""
        bill_rate_ppt = (
            adjusted_ppt * (sheet_kg / pieces_kg) if pieces_kg else adjusted_ppt
        )

        rows.append({
            "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
            "Tarvittavat levyt": sheets_needed,
            "Käyttöaste":        f"{utilization * 100:.1f} %",
            "Levyn kg":          round(sheet_kg, 2),
            "Laskutettava kg":   round(billable_kg, 2),
            "Yhteensä €":        round(total_eur, 2),
            "€/kpl":             cost_per_pc,
            "_total":         total_eur,
            "_ppt":           bill_rate_ppt,
            "_failed":        0,
            "_utilization":   utilization,
            "_sheets":        pack.sheets,
            "_eff_w":         eff_w,
            "_eff_h":         eff_h,
            "_sw":            draw_w,
            "_sh":            draw_h,
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


def _pack_best_orientation(
    parts, sw, sh, clamp, *, run_fn, seed, time_limit_sec, separation
):
    """Pack the sheet in both orientations; return the better attempt.

    Returns ``(pack, eff_w, eff_h, draw_w, draw_h)`` where ``draw_w × draw_h`` is
    the chosen sheet layout (physical size, orientation-independent) and
    ``eff_w × eff_h`` its usable area after the clamp. The winner needs the fewest
    sheets, tie-broken by utilisation; if neither fits, the first attempt (with
    its failure reason) is returned.
    """
    def _try(cw, ch):
        ew, eh = _effective_sheet(cw, ch, clamp)
        pack = greedy_fixed_sheets(
            parts, ew, eh, run_fn=run_fn, seed=seed,
            time_limit_sec=time_limit_sec, separation=separation,
        )
        return pack, ew, eh, cw, ch

    options = [_try(sw, sh)]
    if sw != sh:
        options.append(_try(sh, sw))

    ok = [o for o in options if o[0].ok]
    if not ok:
        return options[0]

    def _key(o):
        pack, ew, eh = o[0], o[1], o[2]
        used = sum(s.used_area for s in pack.sheets)
        cap = pack.sheets_needed * ew * eh
        return (pack.sheets_needed, -(used / cap if cap else 0.0))

    return min(ok, key=_key)


def _failed_row(sw: int, sh: int, reason: str) -> dict:
    """A blanked row for a sheet size the parts don't fit (kept for display)."""
    return {
        "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
        "Hinta (€/tn)":      "",
        "Tarvittavat levyt": "",
        "Käyttöaste":        "",
        "Levyn kg":          "",
        "Laskutettava kg":   "",
        "Yhteensä €":        "",
        "€/kpl":             "",
        "_total":       float("inf"),
        "_ppt":         None,
        "_failed":      1,
        "_utilization": 0.0,
        "_sheets":      [],
        "_reason":      reason,
        "_sw":          sw,
        "_sh":          sh,
    }
