"""The manual calculator tab ("Hintalaskuri"): rectangular product cards, their
sheet usage per material + thickness group (bounding-box packer) and the
pieces summary."""

import uuid

import streamlit as st

from core.pricing import build_lookup, parse_thickness_mm
from core.rect_nesting import rect_options
from core.sheet_cost import group_products
from view.common import (
    ADVANCED_LABEL,
    NESTING_LABELS,
    materials_with_copper,
    render_groups,
    render_main_settings,
    render_material_thickness,
    render_nesting_inputs,
    render_pieces_summary,
    settings_summary,
)
from view.drawing import draw_rect_layout
from view.sheet_usage import render_group


def render(data: dict) -> None:
    lookup = build_lookup(data)

    products = _render_products(materials_with_copper(lookup), lookup)

    st.divider()
    margin_pct, nest_mode = render_main_settings(margin_key="calc_margin_pct")
    with st.expander(ADVANCED_LABEL):
        sheet = render_nesting_inputs()
    st.caption(settings_summary(sheet.values(), NESTING_LABELS))

    def render_one(key, prods):
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        result = rect_options(
            lookup, material, thickness, thickness_mm, prods,
            margin_pct=margin_pct, edges=sheet.gaps(), rankavali_mm=sheet.rankavali_mm,
        )
        ids = "-".join(str(p["id"]) for p in prods)
        return render_group(
            material, thickness, thickness_mm, result, margin_pct=margin_pct,
            key=f"sheet_select::{material}::{thickness}::{ids}",
            draw_layout=lambda option: draw_rect_layout(option, prods, sheet.rankavali_mm),
        )

    groups = group_products(products, nest_mode)
    prices: dict[str, float] = {}
    if groups:
        st.divider()
        prices, _, _ = render_groups(groups, render_one)
    render_pieces_summary(products, prices, title="Yhteenveto",
                          weight_label="Kappaleiden yhteispaino (kg)", lead="#")


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

    prod["material"]  = material
    prod["thickness"] = thickness
    prod["width"]     = w
    prod["height"]    = h
    prod["qty"]       = q
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
