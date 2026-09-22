"""
view/pieces_summary_view.py
===========================
The per-piece summary table at the bottom of both the calculator and the DXF
nesting page: each piece's dimensions, per-piece and batch weight, and — when
the sheet-usage step found a price — the per-piece and batch cost, plus the
total weight and material-cost metrics.

render_pieces_summary() takes the products and the cheapest price-per-tonne per
product id (from view/sheet_usage_view.py). The ``from_dxf`` flag switches the
labels and the leading column: the manual calculator shows a running piece
number ("Kappaleyhteenveto"), while the DXF page shows each part's name
("Osayhteenveto"). The DXF-only name column never appears on the basic
calculator. Draws nothing when no product has valid dimensions.
"""

import streamlit as st
from core.calculator import density_for_material, parse_thickness_mm, piece_weight_kg


def render_pieces_summary(
    products: list[dict],
    cheapest_prices: dict[str, float],
    *,
    from_dxf: bool = False,
    areas_mm2: dict[str, float] | None = None,
) -> None:
    """The per-piece summary table plus its weight/cost totals.

    With ``from_dxf`` the leading column is the DXF part name and the section
    uses the "Osa" wording; otherwise it is a running piece number and the
    "Kappale" wording.

    ``areas_mm2`` maps a product id to its *net cut area* (mm², holes removed).
    A DXF part's real weight is that area × thickness × density — using the
    bounding box would count the empty gaps and holes and overstate the weight
    (a plate could then weigh more than the sheet it is cut from). Manual
    rectangles carry no entry here and fall back to width × height.
    """
    areas_mm2 = areas_mm2 or {}
    title        = "Osayhteenveto" if from_dxf else "Kappaleyhteenveto"
    weight_label = "Osien yhteispaino (kg)" if from_dxf else "Kappaleiden yhteispaino (kg)"

    table_rows      = []
    total_weight_kg = 0.0
    total_cost_eur  = 0.0
    for i, prod in enumerate(products):
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        thickness_mm = parse_thickness_mm(prod["thickness"]) if prod["thickness"] else None
        if thickness_mm is None:
            continue
        net_area = areas_mm2.get(prod["id"])
        if net_area is not None:
            one_weight = net_area * thickness_mm * density_for_material(prod["material"])
        else:
            one_weight = piece_weight_kg(prod["width"], prod["height"], thickness_mm, prod["material"])
        batch_weight = one_weight * prod["qty"]
        total_weight_kg += batch_weight

        ppt          = cheapest_prices.get(prod["id"])
        price_per_kg = ppt / 1000 if ppt else None
        one_cost     = round(one_weight   * price_per_kg, 4) if price_per_kg else ""
        batch_cost   = round(batch_weight * price_per_kg, 2) if price_per_kg else ""
        if price_per_kg:
            total_cost_eur += batch_weight * price_per_kg

        # DXF parts carry a name; manual calculator pieces get a running number.
        lead_col = {"Osa": prod["name"]} if from_dxf else {"#": i + 1}
        table_rows.append({
            **lead_col,
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
    st.markdown(f"**{title}**")
    m1, m2 = st.columns(2)
    m1.metric(weight_label, f"{total_weight_kg:.3f}")
    if total_cost_eur:
        m2.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost_eur:,.2f}")
    st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
