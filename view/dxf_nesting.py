"""
view/dxf_nesting.py
===================
DXF upload section.

The user uploads one or more DXF files — each file is one product. From here the
data will be handed to the Sparrow engine for nesting (work in progress). All the
previous DXF parsing / per-part configuration / nesting / costing logic has been
removed; only the upload step remains.
"""

import streamlit as st

from core.calculator import build_lookup, get_materials
from core.copper import COPPER_MATERIAL
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

    # ── Sparrow input (Phase 7: DXF → Sparrow instance JSON) ───────────────────
    st.divider()
    st.markdown("**Sparrow-syöte**")
    render_sparrow_export(uploaded)
