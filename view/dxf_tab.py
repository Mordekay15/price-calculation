"""The file tab's three numbered steps: 1 · Osat ja tiedostot (the drop area
and the part cards with their work, ``view.dxf_files``, or the same parts as
one table), 2 · Nestaus (the settings, the nesting and every group's sheets,
``view.dxf_nesting``) and 3 · Hinnoittelu (the totals, the breakdown by
batch size and the downloads). Above each, a status strip says what is still
missing and where, and leads to the next step. A card shows a part's preview, measured size,
material, thickness, quantity and work, and returns the product dict the
pricing uses, or None (with the reasons shown) when the part cannot be
priced. The card and the pricing share the same DxfReport, so what the card
shows is exactly what Sparrow nests.

Only the open screen is drawn, but every screen runs the cards, the
nesting and the prices (headless where not drawn, see ``view.ui``), so the
quote bar above the tabs shows the live total on each."""

import re
from dataclasses import dataclass

from core.assemblies import per_unit
from core.dxf import DxfFile, DxfReport, is_slit
from core.items import NO_BLANK, confirm_unit
from core.pricing import build_lookup
from core.sheet_cost import is_ready
from view import assemblies, attachments, bom, material_agent
from view.undo import render_undo, undoable
from view.common import (
    materials_with_copper,
    render_material_thickness,
    render_page_picker,
    render_settings,
)
from view.drawing import preview_svg
from view.dxf_files import (
    CONFIG,
    FILL_ASKED,
    ITEMS,
    REPORTS,
    Item,
    apply_material,
    auto_fill_materials,
    bom_quantities,
    card_bends,
    card_docs,
    card_fasteners,
    card_forms,
    card_report,
    empty_cards,
    evict,
    evict_all,
    fill_material,
    folder_of,
    material_keys,
    model_bends,
    render_drop_area,
    render_waiting,
    source_cards,
    sync,
    sync_assemblies,
)
from view.dxf_nesting import (
    ANGLE_MODE,
    PER_PART,
    NestState,
    render_nesting,
    render_nesting_angles,
)
from view.quote import current_sizes
from view.ui import headless, ids_in, is_headless, keep, st
from view.work import RATE_KEYS, auto_input, render_work_inputs

BOM, NESTING, PRICING = "1 · Osat ja tiedostot", "2 · Nestaus", "3 · Hinnoittelu"
SCREENS = (BOM, NESTING, PRICING)
MENU = "dxf_menu"   # the screens' menu, under the file tab

_ISSUES = "dxf_issues"      # {card id: what it still needs}, as of the last run
_ROWS = "dxf_rows"          # {card id: its row in the parts table}, this run
_ONLY_OPEN = "dxf_only_open"            # show only the cards that still need something
_VIEW, CARDS, TABLE = "dxf_view", "Kortit", "Taulukko"
_TABLE = "dxf_parts_table"

# The keys of the widgets the screens draw, besides the cards'.
_DRAWN_ON = [re.compile(p) for p in (
    r"dxf_(doc_show_linked|doc_page|doc_link_.*|step_k_input|step_3d_.*|step_pick_.*)",  # BOM
    r"dxf_card_page.*|dxf_(only_open|view|parts_table|bulk_.*)"
    r"|asm_(name|fit_min|weld_m|setup_min|eur_per_h|other_eur)_.*",
    r"dxf_(margin_pct|nest_mode|rankavali_mm|edge_.*|sparrow_t|angle_(0|90)|angle_mode"
    r"|su_select::.*)",                                                      # Nestaus
    r"quote_(breakdown_q|pdf_mat|dxf_name|dxf_marks)_dxf",                   # Hinnoittelu
)]


def _keep_inputs() -> None:
    """Keep every screen's and card's inputs, on every run: a screen not
    open draws none of them, and one opened again draws them anew in the
    browser, from their defaults unless set through the session (see
    ``view.ui.keep``). Run before anything is drawn; a card added on this
    run has nothing to keep yet."""
    ss = st.session_state
    cards = ids_in({*ss.get(ITEMS, {}), *ss.get(CONFIG, {})})
    keep(lambda key: any(p.fullmatch(key) for p in _DRAWN_ON) or bool(cards.search(key)))


def render(data: dict, screen: str | None):
    """One of ``SCREENS``; None when another tab is open (then only the
    inputs are kept). Returns the quote's totals for the bar
    (``quote.Totals``), or None."""
    _keep_inputs()
    if screen is None:
        keep(lambda key: key == MENU)   # back on the same screen
        return None
    lookup = build_lookup(data)
    strip = st.container()
    if screen == BOM:
        render_undo()
        parts = render_drop_area()
    else:
        parts = sync()
        sync_assemblies()
    if not parts:
        if screen == BOM:
            _render_start()
        else:
            st.info(f"Ei vielä osia. Lataa tiedostot kohdassa {BOM}.")
            st.button(f"← {BOM}", key="ui_go_start", on_click=_go, args=(BOM,))
        return None

    materials = materials_with_copper(lookup)
    if screen == BOM:
        auto_fill_materials(parts, materials, lookup)
    # Per-part angles are chosen on the Nestaus step, by its switch.
    per_part = len(parts) > 1 and st.session_state.get(ANGLE_MODE) == PER_PART
    with headless(screen != BOM):
        products = _render_part_cards(parts, materials, lookup, per_part)
    with headless(screen != NESTING):
        margin_pct, nest_mode, sheet = render_settings(
            key_prefix="dxf", separate_label="Laske jokainen osa erikseen")
    totals, nest = None, NestState()
    if any(is_ready(p) for p in products):
        # Priced from what is nested on every step, so the bar is live;
        # Sparrow runs only from the Nestaus buttons.
        totals, nest = render_nesting(products, len(parts), lookup, nest_mode, sheet,
                                      margin_pct, nesting=screen == NESTING,
                                      prices=screen == PRICING)
    elif screen != BOM:
        st.info(f"Valitse materiaali ja paksuus vähintään yhdelle osalle ({BOM}).")
    with strip:
        _render_status(screen, nest, totals is not None)
    _render_next(screen)
    return totals


def _render_next(screen: str) -> None:
    """The previous and next step's buttons, at the end of the step: where
    the work on it ends."""
    at = SCREENS.index(screen)
    st.divider()
    back, _, nxt = st.columns([1, 2, 1])
    if at > 0:
        back.button(f"← {SCREENS[at - 1]}", key="ui_go_back", on_click=_go,
                    args=(SCREENS[at - 1],), width="stretch")
    if at + 1 < len(SCREENS):
        nxt.button(f"Seuraava: {SCREENS[at + 1]} →", key="ui_go_next", type="primary",
                   on_click=_go, args=(SCREENS[at + 1],), width="stretch")


# Kept by "Tyhjennä kaikki": the sidebar (price lists, copper, work rates),
# the open menu, and the drop area's counter (its old files must not return).
_KEPT = ("upload_", "copper_", "manual_route", "menu", "dxf_inbox")


def clear_all() -> None:
    """Everything of the quote at once: the files, the cards, the nesting, the
    quote's details and the calculator (and any "Kumoa" still offered)."""
    ss = st.session_state
    keep = set(RATE_KEYS)
    for key in [k for k in ss.keys() if isinstance(k, str)]:
        if key not in keep and not key.startswith(_KEPT):
            del ss[key]
    ss["dxf_inbox"] = ss.get("dxf_inbox", 0) + 1      # a fresh, empty drop area
    ss[MENU] = BOM


def _go(screen: str, only_open: bool = False) -> None:
    """A status-strip button: open ``screen`` (and show only the unfinished
    cards)."""
    st.session_state[MENU] = screen
    if only_open:
        st.session_state[_ONLY_OPEN] = True
        st.session_state[_VIEW] = CARDS


def _render_start() -> None:
    """The empty step 1: what happens, in three steps."""
    steps = (
        ("1 · Pudota tiedostot", "DXF, DWG, STEP, PDF tai ZIP.",
         "Saman osanumeron tiedostot kootaan yhdeksi osaksi, ja kokoonpanot tunnistetaan "
         "STEP-malleista ja kokoonpanopiirustuksista."),
        ("2 · Tarkista osat", "Materiaali ja määrä.",
         "Materiaali ja paksuus täytetään piirustuksista, kun ne löytyvät. Yläreunan tila "
         "kertoo, mitä vielä puuttuu."),
        ("3 · Nestaus ja hinta", "Laske levykäyttö, lataa PDF.",
         "Nestaus lasketaan painikkeesta, ja summa näkyy sivun ylälaidassa. Tarjous-PDF "
         "ladataan kohdasta 3 · Hinnoittelu."),
    )
    for col, (title, text, more) in zip(st.columns(3), steps):
        with col.container(border=True):
            st.markdown(f"**{title}**", help=more)
            st.caption(text)


_PROBLEM = "⛔"
_OPEN = "⚠"


def _render_status(screen: str, nest: NestState, priced: bool) -> None:
    """The strip above the step: what was read, the three steps with their
    state and what is still missing (with a button to those cards). The next
    step's button is at the end of the step (``_render_next``)."""
    ss = st.session_state
    rows = ss.get(_ROWS, {})
    issues = ss.get(_ISSUES, {})
    n_drawings = sum(r["Tila"] == _DRAWING for r in rows.values())
    n_asm = len(assemblies.current(source_cards()))
    n_pdf = sum(d["kind"] == "pdf" for d in attachments.docs().values())
    n_step = len(bom.models())
    read = [f"{len(rows) - n_drawings} osaa"] + [
        text for n, text in ((n_asm, f"{n_asm} kokoonpanoa"), (n_pdf, f"{n_pdf} PDF"),
                             (n_step, f"{n_step} STEP-mallia"),
                             (n_drawings, f"{n_drawings} piirustusta")) if n]

    def state(step: str) -> str:
        if step == BOM:
            return f":orange[{len(issues)} kesken]" if issues else ":green[valmis]"
        if step == NESTING:
            if not nest.ready:
                return ":red[ei voi laskea]"
            if nest.new:
                return f":orange[{nest.new} ryhmää laskematta]"
            if nest.stale:
                return f":orange[{nest.stale} eri asetuksilla]"
            return ":green[valmis]" if nest.groups else ":gray[odottaa osia]"
        return ":green[hinta valmis]" if priced else ":gray[odottaa nestausta]"

    with st.container(border=True):
        flow, action = st.columns([5, 2], vertical_alignment="center")
        flow.markdown("  →  ".join(
            (f"**{s}**" if s == screen else s) + " " + state(s) for s in SCREENS),
            help="Luettu: " + " · ".join(read))
        what = {}
        for reason in issues.values():
            what[reason] = what.get(reason, 0) + 1
        if what:
            flow.caption("Kesken: " + " · ".join(f"{r} ({n})" for r, n in what.items()))
        if issues and not (screen == BOM and ss.get(_ONLY_OPEN)):
            action.button(f"Näytä keskeneräiset ({len(issues)})", key="ui_go_open",
                          on_click=_go, args=(BOM, True), width="stretch")


def _render_part_cards(parts, materials: list[str], lookup: dict,
                       per_part_angles: bool = False) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts. The
    loose parts come first, then each assembly with its cards in it. The
    nesting angle is on the cards only when it is chosen per part. A card
    draws its material and work on BOM."""
    ss = st.session_state
    rows: dict[str, dict] = {}
    ss[_ROWS] = rows                            # filled by the cards below
    unfinished = set(ss.get(_ISSUES, {}))       # as of the last run
    only_open = bool(unfinished) and ss.get(_ONLY_OPEN, False)
    title, view_col, open_col, clear = st.columns([3, 2, 3, 2], vertical_alignment="center")
    title.markdown("**Osat**")
    view = view_col.segmented_control("Näkymä", (CARDS, TABLE), default=CARDS, key=_VIEW,
                                      label_visibility="collapsed") or CARDS
    if unfinished:
        only_open = open_col.toggle(f"Vain keskeneräiset ({len(unfinished)})", key=_ONLY_OPEN)
    if len(parts) > 1:
        # Behind a popover: one stray click shouldn't drop every card's setup.
        with clear.popover("Poista kaikki", width="stretch"):
            st.button(f"Kyllä, poista kaikki {len(parts)} osaa", key="dxf_del_all",
                      type="primary",
                      on_click=undoable(evict_all, f"kaikki {len(parts)} osaa"))
    table_slot = st.container() if view == TABLE else None
    drawing_of = assemblies.drawings()          # assembly id: its drawing's card
    card_of = source_cards()
    index = {fid: idx for idx, (fid, _) in enumerate(parts)}
    per_assembly = per_unit(assemblies.current(card_of))  # card id: kpl in its assemblies
    doc_links = card_docs()
    in_boms = bom_quantities()                  # card id: (kpl, BOMs)
    products: dict[int, dict | None] = {}
    fill = _FillOffer()

    # The table lists the cards shown (every card, or the unfinished ones);
    # a card picked in it opens below it, several get the bulk edit.
    listed = [idx for idx, (fid, _) in enumerate(parts) if not only_open or fid in unfinished]
    picked = [listed[r] for r in (ss.get(_TABLE) or {}).get("selection", {}).get("rows", [])
              if r < len(listed)] if view == TABLE else []

    def draw(idxs: list[int], page_key: str) -> None:
        if view == TABLE:
            shown = set(picked[:1]) if len(picked) == 1 else set()
        else:
            visible = [i for i in idxs if not only_open or parts[i][0] in unfinished]
            shown = {visible[i] for i in _render_page_picker(len(visible), page_key)}
        for idx in idxs:
            fid, part = parts[idx]
            offer = idx in shown
            if offer:
                fill.slot = st.empty()  # above the card: where the question goes if it asks
            with headless(idx not in shown):  # hidden: priced, not drawn
                products[idx] = _render_part_config(fid, part, idx, materials, lookup,
                                                    per_part_angles, fill if offer else None,
                                                    doc_links.get(fid, []),
                                                    per_assembly.get(fid, 0), in_boms.get(fid))

    loose = [idx for idx, (fid, _) in enumerate(parts)
             if fid not in per_assembly and fid not in drawing_of.values()]
    if not loose:
        st.caption("Kaikki osat ovat kokoonpanoissa.")
    elif only_open and view == CARDS and not any(parts[i][0] in unfinished for i in loose):
        st.caption("Kokoonpanoihin kuulumattomat osat ovat valmiita.")
    draw(loose, _PAGE)

    def draw_assembly(aid: str, cids: dict[str, int]) -> None:
        if aid in drawing_of and not is_headless():   # its drawing, not a card
            attachments.render_card_docs(drawing_of[aid], doc_links.get(drawing_of[aid], []))
        # A card in two assemblies is drawn in the first one only.
        mine = sorted(index[c] for c in cids if c in index)
        if mine:
            st.caption("Osat: " + " · ".join(
                f"#{i + 1} {parts[i][1].name} × {cids[parts[i][0]]}" for i in mine))
        elsewhere = [i for i in mine if i in products]
        for i in elsewhere:
            st.caption(f"#{i + 1} · {parts[i][1].name}: kortti on toisessa kokoonpanossa.")
        draw([i for i in mine if i not in products], f"{_PAGE}_{aid}")

    assemblies.render(card_of, draw_assembly, evict)
    for idx, (fid, dxf) in enumerate(parts):
        rows.setdefault(fid, {"#": idx + 1, "Osa": dxf.name, "Tila": _DRAWING})
    ss[_ISSUES] = {fid: r["Tila"] for fid, r in rows.items()
                   if r["Tila"].startswith((_PROBLEM, _OPEN))}
    if table_slot is not None and not is_headless():
        with table_slot:
            _render_table([parts[i][0] for i in listed], rows, materials, lookup,
                          [parts[i][0] for i in picked])
    return [products[i] for i in sorted(products) if products[i] is not None]


_DRAWING = "piirustus"


def _row(fid: str, idx: int, dxf: DxfFile, status: str, **cells) -> None:
    """The card's row in the parts table (and its state for the strip)."""
    st.session_state.setdefault(_ROWS, {})[fid] = {
        "#": idx + 1, "Osa": dxf.name, "Tila": status, **cells}


def _render_table(fids: list[str], rows: dict[str, dict], materials: list[str],
                  lookup: dict, picked: list[str]) -> None:
    """The parts as one table: pick one to open its card below, or several
    to give them a material and thickness at once."""
    columns = ("#", "Osa", "Tila", "Materiaali", "Paksuus", "Määrä", "Koko (mm)")
    st.dataframe([{c: rows[f].get(c) for c in columns} for f in fids if f in rows],
                 key=_TABLE, on_select="rerun", selection_mode="multi-row",
                 hide_index=True, width="stretch")
    if not picked:
        st.caption("Valitse rivi", help="Yksi rivi avaa osan kortin alle; useille riveille "
                                        "voi antaa saman materiaalin kerralla.")
        return
    with st.container(border=True):
        st.markdown(f"**Aseta valituille ({len(picked)} osaa)**")
        cols = st.columns([2, 2, 1], vertical_alignment="bottom")
        material, thickness = render_material_thickness(
            materials, lookup, mat_key="dxf_bulk_mat", thick_key="dxf_bulk_th",
            cols=cols[:2])
        cols[2].button("Aseta", key="ui_go_bulk", type="primary",
                       disabled=not (material and thickness), on_click=_apply_bulk,
                       args=(picked, material, thickness), width="stretch")


def _apply_bulk(fids: list[str], material: str, thickness: str) -> None:
    for fid in fids:
        apply_material(fid, material, thickness)


_PAGE = "dxf_card_page"
_PAGE_SIZE = 20     # cards drawn at once: hundreds would take minutes and the memory


def _render_page_picker(n: int, key: str = _PAGE) -> range:
    """Which cards are drawn: all of a few, else one page of ``_PAGE_SIZE``.
    Every card is priced either way (see ``view.ui``)."""
    return render_page_picker(
        n, _PAGE_SIZE, key=key, label="Näytettävät osat", prefix="#",
        help=f"Osia näytetään {_PAGE_SIZE} kerrallaan, jotta sivu pysyy nopeana. Kaikki "
             f"{n} osaa hinnoitellaan silti, myös piilossa olevat.")


def _file_lines(item: Item) -> list[str]:
    """The card's files: which one is priced (✓) and why."""
    return [f"{'✓ ' if sid == item.chosen else ''}{item.names[sid]} ({item.scores[sid].note})"
            for sid in item.members]


def _render_item_files(fid: str, item: Item) -> None:
    """A choice to price another of the card's files, when it has several."""
    valid = [sid for sid in item.members if item.scores[sid].valid]
    if len(valid) < 2:
        return
    cfg = st.session_state.setdefault(CONFIG, {}).setdefault(
        fid, {"material": None, "thickness": None})
    options = ["", *valid]
    current = cfg.get("source") if cfg.get("source") in valid else ""
    choice = st.selectbox(
        "Muoto tiedostosta", options, index=options.index(current), key=f"dxf_src_{fid}",
        label_visibility="collapsed",
        format_func=lambda sid: "Muoto: " + (f"automaattinen ({item.names[item.chosen]})"
                                             if not sid else item.names[sid]))
    if (choice or None) != cfg.get("source"):
        cfg["source"] = choice or None
        st.session_state.get(REPORTS, {}).pop(fid, None)
        st.rerun()


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
    in_bom: tuple[int, list[str]] | None = None,
) -> dict | None:
    """Draw one part's card, preview left and inputs right; return its product
    dict, or None if not priceable. Its ``angles`` are None (set later from the
    run row) unless ``per_part_angles`` (then its own, chosen on the run
    row). ``fill`` carries the "same material?"
    question from card to card (see ``_offer_same_material``). ``doc_ids``
    are the supporting files linked to it (see ``view.attachments``). A card in
    an assembly (``per_assembly`` kpl in one) takes its quantity from the batch
    sizes and leaves its fasteners and welding to the assembly; a loose card
    listed in a BOM (``in_bom``: kpl, BOM names) takes the BOM's quantity."""
    in_assembly = per_assembly > 0
    with st.container(border=True):
        hdr = st.columns([5, 3, 1], vertical_alignment="center")
        item = st.session_state.get(ITEMS, {}).get(fid)
        folders = sorted({f for f in map(folder_of, item.members if item else [fid]) if f})
        # Where it came from, and the text in the drawing (Mat=…, Thk=…) to
        # cross-check the material: behind the name's tooltip.
        about = (["📁 " + " · ".join(folders)] if folders else []) + (
            ["Tiedostot (hinnoiteltava muoto valitaan automaattisesti: puhdas tasoaihio "
             "ennen piirustusta, ja STEP-mallin kanssa samankokoinen DXF ennen muita):",
             *_file_lines(item)] if item is not None and len(item.members) > 1 else []) + (
            ["Piirustuksen tekstit: " + " · ".join(dxf.texts)] if dxf.texts else [])
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}", help="  \n".join(about) or None)
        hdr[2].button("Poista", key=f"dxf_del_{fid}", args=(fid,),
                      on_click=undoable(evict, f"#{idx + 1} {dxf.name}"))
        src_col, docs_col, model_col = st.columns(3)
        if item is not None and len(item.members) > 1:
            with src_col:
                _render_item_files(fid, item)
        if not is_headless():   # only drawn: nothing here changes the price
            with docs_col:
                attachments.render_card_docs(fid, list(doc_ids))
            if item is not None:
                with model_col:
                    bom.render_card_model(fid, [s for s in item.members if bom.is_source(s)])
            render_waiting(list(doc_ids))

        preview_col, input_col = st.columns([1, 3])
        report = card_report(fid, dxf)
        if item is not None and not any(c.valid for c in item.scores.values()) and any(
                c.note == NO_BLANK for c in item.scores.values()):
            input_col.warning(
                "Piirustus ilman osan aihiota (esim. kokoonpanopiirustus): ei hinnoitella. "
                "Jos siinä on osanumeroita, niistä tehdään kokoonpano (Kokoonpanot); "
                "lataa osien omat DXF- tai STEP-tiedostot, niin niistä tulee kortit.")
            if not is_headless():
                with preview_col:
                    _render_preview(report)
            _row(fid, idx, dxf, _DRAWING)
            return None
        with input_col:
            size = _checked_size(fid, dxf, report, hdr[1], _unit_reason(dxf, report, item, doc_ids))
        if not is_headless():
            with preview_col:
                _render_preview(report, model_bends(fid, report, item))
                if forms := card_forms(item):
                    st.caption(f"{forms} painomuotoa",
                               help="Painomuodot (esim. kidukset) STEP-mallissa: niitä ei "
                                    "lasketa taivutuksiksi.")
        if size is None:
            _row(fid, idx, dxf, f"{_PROBLEM} ei hinnoiteltavissa" if report.problems
                 else f"{_OPEN} vahvista mittayksikkö")
            return None
        with input_col:
            material, thickness, qty = _render_part_inputs(fid, materials, lookup, per_assembly,
                                                             in_bom)
            if fill is not None:
                _offer_same_material(fid, idx, material, thickness, fill)
            work = render_work_inputs(fid, material=material, thickness=thickness, qty=qty,
                                      cut_length_m=report.cut_length_mm / 1000,
                                      bends=card_bends(report, item), threads=report.threads,
                                      fasteners=0 if in_assembly else card_fasteners(dxf, item),
                                      in_assembly=in_assembly)
        # Chosen on the Nestaus step, next to the run button (see
        # ``dxf_nesting._render_angle_controls``); read here for the product.
        with headless():
            angles = render_nesting_angles(fid) if per_part_angles else None
        status = ("✓ valmis" if material and thickness
                  else f"{_OPEN} valitse paksuus" if material
                  else f"{_OPEN} valitse materiaali")
        _row(fid, idx, dxf, status, Materiaali=material or "", Paksuus=thickness or "", Määrä=qty,
             **{"Koko (mm)": f"{size[0]:g} × {size[1]:g}"})

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
        st.caption(f"{slits} rakoa", help="Suorat leikkaukset osan sisällä ovat mukana "
                                         "leikkuupituudessa.")
    if report.dropped_count:
        st.caption(f"{report.dropped_count} kuviota ohitettu",
                   help="Osan ulkopuolella olevat kuviot (esim. lisäkuvat tai irralliset "
                        "viivat) eivät ole osaa; ne ovat harmaalla esikatselussa.")


def _unit_reason(dxf: DxfFile, report: DxfReport, item: Item | None, doc_ids) -> str | None:
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
        st.caption(f"Yksikkö {report.unit_label} vahvistettu",
                   help=f"Mittayksikkö puuttuu tiedostosta; vahvistettu: {confirmed}.")
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


def _render_part_inputs(fid: str, materials: list[str], lookup: dict,
                        per_assembly: int = 0,
                        in_bom: tuple[int, list[str]] | None = None) -> tuple:
    """Material, thickness and quantity; returns ``(material, thickness, qty)``.
    A part is nested for the quantity its BOM asks for, else for the largest
    batch size (in an assembly, ``per_assembly`` kpl in each), unless another
    quantity is typed.

    Material + thickness are persisted per file: the selectboxes are seeded
    from the stored choice, then the current choice is written back.
    """
    cfg = st.session_state.setdefault(CONFIG, {}).setdefault(
        fid, {"material": None, "thickness": None})
    mat_key, thick_key, _ = material_keys(fid, cfg)
    material, thickness = render_material_thickness(
        materials, lookup,
        mat_key=mat_key, thick_key=thick_key,
        mat_default=cfg["material"], thick_default=cfg["thickness"],
    )
    cfg["material"] = material
    cfg["thickness"] = thickness
    note = material_agent.auto_note(fid, material, thickness)
    if note:
        st.caption("Täytetty automaattisesti", help=note)
    elif not material and (check := material_agent.check_note(fid)):
        st.warning(f"Piirustukset eivät ole yksimielisiä materiaalista ({check}). "
                   "Valitse materiaali itse.")
    batch = max(current_sizes())
    if in_bom and not per_assembly:
        auto, why = in_bom[0], f"BOM: {in_bom[0]} kpl ({', '.join(in_bom[1])})"
    else:
        auto = max(per_assembly, 1) * batch
        why = ((f"{per_assembly} kpl / kokoonpano × " if per_assembly else "")
               + f"suurin eräkoko {batch} (Eräkoot, sivun ylälaidassa)")
    col = st.columns(2)[0]
    qty = auto_input(col, "Määrä (kpl)", f"dxf_q_{fid}", auto, step=1.0, fmt="%.0f",
                     help="Levyt nestataan tälle määrälle. Oletus: " + why + ".")
    return material, thickness, max(int(qty), 1)


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
    if fill.shown or fill.source is None or st.session_state.get(FILL_ASKED):
        return
    fill.shown = True
    src_idx, src_mat, src_th = fill.source
    n_empty = len(empty_cards())
    with fill.slot.container(border=True):
        whom = (f"tyhjille osille ({n_empty} kpl)" if n_empty > 1
                else f"osalle #{idx + 1}")
        st.markdown(f"Käytetäänkö {whom} samaa materiaalia ja paksuutta kuin "
                    f"osalla #{src_idx + 1}: **{src_mat} · {src_th} mm**?")
        yes, no, _ = st.columns([1, 1, 4])
        yes.button("Kyllä", key=f"dxf_fill_yes_{fid}", type="primary",
                   on_click=fill_material, args=(src_mat, src_th))
        no.button("Ei", key=f"dxf_fill_no_{fid}", on_click=fill_material, args=(None, None))
