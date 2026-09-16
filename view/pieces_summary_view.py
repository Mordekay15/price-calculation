"""
view/pieces_summary_view.py
===========================
The "Kappaleyhteenveto" section at the bottom of the calculator: a per-product
table with each piece's dimensions, per-piece and batch weight, and — when the
sheet-usage step found a price — the per-piece and batch cost, plus the total
weight and material-cost metrics.

render_pieces_summary() takes the products and the cheapest price-per-tonne
per product id (from view/sheet_usage_view.py, gathered in view/calculator.py)
and renders the table; it draws nothing when no product has valid dimensions.
"""

import streamlit as st
from core.calculator import parse_thickness_mm, piece_weight_kg


def render_pieces_summary(products: list[dict], cheapest_prices: dict[str, float]) -> None:
    """The "Kappaleyhteenveto" table plus its weight/cost totals."""
    table_rows      = []
    total_weight_kg = 0.0
    total_cost_eur  = 0.0
    for i, prod in enumerate(products):
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        thickness_mm = parse_thickness_mm(prod["thickness"]) if prod["thickness"] else None
        if thickness_mm is None:
            continue
        one_weight   = piece_weight_kg(prod["width"], prod["height"], thickness_mm, prod["material"])
        batch_weight = one_weight * prod["qty"]
        total_weight_kg += batch_weight

        ppt          = cheapest_prices.get(prod["id"])
        price_per_kg = ppt / 1000 if ppt else None
        one_cost     = round(one_weight   * price_per_kg, 4) if price_per_kg else ""
        batch_cost   = round(batch_weight * price_per_kg, 2) if price_per_kg else ""
        if price_per_kg:
            total_cost_eur += batch_weight * price_per_kg

        table_rows.append({
            "#":              i + 1,
            "Materiaali":     prod["material"] or "",
            "Paksuus":        prod["thickness"] or "",
            "Leveys (mm)":    prod["width"],
            "Korkeus (mm)":   prod["height"],
            "Määrä (kpl)":    prod["qty"],
            "kg/kpl":         round(one_weight,   3),
            "Yhteensä kg":    round(batch_weight, 3),
            "€/kpl":          one_cost,
            "Yhteensä €":     batch_cost,
        })

    if not table_rows:
        return

    st.divider()
    st.markdown("**Kappaleyhteenveto**")
    m1, m2 = st.columns(2)
    m1.metric("Kappaleiden yhteispaino (kg)", f"{total_weight_kg:.3f}")
    if total_cost_eur:
        m2.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost_eur:,.2f}")
    st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
