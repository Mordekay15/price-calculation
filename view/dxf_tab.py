"""The DXF tab: one uploaded DXF per part, configured on its card; parts are
grouped by material + thickness and Sparrow nests the real shapes on every
priced sheet size, behind a button and a progress bar, cached until an input
changes."""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from core.archive import ArchiveError, is_zip, read_path, unpack
from core.pricing import build_lookup, get_sizes_for_material, parse_thickness_mm
from core.dwg import read_drawing
from core.dxf import DxfFile, DxfReport, fasteners_from_texts, is_slit
from core.geometry import net_area
from core.items import (Choice, Source, choose, confirm_unit, group_sources,
                        looks_like_drawing, score_dxf, score_step)
from core.quote import CleanPart
from core.step import available as step_available
from core.step import bends_on
from core.sheet_cost import (
    group_products,
    is_ready,
    reprice,
    scales_evenly,
    sheet_eur,
    size_prices,
)
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
    thicknesses_for,
)
from view.drawing import draw_sparrow_layout, preview_svg
from view import assemblies, attachments, bom, material_agent
from view.quote import current_sizes, render_batch_prices
from view.sheet_usage import render_group
from view.work import piece_size, render_work_inputs, work_costs
from view.sparrow_progress import SparrowProgress, run_with_progress
from view.ui import headless, is_headless, st

_CACHE = "dxf_sparrow_cache"    # {signature: computed group}
_RUNS = "dxf_sparrow_runs"      # {instance key: SparrowResult}, every Sparrow run so far
_RUNS_MAX = 2000                # the oldest runs are forgotten past this
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
    content: "Vedä ja pudota DXF-, DWG-, PDF-, STEP- tai ZIP-tiedostot tähän";
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
        st.info("Lataa vähintään yksi DXF- tai DWG-tiedosto aloittaaksesi.")
        return

    materials = materials_with_copper(lookup)
    cards, doc_links = _auto_fill_materials(parts, materials, lookup)
    material_agent.render(cards, doc_links, attachments.docs(), materials,
                          lambda m: thicknesses_for(lookup, m), _apply_material)
    # Per-part angles are chosen on the cards, drawn above the run row whose
    # switch decides it: read the switch's value from the last run.
    per_part = len(parts) > 1 and st.session_state.get(_ANGLE_MODE) == _PER_PART
    products = _render_part_cards(parts, materials, lookup, per_part)
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
    replanned = _show(products, groups, settings, cache, _nester(lookup, settings, exe),
                      lambda s: _nester(lookup, s, exe, remembered_only=True),
                      run=run, update=update, renest=renest)
    if run or update or renest or replanned:
        # The button's count was drawn before these groups were nested:
        # redraw it with the new count (e.g. "kaikki laskettu").
        st.rerun()


def _render_drop_area() -> list[tuple[str, DxfFile]]:
    """The drop area; returns every part read so far, in upload order."""
    st.html(_DROPZONE_CSS)
    uploaded = st.file_uploader(
        "Lataa DXF-, DWG-, PDF- tai STEP-tiedostot tai ZIP",
        accept_multiple_files=True,
        key=f"dxf_uploader_{st.session_state.get(_INBOX, 0)}",
        help="Vedä tiedostot alueelle tai valitse ne koneelta. Voit ladata useita "
             "tiedostoja kerralla. Saman osanumeron tiedostot (DXF, DWG, STEP, PDF) "
             "kootaan yhdelle osakortille, ja paras muoto valitaan niistä "
             "automaattisesti; kortin Poista-painike poistaa osan. DWG-tiedosto "
             "muunnetaan ensin DXF:ksi (ODA File Converter tai LibreDWG). Muut "
             "tiedostot (esim. Creon .prt- ja .drw-tiedostot) ohitetaan. ZIP-tiedosto "
             "puretaan: jokainen kansio on yleensä yksi osa, ja kansio auttaa "
             "yhdistämään tiedostot oikeaan osaan.",
    )
    new = _ingest(list(uploaded or []) + _render_path_reader())
    if new:
        st.rerun()  # redraw with the drop area empty
    parts = _sync()
    assemblies.sync_from_models()
    skipped = st.session_state.get(_SKIPPED)
    if skipped:
        more = len(skipped) - _SKIPPED_SHOWN
        st.caption(f"Ohitettiin {len(skipped)} tiedostoa, joita ei tarvita "
                   f"hinnoitteluun: {', '.join(skipped[:_SKIPPED_SHOWN])}"
                   + (f" ja {more} muuta" if more > 0 else ""))
    _render_waiting(None)
    _warn_duplicates(parts)
    labels = {fid: f"#{i + 1} · {dxf.name}" for i, (fid, dxf) in enumerate(parts)}
    attachments.render_doc_list(_card_names(), labels)
    items = st.session_state.get(_ITEMS, {})
    bom.render_bom({sid: labels[cid] for cid, item in items.items() for sid in item.members
                    if cid in labels}, _resync)
    return parts


def _waiting(doc_ids=None) -> list[str]:
    """The big STEP files not read yet (of ``doc_ids``, or all)."""
    docs = attachments.docs()
    waiting = st.session_state.get(_WAITING, set())
    return [d for d in (docs if doc_ids is None else doc_ids) if d in waiting and d in docs]


def _read_waiting(doc_ids: list[str]) -> None:
    """Read the waiting STEP models: their parts, bends and thickness join
    the cards."""
    docs, waiting = attachments.docs(), st.session_state.get(_WAITING, set())
    for d in doc_ids:
        bom.add_model(d, docs[d]["name"], docs[d]["load"]())
        waiting.discard(d)
    _sync()


def _render_waiting(doc_ids: list[str] | None) -> None:
    """A large drop's big STEP models are read only when asked, here for all
    of them (``doc_ids`` None) or on the card they belong to."""
    waiting = _waiting(doc_ids)
    if not waiting:
        return
    docs = attachments.docs()
    mb = sum(docs[d]["size"] for d in waiting) / (1 << 20)
    if doc_ids is None:
        label = f"Lue isot STEP-mallit ({len(waiting)} kpl, {mb:.0f} Mt)"
        text = (f"{len(waiting)} isoa STEP-mallia odottaa, jotta lataus olisi nopeampi. "
                "Ne voi lukea kortilla yksi kerrallaan tai kaikki kerralla.")
        key = "dxf_read_waiting"
    else:
        label = "Lue 3D-malli: " + ", ".join(docs[d]["name"] for d in waiting) + f" ({mb:.0f} Mt)"
        text = ("Iso STEP-malli luetaan vasta pyydettäessä (kestää hetken). Siitä tulevat "
                "taivutukset, paksuus ja mitan tarkistus.")
        key = f"dxf_read_waiting_{'_'.join(waiting)}"
    c1, c2 = st.columns([3, 2], vertical_alignment="center")
    c1.caption(text)
    c2.button(label, key=key, on_click=_read_waiting, args=(waiting,))


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
    loose parts come first, then each assembly with its cards in it. The
    nesting angle is on the cards only when it is chosen per part."""
    title, clear = st.columns([6, 2], vertical_alignment="center")
    title.markdown("**Osat**")
    if len(parts) > 1:
        # Behind a popover: one stray click shouldn't drop every card's setup.
        with clear.popover("Poista kaikki", width="stretch"):
            st.button(f"Kyllä, poista kaikki {len(parts)} osaa", key="dxf_del_all",
                      type="primary", on_click=_evict_all)
    attachments.render_checked_count([fid for fid, _ in parts])
    card_of = _card_of()
    index = {fid: idx for idx, (fid, _) in enumerate(parts)}
    per_assembly: dict[str, int] = {}     # card id: kpl in one of each of its assemblies
    for cids in assemblies.cards(card_of).values():
        for cid, n in cids.items():
            per_assembly[cid] = per_assembly.get(cid, 0) + n
    doc_links = _doc_links()
    products: dict[int, dict | None] = {}
    fill = _FillOffer()

    def draw(idxs: list[int], page_key: str) -> None:
        shown = {idxs[i] for i in _render_page_picker(len(idxs), page_key)}
        _keep_widget_state([parts[i][0] for i in idxs if i not in shown])
        for idx in idxs:
            fid, part = parts[idx]
            if idx in shown:
                fill.slot = st.empty()  # above the card: where the question goes if it asks
                products[idx] = _render_part_config(fid, part, idx, materials, lookup,
                                                    per_part_angles, fill,
                                                    doc_links.get(fid, []),
                                                    per_assembly.get(fid, 0))
            else:
                with headless():    # priced as drawn, not drawn
                    products[idx] = _render_part_config(fid, part, idx, materials, lookup,
                                                        per_part_angles, None,
                                                        doc_links.get(fid, []),
                                                        per_assembly.get(fid, 0))

    loose = [idx for idx, (fid, _) in enumerate(parts) if fid not in per_assembly]
    if not loose:
        st.caption("Kaikki osat ovat kokoonpanoissa.")
    draw(loose, _PAGE)

    def draw_assembly(aid: str, cids: dict[str, int]) -> None:
        # A card in two assemblies is drawn in the first one only.
        mine = sorted(index[c] for c in cids if c in index)
        st.caption("Osat: " + " · ".join(f"#{i + 1} {parts[i][1].name} × {cids[parts[i][0]]}"
                                         for i in mine))
        elsewhere = [i for i in mine if i in products]
        for i in elsewhere:
            st.caption(f"#{i + 1} · {parts[i][1].name}: kortti on toisessa kokoonpanossa.")
        draw([i for i in mine if i not in products], f"{_PAGE}_{aid}")

    assemblies.render(card_of, draw_assembly)
    return [products[i] for i in sorted(products) if products[i] is not None]


_PAGE = "dxf_card_page"
_PAGE_SIZE = 20     # cards drawn at once: hundreds would take minutes and the memory


def _render_page_picker(n: int, key: str = _PAGE) -> range:
    """Which cards are drawn: all of a few, else one page of ``_PAGE_SIZE``.
    Every card is priced either way (see ``view.ui``)."""
    pages = -(-n // _PAGE_SIZE)
    if pages <= 1:
        return range(n)
    if st.session_state.get(key, 0) >= pages:
        st.session_state[key] = 0
    page = st.selectbox(
        "Näytettävät osat", range(pages), key=key,
        format_func=lambda i: f"#{i * _PAGE_SIZE + 1}–{min(n, (i + 1) * _PAGE_SIZE)}",
        help=f"Osia näytetään {_PAGE_SIZE} kerrallaan, jotta sivu pysyy nopeana. Kaikki "
             f"{n} osaa hinnoitellaan silti, myös piilossa olevat.")
    return range(page * _PAGE_SIZE, min(n, (page + 1) * _PAGE_SIZE))


# Keys of a card's buttons: a button's value can't be set (see _keep_widget_state).
_BUTTON_KEYS = ("dxf_del_", "dxf_fill_yes_", "dxf_fill_no_", "dxf_doc_dl_",
                "dxf_read_waiting", "dxf_step_del_", "dxf_doc_del_")


def _keep_widget_state(fids: list[str]) -> None:
    """Keep the inputs of the cards not drawn: Streamlit forgets a widget's
    value when the widget isn't drawn, and setting it through the session
    keeps it. (A card's widget keys end in its id, or have it before "_".)"""
    if not fids:
        return
    ss = st.session_state
    pattern = re.compile("(?:" + "|".join(map(re.escape, sorted(fids, key=len, reverse=True)))
                         + ")(?:_|$)")
    for key in list(ss.keys()):
        if (isinstance(key, str) and not key.startswith(_BUTTON_KEYS)
                and pattern.search(key)):
            ss[key] = ss[key]


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
    """Stable cache key: what decides the nesting — the parts (quantity,
    nesting angles) and the sheet sizes they are nested on (those with a price
    for the material and thickness).

    What only prices it is left out — material, thickness, margin, the €/tn —
    so changing one keeps the result: ``_priced`` prices it again, or plans
    it again from the remembered Sparrow runs, without the calculate button.
    The nesting mode is left out too: a part nested alone is the same nesting
    in either mode. The Sparrow settings are saved with the result
    (``entry["nesting"]``), so changing one doesn't drop every result.
    """
    prod_sig = ",".join(f"{p['id']}:{p.get('source', p['id'])}:{p['qty']}:"
                        f"{'/'.join(map(str, p['angles']))}" for p in products)
    sizes = sorted({(w, h) for w, h, _ in size_prices(settings.lookup, key[0], key[1])})
    return f"{prod_sig}|{sizes}"


def _pricing(lookup: dict, key: tuple, margin_pct: float) -> dict:
    """What a result was priced with: material, thickness, €/tn by size, margin."""
    return {"material": key[0], "thickness_mm": parse_thickness_mm(key[1]),
            "prices": {(w, h): p for w, h, p in size_prices(lookup, key[0], key[1])},
            "margin_pct": margin_pct}


def _sheet_eur(pricing: dict) -> dict:
    """One sheet's price by size, as ``pricing`` prices it."""
    return {(w, h): sheet_eur(w, h, p, pricing["thickness_mm"], pricing["material"],
                              pricing["margin_pct"])
            for (w, h), p in pricing["prices"].items()}


def _priced(entry: dict, now: dict):
    """The entry's result at today's prices ``now``: as it is, re-priced when
    every sheet's price changed by one factor (the same plans stay cheapest),
    or None when they changed unevenly (then it must be planned again)."""
    was = entry.get("pricing")
    if was == now or entry["result"] is None:
        return entry["result"]
    if was is None or not scales_evenly(_sheet_eur(was), _sheet_eur(now)):
        return None
    return reprice(entry["result"], material=now["material"], thickness_mm=now["thickness_mm"],
                   base_ppt=now["prices"], margin_pct=now["margin_pct"])


def _group_label(key: tuple, prods: list[dict]) -> str:
    """The group's heading, e.g. ``S235 · 2 mm``, plus the part's name when it
    is nested on its own."""
    label = f"{key[0]} · {key[1]} mm"
    if len(key) > 2:
        label += " · " + ", ".join(p["name"] for p in prods)
    return label


class _NotRemembered(Exception):
    """A Sparrow question with no remembered answer, in remembered-only mode."""


def _nester(lookup: dict, settings: _Settings, exe, *, remembered_only: bool = False):
    """``nest(key, prods)``: one group nested with Sparrow behind a progress bar,
    returned as a cache entry (None when its thickness can't be read).

    Every successful Sparrow run is remembered for the session: the same
    question (parts, strip, cut gap, search time) gets the same answer without
    running again. ``remembered_only``: plan from remembered runs alone — no
    progress bar; None as soon as a question has no remembered answer (then
    the calculate button is needed). Only a planned sheet nested again
    (``renest_fn``) may still run Sparrow: which sheets those are depends on
    the prices."""
    progress = SparrowProgress()
    # Taken here: the nesting runs in a worker thread, which has no session.
    runs: dict = st.session_state.setdefault(_RUNS, {})

    def key_of(instance, separation) -> str:
        return hashlib.sha1(json.dumps([instance, separation, settings.time_limit],
                                       sort_keys=True).encode()).hexdigest()

    def run_fn(instance, *, separation):
        key = key_of(instance, separation)
        if key in runs:
            return runs[key]
        progress.run_started()
        result = run_sparrow(instance, executable=exe, time_limit_sec=settings.time_limit,
                             min_item_separation=separation)
        if result.ok:                   # a failed run (say a timeout) is tried again
            runs[key] = result
            while len(runs) > _RUNS_MAX:
                runs.pop(next(iter(runs)))
        return result

    def remembered(instance, *, separation):
        key = key_of(instance, separation)
        if key not in runs:
            raise _NotRemembered
        return runs[key]

    def nest(key: tuple, prods: list[dict]) -> dict | None:
        material, thickness = key[0], key[1]
        thickness_mm = parse_thickness_mm(thickness)
        if thickness_mm is None:
            return None
        parts, areas = _parts_for_group(prods)
        args = (lookup, material, thickness, thickness_mm, parts)
        kwargs = dict(margin_pct=settings.margin_pct, edges=settings.sheet.gaps(),
                      rankavali_mm=settings.sheet.rankavali_mm)
        if remembered_only:
            try:
                result = sparrow_options(*args, run_fn=remembered, renest_fn=run_fn, **kwargs)
            except _NotRemembered:
                return None
        else:
            result = run_with_progress(progress, _group_label(key, prods), sparrow_options,
                                       *args, run_fn=run_fn, **kwargs)
        return {"result": result, "parts": parts, "areas": areas,
                "nesting": settings.nesting(),
                "pricing": _pricing(lookup, key, settings.margin_pct)}

    return nest


def _show(products: list[dict], groups, settings: _Settings, cache: dict, nest, replan,
          *, run: bool = False, update: bool = False,
          renest: set = frozenset()) -> bool:
    """Render every group's result, then the parts summary. True when a group
    was planned from remembered Sparrow runs (the run button's count is then
    out of date).

    A group is nested in its own place first — so each result shows as soon
    as it is ready, not after the whole run — when ``run`` and it has no
    result, when ``update`` and its result is outdated, or when its signature
    is in ``renest``. A result is shown at today's prices (``_priced``); when
    they changed unevenly, or a group has no result yet, ``replan(settings)``
    plans it from the remembered runs without the button, if it can.
    """
    areas: dict[str, float] = {}
    current = settings.nesting()
    replanned = []

    def render_one(key, prods):
        sig = _sig(key, prods, settings)
        entry = cache.get(sig)
        if (sig in renest or (run and entry is None)
                or (update and entry is not None and _is_stale(entry, settings))):
            new = nest(key, prods)
            if new is not None:
                cache[sig] = entry = new
        now = _pricing(settings.lookup, key, settings.margin_pct)
        result = None if entry is None else _priced(entry, now)
        if entry is None or (result is None and entry["result"] is not None):
            # No result yet, or the prices changed unevenly (another plan may
            # be cheapest now): plan it again from the remembered runs.
            with st.spinner("Lasketaan muistetuista sijoitteluista…"):
                new = replan(settings if entry is None else _settings_of(entry, settings))(
                    key, prods)
            if new is not None:
                cache[sig] = entry = new
                result = entry["result"]
                replanned.append(sig)
            elif entry is not None:
                cache.pop(sig)          # can't be planned again without Sparrow
                entry = None
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
            key[0], key[1], now["thickness_mm"], result,
            margin_pct=settings.margin_pct, key=f"dxf_su_select::{sig}",
            draw_layout=lambda active: draw_sparrow_layout(active, entry["parts"]),
            note=note,
        )

    prices, grand_total, missing, sheets, totals = render_groups(groups, render_one)
    if missing and grand_total is None:
        st.info("Paina **Laske levykäyttö** laskeaksesi levytarpeen ja hinnan.")
        return bool(replanned)
    if missing:
        st.warning("Yhteissumma sisältää vain lasketut ryhmät — paina **Laske "
                   "levykäyttö** laskeaksesi loput. Jo laskettuja ei "
                   "lasketa uudelleen.")

    ready = [p for p in products if is_ready(p)]
    render_pieces_summary(ready, prices,
                          weight_label="Osien yhteispaino (kg)", lead="Osa",
                          areas_mm2=areas, work=work_costs(ready, groups, sheets))
    render_batch_prices(ready, prices, groups, sheets, totals, lead="Osa", key="dxf",
                        areas_mm2=areas, clean_parts=[_clean_part(p) for p in ready],
                        assemblies=assemblies.current(_card_of()))
    return bool(replanned)


def _clean_part(product: dict) -> CleanPart:
    """A part's cut geometry for the clean-DXF export, as the card shows it."""
    report: DxfReport = product["report"]
    return CleanPart(product["name"], product["material"] or "", product["thickness"] or "",
                     report.outline.points, [h.points for h in report.holes],
                     list(report.open_lines), list(report.reference_lines))


def _settings_of(entry: dict, settings: _Settings) -> _Settings:
    """``settings`` with the Sparrow settings the entry was nested with, so
    planning it again asks the questions whose answers are remembered."""
    nesting = entry.get("nesting", settings.nesting())
    sheet = SheetSettings(nesting["rankavali_mm"], tuple(nesting["edges_mm"]))
    return _Settings(settings.nest_mode, sheet, nesting["time_limit"], settings.margin_pct,
                     settings.lookup)


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
# _ingest() reads each dropped file once with core.dwg.read_drawing (a DWG is
# converted to DXF first) into session state and empties the drop area; the
# cards are the file list, and a card's Poista button removes its file.
# _render_part_config() draws a card — preview, measured size, material /
# thickness / quantity — and returns the product dict the pricing uses, or None (with the reasons
# shown) when the part cannot be priced. The card and the pricing share the
# same DxfReport, so what the card shows is exactly what Sparrow nests.

_STORE   = "dxf_store"         # {card id: DxfFile}: each card's chosen shape
_REPORTS = "dxf_part_reports"  # {card id: DxfReport}
_CONFIG  = "dxf_part_config"   # {card id: {"material", "thickness", "qty", "source"}}
_FILES   = "dxf_files"         # {file id: DxfFile}: every dropped DXF/DWG, in drop order
_ITEMS   = "dxf_items"         # {card id: _Item}: the files on each card
_HIDDEN  = "dxf_hidden"        # model parts removed from the cards
_SRC_REPORTS = "dxf_source_reports"  # {source id: DxfReport}, for choosing
_BEND_LINES = "dxf_bend_lines"  # {(card, shape, flat): STEP bend lines on the DXF}
_INBOX   = "dxf_inbox"         # bumped to give the uploader a fresh, empty key
_FILL_ASKED = "dxf_fill_asked" # the "same material for the others?" question is answered
_DUPES   = "dxf_dupes"         # (names of dropped files already on a card, warn until)
_DUPE_WARNING_S = 15           # how long that warning stays
_SKIPPED = "dxf_skipped"       # names of the last drop's files that aren't used
_FOLDERS = "dxf_folders"       # {file id: folder in the dropped ZIP}
_PATH_READS = "dxf_path_reads"  # ZIPs / folders read from disk so far (for file ids)
_PATH_SKIPPED = "dxf_path_skipped"  # what the last disk read skipped
_WAITING = "dxf_waiting_models"  # big STEP files of a large drop, read when asked
_BIG_STEP_BYTES = 5 << 20        # a STEP file this big waits, in a drop of many files
_USED = ("dxf", "dwg", "pdf", "step", "stp")
_SKIPPED_SHOWN = 12            # names listed in the skipped line


def _ingest(uploaded) -> bool:
    """Move newly dropped files into the session, in upload order, and empty
    the drop area (Streamlit can't remove one file from an uploader, only
    start a new one). A file whose name was dropped already is not added
    again; its name goes to ``_DUPES`` for the warning (a drop without one
    clears it). True when something was dropped."""
    files: dict = st.session_state.setdefault(_FILES, {})
    folders: dict = st.session_state.setdefault(_FOLDERS, {})
    docs = attachments.docs()
    dropped = [up for up in uploaded or [] if up.file_id not in files and up.file_id not in docs]
    names = ({(folders.get(f, ""), dxf.name) for f, dxf in files.items()}
             | {(folders.get(d, ""), doc["name"]) for d, doc in docs.items()})
    dupes, skipped = [], st.session_state.pop(_PATH_SKIPPED, [])
    todo = _unzipped(dropped, skipped)
    bar = st.progress(0.0) if len(todo) > 5 else None
    for n, up in enumerate(todo):
        if bar is not None:
            bar.progress(n / len(todo), text=f"Luetaan tiedostoja {n + 1} / {len(todo)}: "
                                             f"{up.name}")
        folder = getattr(up, "folder", "")
        if up.name.rsplit(".", 1)[-1].lower() not in _USED:
            skipped.append(up.name)     # e.g. Creo's part.prt.1, part.drw.1
            continue
        if (folder, up.name) in names:
            dupes.append(up.name)
            continue
        if folder:
            folders[up.file_id] = folder
        data = up.getvalue()
        if attachments.is_doc(up.name):
            attachments.add_doc(up.file_id, up.name, data,
                                load=up.getvalue if hasattr(up, "folder") else None)
            if bom.is_step(up.name) and step_available():
                if len(todo) > 5 and len(data) > _BIG_STEP_BYTES:
                    st.session_state.setdefault(_WAITING, set()).add(up.file_id)
                else:
                    bom.add_model(up.file_id, up.name, data)
        else:
            files[up.file_id] = read_drawing(data, up.name)
            files[up.file_id].forget_lines()   # the part is all that's used (memory)
        names.add((folder, up.name))
    if bar is not None:
        bar.empty()
    if dropped:
        _sync()
        _release_memory()
        st.session_state[_INBOX] = st.session_state.get(_INBOX, 0) + 1
        st.session_state[_DUPES] = (dupes, time.time() + _DUPE_WARNING_S)
        st.session_state[_SKIPPED] = skipped
    if len(dropped) > len(dupes) + len(skipped):
        st.session_state.pop(_FILL_ASKED, None)  # new empty parts: ask again
    return bool(dropped)


def _release_memory() -> None:
    """Give memory freed while reading back to the system (Linux, e.g. a
    hosted app): Python keeps it otherwise, and the host counts it."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass    # not glibc (Windows, macOS)


def _render_path_reader() -> list:
    """"Lue koneelta": a ZIP or a folder read straight from this computer's
    disk, for an RFQ too big to upload (no upload limit, and it is read one
    file at a time). Returns its files once the button is pressed."""
    with st.expander("Lue ZIP tai kansio koneelta (iso tarjouspyyntö)"):
        st.caption("Liitä ZIP-tiedoston tai kansion polku, esim. "
                   "`D:\\Tarjoukset\\RFQ 580-07.zip` (Resurssienhallinnassa: Shift + "
                   "hiiren oikea → Kopioi polkuna). Tiedostoa ei ladata selaimen kautta, "
                   "joten kokorajaa ei ole.")
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        path = c1.text_input("Polku", key="dxf_path", label_visibility="collapsed",
                             placeholder="D:\\Tarjoukset\\RFQ.zip")
        if not c2.button("Lue", key="dxf_path_read", disabled=not path.strip()):
            return []
    skipped = st.session_state.setdefault(_PATH_SKIPPED, [])
    skipped.clear()
    try:
        entries = read_path(path, _is_used, skipped)
    except ArchiveError as exc:
        st.error(str(exc))
        return []
    n = st.session_state[_PATH_READS] = st.session_state.get(_PATH_READS, 0) + 1
    return [SimpleNamespace(file_id=f"disk{n}/{i}", name=e.name, folder=e.folder,
                            getvalue=e.load) for i, e in enumerate(entries)]


def _is_used(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in _USED


def _unzipped(dropped, skipped: list[str]) -> list:
    """The dropped files with every ZIP opened: its files, folder by folder,
    each carrying its ``folder`` (see ``core.archive``). A ZIP that can't be
    opened goes to ``skipped``."""
    out = []
    for up in dropped:
        if not is_zip(up.name):
            out.append(up)
            continue
        try:
            entries = unpack(up.getvalue(), _is_used, skipped)
        except ArchiveError:
            skipped.append(f"{up.name} (ei voitu avata)")
            continue
        out += [SimpleNamespace(file_id=f"{up.file_id}/{i}", name=e.name, folder=e.folder,
                                getvalue=e.load)
                for i, e in enumerate(entries)]
    return out


# ── Items: one card per part ──────────────────────────────────────────────────

@dataclass
class _Item:
    """The files on one card (source ids, drop order), the one priced, and
    how each scored (see ``core.items``)."""

    members: list[str]
    chosen: str
    scores: dict[str, Choice]
    names: dict[str, str]
    blank: tuple[float, float] | None = None   # the STEP blank's size, mm


def _sync() -> list[tuple[str, DxfFile]]:
    """Group the dropped files into cards and choose each card's shape.
    A card's id is its first file's id, so it keeps its settings while files
    are added; a card that a drawing joins passes its settings on."""
    ss = st.session_state
    files: dict = ss.setdefault(_FILES, {})
    reports: dict = ss.setdefault(_SRC_REPORTS, {})
    config: dict = ss.setdefault(_CONFIG, {})
    old: dict = ss.get(_ITEMS, {})

    def shape(sid: str) -> DxfFile:
        return files[sid] if sid in files else bom.flat_file(sid)

    def report(sid: str) -> DxfReport:
        if sid not in reports:
            reports[sid] = shape(sid).part()
        return reports[sid]

    sources = ([Source(fid, d.name, "dxf", list(d.texts), folder=_folder_of(fid))
                for fid, d in files.items()]
               + bom.sources(ss.setdefault(_HIDDEN, set()), _folder_of))
    groups = group_sources(sources, lambda sid: sid in files
                           and looks_like_drawing(report(sid)))
    store, items = {}, {}
    for members in groups:
        cid = members[0]
        if cid not in config:
            inherited = next((config[m] for m in members if m in config), None)
            if inherited is not None:
                config[cid] = dict(inherited)
        models = [bom.source(m) for m in members if bom.is_source(m)]
        blank = _blank_size(models[0][2]) if models else None
        scores = {sid: (score_dxf(report(sid), files[sid].unit_guessed, files[sid].texts, blank)
                        if sid in files else score_step(True)) for sid in members}
        chosen = choose(scores, config.get(cid, {}).get("source")) or members[0]
        if cid in old and old[cid].chosen != chosen:
            ss.get(_REPORTS, {}).pop(cid, None)
        store[cid] = shape(chosen)
        items[cid] = _Item(members, chosen, scores, {m: shape(m).name for m in members}, blank)
        if models and "qty" not in config.get(cid, {}):
            config.setdefault(cid, {"material": None, "thickness": None})["qty"] = max(
                part.qty for _, part, _ in models)
    ss[_STORE], ss[_ITEMS] = store, items
    return list(store.items())


def _resync() -> None:
    """After the models changed (K factor, a model removed): unfold and
    choose again."""
    st.session_state.pop(_BEND_LINES, None)
    st.session_state.get(_SRC_REPORTS, {}).clear()
    st.session_state.get(_REPORTS, {}).clear()
    _sync()


def _blank_size(flat) -> tuple[float, float] | None:
    if flat is None or not flat.outline:
        return None
    xs = [p[0] for p in flat.outline]
    ys = [p[1] for p in flat.outline]
    return max(xs) - min(xs), max(ys) - min(ys)


def _card_of() -> dict[str, str]:
    """Every file's card: ``{source id: card id}``."""
    return {sid: cid for cid, item in st.session_state.get(_ITEMS, {}).items()
            for sid in item.members}


def _folder_of(sid: str) -> str:
    """The ZIP folder a file came in ("" when dropped on its own); a STEP
    model's part is in its file's folder."""
    return st.session_state.get(_FOLDERS, {}).get(sid.split("|", 1)[0], "")


def _doc_links() -> dict[str, list[str]]:
    """Each card's PDFs (see ``attachments.links``)."""
    card_folders = {cid: {f for f in map(_folder_of, item.members) if f}
                    for cid, item in st.session_state.get(_ITEMS, {}).items()}
    return attachments.links(_card_names(), card_folders, st.session_state.get(_FOLDERS, {}))


def _card_names() -> dict[str, list[str]]:
    """Every card's file names (for linking the PDFs)."""
    return {cid: list(item.names.values())
            for cid, item in st.session_state.get(_ITEMS, {}).items()}


def _with_all_texts(parts: list[tuple[str, DxfFile]]) -> list[tuple[str, object]]:
    """The cards for the material search: the texts of all their files (the
    drawing's title block names the material, the flat pattern may not) and
    the STEP model's sheet thickness."""
    files = st.session_state.get(_FILES, {})
    items = st.session_state.get(_ITEMS, {})
    out = []
    for cid, dxf in parts:
        item = items.get(cid)
        texts = [t for m in (item.members if item else [cid]) if m in files
                 for t in files[m].texts] or list(dxf.texts)
        flats = _step_flats(item)
        out.append((cid, SimpleNamespace(
            name=dxf.name, texts=texts,
            step_thickness=flats[0].thickness_mm if flats else None)))
    return out


def _auto_fill_materials(parts, materials: list[str], lookup: dict) -> tuple[list, dict]:
    """Fill the empty cards' material and thickness from their own files (see
    ``material_agent.auto_fill``); returns the cards for the material panel
    and their linked files."""
    cards, doc_links = _with_all_texts(parts), _doc_links()
    config = st.session_state.get(_CONFIG, {})
    material_agent.auto_fill(cards, doc_links, attachments.docs(), materials,
                             lambda m: thicknesses_for(lookup, m), _apply_material,
                             lambda fid: bool(config.get(fid, {}).get("material")))
    return cards, doc_links


def _step_flats(item: _Item | None) -> list:
    """The unfolded STEP parts on a card."""
    if item is None:
        return []
    return [src[2] for src in map(bom.source, item.members)
            if src is not None and src[2] is not None and src[2].is_sheet]


def _bends(report: DxfReport, item: _Item | None) -> int:
    """The bends of a part: the STEP model's when the card has one (a flat
    DXF often has no bend lines), else the DXF's bend lines."""
    flats = _step_flats(item)
    return len(flats[0].bends) if flats else report.bends


def _fasteners(dxf: DxfFile, item: _Item | None) -> int:
    """The fasteners the notes of the card's files ask for (the most any one
    file asks for: a flat and its drawing repeat the same note)."""
    files = st.session_state.get(_FILES, {})
    texts = [files[m].texts for m in (item.members if item else []) if m in files]
    return max((fasteners_from_texts(t) for t in [dxf.texts, *texts]), default=0)


def _render_item_files(fid: str, item: _Item) -> None:
    """The card's files, which one is priced and why, and a choice to price
    another."""
    valid = [sid for sid in item.members if item.scores[sid].valid]
    lines = []
    for sid in item.members:
        mark = "✓ " if sid == item.chosen else ""
        lines.append(f"{mark}{item.names[sid]} ({item.scores[sid].note})")
    st.caption("Tiedostot: " + " · ".join(lines))
    if len(valid) < 2:
        return
    cfg = st.session_state.setdefault(_CONFIG, {}).setdefault(
        fid, {"material": None, "thickness": None})
    options = ["", *valid]
    current = cfg.get("source") if cfg.get("source") in valid else ""
    choice = st.selectbox(
        "Muoto tiedostosta", options, index=options.index(current), key=f"dxf_src_{fid}",
        format_func=lambda sid: (f"Automaattinen ({item.names[item.chosen]})" if not sid
                                 else item.names[sid]),
        help="Hinnoiteltava muoto valitaan automaattisesti: puhdas tasoaihio ennen "
             "piirustusta, ja STEP-mallin kanssa samankokoinen DXF ennen muita.")
    if (choice or None) != cfg.get("source"):
        cfg["source"] = choice or None
        st.session_state.get(_REPORTS, {}).pop(fid, None)
        st.rerun()


def _evict(fid: str) -> None:
    """A card's "Poista" button: drop the card's files (its PDFs too, unless
    another card has them) and everything kept for it, including its widget
    state."""
    ss = st.session_state
    item = ss.get(_ITEMS, {}).get(fid)
    doc_links = _doc_links()
    files = ss.get(_FILES, {})
    for sid in (item.members if item else [fid]):
        files.pop(sid, None)
        ss.get(_FOLDERS, {}).pop(sid, None)
        ss.get(_SRC_REPORTS, {}).pop(sid, None)
        src = bom.source(sid)
        if src is not None:
            model_id = sid.split("|", 1)[0]
            if len(src[0]["model"].parts) == 1:
                bom.remove_model(model_id)
            else:
                ss.setdefault(_HIDDEN, set()).add(sid)
    others = {d for cid, ids in doc_links.items() if cid != fid for d in ids}
    for d in doc_links.get(fid, []):
        if d not in others:
            attachments.remove_doc(d)
    ss.get(_STORE, {}).pop(fid, None)
    cfg = ss.get(_CONFIG, {}).pop(fid, {})
    ss.get(_REPORTS, {}).pop(fid, None)
    for key in (*_material_keys(fid, cfg), f"dxf_q_{fid}", f"dxf_unit_ok_{fid}",
                f"dxf_src_{fid}", attachments.CHECKED.format(fid),
                *(f"dxf_angle_{a}_{fid}" for a in NESTING_ANGLES)):
        ss.pop(key, None)
    _sync()


def _evict_all() -> None:
    """The "Poista kaikki" button: every card's "Poista" at once."""
    for fid in list(st.session_state.get(_STORE, {})):
        _evict(fid)
    for doc_id in list(attachments.docs()):
        attachments.remove_doc(doc_id)
    for key in (bom.MODELS, _FILES, _ITEMS, _HIDDEN, _SRC_REPORTS, _FILL_ASKED,
                _DUPES, _SKIPPED,   # these warnings name files that are gone
                _FOLDERS, _WAITING,
                _BEND_LINES,
                *material_agent.AUTO_KEYS, *assemblies.KEYS):
        st.session_state.pop(key, None)


def _render_part_config(
    fid: str,
    dxf: DxfFile,
    idx: int,
    materials: list[str],
    lookup: dict,
    per_part_angles: bool = False,
    fill: "_FillOffer | None" = None,
    doc_ids: list[str] = (),
    per_assembly: int = 0,
) -> dict | None:
    """Draw one part's card, preview left and inputs right; return its product
    dict, or None if not priceable. Its ``angles`` are None (set later from the
    run row) unless ``per_part_angles``. ``fill`` carries the "same material?"
    question from card to card (see ``_offer_same_material``). ``doc_ids``
    are the supporting files linked to it (see ``view.attachments``). A card in
    an assembly (``per_assembly`` kpl in one) takes its quantity from the batch
    sizes and leaves its fasteners and welding to the assembly."""
    in_assembly = per_assembly > 0
    with st.container(border=True):
        hdr = st.columns([5, 2, 2, 1], vertical_alignment="center")
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}")
        hdr[2].checkbox("Tarkastettu", key=attachments.CHECKED.format(fid))
        hdr[3].button("Poista", key=f"dxf_del_{fid}", on_click=_evict, args=(fid,))
        item = st.session_state.get(_ITEMS, {}).get(fid)
        folders = sorted({f for f in map(_folder_of, item.members if item else [fid]) if f})
        if folders:
            st.caption("📁 " + " · ".join(folders))
        if item is not None and len(item.members) > 1:
            _render_item_files(fid, item)

        # Text found in the drawing (Mat=…, Thk=…) — shown to cross-check the
        # material choice, never nested.
        if dxf.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(dxf.texts))
        if not is_headless():   # only drawn: nothing here changes the price
            attachments.render_card_docs(fid, list(doc_ids))
            _render_waiting(list(doc_ids))
            if item is not None:
                bom.render_card_model(fid, [s for s in item.members if bom.is_source(s)])

        preview_col, input_col = st.columns([1, 3])
        report = _part(fid, dxf)
        with input_col:
            size = _checked_size(fid, dxf, report, hdr[1], _unit_reason(dxf, report, item, doc_ids))
        if not is_headless():
            with preview_col:
                _render_preview(report, _model_bends(fid, report, item))
        if size is None:
            return None
        with input_col:
            material, thickness, qty = _render_part_inputs(fid, materials, lookup, per_assembly)
            if fill is not None:
                _offer_same_material(fid, idx, material, thickness, fill)
            angles = _render_nesting_angles(fid) if per_part_angles else None
            work = render_work_inputs(fid, material=material, thickness=thickness, qty=qty,
                                      cut_length_m=report.cut_length_mm / 1000,
                                      bends=_bends(report, item), threads=report.threads,
                                      fasteners=0 if in_assembly else _fasteners(dxf, item),
                                      in_assembly=in_assembly,
                                      **piece_size(report.outline.area_mm2 - sum(
                                          h.area_mm2 for h in report.holes),
                                          material, thickness))
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
        "work":      work,
        "source":    item.chosen if item is not None else fid,
    }


def _render_preview(report: DxfReport, bends: list | None = None) -> None:
    """The part as it will be cut; shapes left outside it are greyed out.
    ``bends`` are the STEP model's bend lines placed on it."""
    preview = preview_svg(report, bends=bends)
    if preview:
        st.markdown(preview, unsafe_allow_html=True)
    if bends:
        st.caption("Taivutuslinjat STEP-mallista: :green[ylös] · :orange[alas]")
    slits = sum(map(is_slit, report.open_lines))
    if slits:
        st.caption(f"{slits} rakoa (suoria leikkauksia osan sisällä) on mukana "
                   "leikkuupituudessa.")
    if report.dropped_count:
        st.caption(
            f"Osan ulkopuolelta ohitettiin {report.dropped_count} kuviota "
            "(esim. lisäkuvat tai irralliset viivat) — harmaalla esikatselussa."
        )


def _model_bends(fid: str, report: DxfReport, item: _Item | None) -> list | None:
    """The card's STEP bend lines on its DXF (a flat DXF often has no bend
    lines of its own), worked out once per shape."""
    flats = _step_flats(item)
    if not flats or report.bends or report.outline is None:
        return None
    cache = st.session_state.setdefault(_BEND_LINES, {})
    key = (fid, item.chosen, id(flats[0]))
    if key not in cache:
        cache[key] = bends_on(flats[0], [report.outline.points]
                              + [h.points for h in report.holes])
    return cache[key]


def _unit_reason(dxf: DxfFile, report: DxfReport, item: _Item | None, doc_ids) -> str | None:
    """Why the guessed unit of a card's DXF is right: its STEP model or PDF
    drawing agrees with the size (see ``core.items.confirm_unit``)."""
    if not dxf.unit_guessed or report.outline is None:
        return None
    return confirm_unit((report.outline.width_mm, report.outline.height_mm),
                        item.blank if item is not None else None,
                        [attachments.text(d) for d in doc_ids])


def _checked_size(fid: str, dxf: DxfFile, report: DxfReport, badge,
                  confirmed: str | None = None) -> tuple | None:
    """The part's ``(width, height)`` in mm, or None (with the reason shown)
    while it can't be priced. ``badge`` is the card header's status slot;
    ``confirmed`` why a guessed unit is right (see ``core.items.confirm_unit``)."""
    if report.problems:
        badge.markdown(":red[ei hinnoiteltavissa]")
        st.error(
            "**Tätä osaa ei voi vielä hinnoitella:**\n\n"
            + "\n".join(f"- {p}" for p in report.problems)
        )
        return None

    width = round(report.outline.width_mm, 1)
    height = round(report.outline.height_mm, 1)

    # No unit in the file: the part's other files (the STEP model, the PDF
    # drawing's dimensions) confirm the guess; else show the size the guess
    # gives and price only once the user confirms it. The warning goes above
    # the checkbox and only while it is unticked.
    if dxf.unit_guessed and confirmed:
        st.caption(f"Mittayksikkö puuttuu tiedostosta; {report.unit_label} vahvistettu: "
                   f"{confirmed}.")
    elif dxf.unit_guessed:
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

    # The drawing's own unit only when it isn't mm ("… mm · mm" said nothing).
    drawn = "" if report.unit_label == "mm" else f" · piirretty: {report.unit_label}"
    badge.markdown(f":gray[{width:g} × {height:g} mm{drawn}]")
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


def _render_part_inputs(fid: str, materials: list[str], lookup: dict,
                        per_assembly: int = 0) -> tuple:
    """Material, thickness and quantity; returns ``(material, thickness, qty)``.
    A part in an assembly (``per_assembly`` kpl in one) is nested for the
    largest batch of assemblies, so its quantity is not typed.

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
    note = material_agent.auto_note(fid, material, thickness)
    if note:
        st.caption(note)
    if per_assembly:
        batch = max(current_sizes())
        qty = per_assembly * batch
        st.number_input("Määrä (kpl)", value=qty, disabled=True,
                        help=f"{per_assembly} kpl / kokoonpano × suurin eräkoko {batch}. "
                             "Eräkoot muutetaan sivun yläosan Tarjous-kohdasta.")
        return material, thickness, qty
    qty = int(st.number_input("Määrä (kpl)", min_value=1, value=int(cfg.get("qty", 1)),
                              step=1,
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


def _apply_material(fid: str, material: str, thickness: str | None) -> None:
    """Set one card's material and thickness (a proposal the user ticked);
    its selectboxes get new keys so they start from the new choice."""
    config = st.session_state.setdefault(_CONFIG, {})
    old = config.get(fid, {})
    for key in _material_keys(fid, old):
        st.session_state.pop(key, None)
    config[fid] = {**old, "material": material, "thickness": thickness,
                   "v": old.get("v", 0) + 1}


def _part(fid: str, dxf: DxfFile) -> DxfReport:
    """The part from the default cut layers, memoised (building it scans every
    point)."""
    cache: dict = st.session_state.setdefault(_REPORTS, {})
    if fid not in cache:
        cache[fid] = dxf.part()
    return cache[fid]
