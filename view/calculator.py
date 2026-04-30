"""
view/calculator.py
==================
Price calculator tab.

Flow:
  1. User picks a material.
  2. Available thicknesses for that material appear.
  3. User adds products (width × height in mm, default 0).
     Per-piece weight and total weight are calculated.
  4. The "Sheet usage" table compares every sheet size available for the
     chosen material + thickness — sheets needed, utilisation, total cost —
     and highlights the cheapest option.
"""

import streamlit as st
from core.calculator import (
    build_lookup,
    get_materials,
    get_sizes_for_material,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.nesting import expand_products, pack, parse_size, summarise

_PLACEHOLDER_MAT   = "— Select material —"
_PLACEHOLDER_THICK = "— Select thickness —"
_DEFAULT_PRODUCT   = {"width": 0.0, "height": 0.0, "qty": 1}

STEEL_DENSITY_KG_PER_MM3 = 7.85e-6


def _init_products() -> None:
    if "calc_products" not in st.session_state:
        st.session_state.calc_products = [_DEFAULT_PRODUCT.copy()]


def render(data: dict) -> None:
    lookup = build_lookup(data)

    if not lookup:
        st.info("No price data available.")
        return

    st.subheader("Price calculator")

    # ── Selectors ────────────────────────────────────────────────────────────

    material     = None
    thickness    = None
    thickness_mm = None

    mat_opts = [_PLACEHOLDER_MAT] + get_materials(lookup)
    mat_raw  = st.selectbox("1. Material", mat_opts, index=0)
    if mat_raw != _PLACEHOLDER_MAT:
        material = mat_raw

    if material is not None:
        thicknesses = get_thicknesses_for_material(lookup, material)
    else:
        thicknesses = []

    if thicknesses:
        th_opts = [_PLACEHOLDER_THICK] + thicknesses
        th_raw  = st.selectbox("2. Thickness (mm)", th_opts, index=0)
        if th_raw != _PLACEHOLDER_THICK:
            thickness    = th_raw
            thickness_mm = parse_thickness_mm(thickness)
    else:
        st.selectbox("2. Thickness (mm)", [_PLACEHOLDER_THICK], index=0, disabled=True)

    if thickness is None or thickness_mm is None:
        return

    # ── Products ──────────────────────────────────────────────────────────────

    st.divider()
    st.markdown("**Products**")
    st.caption(f"Material: **{material}** · Thickness: **{thickness} mm**")

    _init_products()

    to_delete       = None
    total_weight_kg = 0.0
    table_rows      = []

    for i, prod in enumerate(st.session_state.calc_products):
        with st.container():
            hdr_cols = st.columns([6, 1])
            hdr_cols[0].markdown(f"**Product {i + 1}**")
            if len(st.session_state.calc_products) > 1:
                if hdr_cols[1].button("Remove", key=f"del_{i}"):
                    to_delete = i

            inp_cols = st.columns(3)
            w = inp_cols[0].number_input(
                "Width (mm)", min_value=0.0, value=float(prod["width"]),
                step=10.0, key=f"w_{i}",
            )
            h = inp_cols[1].number_input(
                "Height (mm)", min_value=0.0, value=float(prod["height"]),
                step=10.0, key=f"h_{i}",
            )
            q = inp_cols[2].number_input(
                "Quantity (pcs)", min_value=1, value=int(prod["qty"]),
                step=1, key=f"q_{i}",
            )

            st.session_state.calc_products[i]["width"]  = w
            st.session_state.calc_products[i]["height"] = h
            st.session_state.calc_products[i]["qty"]    = q

            if w > 0 and h > 0:
                one_weight   = piece_weight_kg(w, h, thickness_mm)
                batch_weight = one_weight * q
                total_weight_kg += batch_weight
                table_rows.append({
                    "#":           i + 1,
                    "Width (mm)":  w,
                    "Height (mm)": h,
                    "Qty (pcs)":   q,
                    "kg/pc":       round(one_weight,   3),
                    "Total kg":    round(batch_weight, 3),
                })
                res_cols = st.columns(2)
                res_cols[0].caption(f"Per piece: **{one_weight:.3f} kg**")
                res_cols[1].caption(f"Batch weight: **{batch_weight:.3f} kg**")

    if st.button("+ Add product"):
        st.session_state.calc_products.append(_DEFAULT_PRODUCT.copy())
        st.rerun()

    if to_delete is not None:
        st.session_state.calc_products.pop(to_delete)
        st.rerun()

    # ── Summary + sheet usage ────────────────────────────────────────────────

    if table_rows:
        st.divider()
        st.markdown("**Pieces summary**")
        st.metric("Total piece weight (kg)", f"{total_weight_kg:.3f}")
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

        _render_sheet_usage(
            lookup=lookup,
            material=material,
            thickness=thickness,
            thickness_mm=thickness_mm,
        )


# ── Sheet usage comparison ────────────────────────────────────────────────────

def _fmt_m(mm: int) -> str:
    """Format a mm value as metres: 1000 -> '1.0', 1250 -> '1.25', 1500 -> '1.5'."""
    s = f"{mm / 1000:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _render_sheet_usage(lookup: dict, material: str, thickness: str,
                        thickness_mm: float) -> None:
    """
    Compare every sheet size available for the chosen material + thickness:
    how many physical sheets are needed for the entered products, sheet
    utilisation, and the cost of buying that many whole sheets.
    """
    products = st.session_state.get("calc_products", [])
    pieces   = expand_products(products)
    if not pieces:
        return

    candidates: list[tuple[str, int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))

    if not candidates:
        st.info(
            f"No sheet-size pricing available for {material} at {thickness} mm."
        )
        return

    st.divider()
    st.markdown("**Sheet usage** — which sheet size is cheapest")
    st.caption(
        "Pieces from different products share leftover space on the same sheet "
        "(rotation allowed). Packing is a greedy heuristic, so real-world "
        "savings may be slightly better with manual nesting."
    )

    rows = []
    for _size_label, sw, sh, price_per_tonne in candidates:
        sheets, failed  = pack(pieces, sw, sh, allow_rotation=True)
        summary         = summarise(sw, sh, sheets, len(failed))
        has_failures    = summary["failed_pieces"] > 0
        sheet_weight_kg = sw * sh * thickness_mm * STEEL_DENSITY_KG_PER_MM3
        total_kg        = sheet_weight_kg * summary["sheets_needed"]
        total_eur       = total_kg * (price_per_tonne / 1000)
        rows.append({
            "Sheet size":     f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Price (€/tn)":   f"{price_per_tonne:,.2f}",
            "Sheets needed":  "" if has_failures else summary["sheets_needed"],
            "Utilisation":    "" if has_failures else f"{summary['utilization'] * 100:.1f} %",
            "Sheet kg":       "" if has_failures else round(total_kg, 2),
            "Total €":        "" if has_failures else round(total_eur, 2),
            "_total":         total_eur,
            "_failed":        summary["failed_pieces"],
        })

    valid_rows = [r for r in rows if r["_failed"] == 0]
    if valid_rows:
        cheapest = min(valid_rows, key=lambda r: r["_total"])
        for r in rows:
            r["Best"] = "◀" if r is cheapest else ""

        st.success(
            f"Cheapest option: **{cheapest['Sheet size']}** — "
            f"{cheapest['Sheets needed']} sheet(s), "
            f"total **{cheapest['Total €']:,.2f} €** "
            f"at {cheapest['Utilisation']} utilisation."
        )
    else:
        for r in rows:
            r["Best"] = ""

    display_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in rows
    ]
    st.dataframe(display_rows, use_container_width=True, hide_index=True)
