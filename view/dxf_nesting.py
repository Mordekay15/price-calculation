"""
view/dxf_nesting.py
===================
DXF tab — Sparrow shape nesting with the same sheet-usage design as the
manual calculator.

The user uploads DXF files (one product each) and configures each part on its
card (view/dxf_part_view.py). Parts are grouped by material + thickness and,
for each group, Sparrow nests the real shapes on every priced sheet size
(core/sparrow_pack.py) to find the cheapest (view/sheet_usage.py).

Sparrow runs are slow, so they happen behind a button with a progress bar
(view/sparrow_progress.py) and the results are cached per input signature
until something changes.
"""

from dataclasses import dataclass

import streamlit as st

from core.calculator import build_lookup, parse_thickness_mm
from core.geometry import net_area
from core.sparrow_input import part_from_report
from core.sparrow_pack import sparrow_options
from core.sparrow_runner import find_executable, run_sparrow
from view.common import (
    group_products,
    is_ready,
    materials_with_copper,
    render_grand_total,
    render_groups,
    render_margin,
    render_nesting_settings,
    render_pieces_summary,
)
from view.dxf_part_view import render_part_config, sync_store
from view.sheet_usage import draw_sparrow_layout, render_group
from view.sparrow_progress import SparrowProgress, run_with_progress

_ROTATIONS: dict[str, tuple[float, ...]] = {
    "0° / 90° / 180° / 270°": (0.0, 90.0, 180.0, 270.0),
    "0° / 90°": (0.0, 90.0),
}
_CACHE = "dxf_sparrow_cache"  # {signature: computed group}


@dataclass(frozen=True)
class _Settings:
    """Every input besides the parts that changes a Sparrow result."""

    nest_mode: str
    rankavali_mm: int
    clamp_mm: int
    rotations: tuple[float, ...]
    time_limit: int
    seed: int
    margin_pct: float


def render(data: dict) -> None:
    lookup = build_lookup(data)
    st.subheader("DXF-nestaus")
    st.caption(
        "Lataa osat DXF-tiedostoina. Ohjelma lukee kunkin osan todellisen "
        "muodon ja mitat, sijoittelee ne Sparrow-moottorilla ja vertaa, mille "
        "levykoolle osat mahtuvat edullisimmin."
    )
    margin_pct = render_margin("dxf_margin_pct")

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

    products = _render_part_cards(parts, materials_with_copper(lookup), lookup)
    settings = _render_settings(margin_pct)
    groups = group_products(products, settings.nest_mode)
    if not groups:
        st.info("Valitse materiaali ja paksuus vähintään yhdelle osalle.")
        return

    exe = find_executable()
    if exe is None:
        st.error(
            "Sparrow-suoritustiedostoa ei löytynyt — levylaskenta vaatii sen. "
            "Aseta polku `SPARROW_BIN`-ympäristömuuttujaan tai lisää binääri "
            "`bin/sparrow`-tiedostoksi."
        )
        return

    st.divider()
    st.markdown("**Levyn käyttö**")
    cache: dict = st.session_state.setdefault(_CACHE, {})
    if st.button("Laske levykäyttö (Sparrow)", key="dxf_sparrow_run"):
        cache.clear()
        _run(groups, lookup, settings, exe, cache)
    _show(products, groups, settings, cache)


def _render_part_cards(parts, materials: list[str], lookup: dict) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts."""
    st.markdown("**Osat**")
    products = []
    for idx, (fid, part) in enumerate(parts):
        product = render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)
    return products


def _render_settings(margin_pct: float) -> _Settings:
    nest_mode, rankavali_mm, clamp_mm = render_nesting_settings(
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen",
    )
    c1, c2, c3 = st.columns(3)
    rot_label = c1.selectbox("Sallitut kierrot", list(_ROTATIONS), key="dxf_rot")
    time_limit = c2.number_input("Sparrow-aikaraja / ajo (s)", min_value=1,
                                 value=8, step=1, key="dxf_sparrow_t")
    seed = c3.number_input("Siemen (seed)", min_value=0, value=0, step=1,
                           key="dxf_sparrow_seed")
    return _Settings(nest_mode, rankavali_mm, clamp_mm, _ROTATIONS[rot_label],
                     int(time_limit), int(seed), margin_pct)


def _sig(key: tuple, products: list[dict], settings: _Settings) -> str:
    """Stable cache key: the group, its parts and quantities, and the settings."""
    prod_sig = ",".join(f"{p['id']}:{p['qty']}" for p in products)
    return f"{key}|{prod_sig}|{settings}"


def _run(groups, lookup: dict, settings: _Settings, exe, cache: dict) -> None:
    """Nest every group with Sparrow (behind a progress bar) into ``cache``."""
    progress = SparrowProgress()

    def run_fn(instance, *, seed, time_limit_sec, separation):
        progress.run_started()
        return run_sparrow(
            instance, executable=exe, time_limit_sec=int(time_limit_sec),
            seed=int(seed), min_item_separation=separation,
        )

    for key, prods in groups.items():
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            continue
        parts, areas = _parts_for_group(prods, settings.rotations)
        result = run_with_progress(
            progress, f"{material} · {thickness} mm",
            sparrow_options,
            lookup, material, thickness, thickness_mm, parts,
            run_fn=run_fn, margin_pct=settings.margin_pct,
            long_side_clamp_mm=settings.clamp_mm,
            rankavali_mm=settings.rankavali_mm, seed=settings.seed,
            time_limit_sec=settings.time_limit,
        )
        cache[_sig(key, prods, settings)] = {
            "result": result, "parts": parts, "areas": areas,
            "thickness_mm": thickness_mm,
        }


def _show(products: list[dict], groups, settings: _Settings, cache: dict) -> None:
    """Render the cached result of every group, then the parts summary."""
    areas: dict[str, float] = {}

    def render_one(key, prods):
        sig = _sig(key, prods, settings)
        entry = cache.get(sig)
        if entry is None:
            return None
        areas.update(entry["areas"])
        return render_group(
            key[0], key[1], entry["thickness_mm"], entry["result"],
            margin_pct=settings.margin_pct, key=f"dxf_su_select::{sig}",
            draw_layout=lambda active: draw_sparrow_layout(active, entry["parts"], sig),
        )

    prices, grand_total, missing = render_groups(groups, render_one)
    if missing and grand_total is None:
        st.info("Paina **Laske levykäyttö (Sparrow)** laskeaksesi levytarpeen ja hinnan.")
        return
    if missing:
        st.warning("Asetukset muuttuivat — laske uudelleen päivittääksesi kaikki ryhmät.")
    render_grand_total(grand_total, len(groups))

    ready = [p for p in products if is_ready(p)]
    render_pieces_summary(ready, prices, from_dxf=True, areas_mm2=areas)


def _parts_for_group(
    products: list[dict], rotations: tuple
) -> tuple[list, dict[str, float]]:
    """Sparrow parts for a group, plus each product's real area (mm²/piece).

    The parts come from the same read result the card showed; the area is the
    outline minus its holes.
    """
    parts = [
        part_from_report(p["report"], int(p["qty"]), allowed_orientations=rotations)
        for p in products
    ]
    areas = {p["id"]: net_area(sp.outer, sp.holes) for p, sp in zip(products, parts)}
    return parts, areas
