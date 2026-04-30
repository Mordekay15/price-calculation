"""
view/calculator.py
==================
Price calculator tab.

Flow:
  1. User picks a material name.
  2. Available sizes for that material appear.
  3. Available thicknesses for that material+size appear.
  4. Price per tonne and per kg is displayed.
  5. User adds one or more products (width × height in mm).
     Weight and cost are calculated per piece and in total.
"""

import streamlit as st
from core.calculator import (
    build_lookup,
    get_materials,
    get_sizes_for_material,
    get_thicknesses_for_material_size,
    parse_thickness_mm,
    piece_weight_kg,
)

_DEFAULT_PRODUCT = {"width": 1000.0, "height": 2000.0, "qty": 1}


def _init_products() -> None:
    if "calc_products" not in st.session_state:
        st.session_state.calc_products = [_DEFAULT_PRODUCT.copy()]


def render(data: dict) -> None:
    lookup = build_lookup(data)

    if not lookup:
        st.info("No price data available.")
        return

    st.subheader("Price calculator")

    # ── Step 1: Material ──────────────────────────────────────────────────────

    materials = get_materials(lookup)
    material = st.selectbox("1. Material", materials)

    # ── Step 2: Size ──────────────────────────────────────────────────────────

    sizes = get_sizes_for_material(lookup, material)
    if sizes:
        size = st.selectbox("2. Sheet size", sizes)
    else:
        size = ""
        st.caption("No size variants available for this material.")

    # ── Step 3: Thickness ─────────────────────────────────────────────────────

    thicknesses = get_thicknesses_for_material_size(lookup, material, size)
    if not thicknesses:
        st.warning("No price data found for this combination.")
        return

    thickness = st.selectbox("3. Thickness (mm)", thicknesses)

    # ── Price display ─────────────────────────────────────────────────────────

    target_label = f"{material} | {size}" if size else material
    price_per_tonne = lookup.get((thickness, target_label))

    if price_per_tonne is None:
        st.warning("No price found for this combination.")
        return

    price_per_kg = price_per_tonne / 1000
    thickness_mm = parse_thickness_mm(thickness)

    st.divider()
    c1, c2 = st.columns(2)
    c1.metric("Price (€/tonne)", f"{price_per_tonne:,.2f}")
    c2.metric("Price (€/kg)", f"{price_per_kg:.4f}")

    # ── Products ──────────────────────────────────────────────────────────────

    st.divider()
    st.markdown("**Products**")
    st.caption(
        f"Material locked to: **{material}**"
        + (f" · **{size}**" if size else "")
        + f" · **{thickness} mm**"
    )

    _init_products()

    if st.button("＋ Add product"):
        st.session_state.calc_products.append(_DEFAULT_PRODUCT.copy())

    to_delete = None
    total_weight_kg = 0.0
    total_cost_eur = 0.0
    table_rows = []

    for i, prod in enumerate(st.session_state.calc_products):
        with st.container():
            hdr_cols = st.columns([6, 1])
            hdr_cols[0].markdown(f"**Product {i + 1}**")
            if len(st.session_state.calc_products) > 1:
                if hdr_cols[1].button("✕ Remove", key=f"del_{i}"):
                    to_delete = i

            inp_cols = st.columns(3)
            w = inp_cols[0].number_input(
                "Width (mm)", min_value=1.0, value=float(prod["width"]),
                step=10.0, key=f"w_{i}",
            )
            h = inp_cols[1].number_input(
                "Height (mm)", min_value=1.0, value=float(prod["height"]),
                step=10.0, key=f"h_{i}",
            )
            q = inp_cols[2].number_input(
                "Quantity (pcs)", min_value=1, value=int(prod["qty"]),
                step=1, key=f"q_{i}",
            )

            st.session_state.calc_products[i]["width"] = w
            st.session_state.calc_products[i]["height"] = h
            st.session_state.calc_products[i]["qty"] = q

            if thickness_mm:
                one_weight = piece_weight_kg(w, h, thickness_mm)
                batch_weight = one_weight * q
                batch_cost = batch_weight * price_per_kg
                total_weight_kg += batch_weight
                total_cost_eur += batch_cost
                table_rows.append({
                    "#": i + 1,
                    "Width (mm)": w,
                    "Height (mm)": h,
                    "Qty (pcs)": q,
                    "kg/pc": round(one_weight, 3),
                    "Total kg": round(batch_weight, 3),
                    "Total €": round(batch_cost, 2),
                })
                res_cols = st.columns(3)
                res_cols[0].caption(f"Per piece: **{one_weight:.3f} kg**")
                res_cols[1].caption(f"Batch weight: **{batch_weight:.3f} kg**")
                res_cols[2].caption(f"Batch cost: **{batch_cost:.2f} €**")

    if to_delete is not None:
        st.session_state.calc_products.pop(to_delete)
        st.rerun()

    # ── Totals ────────────────────────────────────────────────────────────────

    if table_rows:
        st.divider()
        st.markdown("**Summary**")
        t1, t2 = st.columns(2)
        t1.metric("Total weight (kg)", f"{total_weight_kg:.3f}")
        t2.metric("Total cost (€)", f"{total_cost_eur:.2f}")

        st.dataframe(table_rows, use_container_width=True, hide_index=True)
