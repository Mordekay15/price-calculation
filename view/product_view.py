"""
view/product_view.py
=====================
Product input section of the calculator: the list of product cards where the
user picks material, thickness, width × height (mm) and quantity, plus the
"+ Lisää tuote" / "Poista" controls.

This module owns the ``st.session_state.calc_products`` list. render_products()
is the entry point; view/calculator.py calls it once per rerun and then reads
the returned products for pricing and the summary table.
"""

import uuid

import streamlit as st
from core.calculator import get_thicknesses_for_material
from core.copper import COPPER_MATERIAL, COPPER_THICKNESSES

_PLACEHOLDER_MAT   = "— Valitse materiaali —"
_PLACEHOLDER_THICK = "— Valitse paksuus —"


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


def _thicknesses_for(lookup: dict, material: str | None) -> list[str]:
    """Thicknesses available for ``material`` (empty if none is picked)."""
    if material == COPPER_MATERIAL:
        # Copper's thicknesses are fixed and available even with no price.
        return COPPER_THICKNESSES
    if material is not None:
        return get_thicknesses_for_material(lookup, material)
    return []


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

        mat_opts = [_PLACEHOLDER_MAT] + materials
        mat_default = prod["material"] if prod["material"] in materials else _PLACEHOLDER_MAT
        mat_raw = st.selectbox(
            "Materiaali",
            mat_opts,
            index=mat_opts.index(mat_default),
            key=f"mat_{pid}",
        )
        material = mat_raw if mat_raw != _PLACEHOLDER_MAT else None

        thicknesses = _thicknesses_for(lookup, material)
        if thicknesses:
            th_opts = [_PLACEHOLDER_THICK] + thicknesses
            th_default = prod["thickness"] if prod["thickness"] in thicknesses else _PLACEHOLDER_THICK
            th_raw = st.selectbox(
                "Paksuus (mm)",
                th_opts,
                index=th_opts.index(th_default),
                key=f"th_{pid}",
            )
            thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
        else:
            st.selectbox("Paksuus (mm)", [_PLACEHOLDER_THICK], index=0, disabled=True, key=f"th_{pid}_disabled")
            thickness = None

        inp_cols = st.columns(3)
        w = inp_cols[0].number_input(
            "Leveys (mm)", min_value=0.0, value=float(prod["width"]),
            step=10.0, key=f"w_{pid}",
        )
        h = inp_cols[1].number_input(
            "Korkeus (mm)", min_value=0.0, value=float(prod["height"]),
            step=10.0, key=f"h_{pid}",
        )
        q = inp_cols[2].number_input(
            "Määrä (kpl)", min_value=1, value=int(prod["qty"]),
            step=1, key=f"q_{pid}",
        )

    prod["material"]  = material
    prod["thickness"] = thickness
    prod["width"]     = w
    prod["height"]    = h
    prod["qty"]       = q
    return delete_requested


def render_products(materials: list[str], lookup: dict) -> list[dict]:
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
