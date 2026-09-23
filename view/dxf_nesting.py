"""
view/dxf_nesting.py
===================
DXF nesting section (orchestrator) — Sparrow backend, sheet-usage design.

The user uploads one or more DXF files — each file is one product — and
configures each on its card (view/dxf_part_view.py). The parts are grouped by
material + thickness, and for each group we answer "which priced sheet size is
cheapest": Sparrow nests the real shapes onto fixed sheets one at a time
(core/sparrow_pack + core/sparrow_sheet_cost), and the result is shown with the
same design as the manual calculator — the cheapest-sheet table, headline
metrics, a per-sheet layout, and a price breakdown (view/dxf_sparrow_usage_view).

Sparrow does the nesting, so a working Sparrow binary is required; because each
run is slow, the analysis is computed behind a button and cached per input
signature until something changes. While it runs, a progress bar shows the
sheet size being nested, the Sparrow run count, placed parts and elapsed time.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import streamlit as st

from core.calculator import (
    build_lookup,
    density_for_material,
    get_materials,
    parse_thickness_mm,
)
from core.copper import COPPER_MATERIAL
from core.sparrow_input import parts_from_dxf
from core.sparrow_runner import find_executable, run_sparrow
from core.sparrow_sheet_cost import compute_options_sparrow
from view.dxf_part_view import render_part_config, sync_store
from view.dxf_sparrow_usage_view import render_group_sparrow
from view.margin_view import render_margin
from view.nesting_settings_view import render_nesting_settings
from view.pieces_summary_view import render_pieces_summary

_ROTATIONS: dict[str, tuple[float, ...]] = {
    "0° / 90° / 180° / 270°": (0.0, 90.0, 180.0, 270.0),
    "0° / 90°": (0.0, 90.0),
}


def render(data: dict) -> None:
    lookup = build_lookup(data)

    st.subheader("DXF-nestaus")
    st.caption(
        "Lataa osat DXF-tiedostoina. Ohjelma lukee kunkin osan todellisen "
        "muodon ja mitat, sijoittelee ne Sparrow-moottorilla ja vertaa, mille "
        "levykoolle osat mahtuvat edullisimmin."
    )

    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])

    margin_pct = render_margin("dxf_margin_pct")  # getting kate

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
    uploaded_by_id = {u.file_id: u for u in (uploaded or [])}

    # ── Per-part cards ─────────────────────────────────────────────────────────
    st.markdown("**Osat**")
    products: list[dict] = []
    for idx, (fid, part) in enumerate(parts):
        product = render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)

    # ── Placement settings ─────────────────────────────────────────────────────
    nest_mode, rankavali_mm, long_side_clamp_mm = render_nesting_settings(
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen",
    )
    rc1, rc2, rc3 = st.columns(3)
    rot_label = rc1.selectbox("Sallitut kierrot", list(_ROTATIONS.keys()), key="dxf_rot")
    rotations = _ROTATIONS[rot_label]
    time_limit = int(rc2.number_input("Sparrow-aikaraja / ajo (s)", min_value=1,
                                      value=8, step=1, key="dxf_sparrow_t"))
    seed = int(rc3.number_input("Siemen (seed)", min_value=0, value=0, step=1,
                                key="dxf_sparrow_seed"))

    ready = [
        p for p in products
        if p["material"] and p["thickness"] and p["width"] > 0 and p["height"] > 0
    ]
    if not ready:
        st.info("Valitse materiaali ja paksuus vähintään yhdelle osalle.")
        return

    # ── Require Sparrow ─────────────────────────────────────────────────────────
    exe = find_executable()
    if exe is None:
        st.error(
            "Sparrow-suoritustiedostoa ei löytynyt — levylaskenta vaatii sen. "
            "Aseta polku `SPARROW_BIN`-ympäristömuuttujaan tai lisää binääri "
            "`bin/sparrow`-tiedostoksi."
        )
        return

    progress = _Progress()

    def run_fn(instance, *, seed, time_limit_sec, separation):
        progress.run_started()
        return run_sparrow(
            instance, executable=exe, time_limit_sec=int(time_limit_sec),
            seed=int(seed), min_item_separation=separation,
        )

    # ── Group by material + thickness (or per part) ─────────────────────────────
    groups: dict[tuple, list[dict]] = {}
    for prod in ready:
        if nest_mode == "separate":
            key = (prod["material"], prod["thickness"], prod["id"])
        else:
            key = (prod["material"], prod["thickness"])
        groups.setdefault(key, []).append(prod)
    # ─────────────────────────────

    st.divider()
    st.markdown("**Levyn käyttö**")

    cache: dict = st.session_state.setdefault("dxf_sparrow_cache", {})
    if st.button("Laske levykäyttö (Sparrow)", key="dxf_sparrow_run"):
        cache.clear()
        for gkey, gprods in groups.items():
            material, thickness = gkey[0], gkey[1]
            thickness_mm = parse_thickness_mm(thickness)
            if thickness_mm is None:
                continue
            gparts, net_area_by_fid = _parts_for_group(gprods, uploaded_by_id, rotations)
            # Weigh each part by its real *net cut area* (holes removed), not the
            # bounding box — otherwise interlocked parts + holes overstate the
            # weight (a plate could weigh more than its sheet). The sheet cost is
            # then spread over that same weight so the per-part summary total
            # matches the headline total.
            density = density_for_material(material)
            pieces_kg = sum(
                net_area_by_fid.get(p["id"], 0.0) * thickness_mm * density * int(p["qty"])
                for p in gprods
            )
            # Nest in a worker thread so this (script) thread can keep the
            # progress bar moving; Streamlit calls stay on this thread.
            progress.reset()
            label = f"{material} · {thickness} mm"
            bar = st.progress(0.0, text=f"Sparrow laskee: {label}…")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    compute_options_sparrow,
                    lookup, material, thickness, thickness_mm, gparts,
                    run_fn=run_fn, margin_pct=margin_pct,
                    long_side_clamp_mm=long_side_clamp_mm,
                    rankavali_mm=rankavali_mm, seed=seed, time_limit_sec=time_limit,
                    pieces_kg_override=pieces_kg, on_progress=progress.event,
                )
                while not wait([future], timeout=0.5).done:
                    value, text = progress.snapshot()
                    bar.progress(value, text=f"{label} — {text}")
                result = future.result()
            bar.empty()
            cache[_sig(gkey, gprods, long_side_clamp_mm, rankavali_mm, rotations,
                       time_limit, seed, margin_pct)] = {
                "result": result, "parts": gparts, "areas": net_area_by_fid,
                "material": material, "thickness": thickness, "thickness_mm": thickness_mm,
            }

    # ── Render cached results per group ─────────────────────────────────────────
    grand_total_eur = 0.0
    any_priced = False
    cheapest_prices: dict[str, float] = {}
    areas_by_id: dict[str, float] = {}
    stale = False
    for gkey, gprods in groups.items():
        sig = _sig(gkey, gprods, long_side_clamp_mm, rankavali_mm, rotations,
                   time_limit, seed, margin_pct)
        entry = cache.get(sig)
        if entry is None:
            stale = True
            continue
        areas_by_id.update(entry.get("areas", {}))
        total, ppt = render_group_sparrow(
            entry["material"], entry["thickness"], entry["thickness_mm"],
            entry["result"], entry["parts"], long_side_clamp_mm=long_side_clamp_mm,
            margin_pct=margin_pct, key_suffix=sig,
        )
        if total is not None:
            grand_total_eur += total
            any_priced = True
        if ppt is not None:
            for gp in gprods:
                cheapest_prices[gp["id"]] = ppt

    if stale and not any_priced:
        st.info("Paina **Laske levykäyttö (Sparrow)** laskeaksesi levytarpeen ja hinnan.")
        return
    if stale:
        st.warning("Asetukset muuttuivat — laske uudelleen päivittääksesi kaikki ryhmät.")

    if any_priced and len(groups) > 1:
        st.divider()
        st.metric("Yhdistetty edullisin yhteissumma (€)", f"{grand_total_eur:,.2f}")

    render_pieces_summary(ready, cheapest_prices, from_dxf=True, areas_mm2=areas_by_id)


def _parts_for_group(
    products: list[dict], uploaded_by_id: dict, rotations: tuple
) -> tuple[list, dict[str, float]]:
    """Build Sparrow parts for a group, plus each product's net area (mm²/piece).

    Returns ``(parts, net_area_by_id)`` where the area is one piece's true cut
    area (outer outline minus holes), summed over the product's contours.
    """
    out: list = []
    net_area_by_fid: dict[str, float] = {}
    for prod in products:
        up = uploaded_by_id.get(prod["id"])
        if up is None:
            continue
        gparts, _report = parts_from_dxf(
            up.getvalue(), prod.get("name") or up.name, int(prod["qty"]),
            allowed_orientations=rotations,
        )
        out.extend(gparts)
        net_area_by_fid[prod["id"]] = sum(_net_area(p) for p in gparts)
    return out, net_area_by_fid


def _net_area(part) -> float:
    """One part's true cut area (mm²): outer outline minus its holes."""
    return _poly_area(part.outer) - sum(_poly_area(h) for h in part.holes)


def _poly_area(points) -> float:
    """Absolute shoelace area of a ring."""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


class _Progress:
    """Nesting progress, written by the Sparrow worker thread, read by the UI.

    The bar moves per sheet size; within a size it follows the placed parts.
    The per-sheet search (several Sparrow runs before the first sheet is
    settled) places nothing yet, so each run also eases the bar forward a
    little — it never sits still while Sparrow is working.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()
            self._runs = 0
            self._size_index, self._size_count, self._size = 0, 1, ""
            self._size_runs = 0
            self._placed: dict[tuple, tuple[int, int]] = {}

    def run_started(self) -> None:
        with self._lock:
            self._runs += 1
            self._size_runs += 1

    def event(self, kind: str, **kw) -> None:
        with self._lock:
            if kind == "size":
                self._size_index, self._size_count = kw["index"], kw["count"]
                self._size = f"{kw['w']}×{kw['h']}"
                self._size_runs = 0
                self._placed = {}
            elif kind == "sheet":
                self._placed[(kw["w"], kw["h"])] = (kw["placed"], kw["total"])

    def snapshot(self) -> tuple[float, str]:
        """``(bar value 0–1, status text)``."""
        with self._lock:
            done = min((p / t for p, t in self._placed.values() if t), default=0.0)
            within = max(done, min(0.9, 1 - 0.85 ** self._size_runs))
            value = min(0.99, (self._size_index + within) / self._size_count)
            text = (
                f"levykoko {self._size} ({self._size_index + 1}/{self._size_count})"
                f" · Sparrow-ajo {self._runs}"
            )
            if self._placed:
                placed = min(p for p, _ in self._placed.values())
                total = next(iter(self._placed.values()))[1]
                text += f" · sijoitettu {placed}/{total} kpl"
            text += f" · {time.monotonic() - self._start:.0f} s"
        return value, text


def _sig(gkey, products, clamp, rankavali, rotations, time_limit, seed, margin_pct) -> str:
    """Stable cache key: group + every input that changes the result."""
    prod_sig = ",".join(f"{p['id']}:{p['qty']}" for p in products)
    return (
        f"{gkey}|{prod_sig}|c{clamp}|r{rankavali}|rot{rotations}"
        f"|t{time_limit}|s{seed}|m{margin_pct}"
    )
