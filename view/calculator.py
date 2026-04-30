"""
views/calculator.py
===================
Price calculator tab.

To add new inputs (e.g. weight per m², delivery cost):
  - Add a st.number_input in the Adjustments section
  - Pass the new value into core.calculator.calculate()
"""

import streamlit as st
from core.calculator import (
    build_lookup,
    calculate,
    compare_thicknesses,
    sorted_products,
    sorted_thicknesses,
)


def render(data: dict) -> None:
    lookup = build_lookup(data)

    if not lookup:
        st.info("No price data available.")
        return

    st.subheader("Price calculator")
    st.caption("Pick a material, enter quantity, and apply any adjustments.")

    # ── Selectors ─────────────────────────────────────────────────────────────

    col1, col2 = st.columns(2)

    with col1:
        product   = st.selectbox("Material / product", sorted_products(lookup))
        available = sorted_thicknesses(lookup, product)
        thickness = st.selectbox("Thickness (mm)", available or ["—"])

    with col2:
        quantity = st.number_input(
            "Quantity (tonnes)", min_value=0.0, value=10.0, step=0.5
        )

    # ── Adjustments ───────────────────────────────────────────────────────────

    st.divider()
    st.markdown("**Adjustments**")

    col3, col4, col5 = st.columns(3)
    with col3:
        margin_pct = st.number_input(
            "Margin %", min_value=0.0, max_value=200.0, value=0.0, step=0.5,
            help="Added on top of the base price",
        )
    with col4:
        surcharge = st.number_input(
            "Extra surcharge (€/tn)", min_value=0.0, value=0.0, step=5.0,
            help="E.g. custom cut sizes +30 €/tn",
        )
    with col5:
        fx_rate = st.number_input(
            "Currency multiplier", min_value=0.01, value=1.0, step=0.01,
            help="1.0 = keep EUR. Use e.g. 1.08 to convert to another currency.",
        )

    # ── Result ────────────────────────────────────────────────────────────────

    base_price = lookup.get((thickness, product))

    if base_price is None:
        st.warning("No price found for this combination.")
        return

    result = calculate(base_price, quantity, margin_pct, surcharge, fx_rate)

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Base price (€/tn)",         f"{result['base_price']:,.2f}")
    m2.metric("After margin & surcharge",  f"{result['after_surcharge']:,.2f}")
    m3.metric("After currency adj.",       f"{result['after_fx']:,.2f}")
    m4.metric(f"Total for {quantity} tn",  f"{result['total']:,.2f}")

    st.divider()

    st.markdown(f"**All thicknesses for** *{product}*")
    comp = compare_thicknesses(
        lookup, product, quantity, margin_pct, surcharge, fx_rate, thickness
    )
    st.dataframe(comp, use_container_width=True, hide_index=True)
