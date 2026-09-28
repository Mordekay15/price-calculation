"""
view/calculator.py
==================
Price calculator tab.

  1. The user adds products (view/product_view.py): material, thickness,
     width × height (mm) and quantity.
  2. "Levyn käyttö" groups the pieces by material + thickness (or per product
     with the "Sijoittelutapa" toggle) and, for each group, compares every
     priced sheet size with the bounding-box packer and highlights the cheapest
     (view/sheet_usage.py).
  3. The pieces summary lists every product's weight and cost.
"""

import streamlit as st

from core.calculator import build_lookup, parse_thickness_mm
from core.nesting import rect_options
from view.common import (
    group_products,
    materials_with_copper,
    render_grand_total,
    render_groups,
    render_margin,
    render_nesting_settings,
    render_pieces_summary,
)
from view.product_view import render_products
from view.sheet_usage import draw_rect_layout, render_group, render_mix_costs


def render(data: dict) -> None:
    lookup = build_lookup(data)
    st.subheader("Hintalaskuri")

    margin_pct = render_margin("calc_margin_pct")
    products = render_products(materials_with_copper(lookup), lookup)
    nest_mode, rankavali_mm, long_side_clamp_mm = render_nesting_settings()

    def render_one(key, prods):
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        result = rect_options(
            lookup, material, thickness, thickness_mm, prods,
            margin_pct=margin_pct, long_side_clamp_mm=long_side_clamp_mm,
            rankavali_mm=rankavali_mm,
        )

        def draw(active):
            render_mix_costs(active, prods, thickness_mm, material)
            draw_rect_layout(active, prods, rankavali_mm)

        ids = "-".join(str(p["id"]) for p in prods)
        return render_group(
            material, thickness, thickness_mm, result, margin_pct=margin_pct,
            key=f"sheet_select::{material}::{thickness}::{ids}", draw_layout=draw,
        )

    groups = group_products(products, nest_mode)
    prices: dict[str, float] = {}
    if groups:
        st.divider()
        st.markdown("**Levyn käyttö**")
        prices, grand_total, _ = render_groups(groups, render_one)
        render_grand_total(grand_total, len(groups))
    render_pieces_summary(products, prices)
