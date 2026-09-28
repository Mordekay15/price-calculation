"""
view/calculator.py
==================
Price calculator tab.

Flow:
  1. User adds products. For each product they pick material, thickness,
     width × height (mm) and quantity. The product cards themselves live in
     view/product_view.py.
  2. The "Sheet usage" section groups pieces by (material, thickness) and
     for each group compares every sheet size that has a price — sheets
     needed, utilisation, total cost — and highlights the cheapest option.
     A "Sijoittelutapa" toggle lets the user switch from this combined
     nesting to per-product nesting where each product gets its own sheet.
  3. The "Pieces summary" at the bottom lists every product with its
     per-piece and batch weight, plus the grand total.

``render`` is the public entry point; it wires together the per-section view
modules (view/margin_view.py, view/product_view.py,
view/nesting_settings_view.py, view/sheet_usage.py,
view/pieces_summary_view.py) and the small ``_render_*`` / ``_build_*`` helpers
below, each of which owns one section of the page.
"""

import streamlit as st

from core.calculator import build_lookup, get_materials, parse_thickness_mm
from core.copper import COPPER_MATERIAL
from core.nesting import rect_options
from view.margin_view import render_margin
from view.nesting_settings_view import render_nesting_settings
from view.pieces_summary_view import render_pieces_summary
from view.product_view import render_products
from view.sheet_usage import draw_rect_layout, render_group, render_mix_costs


def _build_groups(products: list[dict], nest_mode: str) -> dict[tuple, list[dict]]:
    """Group priced-and-sized products by (material, thickness).

    In "separate" mode each product gets its own group (id in the key) so it
    is nested on its own sheet. Products missing a material, thickness or a
    positive width/height are skipped.
    """
    groups: dict[tuple, list[dict]] = {}
    for i, prod in enumerate(products):
        if not prod["material"] or not prod["thickness"]:
            continue
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        if nest_mode == "separate":
            group_key = (prod["material"], prod["thickness"], prod["id"])
        else:
            group_key = (prod["material"], prod["thickness"])
        groups.setdefault(group_key, []).append({**prod, "_global_idx": i})
    return groups


def _render_sheet_usage(
    groups: dict[tuple, list[dict]],
    lookup: dict,
    margin_pct: float,
    rankavali_mm: int,
    long_side_clamp_mm: int,
) -> dict[str, float]:
    """Render the "Levyn käyttö" section for each group.

    Returns a mapping of product id → cheapest price-per-tonne, used by the
    pieces summary to split each sheet's cost across its pieces.
    """
    cheapest_prices: dict[str, float] = {}
    if not groups:
        return cheapest_prices

    st.divider()
    st.markdown("**Levyn käyttö**")

    grand_total_eur = 0.0
    any_priced      = False
    for group_key, group_prods in groups.items():
        material, thickness = group_key[0], group_key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            continue
        result = rect_options(
            lookup, material, thickness, thickness_mm, group_prods,
            margin_pct=margin_pct, long_side_clamp_mm=long_side_clamp_mm,
            rankavali_mm=rankavali_mm,
        )

        def draw(active, prods=group_prods, t_mm=thickness_mm, mat=material):
            render_mix_costs(active, prods, t_mm, mat)
            draw_rect_layout(active, prods, rankavali_mm)

        ids = "-".join(str(p["id"]) for p in group_prods)
        cheapest_eur, cheapest_ppt = render_group(
            material, thickness, thickness_mm, result, margin_pct=margin_pct,
            key=f"sheet_select::{material}::{thickness}::{ids}", draw_layout=draw,
        )
        if cheapest_eur is not None:
            grand_total_eur += cheapest_eur
            any_priced = True
        if cheapest_ppt is not None:
            for gp in group_prods:
                cheapest_prices[gp["id"]] = cheapest_ppt

    if any_priced and len(groups) > 1:
        st.divider()
        st.metric("Yhdistetty edullisin yhteissumma (€)", f"{grand_total_eur:,.2f}")

    return cheapest_prices


def render(data: dict) -> None:
    lookup = build_lookup(data)

    st.subheader("Hintalaskuri")

    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])

    margin_pct = render_margin("calc_margin_pct") #getting kate

    products = render_products(materials, lookup) #getting products
    nest_mode, rankavali_mm, long_side_clamp_mm = render_nesting_settings() # nesting setting input

    groups = _build_groups(products, nest_mode)
    cheapest_prices = _render_sheet_usage(
        groups, lookup, margin_pct, rankavali_mm, long_side_clamp_mm,
    )
    render_pieces_summary(products, cheapest_prices)
