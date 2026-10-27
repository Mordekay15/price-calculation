"""The manual calculator tab ("Peruslaskuri"): rectangular product cards, their
sheet usage per material + thickness group (bounding-box packer) and the
pieces summary."""

import uuid

import streamlit as st

from core.pricing import build_lookup, parse_thickness_mm
from core.rect_nesting import rect_options
from core.sheet_cost import EdgeGaps, group_products, price_by_size, reprice
from core.work import rect_cut_length_m
from view.common import (
    materials_with_copper,
    render_groups,
    render_material_thickness,
    render_pieces_summary,
    render_settings,
)
from view.drawing import draw_rect_layout
from view.quote import render_batch_prices
from view.sheet_usage import render_group
from view.work import render_work_inputs, work_costs


def render(data: dict):
    """The manual calculator; returns its totals for the bar (``quote.Totals``)."""
    lookup = build_lookup(data)

    products = _render_products(materials_with_copper(lookup), lookup)

    margin_pct, nest_mode, sheet = render_settings(
        key_prefix="calc", separate_label="Laske jokainen tuote erikseen")

    def render_one(key, prods):
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        gaps = sheet.gaps()
        result = _options(
            lookup, material, thickness, thickness_mm,
            [{k: v for k, v in p.items() if k != "work"} for p in prods],
            (gaps.top, gaps.bottom, gaps.left, gaps.right), sheet.rankavali_mm,
        )
        if result is not None:
            # The margin only prices the plans: applied here, so changing it
            # is instant (one factor on every sheet keeps the same plans).
            result = reprice(result, material=material, thickness_mm=thickness_mm,
                             base_ppt=price_by_size(lookup, material, thickness),
                             margin_pct=margin_pct)
        ids = "-".join(str(p["id"]) for p in prods)
        return render_group(
            material, thickness, thickness_mm, result, margin_pct=margin_pct,
            key=f"sheet_select::{material}::{thickness}::{ids}",
            draw_layout=lambda option: draw_rect_layout(option, prods, sheet.rankavali_mm),
        )

    groups = group_products(products, nest_mode)
    prices: dict[str, float] = {}
    sheets: dict[tuple, int] = {}
    totals: dict[tuple, float] = {}
    if groups:
        st.divider()
        prices, _, _, sheets, totals = render_groups(groups, render_one)
    render_pieces_summary(products, prices,
                          weight_label="Kappaleiden yhteispaino (kg)", lead="#",
                          work=work_costs(products, groups, sheets))
    return render_batch_prices(products, prices, groups, sheets, totals, lead="#", key="calc",
                               margin_pct=margin_pct)


@st.cache_data(max_entries=64, show_spinner="Etsitään vähiten levyjä käyttävää suunnitelmaa…")
def _options(lookup, material, thickness, thickness_mm, prods, edges, rankavali_mm):
    """``rect_options`` without the margin, kept until a part or setting
    changes: the fewest-sheets search can take a few seconds on a large
    order. The caller applies the margin (``reprice``)."""
    return rect_options(lookup, material, thickness, thickness_mm, prods,
                        edges=EdgeGaps(*edges), rankavali_mm=rankavali_mm)


# ── Product cards ─────────────────────────────────────────────────────────────

def _new_product() -> dict:
    return {
        "id":        uuid.uuid4().hex,
        "material":  None,
        "thickness": None,
        "width":     0.0,
        "height":    0.0,
        "qty":       1,
    }


def _init_products() -> None:
    if "calc_products" not in st.session_state:
        st.session_state.calc_products = [_new_product()]


def _render_product(prod: dict, index: int, materials: list[str], lookup: dict) -> bool:
    """Render one product's inputs and write them back into ``prod``.

    Returns True if the user pressed this product's "Poista" (delete) button.
    """
    pid = prod["id"]
    delete_requested = False

    with st.container(border=True):
        hdr_cols = st.columns([6, 1])
        hdr_cols[0].markdown(f"**Tuote {index + 1}**")
        if len(st.session_state.calc_products) > 1:
            if hdr_cols[1].button("Poista", key=f"del_{pid}"):
                delete_requested = True

        # One row: Materiaali | Paksuus | Leveys | Korkeus | Määrä.
        mat_col, thick_col, w_col, h_col, q_col = st.columns([3, 2, 2, 2, 2])
        material, thickness = render_material_thickness(
            materials, lookup,
            mat_key=f"mat_{pid}", thick_key=f"th_{pid}",
            mat_default=prod["material"], thick_default=prod["thickness"],
            cols=(mat_col, thick_col),
        )
        w = w_col.number_input(
            "Leveys (mm)", min_value=0.0, value=float(prod["width"]),
            step=10.0, key=f"w_{pid}",
        )
        h = h_col.number_input(
            "Korkeus (mm)", min_value=0.0, value=float(prod["height"]),
            step=10.0, key=f"h_{pid}",
        )
        q = q_col.number_input(
            "Määrä (kpl)", min_value=1, value=int(prod["qty"]),
            step=1, key=f"q_{pid}",
        )

        work = render_work_inputs(pid, material=material, thickness=thickness, qty=q,
                                  cut_length_m=rect_cut_length_m(w, h))

    prod["material"]  = material
    prod["thickness"] = thickness
    prod["width"]     = w
    prod["height"]    = h
    prod["qty"]       = q
    prod["work"]      = work
    return delete_requested


def _render_products(materials: list[str], lookup: dict) -> list[dict]:
    """Render the "Tuotteet" section: every product card plus add/delete.

    Returns the current products list (the same object stored in
    ``st.session_state.calc_products``).
    """
    _init_products()

    st.markdown("**Tuotteet**")

    to_delete = None
    for i, prod in enumerate(st.session_state.calc_products):
        if _render_product(prod, i, materials, lookup):
            to_delete = i

    if st.button("+ Lisää tuote"):
        st.session_state.calc_products.append(_new_product())
        st.rerun()

    if to_delete is not None:
        st.session_state.calc_products.pop(to_delete)
        st.rerun()

    return st.session_state.calc_products
