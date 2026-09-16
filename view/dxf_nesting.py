"""
view/dxf_nesting.py
===================
DXF nesting section (orchestrator).

The user uploads one or more DXF files — each file is one product. We parse the
real cuttable outline and bounding-box size from every drawing (core/dxf.py),
skipping annotation text/dimensions so nothing extra is nested. The user picks
material / thickness / quantity per part (and can drop non-part layers or
correct the size), then the parts run through the same sheet-usage costing and
layout as the manual calculator (view/sheet_usage_view.py) — except each piece
is drawn as its true DXF shape instead of a plain rectangle.

This module wires the sections together; the pieces live in dedicated modules:
  - view/dxf_part_view.py       — upload store + per-part cards (input side)
  - view/tight_nesting_view.py  — shape-aware "tight" nesting (cost + draw)
  - view/sheet_usage_view.py    — the box-nesting sheet-usage table (shared)
"""

import streamlit as st

from core.calculator import build_lookup, get_materials
from core.copper import COPPER_MATERIAL
from view.dxf_part_view import render_part_config, sync_store
from view.margin_view import render_margin
from view.nesting_settings_view import render_nesting_settings
from view.pieces_summary_view import render_pieces_summary
from view.sheet_usage_view import render_group
from view.tight_nesting_view import render_tight_group, render_tight_options


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

    # ── Per-part configuration ─────────────────────────────────────────────────
    st.markdown("**Osat**")

    products: list[dict] = []
    for idx, (fid, part) in enumerate(parts):
        product = render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)

    # ── Placement options (mirrors the manual calculator) ──────────────────────
    nest_mode, rankavali_mm, long_side_clamp_mm = render_nesting_settings(
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen")

    pack_mode = st.radio(
        "Nestaustapa",
        options=("box", "tight"),
        format_func=lambda v: {
            "box":   "Laatikkonestaus (nopea)",
            "tight": "Tiivis muotonestaus (tarkka)",
        }[v],
        horizontal=True,
        key="dxf_pack_mode",
        help=(
            "Laatikkonestaus sijoittelee osat suorakulmion mukaan (nopea). "
            "Tiivis muotonestaus sijoittelee osien todelliset muodot niin, että "
            "ne lomittuvat toistensa lovien sisään — vähemmän hukkaa, mutta "
            "laskenta kestää hetken."
        ),
    )

    angles: tuple[float, ...] = (0.0, 90.0)
    res = 3.0
    if pack_mode == "tight":
        angles, res = render_tight_options()

    # ── Group and render ───────────────────────────────────────────────────────
    ready = [
        p for p in products
        if p["material"] and p["thickness"] and p["width"] > 0 and p["height"] > 0
    ]
    if not ready:
        st.info("Valitse materiaali ja paksuus vähintään yhdelle osalle.")
        return

    groups: dict[tuple, list[dict]] = {}
    for prod in ready:
        if nest_mode == "separate":
            group_key = (prod["material"], prod["thickness"], prod["id"])
        else:
            group_key = (prod["material"], prod["thickness"])
        groups.setdefault(group_key, []).append(prod)

    st.divider()
    st.markdown("**Levyn käyttö**")

    if pack_mode == "tight":
        total_pieces = sum(int(p["qty"]) for p in ready)
        if total_pieces > 120:
            st.warning(
                f"Osia on {total_pieces} kpl — tiivis muotonestaus voi kestää "
                "kymmeniä sekunteja. Voit pienentää määrää tai valita "
                "laatikkonestauksen."
            )

    grand_total_eur = 0.0
    any_priced = False
    cheapest_prices: dict[str, float] = {}
    for group_key, group_prods in groups.items():
        material, thickness = group_key[0], group_key[1]
        if pack_mode == "tight":
            cheapest_eur, cheapest_ppt = render_tight_group(
                lookup=lookup,
                material=material,
                thickness=thickness,
                products=group_prods,
                margin_pct=margin_pct,
                long_side_clamp_mm=long_side_clamp_mm,
                rankavali_mm=rankavali_mm,
                angles=angles,
                res=res,
            )
        else:
            cheapest_eur, cheapest_ppt = render_group(
                lookup=lookup,
                material=material,
                thickness=thickness,
                products=group_prods,
                margin_pct=margin_pct,
                long_side_clamp_mm=long_side_clamp_mm,
                rankavali_mm=rankavali_mm,
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

    render_pieces_summary(ready, cheapest_prices, from_dxf=True)
