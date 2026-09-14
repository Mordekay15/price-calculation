"""
view/sidebar.py
===============
The sidebar: one PDF upload slot per supplier, plus the manual copper price.

`render()` draws the whole sidebar and returns the merged price data that the
calculator consumes. Copper is always merged in, so the calculator works even
before any supplier PDF has been uploaded.
"""

import streamlit as st
from core import price_store
from core.price_store import SUPPLIERS, Supplier
from core.copper import (
    build_copper_section,
    COPPER_PRICE_MIN,
    COPPER_PRICE_MAX,
    COPPER_PRICE_STEP,
)


def render() -> dict:
    merged: dict = {}

    with st.sidebar:
        st.header("Hinnastot")
        st.caption("Lataa kunkin toimittajan kuukausittainen PDF erikseen.")

        for supplier in SUPPLIERS:
            st.divider()
            stored = _render_supplier(supplier)
            if stored:
                merged.update(stored["data"])

        st.divider()
        copper_price_kg = _render_copper()

    merged.update(build_copper_section(copper_price_kg))
    return merged


# ── One supplier slot ─────────────────────────────────────────────────────────

def _render_supplier(supplier: Supplier) -> dict | None:
    """Draw one upload slot. Returns the stored payload, or None if empty."""
    st.markdown(f"**{supplier.label}**")

    stored = price_store.load(supplier)
    if stored:
        st.success(
            f"Ladattu: **{stored['source_file']}**\n\n"
            f"Päivitetty: {stored['updated_at']}"
        )

    uploaded = st.file_uploader(
        f"Lataa uusi {supplier.label} PDF" if stored else f"Lataa {supplier.label} PDF",
        type="pdf",
        key=f"upload_{supplier.key}",
    )

    # st.file_uploader keeps the file across reruns; track the file_id so we
    # only parse + save once per upload (otherwise updated_at ticks every rerun).
    seen_key = f"processed_upload_{supplier.key}"
    if uploaded and st.session_state.get(seen_key) != uploaded.file_id:
        with st.spinner(f"Käsitellään {supplier.label} PDF..."):
            parsed = supplier.parser(uploaded.read())
            price_store.save(supplier, parsed, uploaded.name)
        st.session_state[seen_key] = uploaded.file_id
        st.success(
            f"Tallennettu {supplier.label}-hinnat tiedostosta **{uploaded.name}**."
        )
        st.rerun()

    return stored


# ── Copper ────────────────────────────────────────────────────────────────────

def _render_copper() -> float | None:
    """
    Copper carries no list price; the user sets the price per kilo here. The
    input starts empty so no price exists until the user enters one.
    """
    st.markdown("**Kupari**")
    return st.number_input(
        "Kuparin hinta (€/kg)",
        min_value=COPPER_PRICE_MIN,
        max_value=COPPER_PRICE_MAX,
        value=None,
        step=COPPER_PRICE_STEP,
        key="copper_price_kg",
        placeholder=f"{COPPER_PRICE_MIN:g}–{COPPER_PRICE_MAX:g}",
        help=(
            "Aseta kuparin hinta per kilo (15,0–15,9 €/kg). Kupari on aina "
            "valittavissa, mutta sille ei lasketa hintaa ennen kuin syötät sen."
        ),
    )