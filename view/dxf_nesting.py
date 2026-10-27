"""The DXF tab's nesting: the run row (nesting angle, search time, the
buttons), Sparrow behind a progress bar, and every group's result, cached
until an input changes. A result is kept by what decides the nesting (the
parts and the sheet sizes) and priced again when only the prices change."""

import hashlib
import json
from dataclasses import dataclass, field

from core.dxf import DxfReport
from core.geometry import net_area
from core.pricing import parse_thickness_mm
from core.quote import CleanPart
from core.sheet_cost import (
    group_products,
    is_ready,
    price_by_size,
    reprice,
    scales_evenly,
    sheet_eur,
)
from core.sparrow import (
    NESTING_ANGLES,
    find_executable,
    part_from_report,
    run_sparrow,
    sparrow_options,
)
from view import assemblies
from view.common import NESTING_LABELS, SheetSettings, render_groups
from view.drawing import draw_sparrow_layout, orientation_svg
from view.dxf_files import source_cards
from view.quote import render_batch_prices
from view.sheet_usage import render_group
from view.sparrow_progress import SparrowProgress, run_with_progress
from view.ui import headless, is_headless, st

_CACHE = "dxf_sparrow_cache"    # {signature: computed group}
_RUNS = "dxf_sparrow_runs"      # {instance key: SparrowResult}, every Sparrow run so far
_RUNS_MAX = 2000                # the oldest runs are forgotten past this
_RENEST = "dxf_sparrow_renest"  # {signature} of groups to re-nest on this run


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


@dataclass(frozen=True)
class NestState:
    """Where the nesting is, for the status strip: groups in all, with no
    result yet, and with a result made with other Sparrow settings."""

    groups: int = 0
    new: int = 0
    stale: int = 0
    ready: bool = True      # False: Sparrow is missing, or no nesting angle


def render_nesting(products: list[dict], n_parts: int, lookup: dict, nest_mode: str,
                   sheet: SheetSettings, margin_pct: float, *, nesting: bool = True,
                   prices: bool = False):
    """The run row and every group's result (drawn when ``nesting``), then
    the prices (drawn when ``prices``); both run either way, and Sparrow
    runs only from the buttons. ``products`` are what the part cards return.
    Returns ``(quote.Totals or None, NestState)``."""
    with headless(not nesting):
        priced, state = _render_nesting(products, n_parts, lookup, nest_mode, sheet,
                                        margin_pct)
    with headless(not prices):
        totals = _render_prices(products, priced, margin_pct)
    return totals, state


def _render_nesting(products, n_parts, lookup, nest_mode, sheet, margin_pct):
    """The run row and the groups; returns what ``_show`` priced (or None)
    and the ``NestState``."""
    exe = find_executable()
    if exe is None:
        st.error(
            "Sparrow-suoritustiedostoa ei löytynyt — levylaskenta vaatii sen. "
            "Aseta polku `SPARROW_BIN`-ympäristömuuttujaan tai lisää binääri "
            "`bin/sparrow`-tiedostoksi."
        )
        return None, NestState(ready=False)

    st.divider()
    # Two rows above the button they apply to: the nesting angle, then the
    # search time with the buttons.
    shared = _render_angle_controls(n_parts, products)
    if shared == () or shared is None and any(p["angles"] == () for p in products):
        return None, NestState(ready=False)  # no angle ticked: the warning says so
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
    replanned, priced = _show(groups, settings, cache, _nester(lookup, settings, exe),
                              lambda s: _nester(lookup, s, exe, remembered_only=True),
                              run=run, update=update, renest=renest)
    if run or update or renest or replanned:
        # The button's count was drawn before these groups were nested:
        # redraw it with the new count (e.g. "kaikki laskettu").
        st.rerun()
    return priced, NestState(len(groups), n_new, n_stale)


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
    sizes = sorted(price_by_size(settings.lookup, key[0], key[1]))
    return f"{prod_sig}|{sizes}"


def _pricing(lookup: dict, key: tuple, margin_pct: float) -> dict:
    """What a result was priced with: material, thickness, €/tn by size, margin."""
    return {"material": key[0], "thickness_mm": parse_thickness_mm(key[1]),
            "prices": price_by_size(lookup, key[0], key[1]),
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


def _show(groups, settings: _Settings, cache: dict, nest, replan,
          *, run: bool = False, update: bool = False,
          renest: set = frozenset()) -> tuple[bool, tuple]:
    """Render every group's result. Returns whether a group was planned from
    remembered Sparrow runs (the run button's count is then out of date), and
    what the prices take: ``(groups, areas, render_groups' result)``.

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

    out = render_groups(groups, render_one)
    _, grand_total, missing, _, _ = out
    if missing and grand_total is None:
        st.info("Paina **Laske levykäyttö** laskeaksesi levytarpeen ja hinnan.")
    elif missing:
        st.warning("Yhteissumma sisältää vain lasketut ryhmät — paina **Laske "
                   "levykäyttö** laskeaksesi loput. Jo laskettuja ei "
                   "lasketa uudelleen.")
    return bool(replanned), (groups, areas, out)


def _render_prices(products: list[dict], priced: tuple | None, margin_pct: float):
    """The batch prices, from what ``_show`` priced; returns their totals
    (``quote.Totals``), or None."""
    if priced is None:
        st.info("Levylaskenta ei ole valmis: katso 2 · Nestaus.")
        return None
    groups, areas, (prices, grand_total, missing, sheets, totals) = priced
    if missing and grand_total is None:
        st.info("Levyjä ei ole vielä nestattu: paina **Laske levykäyttö** kohdassa "
                "2 · Nestaus.")
        return None
    if missing:
        st.warning("Hinnoissa ovat vain lasketut ryhmät: laske loput "
                   "kohdassa 2 · Nestaus.")
    ready = [p for p in products if is_ready(p)]
    return render_batch_prices(ready, prices, groups, sheets, totals, lead="Osa", key="dxf",
                               areas_mm2=areas, clean_parts=[_clean_part(p) for p in ready],
                               assemblies=assemblies.current(source_cards()), table=False,
                               margin_pct=margin_pct, missing=bool(missing))


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


_ANGLE_LABELS = {0: "0/180", 90: "90/270"}
_ANGLE_HELP = (
    "Nuoli ja viivat näyttävät valssaussuunnan (levyn pitkä sivu). 0/180: osa "
    "levyssä niin kuin kortti sen näyttää, 90/270: käännettynä poikittain. "
    "Kun molemmat on valittu, osa saa kääntyä vapaasti."
)


ANGLE_MODE = "dxf_angle_mode"
_SHARED, PER_PART = "Sama kaikille", "Osakohtainen"


def render_nesting_angles(fid: str = "", report: DxfReport | None = None,
                          label: str = "Sallittu nestauskulma") -> tuple[int, ...]:
    """Allowed nesting angles (see ``core.sparrow.NESTING_ANGLES``), both
    ticked by default; () with a warning when none is. Each is a tile showing
    the part (``report``'s, else a sample) as it would lie on the sheet, with
    its tick under it, below ``label``. ``fid`` keys a part's own choice;
    without it, the shared one."""
    suffix = f"_{fid}" if fid else ""
    rings = ([report.outline.points, *(h.points for h in report.holes)]
             if report is not None and report.outline else None)
    angles = []
    box = st.container(width=240)   # the tiles side by side, not spread apart
    box.caption(label, help=_ANGLE_HELP)
    for a, col in zip(NESTING_ANGLES, box.columns(len(NESTING_ANGLES))):
        key = f"dxf_angle_{a}{suffix}"
        if not is_headless():
            col.markdown(orientation_svg(rings, a, st.session_state.get(key, True)),
                         unsafe_allow_html=True)
        if col.checkbox(_ANGLE_LABELS[a], value=True, key=key):
            angles.append(a)
    if not angles:
        st.warning("Valitse vähintään yksi nestauskulma.")
    return tuple(angles)


_PER_ROW = 4


def _render_angle_controls(n_parts: int, products: list[dict]) -> tuple[int, ...] | None:
    """The nesting-angle row above the run button: the angles every part gets,
    or None when each part has its own (a switch offered only for several
    parts, left of the angles); then each part's tiles are drawn here, a grid
    under the switch, so they are chosen where they are used. One part is
    drawn on the tiles itself; shared by several, a sample part."""
    if n_parts == 1:
        with st.columns([1, 3])[0]:
            return render_nesting_angles(report=products[0]["report"] if products else None)
    switch, angles = st.columns([2, 3], vertical_alignment="center")
    if switch.radio("Nestauskulma", (_SHARED, PER_PART), horizontal=True,
                    key=ANGLE_MODE) != PER_PART:
        with angles:
            return render_nesting_angles()
    for start in range(0, len(products), _PER_ROW):
        for p, col in zip(products[start:start + _PER_ROW], st.columns(_PER_ROW)):
            with col:
                render_nesting_angles(p["id"], p["report"], p["name"])
    return None
