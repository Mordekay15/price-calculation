"""
view/tight_nesting_view.py
==========================
Shape-aware ("tight") nesting for the DXF page. Where the box path reuses
view/sheet_usage_view.render_group(), the tight path packs the parts' real
outlines so they interlock — less waste, slower to compute.

render_tight_options() draws the angle/quality selectors (only shown when the
user picked tight nesting). render_tight_group() compares every priced sheet
size for one material+thickness group using the tight nester, renders the
comparison table, headline metrics, per-part cost split and the placement
layout, and returns the active row's ``(total_eur, price_per_tonne)`` — matching
render_group()'s contract so the DXF page can treat both packing modes alike.

_run_nest() is cached on geometry (not price), so re-pricing does not re-nest.
"""

import streamlit as st

from core import true_nesting as tn
from core.calculator import (
    calculate,
    density_for_material,
    get_sizes_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.copper import COPPER_MATERIAL
from core.nesting import parse_size
from core.sheet_usage import _fmt_m
from view.sheet_usage_view import _PRODUCT_PALETTE, _render_breakdown

# Rotation presets for tight nesting: label -> angles tried per piece.
_ANGLE_PRESETS: dict[str, tuple[float, ...]] = {
    "0 / 90°":               (0.0, 90.0),
    "0 / 45 / 90 / 135°":    (0.0, 45.0, 90.0, 135.0),
    "30° välein":            (0.0, 30.0, 60.0, 90.0, 120.0, 150.0),
    "15° välein":            tuple(float(a) for a in range(0, 180, 15)),
}
_QUALITY_RES = {"Normaali (nopeampi)": 3.0, "Tarkka (hitaampi)": 2.0}


def render_tight_options() -> tuple[tuple[float, ...], float]:
    """Angle-preset and resolution selectors for tight nesting.

    Returns ``(angles, res)`` to feed render_tight_group().
    """
    tcols = st.columns(2)
    angle_label = tcols[0].selectbox(
        "Sallitut kiertokulmat",
        options=list(_ANGLE_PRESETS.keys()),
        index=1,
        key="dxf_angles",
        help="Enemmän kulmia = tiiviimpi tulos mutta hitaampi laskenta.",
    )
    quality_label = tcols[1].selectbox(
        "Laskennan tarkkuus",
        options=list(_QUALITY_RES.keys()),
        index=0,
        key="dxf_quality",
    )
    return _ANGLE_PRESETS[angle_label], _QUALITY_RES[quality_label]


@st.cache_data(show_spinner=False)
def _run_nest(parts_key, sheet_w, sheet_h, res, angles, kerf_mm, clamp_mm):
    """Cached wrapper around the tight nester (keyed by geometry, not price)."""
    parts = [
        {"idx": idx, "polylines": [list(poly) for poly in polys], "qty": qty}
        for (idx, qty, polys) in parts_key
    ]
    return tn.nest(
        parts, sheet_w, sheet_h,
        res=res, angles=angles, kerf_mm=kerf_mm, long_side_clamp_mm=clamp_mm,
    )


def _priced_candidates(lookup: dict, material: str, thickness: str) -> list[tuple]:
    """Sheet sizes with a price for this material+thickness: (label, w, h, price)."""
    candidates = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))
    return candidates


def _nest_rows(candidates, parts_key, material, thickness_mm, n_pieces, pieces_kg,
               margin_pct, rankavali_mm, long_side_clamp_mm, angles, res) -> list[dict]:
    """Run the tight nester for each candidate sheet size and build table rows."""
    rows = []
    for _size_label, sw, sh, price in candidates:
        result = _run_nest(
            parts_key, sw, sh, res, tuple(angles),
            float(rankavali_mm), int(long_side_clamp_mm),
        )
        sheets_needed = len(result.sheets)
        has_fail = result.failed > 0 or sheets_needed == 0
        used_area = sum(s.used_area for s in result.sheets)
        util = used_area / (sw * sh * sheets_needed) if sheets_needed else 0.0
        sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
        sheet_kg = sheet_weight_kg * sheets_needed
        billable_kg = sheet_kg
        calc = calculate(price, billable_kg / 1000, margin_pct=margin_pct)
        adjusted_ppt = calc["after_margin"]
        total_eur = calc["total"]
        cost_per_pc = round(total_eur / n_pieces, 2) if (not has_fail and n_pieces) else ""
        bill_rate_ppt = adjusted_ppt * (sheet_kg / pieces_kg) if pieces_kg else adjusted_ppt
        rows.append({
            "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
            "Tarvittavat levyt": "" if has_fail else sheets_needed,
            "Käyttöaste":        "" if has_fail else f"{util * 100:.1f} %",
            "Levyn kg":          "" if has_fail else round(sheet_kg, 2),
            "Yhteensä €":        "" if has_fail else round(total_eur, 2),
            "€/kpl":             cost_per_pc,
            "_total":       total_eur,
            "_ppt":         bill_rate_ppt,
            "_failed":      result.failed,
            "_util":        util,
            "_sheets":      result.sheets,
            "_sw":          sw,
            "_sh":          sh,
            "_breakdown": {
                "sw": sw, "sh": sh, "base_ppt": price, "adjusted_ppt": adjusted_ppt,
                "sheet_weight_kg": sheet_weight_kg, "sheets_needed": sheets_needed,
                "sheet_kg": sheet_kg, "pieces_kg": pieces_kg, "billable_kg": billable_kg,
                "total_eur": total_eur, "cost_per_pc": cost_per_pc,
            },
        })
    return rows


def render_tight_group(
    lookup: dict,
    material: str,
    thickness: str,
    products: list[dict],
    margin_pct: float,
    long_side_clamp_mm: int,
    rankavali_mm: int,
    angles: tuple[float, ...],
    res: float,
) -> tuple[float | None, float | None]:
    """Compare priced sheet sizes using shape-aware nesting for one group."""
    thickness_mm = parse_thickness_mm(thickness)
    if thickness_mm is None:
        return None, None
    n_pieces = sum(int(p["qty"]) for p in products)
    if n_pieces == 0:
        return None, None

    st.markdown(f"**{material}** · **{thickness} mm**")

    candidates = _priced_candidates(lookup, material, thickness)
    if not candidates:
        if material == COPPER_MATERIAL:
            st.info("Aseta kuparin hinta (€/kg) sivupalkista, niin levylaskenta tulee näkyviin.")
        else:
            st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")
        return None, None

    pieces_kg = sum(
        piece_weight_kg(p["width"], p["height"], thickness_mm, material) * p["qty"]
        for p in products
    )

    parts_key = tuple(
        (i, int(p["qty"]),
         tuple(tuple((round(x, 3), round(y, 3)) for x, y in poly) for poly in p["_polylines"]))
        for i, p in enumerate(products)
    )

    with st.spinner("Lasketaan tiivistä muotonestausta…"):
        rows = _nest_rows(
            candidates, parts_key, material, thickness_mm, n_pieces, pieces_kg,
            margin_pct, rankavali_mm, long_side_clamp_mm, angles, res,
        )

    valid = [i for i, r in enumerate(rows) if r["_failed"] == 0]
    cheapest_idx = (
        min(valid, key=lambda i: (rows[i]["_total"], -rows[i]["_util"])) if valid else None
    )
    for i, r in enumerate(rows):
        r["Paras"] = "🚫" if r["_failed"] > 0 else ("◀ edullisin" if i == cheapest_idx else "")

    display_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    select_key = f"tight_select::{material}::{thickness}::" + "-".join(str(p["id"]) for p in products)
    event = st.dataframe(
        display_rows, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row", key=select_key,
    )

    selected_idx = cheapest_idx
    sel_rows = list(getattr(event.selection, "rows", []) or [])
    if sel_rows:
        idx = sel_rows[0]
        if 0 <= idx < len(rows) and rows[idx]["_failed"] == 0:
            selected_idx = idx
        else:
            st.warning(f"**{rows[idx]['Levykoko']}** ei kelpaa — osia ei saatu mahtumaan.")
    if selected_idx is None:
        st.warning("Osia ei saatu mahtumaan millekään levykoolle.")
        return None, None

    active = rows[selected_idx]
    if cheapest_idx is not None:
        c = rows[cheapest_idx]
        if selected_idx != cheapest_idx:
            st.info(
                f"Valittu: **{active['Levykoko']}** "
                f"(+{active['_total'] - c['_total']:,.2f} € vs. {c['Levykoko']})."
            )
        else:
            st.success(
                f"Edullisin: **{c['Levykoko']}** — {c['Tarvittavat levyt']} levyä, "
                f"käyttöaste {c['Käyttöaste']}."
            )

    avg = active["_total"] / n_pieces if n_pieces else 0.0
    mc = st.columns([2, 1, 1, 1])
    mc[0].metric("Materiaalikulu €/kpl (ka.)", f"{avg:,.2f} €")
    mc[1].metric("Yhteensä €", f"{active['_total']:,.2f}")
    mc[2].metric("Levyjä", str(active["Tarvittavat levyt"]))
    mc[3].metric("Käyttöaste", active["Käyttöaste"] or "—")

    if len(products) >= 2:
        brk = []
        for prod in products:
            one_kg = piece_weight_kg(prod["width"], prod["height"], thickness_mm, material)
            unit = one_kg * active["_ppt"] / 1000
            brk.append({
                "Osa": prod.get("name") or f"#{prod['_global_idx'] + 1}",
                "Mitat (mm)": f"{prod['width']:g} × {prod['height']:g}",
                "Määrä (kpl)": prod["qty"],
                "€/kpl": f"{unit:,.2f}",
                "Yhteensä €": f"{unit * prod['qty']:,.2f}",
            })
        st.caption("Materiaalikulu osittain:")
        st.dataframe(brk, use_container_width=True, hide_index=True)

    _render_tight_layout(active["_sheets"], active["_sw"], active["_sh"],
                         products, long_side_clamp_mm)
    _render_breakdown(material, thickness, thickness_mm, margin_pct, n_pieces, active["_breakdown"])
    return active["_total"], active["_ppt"]


def _render_tight_layout(sheets, sheet_w, sheet_h, products, long_side_clamp_mm):
    if not sheets:
        return
    st.markdown(f"**Sijoittelu** — {len(sheets)} levyä")

    legend_bits = []
    for prod in products:
        color = _PRODUCT_PALETTE[prod["_global_idx"] % len(_PRODUCT_PALETTE)]
        dims = f"{prod['width']:g}×{prod['height']:g}"
        name = prod.get("name")
        label = f"{name} ({dims})" if name else f"Tuote #{prod['_global_idx'] + 1} ({dims})"
        legend_bits.append(
            f'<span style="display:inline-flex;align-items:center;margin-right:14px;'
            f'font-size:13px;"><span style="display:inline-block;width:14px;height:14px;'
            f'background:{color};opacity:0.55;border:2px solid {color};margin-right:6px;'
            f'border-radius:2px;"></span>{label}</span>'
        )
    st.markdown(f'<div style="margin-bottom:8px;">{"".join(legend_bits)}</div>', unsafe_allow_html=True)

    target_px = 460
    scale = target_px / max(sheet_w, sheet_h)
    cols_per_row = min(4, len(sheets))
    for row_start in range(0, len(sheets), cols_per_row):
        row = sheets[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for ci, sheet in enumerate(row):
            with cols[ci]:
                st.markdown(
                    f"**Levy {row_start + ci + 1}** · käyttöaste {sheet.utilization * 100:.1f} %"
                )
                st.markdown(
                    _tight_sheet_svg(sheet, sheet_w, sheet_h, sheet_w * scale,
                                     sheet_h * scale, products, long_side_clamp_mm),
                    unsafe_allow_html=True,
                )


def _tight_sheet_svg(sheet, sheet_w, sheet_h, px_w, px_h, products, clamp):
    parts = [
        f'<svg width="{px_w:.0f}" height="{px_h:.0f}" viewBox="0 0 {sheet_w} {sheet_h}" '
        f'preserveAspectRatio="xMidYMid meet" style="background:#f8fafc;border:2px solid '
        f'#475569;border-radius:4px;display:block;max-width:100%;height:auto;">'
    ]
    # Claw strip (unusable), matching where the nester blocked the sheet.
    if clamp > 0:
        if sheet_w >= sheet_h:
            parts.append(f'<rect x="0" y="0" width="{sheet_w}" height="{clamp}" '
                         f'fill="#cbd5e1" fill-opacity="0.5"/>')
        else:
            parts.append(f'<rect x="{sheet_w - clamp}" y="0" width="{clamp}" height="{sheet_h}" '
                         f'fill="#cbd5e1" fill-opacity="0.5"/>')

    for placed in sheet.placed:
        prod = products[placed.part_idx]
        gidx = prod["_global_idx"]
        color = _PRODUCT_PALETTE[gidx % len(_PRODUCT_PALETTE)]
        segs = []
        for poly in placed.polylines:
            if len(poly) < 2:
                continue
            d = "M " + " L ".join(
                f"{placed.x_mm + x:.1f} {sheet_h - (placed.y_mm + y):.1f}" for x, y in poly
            ) + " Z"
            segs.append(d)
        parts.append(
            f'<path d="{" ".join(segs)}" fill="{color}" fill-opacity="0.55" '
            f'fill-rule="evenodd" stroke="{color}" stroke-width="4" stroke-linejoin="round"/>'
        )
        cx = placed.x_mm + placed.w_mm / 2
        cy = sheet_h - (placed.y_mm + placed.h_mm / 2)
        fs = max(36.0, min(placed.w_mm, placed.h_mm) / 5.5)
        parts.append(
            f'<text x="{cx:.0f}" y="{cy:.0f}" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="sans-serif" font-size="{fs:.0f}" font-weight="700" '
            f'fill="#0f172a">#{gidx + 1}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
