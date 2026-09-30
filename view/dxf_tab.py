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
    materials_with_copper,
    render_grand_total,
    render_groups,
    render_margin,
    render_nesting_settings,
    render_material_thickness,
    render_pieces_summary,
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
    "rankavali_mm": lambda v: f"rankaväli {v} mm",
    "clamp_mm":     lambda v: f"kynsiraina {v} mm",
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
    parts = _sync_store(uploaded)
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
    # Only groups without a result are nested. A group keeps its result when
    # other parts are added or changed, and when a Sparrow setting changes —
    # then its own button re-nests just that group with the new settings.
    n_new = sum(_sig(k, p, settings) not in cache and parse_thickness_mm(k[1]) is not None
                for k, p in groups.items())
    b1, b2 = st.columns(2)
    run = b1.button(_run_label(n_new, len(groups)), key="dxf_sparrow_run",
                    disabled=n_new == 0,
                    help="Laskee vain ryhmät, joilla ei vielä ole tulosta. Jo "
                         "lasketut säilyvät, vaikka asetuksia muutettaisiin.")
    rerun = b2.button("Laske kaikki uudelleen", key="dxf_sparrow_rerun",
                      help="Laskee jokaisen ryhmän uudelleen nykyisillä asetuksilla.")
    renest = st.session_state.pop(_RENEST, set())
    _show(products, groups, settings, cache, _nester(lookup, settings, exe),
          run=run, rerun=rerun, renest=renest)
    if run or rerun:
        st.rerun()  # redraw the button with the new count ("kaikki laskettu")


def _request_renest(sig: str) -> None:
    """A group's "Laske uudelleen" button: re-nest it on this run."""
    st.session_state.setdefault(_RENEST, set()).add(sig)


def _run_label(n_new: int, n_groups: int) -> str:
    """The run button's label: how many groups a click would nest."""
    if n_new == 0:
        return "Laske levykäyttö (Sparrow) — kaikki laskettu"
    if n_new < n_groups:
        return f"Laske levykäyttö (Sparrow) — {n_new} uutta"
    return "Laske levykäyttö (Sparrow)"


def _render_part_cards(parts, materials: list[str], lookup: dict) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts."""
    st.markdown("**Osat**")
    products = []
    for idx, (fid, part) in enumerate(parts):
        product = _render_part_config(fid, part, idx, materials, lookup)
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
                                 value=4, step=1, key="dxf_sparrow_t")
    seed = c3.number_input("Siemen (seed)", min_value=0, value=0, step=1,
                           key="dxf_sparrow_seed")
    return _Settings(nest_mode, rankavali_mm, clamp_mm, _ROTATIONS[rot_label],
                     int(time_limit), int(seed), margin_pct)


def _sig(key: tuple, products: list[dict], settings: _Settings) -> str:
    """Stable cache key: the group, its parts (quantity, layers) and the margin.

    The Sparrow settings are left out on purpose: they are saved with the
    result (``entry["nesting"]``), so changing one doesn't drop every result.
    """
    prod_sig = ",".join(f"{p['id']}:{p['qty']}:{p['layers']}" for p in products)
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
          *, run: bool = False, rerun: bool = False, renest: set = frozenset()) -> None:
    """Render every group's result, then the parts summary.

    A group is nested in its own place first — so each result shows as soon
    as it is ready, not after the whole run — when ``run`` and it has no
    result, when ``rerun``, or when its signature is in ``renest``.
    """
    areas: dict[str, float] = {}
    current = settings.nesting()

    def render_one(key, prods):
        sig = _sig(key, prods, settings)
        entry = cache.get(sig)
        if rerun or sig in renest or (run and entry is None):
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
    render_grand_total(grand_total, len(groups))

    ready = [p for p in products if is_ready(p)]
    render_pieces_summary(ready, prices, title="Osayhteenveto",
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
# card — layer picker, preview, measured size, material / thickness / quantity —
# and returns the product dict the pricing uses, or None (with the reasons
# shown) when the part cannot be priced. The card and the pricing share the
# same DxfReport, so what the card shows is exactly what Sparrow nests.

_STORE   = "dxf_store"         # {file_id: DxfFile}
_REPORTS = "dxf_part_reports"  # {(file_id, layers): DxfReport}
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
    reports: dict = st.session_state.get(_REPORTS, {})
    for key in [k for k in reports if k[0] == fid]:
        del reports[key]
    for key in (f"dxf_mat_{fid}", f"dxf_th_{fid}", f"dxf_th_{fid}_disabled",
                f"dxf_q_{fid}", f"dxf_layers_{fid}", f"dxf_unit_ok_{fid}"):
        st.session_state.pop(key, None)


def _render_part_config(
    fid: str,
    dxf: DxfFile,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card; return its product dict, or None if not priceable."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}")

        # Text found in the drawing (Mat=…, Thk=…) — shown to cross-check the
        # material choice, never nested.
        if dxf.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(dxf.texts))
        if dxf.unit_note and not dxf.unit_guessed:
            st.caption(f"Yksikkö {dxf.unit_note}: {dxf.unit_label}.")

        layers = _render_layer_picker(fid, dxf)
        report = _part(fid, dxf, layers)

        preview = preview_svg(report)
        if preview:
            st.markdown(preview, unsafe_allow_html=True)
        if report.dropped:
            st.caption(
                f"Osan ulkopuolelta ohitettiin {len(report.dropped)} kuviota "
                "(esim. lisäkuvat tai irralliset viivat) — harmaalla esikatselussa."
            )

        if report.problems:
            hdr[1].markdown(":red[ei hinnoiteltavissa]")
            st.error(
                "**Tätä osaa ei voi vielä hinnoitella:**\n\n"
                + "\n".join(f"- {p}" for p in report.problems)
            )
            return None

        width = round(report.outline.width_mm, 1)
        height = round(report.outline.height_mm, 1)

        # No unit in the file: show the size the guess gives and price only
        # once the user confirms it.
        if dxf.unit_guessed:
            st.warning(
                "Piirustuksesta puuttuu mittayksikkö. Oletimme yksiköksi "
                f"**{report.unit_label}**, jolloin osan koko on "
                f"**{width:g} × {height:g} mm**. Tarkista mitat piirustuksesta."
            )
            if not st.checkbox(f"Koko {width:g} × {height:g} mm on oikein",
                               key=f"dxf_unit_ok_{fid}"):
                hdr[1].markdown(":orange[vahvista yksikkö]")
                return None

        hdr[1].markdown(f":gray[{width:g} × {height:g} mm · {report.unit_label}]")

        # Material + thickness — persisted per file and shared with the manual
        # calculator cards. Seed the selectboxes from the stored choice, then
        # write the current choice back into it.
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

    return {
        "id":        fid,
        "name":      dxf.name,
        "material":  material,
        "thickness": thickness,
        "width":     width,
        "height":    height,
        "qty":       qty,
        "layers":    None if layers is None else tuple(sorted(layers)),
        "report":    report,
    }


def _part(fid: str, dxf: DxfFile, layers: set[str] | None) -> DxfReport:
    """The part for this layer choice, memoised (building it scans every point)."""
    cache: dict = st.session_state.setdefault(_REPORTS, {})
    key = (fid, None if layers is None else tuple(sorted(layers)))
    if key not in cache:
        cache[key] = dxf.part(layers)
    return cache[key]


def _render_layer_picker(fid: str, dxf: DxfFile) -> set[str] | None:
    """Layer multiselect, shown when a drawing has more than one layer.

    Frame / title / text / dimension / bend / info layers are left out by
    default. Returns the chosen layers, or None for the default choice.
    """
    avail = dxf.available_layers()
    if len(avail) <= 1:
        return None
    suggested = dxf.suggested_layers()
    sizes = dxf.layer_sizes()

    def label(name: str) -> str:
        n, w, h = sizes.get(name, (0, 0, 0))
        return f"{name}  ·  {n} obj  ·  {w:.0f}×{h:.0f} mm"

    chosen = set(st.multiselect(
        "Leikattavat tasot (layers)",
        options=avail,
        default=suggested,
        format_func=label,
        key=f"dxf_layers_{fid}",
        help="Vain osan leikattavat tasot. Kehys, otsikko, mitat, tekstit, "
             "taivutusviivat ja info-tasot jätetään oletuksena pois — lisää tai "
             "poista tasoja ja katso esikatselusta, että vain osa jää.",
    ))
    hidden = [n for n in avail if n not in suggested]
    if hidden:
        st.caption("Jätetty oletuksena pois: " + ", ".join(hidden))
    return chosen
