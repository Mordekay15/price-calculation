"""
view/dxf_nesting.py
===================
DXF upload + per-part configuration section.

The user uploads one or more DXF files — each file is one product. Every file is
shown as its own configuration card (view/dxf_part_view.py): the real outline
preview, the layer picker, the "main part only" toggle, material / thickness /
size, and the quantity. The per-part quantities then drive the Sparrow instance
that the export/run/reconstruct step builds (view/sparrow_export_view.py).

The original DXF is always kept as the source of truth; the card's geometry is a
preview, and Sparrow re-parses each file for placement (core/sparrow_input.py).
"""

import streamlit as st

from core.calculator import build_lookup, get_materials
from core.copper import COPPER_MATERIAL
from view.dxf_part_view import render_part_config, sync_store
from view.margin_view import render_margin
from view.sparrow_export_view import render as render_sparrow_export


def render(data: dict) -> None:
    lookup = build_lookup(data)

    st.subheader("DXF-nestaus")
    st.caption(
        "Lataa osat DXF-tiedostoina. Ohjelma lukee kunkin osan todellisen "
        "muodon ja mitat (tekstit ja mitoitukset ohitetaan) ja sijoittelee ne "
        "levylle mahdollisimman tehokkaasti."
    )

    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])

    margin_pct = render_margin("dxf_margin_pct") #getting kate

    # ── Upload ────────────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Lataa DXF-tiedostot",
        type="dxf",
        accept_multiple_files=True,
        key="dxf_uploader",
        help="Voit ladata useita tiedostoja kerralla. Jokainen tiedosto on yksi tuote.",
    )

    parts = sync_store(uploaded)
    if not parts:
        st.info("Lataa vähintään yksi DXF-tiedosto aloittaaksesi.")
        return

    # ── Per-part configuration cards (one per uploaded product) ────────────────
    st.markdown("**Osat**")

    products: list[dict] = []
    for idx, (fid, part) in enumerate(parts):
        product = render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)

    # ── Sparrow input / run / reconstruct (Phases 7–9) ─────────────────────────
    # Quantities come from the cards above (keyed by file id) so the user sets
    # each amount once, on the part card.
    st.divider()
    st.markdown("**Sparrow-syöte**")
    render_sparrow_export(uploaded, products)
