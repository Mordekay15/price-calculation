"""The DXF tab: one uploaded DXF per part, configured on its card; parts are
grouped by material + thickness and Sparrow nests the real shapes on every
priced sheet size, behind a button and a progress bar, cached until an input
changes."""

from dataclasses import dataclass

import streamlit as st

from core.pricing import build_lookup, parse_thickness_mm
from core.dxf import DxfFile, DxfReport, read_dxf
from core.geometry import net_area
from core.sheet_cost import group_products, is_ready
from core.sparrow import find_executable, part_from_report, run_sparrow, sparrow_options
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
from view.drawing import draw_sparrow_layout, preview_svg
from view.sheet_usage import render_group
from view.sparrow_progress import SparrowProgress, run_with_progress

_ROTATIONS: dict[str, tuple[float, ...]] = {
    "0° / 90° / 180° / 270°": (0.0, 90.0, 180.0, 270.0),
    "0° / 90°": (0.0, 90.0),
}
_CACHE = "dxf_sparrow_cache"    # {signature: computed group}
_RENEST = "dxf_sparrow_renest"  # {signature} of groups to re-nest on this run


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

    def nesting(self) -> dict:
        """The Sparrow settings a result is saved with. Changing one keeps the
        saved results (marked as made with other settings) instead of
        dropping them, so each group can be re-nested on its own."""
        return {"rankavali_mm": self.rankavali_mm, "clamp_mm": self.clamp_mm,
                "rotations": self.rotations, "time_limit": self.time_limit,
                "seed": self.seed}


_NESTING_LABELS = {
    **NESTING_LABELS,
    "rotations":    lambda v: "kierrot " + next(
        (k for k, r in _ROTATIONS.items() if r == v), str(v)),
    "time_limit":   lambda v: f"aikaraja {v} s",
    "seed":         lambda v: f"siemen {v}",
}


def _nesting_diff(saved: dict, current: dict) -> str:
    """Each setting that differs, as e.g. ``aikaraja 4 s (nyt 10 s)``."""
    return ", ".join(
        f"{label(saved[k])} (nyt {label(current[k]).split(' ', 1)[1]})"
        for k, label in _NESTING_LABELS.items() if saved[k] != current[k]
    )


def render(data: dict) -> None:
    lookup = build_lookup(data)

    uploaded = st.file_uploader(
        "Lataa DXF-tiedostot",
        type="dxf",
        accept_multiple_files=True,
        key="dxf_uploader",
        help="Voit ladata useita tiedostoja kerralla. Jokainen tiedosto on yksi tuote.",
    )
    parts = _sync_store(uploaded)
    if not parts:
        st.info("Lataa vähintään yksi DXF-tiedosto aloittaaksesi.")
        return

    products = _render_part_cards(parts, materials_with_copper(lookup), lookup)
    settings = _render_settings()
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
    # Two buttons that never overlap: the first nests only groups with no
    # result, the second only results made with other Sparrow settings. A
    # group's own button re-nests just that group.
    n_new = n_stale = 0
    for k, p in groups.items():
        entry = cache.get(_sig(k, p, settings))
        if entry is None:
            n_new += parse_thickness_mm(k[1]) is not None
        else:
            n_stale += _is_stale(entry, settings)
    b1, b2 = st.columns(2)
    run = b1.button(_run_label(n_new, n_stale, len(groups)), key="dxf_sparrow_run",
                    type="primary", disabled=n_new == 0,
                    help="Laskee vain osat, joilla ei vielä ole tulosta. Jo "
                         "laskettuihin ei kosketa.")
    # Shown only when there is something to update.
    update = n_stale > 0 and b2.button(
        f"Päivitä eri asetuksilla lasketut ({n_stale})", key="dxf_sparrow_update",
        help="Laskee nykyisillä asetuksilla uudelleen kaikki tulokset, jotka on "
             "laskettu eri asetuksilla. Yksittäisen osan voi päivittää sen omasta "
             "painikkeesta.")
    renest = st.session_state.pop(_RENEST, set())
    _show(products, groups, settings, cache, _nester(lookup, settings, exe),
          run=run, update=update, renest=renest)
    if run or update or renest:
        # The button's count was drawn before these groups were nested:
        # redraw it with the new count (e.g. "kaikki laskettu").
        st.rerun()


def _is_stale(entry: dict, settings: _Settings) -> bool:
    """True when a saved result was nested with other Sparrow settings."""
    return entry.get("nesting", settings.nesting()) != settings.nesting()


def _request_renest(sig: str) -> None:
    """A group's "Laske uudelleen" button: re-nest it on this run."""
    st.session_state.setdefault(_RENEST, set()).add(sig)


def _run_label(n_new: int, n_stale: int, n_groups: int) -> str:
    """The run button's label: how many new groups a click would nest."""
    base = "Laske levykäyttö (Sparrow)"
    if n_new == 0:
        return f"{base} — " + ("ei uusia osia" if n_stale else "kaikki laskettu")
    if n_new == n_groups:
        return base
    return f"{base} — " + (f"{n_new} uusi" if n_new == 1 else f"{n_new} uutta")


def _render_part_cards(parts, materials: list[str], lookup: dict) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts."""
    st.markdown("**Osat**")
    products = []
    for idx, (fid, part) in enumerate(parts):
        product = _render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)
    return products


def _render_settings() -> _Settings:
    st.divider()
    margin_pct, nest_mode = render_main_settings(
        margin_key="dxf_margin_pct",
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen",
    )
    with st.expander(ADVANCED_LABEL):
        rankavali_mm, clamp_mm = render_nesting_inputs(key_prefix="dxf")
        c1, c2, c3 = st.columns(3)
        rot_label = c1.selectbox("Sallitut kierrot", list(_ROTATIONS), key="dxf_rot")
        time_limit = c2.number_input("Sparrow-aikaraja / ajo (s)", min_value=1,
                                     value=4, step=1, key="dxf_sparrow_t")
        seed = c3.number_input("Siemen (seed)", min_value=0, value=0, step=1,
                               key="dxf_sparrow_seed")
    settings = _Settings(nest_mode, rankavali_mm, clamp_mm, _ROTATIONS[rot_label],
                         int(time_limit), int(seed), margin_pct)
    st.caption(settings_summary(settings.nesting(), _NESTING_LABELS))
    return settings


def _sig(key: tuple, products: list[dict], settings: _Settings) -> str:
    """Stable cache key: the group, its parts (quantity) and the margin.

    The Sparrow settings are left out on purpose: they are saved with the
    result (``entry["nesting"]``), so changing one doesn't drop every result.
    """
    prod_sig = ",".join(f"{p['id']}:{p['qty']}" for p in products)
    return f"{key}|{prod_sig}|{settings.margin_pct}"


def _group_label(key: tuple, prods: list[dict]) -> str:
    """The group's heading, e.g. ``S235 · 2 mm``, plus the part's name when it
    is nested on its own."""
    label = f"{key[0]} · {key[1]} mm"
    if len(key) > 2:
        label += " · " + ", ".join(p["name"] for p in prods)
    return label


def _nester(lookup: dict, settings: _Settings, exe):
    """``nest(key, prods)``: one group nested with Sparrow behind a progress bar,
    returned as a cache entry (None when its thickness can't be read)."""
    progress = SparrowProgress()

    def run_fn(instance, *, seed, time_limit_sec, separation):
        progress.run_started()
        return run_sparrow(
            instance, executable=exe, time_limit_sec=int(time_limit_sec),
            seed=int(seed), min_item_separation=separation,
        )

    def nest(key: tuple, prods: list[dict]) -> dict | None:
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        parts, areas = _parts_for_group(prods, settings.rotations)
        result = run_with_progress(
            progress, _group_label(key, prods),
            sparrow_options,
            lookup, material, thickness, thickness_mm, parts,
            run_fn=run_fn, margin_pct=settings.margin_pct,
            long_side_clamp_mm=settings.clamp_mm,
            rankavali_mm=settings.rankavali_mm, seed=settings.seed,
            time_limit_sec=settings.time_limit,
        )
        return {"result": result, "parts": parts, "areas": areas,
                "thickness_mm": thickness_mm, "nesting": settings.nesting()}

    return nest


def _show(products: list[dict], groups, settings: _Settings, cache: dict, nest,
          *, run: bool = False, update: bool = False,
          renest: set = frozenset()) -> None:
    """Render every group's result, then the parts summary.

    A group is nested in its own place first — so each result shows as soon
    as it is ready, not after the whole run — when ``run`` and it has no
    result, when ``update`` and its result is outdated, or when its signature
    is in ``renest``.
    """
    areas: dict[str, float] = {}
    current = settings.nesting()

    def render_one(key, prods):
        sig = _sig(key, prods, settings)
        entry = cache.get(sig)
        if (sig in renest or (run and entry is None)
                or (update and entry is not None and _is_stale(entry, settings))):
            new = nest(key, prods)
            if new is not None:
                cache[sig] = entry = new
        if entry is None:
            st.markdown(f"**{_group_label(key, prods)}**")
            st.caption("Odottaa laskentaa.")
            return None
        areas.update(entry["areas"])
        diff = _nesting_diff(entry.get("nesting", current), current)

        def note():
            # Made with other Sparrow settings: kept as is, re-nested on request.
            if diff:
                c1, c2 = st.columns([3, 2])
                c1.caption(f"Laskettu eri asetuksilla: {diff}.")
                c2.button("Laske uudelleen nykyisillä asetuksilla",
                          key=f"dxf_renest::{sig}", on_click=_request_renest, args=(sig,))

        return render_group(
            key[0], key[1], entry["thickness_mm"], entry["result"],
            margin_pct=settings.margin_pct, key=f"dxf_su_select::{sig}",
            draw_layout=lambda active: draw_sparrow_layout(active, entry["parts"], sig),
            heading=f"**{_group_label(key, prods)}**", after_heading=note,
        )

    prices, grand_total, missing = render_groups(groups, render_one)
    if missing and grand_total is None:
        st.info("Paina **Laske levykäyttö (Sparrow)** laskeaksesi levytarpeen ja hinnan.")
        return
    if missing:
        st.warning("Yhteissumma sisältää vain lasketut ryhmät — paina **Laske "
                   "levykäyttö (Sparrow)** laskeaksesi loput. Jo laskettuja ei "
                   "lasketa uudelleen.")

    ready = [p for p in products if is_ready(p)]
    render_pieces_summary(ready, prices, title="Yhteenveto",
                          weight_label="Osien yhteispaino (kg)", lead="Osa",
                          areas_mm2=areas)


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


# ── Part cards ────────────────────────────────────────────────────────────────
#
# _sync_store() reads each uploaded file once with core.dxf.read_dxf (cached in
# session state, pruned when a file is removed). _render_part_config() draws a
# card — preview, measured size, material / thickness / quantity —
# and returns the product dict the pricing uses, or None (with the reasons
# shown) when the part cannot be priced. The card and the pricing share the
# same DxfReport, so what the card shows is exactly what Sparrow nests.

_STORE   = "dxf_store"         # {file_id: DxfFile}
_REPORTS = "dxf_part_reports"  # {file_id: DxfReport}
_CONFIG  = "dxf_part_config"   # {file_id: {"material", "thickness"}}


def _sync_store(uploaded) -> list[tuple[str, DxfFile]]:
    """Read newly uploaded files once, drop removed ones, keep upload order."""
    store: dict = st.session_state.setdefault(_STORE, {})
    uploaded = uploaded or []
    current_ids = {u.file_id for u in uploaded}
    for fid in [f for f in store if f not in current_ids]:
        _evict(fid)

    files = []
    for up in uploaded:
        if up.file_id not in store:
            store[up.file_id] = read_dxf(up.getvalue(), up.name)
        files.append((up.file_id, store[up.file_id]))
    return files


def _evict(fid: str) -> None:
    """Drop everything kept for a removed file, including its widget state."""
    st.session_state.get(_STORE, {}).pop(fid, None)
    st.session_state.get(_CONFIG, {}).pop(fid, None)
    st.session_state.get(_REPORTS, {}).pop(fid, None)
    for key in (f"dxf_mat_{fid}", f"dxf_th_{fid}", f"dxf_th_{fid}_disabled",
                f"dxf_q_{fid}", f"dxf_unit_ok_{fid}"):
        st.session_state.pop(key, None)


def _render_part_config(
    fid: str,
    dxf: DxfFile,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card, preview left and inputs right; return its product
    dict, or None if not priceable."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}")

        # Text found in the drawing (Mat=…, Thk=…) — shown to cross-check the
        # material choice, never nested.
        if dxf.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(dxf.texts))

        preview_col, input_col = st.columns([1, 3])
        report = _part(fid, dxf)
        with input_col:
            size = _checked_size(fid, dxf, report, hdr[1])
        with preview_col:
            _render_preview(report)
        if size is None:
            return None
        with input_col:
            material, thickness, qty = _render_part_inputs(fid, materials, lookup)

    return {
        "id":        fid,
        "name":      dxf.name,
        "material":  material,
        "thickness": thickness,
        "width":     size[0],
        "height":    size[1],
        "qty":       qty,
        "report":    report,
    }


def _render_preview(report: DxfReport) -> None:
    """The part as it will be cut; shapes left outside it are greyed out."""
    preview = preview_svg(report)
    if preview:
        st.markdown(preview, unsafe_allow_html=True)
    if report.dropped:
        st.caption(
            f"Osan ulkopuolelta ohitettiin {len(report.dropped)} kuviota "
            "(esim. lisäkuvat tai irralliset viivat) — harmaalla esikatselussa."
        )


def _checked_size(fid: str, dxf: DxfFile, report: DxfReport, badge) -> tuple | None:
    """The part's ``(width, height)`` in mm, or None (with the reason shown)
    while it can't be priced. ``badge`` is the card header's status slot."""
    if report.problems:
        badge.markdown(":red[ei hinnoiteltavissa]")
        st.error(
            "**Tätä osaa ei voi vielä hinnoitella:**\n\n"
            + "\n".join(f"- {p}" for p in report.problems)
        )
        return None

    width = round(report.outline.width_mm, 1)
    height = round(report.outline.height_mm, 1)

    # No unit in the file: show the size the guess gives and price only once
    # the user confirms it. The warning goes above the checkbox and only
    # while it is unticked.
    if dxf.unit_guessed:
        note = st.empty()
        if not st.checkbox(f"Koko {width:g} × {height:g} mm on oikein",
                           key=f"dxf_unit_ok_{fid}"):
            note.warning(
                "Piirustuksesta puuttuu mittayksikkö. Oletimme yksiköksi "
                f"**{report.unit_label}**, jolloin osan koko on "
                f"**{width:g} × {height:g} mm**. Tarkista mitat piirustuksesta."
            )
            badge.markdown(":orange[vahvista yksikkö]")
            return None

    badge.markdown(f":gray[{width:g} × {height:g} mm · {report.unit_label}]")
    return width, height


def _render_part_inputs(fid: str, materials: list[str], lookup: dict) -> tuple:
    """Material, thickness and quantity; returns ``(material, thickness, qty)``.

    Material + thickness are persisted per file: the selectboxes are seeded
    from the stored choice, then the current choice is written back.
    """
    cfg = st.session_state.setdefault(_CONFIG, {}).setdefault(
        fid, {"material": None, "thickness": None})
    material, thickness = render_material_thickness(
        materials, lookup,
        mat_key=f"dxf_mat_{fid}", thick_key=f"dxf_th_{fid}",
        mat_default=cfg["material"], thick_default=cfg["thickness"],
    )
    cfg["material"] = material
    cfg["thickness"] = thickness
    qty = int(st.number_input("Määrä (kpl)", min_value=1, value=1, step=1,
                              key=f"dxf_q_{fid}"))
    return material, thickness, qty


def _part(fid: str, dxf: DxfFile) -> DxfReport:
    """The part from the default cut layers, memoised (building it scans every
    point)."""
    cache: dict = st.session_state.setdefault(_REPORTS, {})
    if fid not in cache:
        cache[fid] = dxf.part()
    return cache[fid]
