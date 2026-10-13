"""The DXF tab: one uploaded DXF per part, configured on its card; parts are
grouped by material + thickness and Sparrow nests the real shapes on every
priced sheet size, behind a button and a progress bar, cached until an input
changes."""

import time
from dataclasses import dataclass, field

import streamlit as st

from core.pricing import build_lookup, get_sizes_for_material, parse_thickness_mm
from core.dxf import DxfFile, DxfReport, read_dxf
from core.geometry import net_area
from core.sheet_cost import group_products, is_ready
from core.sparrow import (
    NESTING_ANGLES,
    find_executable,
    part_from_report,
    run_sparrow,
    sparrow_options,
)
from view.common import (
    ADVANCED_LABEL,
    NESTING_LABELS,
    materials_with_copper,
    render_groups,
    render_main_settings,
    render_material_thickness,
    render_nesting_inputs,
    SheetSettings,
    render_pieces_summary,
    settings_summary,
)
from view.drawing import draw_sparrow_layout, preview_svg
from view.sheet_usage import render_group
from view.sparrow_progress import SparrowProgress, run_with_progress

_CACHE = "dxf_sparrow_cache"    # {signature: computed group}
_RENEST = "dxf_sparrow_renest"  # {signature} of groups to re-nest on this run

# Streamlit's uploader already takes dropped files, but it looks like a plain
# button. Make it a tall dashed drop zone that says files can be dropped on it.
_DROPZONE_CSS = """
<style>
[class*="st-key-dxf_uploader"] [data-testid="stFileUploaderDropzone"] {
    min-height: 9rem;
    border: 2px dashed rgba(128, 128, 128, 0.6);
    justify-content: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
}
[class*="st-key-dxf_uploader"] [data-testid="stFileUploaderDropzone"]::before {
    content: "Vedä ja pudota DXF-tiedostot tähän";
    width: 100%;
    text-align: center;
    font-weight: 600;
}
</style>
"""


@dataclass(frozen=True)
class _Settings:
    """Every input besides the parts that changes a Sparrow result, the
    price list (``lookup``) included."""

    nest_mode: str
    sheet: SheetSettings
    time_limit: int
    margin_pct: float
    lookup: dict = field(default_factory=dict)

    def nesting(self) -> dict:
        """The Sparrow settings a result is saved with. Changing one keeps the
        saved results (marked as made with other settings) instead of
        dropping them, so each group can be re-nested on its own."""
        return {**self.sheet.values(), "time_limit": self.time_limit}


_NESTING_LABELS = {
    **NESTING_LABELS,
    "time_limit":   lambda v: f"hakuaika {v} s",
}


def _nesting_diff(saved: dict, current: dict) -> str:
    """Each setting that differs, as e.g. ``hakuaika 4 s (nyt 10 s)``."""
    return ", ".join(
        f"{label(saved[k])} (nyt {label(current[k]).split(' ', 1)[1]})"
        for k, label in _NESTING_LABELS.items() if saved[k] != current[k]
    )


def render(data: dict) -> None:
    lookup = build_lookup(data)

    parts = _render_drop_area()
    if not parts:
        st.info("Lataa vähintään yksi DXF-tiedosto aloittaaksesi.")
        return

    # Per-part angles are chosen on the cards, drawn above the run row whose
    # switch decides it: read the switch's value from the last run.
    per_part = len(parts) > 1 and st.session_state.get(_ANGLE_MODE) == _PER_PART
    products = _render_part_cards(parts, materials_with_copper(lookup), lookup, per_part)
    margin_pct, nest_mode, sheet = _render_settings()
    if not any(is_ready(p) for p in products):
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
    # Two rows above the button they apply to: the nesting angle, then the
    # search time with the buttons.
    shared = _render_angle_controls(len(parts))
    if shared == ():
        return  # no angle ticked: the warning says so
    c_time, c_run, c_update = st.columns([1, 2, 2], vertical_alignment="bottom")
    time_limit = c_time.number_input(
        "Sijoittelun hakuaika (s)", min_value=1, value=4, step=1, key="dxf_sparrow_t",
        help="Aikaraja yhdelle sijoitteluyritykselle (levyä kohden tehdään "
             "yksi tai useampi). Pidempi aika voi löytää tiiviimmän sijoittelun, "
             "mutta laskenta kestää kauemmin.")
    if shared is not None:
        products = [{**p, "angles": shared} for p in products]
    groups = group_products(products, nest_mode)
    settings = _Settings(nest_mode, sheet, int(time_limit), margin_pct, lookup)
    cache: dict = st.session_state.setdefault(_CACHE, {})
    n_new, n_stale = _count_pending(groups, settings, cache)
    run, update = _render_run_buttons(c_run, c_update, n_new, n_stale, len(groups))
    st.divider()  # the run buttons above, the results below
    renest = st.session_state.pop(_RENEST, set())
    _show(products, groups, settings, cache, _nester(lookup, settings, exe),
          run=run, update=update, renest=renest)
    if run or update or renest:
        # The button's count was drawn before these groups were nested:
        # redraw it with the new count (e.g. "kaikki laskettu").
        st.rerun()


def _render_drop_area() -> list[tuple[str, DxfFile]]:
    """The drop area; returns every part read so far, in upload order."""
    st.html(_DROPZONE_CSS)
    uploaded = st.file_uploader(
        "Lataa DXF-tiedostot",
        type="dxf",
        accept_multiple_files=True,
        key=f"dxf_uploader_{st.session_state.get(_INBOX, 0)}",
        help="Vedä tiedostot alueelle tai valitse ne koneelta. Voit ladata useita "
             "tiedostoja kerralla. Jokainen tiedosto on yksi tuote; ne siirtyvät "
             "alle osakorteiksi, ja kortin Poista-painike poistaa osan.",
    )
    if _ingest(uploaded):
        st.rerun()  # redraw with the drop area empty
    parts = list(st.session_state.get(_STORE, {}).items())
    _warn_duplicates(parts)
    return parts


def _warn_duplicates(parts: list[tuple[str, DxfFile]]) -> None:
    """The warning about files dropped again, for ``_DUPE_WARNING_S`` seconds
    after the drop. It is drawn in a fragment that reruns when the time is up,
    so it goes away on its own, without a click."""
    names, until = st.session_state.get(_DUPES, ([], 0.0))
    left = until - time.time()
    if not names or left <= 0:
        st.session_state.pop(_DUPES, None)
        return
    st.fragment(_duplicate_warning, run_every=left + 0.1)(names, until, parts)


def _duplicate_warning(names: list[str], until: float,
                       parts: list[tuple[str, DxfFile]]) -> None:
    """Name each dropped file that was already a card, and that card's number;
    nothing once the time is up."""
    if time.time() >= until:
        st.session_state.pop(_DUPES, None)
        return
    idx = {dxf.name: i for i, (_, dxf) in enumerate(parts)}
    lines = [f"- **{n}** (osa #{idx[n] + 1})" if n in idx else f"- **{n}**"
             for n in names]
    st.warning(
        ("Tiedosto on jo lisätty, joten sitä ei lisätty uudelleen:"
         if len(names) == 1 else
         "Tiedostot on jo lisätty, joten niitä ei lisätty uudelleen:")
        + "\n\n" + "\n".join(lines)
        + "\n\nJos tiedosto on muuttunut, poista vanha osa ensin ja lataa se sitten uudelleen."
    )


def _count_pending(groups, settings: _Settings, cache: dict) -> tuple[int, int]:
    """``(n_new, n_stale)``: groups with no result yet (and a readable
    thickness), and results made with other Sparrow settings."""
    n_new = n_stale = 0
    for key, prods in groups.items():
        entry = cache.get(_sig(key, prods, settings))
        if entry is None:
            n_new += parse_thickness_mm(key[1]) is not None
        else:
            n_stale += _is_stale(entry, settings)
    return n_new, n_stale


def _render_run_buttons(c_run, c_update, n_new: int, n_stale: int,
                        n_groups: int) -> tuple[bool, bool]:
    """The two buttons, which never overlap: the first nests only groups with
    no result, the second (shown only when needed) only results made with
    other Sparrow settings. A group's own button re-nests just that group.
    Returns ``(run, update)`` — whether each was clicked."""
    run = c_run.button(_run_label(n_new, n_stale, n_groups), key="dxf_sparrow_run",
                       type="primary", disabled=n_new == 0,
                       help="Laskee vain osat, joilla ei vielä ole tulosta. Jo "
                            "laskettuihin ei kosketa.")
    update = n_stale > 0 and c_update.button(
        f"Päivitä eri asetuksilla lasketut ({n_stale})", key="dxf_sparrow_update",
        help="Laskee nykyisillä asetuksilla uudelleen kaikki tulokset, jotka on "
             "laskettu eri asetuksilla. Yksittäisen osan voi päivittää sen omasta "
             "painikkeesta.")
    return run, update


def _is_stale(entry: dict, settings: _Settings) -> bool:
    """True when a saved result was nested with other Sparrow settings."""
    return entry.get("nesting", settings.nesting()) != settings.nesting()


def _request_renest(sig: str) -> None:
    """A group's "Laske uudelleen" button: re-nest it on this run."""
    st.session_state.setdefault(_RENEST, set()).add(sig)


def _run_label(n_new: int, n_stale: int, n_groups: int) -> str:
    """The run button's label: how many new groups a click would nest."""
    base = "Laske levykäyttö"
    if n_new == 0:
        return f"{base} — " + ("ei uusia osia" if n_stale else "kaikki laskettu")
    if n_new == n_groups:
        return base
    return f"{base} — " + (f"{n_new} uusi" if n_new == 1 else f"{n_new} uutta")


def _render_part_cards(parts, materials: list[str], lookup: dict,
                       per_part_angles: bool = False) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts. The
    nesting angle is on the cards only when it is chosen per part."""
    title, clear = st.columns([6, 2], vertical_alignment="center")
    title.markdown("**Osat**")
    if len(parts) > 1:
        # Behind a popover: one stray click shouldn't drop every card's setup.
        with clear.popover("Poista kaikki", width="stretch"):
            st.button(f"Kyllä, poista kaikki {len(parts)} osaa", key="dxf_del_all",
                      type="primary", on_click=_evict_all)
    products, fill = [], _FillOffer()
    for idx, (fid, part) in enumerate(parts):
        fill.slot = st.empty()  # above the card: where the question goes if it asks
        product = _render_part_config(fid, part, idx, materials, lookup,
                                      per_part_angles, fill)
        if product is not None:
            products.append(product)
    return products


def _render_settings() -> tuple[float, str, SheetSettings]:
    """Margin, nesting mode and the folded sheet settings; returns
    ``(margin_pct, nest_mode, sheet)``. The search time is read later, next
    to the run button."""
    st.divider()
    margin_pct, nest_mode = render_main_settings(
        margin_key="dxf_margin_pct",
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen",
    )
    with st.expander(ADVANCED_LABEL):
        sheet = render_nesting_inputs(key_prefix="dxf")
    st.caption(settings_summary(sheet.values(), NESTING_LABELS))
    return margin_pct, nest_mode, sheet


def _sig(key: tuple, products: list[dict], settings: _Settings) -> str:
    """Stable cache key: material + thickness, the parts (quantity, nesting
    angles), the margin and the prices — what decides the nesting and its
    price. With the prices in it, a price set or changed later (say copper's,
    first left empty) makes the group new again instead of keeping the result
    made without it.

    The nesting mode is left out: a part nested alone is the same nesting in
    either mode, so switching to "separate" keeps a group of one part, and
    back again keeps each single-part group. The Sparrow settings are left
    out too: they are saved with the result (``entry["nesting"]``), so
    changing one doesn't drop every result.
    """
    material, thickness = key[0], key[1]
    prod_sig = ",".join(f"{p['id']}:{p['qty']}:{'/'.join(map(str, p['angles']))}"
                        for p in products)
    prices = [(size, settings.lookup.get((thickness, f"{material} | {size}")))
              for size in get_sizes_for_material(settings.lookup, material)]
    return f"{material}|{thickness}|{prod_sig}|{settings.margin_pct}|{prices}"


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

    def run_fn(instance, *, separation):
        progress.run_started()
        return run_sparrow(instance, executable=exe, time_limit_sec=settings.time_limit,
                           min_item_separation=separation)

    def nest(key: tuple, prods: list[dict]) -> dict | None:
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        parts, areas = _parts_for_group(prods)
        result = run_with_progress(
            progress, _group_label(key, prods),
            sparrow_options,
            lookup, material, thickness, thickness_mm, parts,
            run_fn=run_fn, margin_pct=settings.margin_pct,
            edges=settings.sheet.gaps(), rankavali_mm=settings.sheet.rankavali_mm,
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
            draw_layout=lambda active: draw_sparrow_layout(active, entry["parts"]),
            note=note,
        )

    prices, grand_total, missing = render_groups(groups, render_one)
    if missing and grand_total is None:
        st.info("Paina **Laske levykäyttö** laskeaksesi levytarpeen ja hinnan.")
        return
    if missing:
        st.warning("Yhteissumma sisältää vain lasketut ryhmät — paina **Laske "
                   "levykäyttö** laskeaksesi loput. Jo laskettuja ei "
                   "lasketa uudelleen.")

    ready = [p for p in products if is_ready(p)]
    render_pieces_summary(ready, prices,
                          weight_label="Osien yhteispaino (kg)", lead="Osa",
                          areas_mm2=areas)


def _parts_for_group(products: list[dict]) -> tuple[list, dict[str, float]]:
    """Sparrow parts for a group, plus each product's real area (mm²/piece).

    The parts come from the same read result the card showed; the area is the
    outline minus its holes.
    """
    parts = [
        part_from_report(p["report"], int(p["qty"]), p["angles"]) for p in products
    ]
    areas = {p["id"]: net_area(sp.outer, sp.holes) for p, sp in zip(products, parts)}
    return parts, areas


# ── Part cards ────────────────────────────────────────────────────────────────
#
# _ingest() reads each dropped file once with core.dxf.read_dxf into session
# state and empties the drop area; the cards are the file list, and a card's
# Poista button removes its file. _render_part_config() draws a
# card — preview, measured size, material / thickness / quantity —
# and returns the product dict the pricing uses, or None (with the reasons
# shown) when the part cannot be priced. The card and the pricing share the
# same DxfReport, so what the card shows is exactly what Sparrow nests.

_STORE   = "dxf_store"         # {file_id: DxfFile}
_REPORTS = "dxf_part_reports"  # {file_id: DxfReport}
_CONFIG  = "dxf_part_config"   # {file_id: {"material", "thickness"}}
_INBOX   = "dxf_inbox"         # bumped to give the uploader a fresh, empty key
_FILL_ASKED = "dxf_fill_asked" # the "same material for the others?" question is answered
_DUPES   = "dxf_dupes"         # (names of dropped files already on a card, warn until)
_DUPE_WARNING_S = 15           # how long that warning stays


def _ingest(uploaded) -> bool:
    """Move newly dropped files into the store, in upload order, and empty the
    drop area (Streamlit can't remove one file from an uploader, only start a
    new one). A file whose name is already on a card is not added again; its
    name goes to ``_DUPES`` for the warning (a drop without one clears it). True when something was dropped."""
    store: dict = st.session_state.setdefault(_STORE, {})
    dropped = [up for up in uploaded or [] if up.file_id not in store]
    names = {dxf.name for dxf in store.values()}
    dupes = []
    for up in dropped:
        if up.name in names:
            dupes.append(up.name)
            continue
        store[up.file_id] = read_dxf(up.getvalue(), up.name)
        names.add(up.name)
    if dropped:
        st.session_state[_INBOX] = st.session_state.get(_INBOX, 0) + 1
        st.session_state[_DUPES] = (dupes, time.time() + _DUPE_WARNING_S)
    if len(dropped) > len(dupes):
        st.session_state.pop(_FILL_ASKED, None)  # new empty parts: ask again
    return bool(dropped)


def _evict(fid: str) -> None:
    """A card's "Poista" button: drop everything kept for the file, including
    its widget state."""
    st.session_state.get(_STORE, {}).pop(fid, None)
    cfg = st.session_state.get(_CONFIG, {}).pop(fid, {})
    st.session_state.get(_REPORTS, {}).pop(fid, None)
    for key in (*_material_keys(fid, cfg), f"dxf_q_{fid}", f"dxf_unit_ok_{fid}",
                *(f"dxf_angle_{a}_{fid}" for a in NESTING_ANGLES)):
        st.session_state.pop(key, None)


def _evict_all() -> None:
    """The "Poista kaikki" button: every card's "Poista" at once."""
    for fid in list(st.session_state.get(_STORE, {})):
        _evict(fid)
    st.session_state.pop(_FILL_ASKED, None)
    st.session_state.pop(_DUPES, None)  # it names cards that are gone


def _render_part_config(
    fid: str,
    dxf: DxfFile,
    idx: int,
    materials: list[str],
    lookup: dict,
    per_part_angles: bool = False,
    fill: "_FillOffer | None" = None,
) -> dict | None:
    """Draw one part's card, preview left and inputs right; return its product
    dict, or None if not priceable. Its ``angles`` are None (set later from the
    run row) unless ``per_part_angles``. ``fill`` carries the "same material?"
    question from card to card (see ``_offer_same_material``)."""
    with st.container(border=True):
        hdr = st.columns([6, 2, 1], vertical_alignment="center")
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}")
        hdr[2].button("Poista", key=f"dxf_del_{fid}", on_click=_evict, args=(fid,))

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
            if fill is not None:
                _offer_same_material(fid, idx, material, thickness, fill)
            angles = _render_nesting_angles(fid) if per_part_angles else None
        if angles == ():
            return None

    return {
        "id":        fid,
        "name":      dxf.name,
        "material":  material,
        "thickness": thickness,
        "width":     size[0],
        "height":    size[1],
        "qty":       qty,
        "angles":    angles,
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


def _material_keys(fid: str, cfg: dict) -> tuple[str, str, str]:
    """The material, thickness and disabled-thickness selectbox keys of a card.
    ``cfg["v"]`` is bumped to give them new keys: a fresh selectbox starts from
    the stored choice. (Setting or deleting a widget's value doesn't stick —
    the browser sends its old value back, and Streamlit warns.)"""
    v = cfg.get("v", 0)
    suffix = f"_{v}" if v else ""
    return (f"dxf_mat_{fid}{suffix}", f"dxf_th_{fid}{suffix}",
            f"dxf_th_{fid}{suffix}_disabled")


def _render_part_inputs(fid: str, materials: list[str], lookup: dict) -> tuple:
    """Material, thickness and quantity; returns ``(material, thickness, qty)``.

    Material + thickness are persisted per file: the selectboxes are seeded
    from the stored choice, then the current choice is written back.
    """
    cfg = st.session_state.setdefault(_CONFIG, {}).setdefault(
        fid, {"material": None, "thickness": None})
    mat_key, thick_key, _ = _material_keys(fid, cfg)
    material, thickness = render_material_thickness(
        materials, lookup,
        mat_key=mat_key, thick_key=thick_key,
        mat_default=cfg["material"], thick_default=cfg["thickness"],
    )
    cfg["material"] = material
    cfg["thickness"] = thickness
    qty = int(st.number_input("Määrä (kpl)", min_value=1, value=1, step=1,
                              key=f"dxf_q_{fid}"))
    return material, thickness, qty


_ANGLE_LABELS = {0: "0/180", 90: "90/270"}
_ANGLE_HELP = (
    "Kulmat lasketaan piirustuksesta: 0/180 pitää piirustuksen X-akselin "
    "valssaussuunnassa eli levyn pitkän sivun suuntaisena, 90/270 sitä vastaan "
    "kohtisuorassa. Kun molemmat on valittu, osa saa kääntyä vapaasti."
)


_ANGLE_MODE = "dxf_angle_mode"
_SHARED, _PER_PART = "Sama kaikille", "Osakohtainen"


def _render_nesting_angles(fid: str = "") -> tuple[int, ...]:
    """Allowed nesting angles (see ``core.sparrow.NESTING_ANGLES``), both
    ticked by default; () with a warning when none is. ``fid`` keys a card's
    own choice; without it, the run row's shared one."""
    suffix = f"_{fid}" if fid else ""
    cols = st.columns([2, 1, 1], vertical_alignment="center")
    cols[0].markdown("Sallittu nestauskulma", help=_ANGLE_HELP)
    angles = tuple(a for a, col in zip(NESTING_ANGLES, cols[1:])
                   if col.checkbox(_ANGLE_LABELS[a], value=True, key=f"dxf_angle_{a}{suffix}"))
    if not angles:
        st.warning("Valitse vähintään yksi nestauskulma.")
    return angles


def _render_angle_controls(n_parts: int) -> tuple[int, ...] | None:
    """The nesting-angle row above the run button: the angles every part gets,
    or None when they are chosen per part on the cards (a switch offered only
    for several parts, left of the angles)."""
    if n_parts == 1:
        with st.columns([3, 2])[0]:
            return _render_nesting_angles()
    switch, angles = st.columns([2, 3], vertical_alignment="center")
    if switch.radio("Nestauskulma", (_SHARED, _PER_PART), horizontal=True,
                    key=_ANGLE_MODE) == _PER_PART:
        angles.caption("Sallittu nestauskulma valitaan osakorteilla.")
        return None
    with angles:
        return _render_nesting_angles()


@dataclass
class _FillOffer:
    """The "same material?" question for one run of the cards: the last card
    so far with a material and thickness, whether the question is out, and
    the place above the card being drawn."""

    source: tuple[int, str, str] | None = None   # (card index, material, thickness)
    shown: bool = False
    slot: object = None                          # st.empty() above the current card


def _offer_same_material(fid: str, idx: int, material: str | None,
                         thickness: str | None, fill: _FillOffer) -> None:
    """Ask once, between a filled card and the first empty card below it,
    whether the empty cards get that card's material and thickness. Answered,
    it isn't asked again until new files are dropped — then between the last
    old card and the first new one."""
    if material and thickness:
        fill.source = (idx, material, thickness)
        return
    if fill.shown or fill.source is None or st.session_state.get(_FILL_ASKED):
        return
    fill.shown = True
    src_idx, src_mat, src_th = fill.source
    n_empty = len(_empty_cards())
    with fill.slot.container(border=True):
        whom = (f"tyhjille osille ({n_empty} kpl)" if n_empty > 1
                else f"osalle #{idx + 1}")
        st.markdown(f"Käytetäänkö {whom} samaa materiaalia ja paksuutta kuin "
                    f"osalla #{src_idx + 1}: **{src_mat} · {src_th} mm**?")
        yes, no, _ = st.columns([1, 1, 4])
        yes.button("Kyllä", key=f"dxf_fill_yes_{fid}", type="primary",
                   on_click=_fill_material, args=(src_mat, src_th))
        no.button("Ei", key=f"dxf_fill_no_{fid}", on_click=_fill_material, args=(None, None))


def _empty_cards() -> list[str]:
    """The cards with no material or thickness yet."""
    config = st.session_state.get(_CONFIG, {})
    return [f for f in st.session_state.get(_STORE, {})
            if not (config.get(f, {}).get("material") and config.get(f, {}).get("thickness"))]


def _fill_material(material: str | None, thickness: str | None) -> None:
    """Answer the question: give every empty card the material and thickness
    (none for "Ei") and stop asking. The empty cards are found when the button
    is clicked, so a card filled meanwhile keeps its own choice. Their
    selectboxes get new keys (see ``_material_keys``), so they start from the
    stored choice."""
    config = st.session_state.setdefault(_CONFIG, {})
    for f in _empty_cards() if material else []:
        old = config.get(f, {})
        for key in _material_keys(f, old):
            st.session_state.pop(key, None)
        config[f] = {**old, "material": material, "thickness": thickness,
                     "v": old.get("v", 0) + 1}
    st.session_state[_FILL_ASKED] = True


def _part(fid: str, dxf: DxfFile) -> DxfReport:
    """The part from the default cut layers, memoised (building it scans every
    point)."""
    cache: dict = st.session_state.setdefault(_REPORTS, {})
    if fid not in cache:
        cache[fid] = dxf.part()
    return cache[fid]
