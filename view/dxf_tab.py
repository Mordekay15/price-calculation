"""The DXF tab's three screens, Offeri's: BOM & Tiedostot (the drop area
and the part cards with their work, ``view.dxf_files``), Nestaus (the
settings, the nesting and every group's sheets, ``view.dxf_nesting``) and
Hinnoittelu (the totals, the breakdown by batch size and the downloads). A card shows a part's preview, measured size,
material, thickness, quantity and work, and returns the product dict the
pricing uses, or None (with the reasons shown) when the part cannot be
priced. The card and the pricing share the same DxfReport, so what the card
shows is exactly what Sparrow nests.

Only the open screen is drawn, but every screen runs the cards (headless
where not drawn, see ``view.ui``), so each one prices every part."""

import re
from dataclasses import dataclass

from core.assemblies import per_unit
from core.dxf import DxfFile, DxfReport, is_slit
from core.items import NO_BLANK, confirm_unit
from core.pricing import build_lookup
from core.sheet_cost import is_ready
from view import assemblies, attachments, bom, material_agent
from view.common import (
    materials_with_copper,
    render_material_thickness,
    render_page_picker,
    render_settings,
    thicknesses_for,
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
    card_bends,
    card_docs,
    card_fasteners,
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
from view.dxf_nesting import ANGLE_MODE, PER_PART, render_nesting, render_nesting_angles
from view.quote import current_sizes
from view.ui import headless, ids_in, is_headless, keep, st
from view.work import render_work_inputs

BOM, NESTING, PRICING = "BOM & Tiedostot", "Nestaus", "Hinnoittelu"
SCREENS = (BOM, NESTING, PRICING)
MENU = "dxf_menu"   # the screens' menu, under "Edistynyt"

# The keys of the widgets each screen draws, besides the cards' (all on BOM).
_DRAWN_ON = [(re.compile(p), on) for p, on in (
    (r"dxf_(doc_show_linked|doc_page|doc_link_.*|step_k_input|step_3d_.*|step_pick_.*"
     r"|rfq_note|material_remember)", {BOM}),
    (r"dxf_card_page.*|asm_(name|fit_min|weld_m|setup_min|eur_per_h|other_eur)_.*", {BOM}),
    (r"dxf_(margin_pct|nest_mode|rankavali_mm|edge_.*|sparrow_t|angle_(0|90)|angle_mode"
     r"|su_select::.*)", {NESTING}),
    (r"quote_(breakdown_q|pdf_mat|dxf_name|dxf_marks)_dxf", {PRICING}),
)]


def _keep_closed(screen: str | None, fids: list[str]) -> None:
    """Keep the inputs of the screens not open (``screen`` None: another tab)."""
    cards = ids_in(fids)

    def closed(key: str) -> bool:
        for pattern, on in _DRAWN_ON:
            if pattern.fullmatch(key):
                return screen not in on
        if cards.search(key):
            return screen != BOM
        return False

    keep(closed)




def render(data: dict, screen: str | None) -> None:
    """One of ``SCREENS``; None when another tab is open (then only the
    inputs are kept)."""
    if screen is None:
        _keep_closed(None, list(st.session_state.get(CONFIG, {})))
        keep(lambda key: key == MENU)   # back on the same screen
        return
    lookup = build_lookup(data)
    if screen == BOM:
        parts = render_drop_area()
    else:
        parts = sync()
        sync_assemblies()
    _keep_closed(screen, [fid for fid, _ in parts])
    if not parts:
        st.info("Lataa vähintään yksi DXF- tai DWG-tiedosto aloittaaksesi"
                + ("." if screen == BOM else f" ({BOM})."))
        return

    materials = materials_with_copper(lookup)
    if screen == BOM:
        cards, doc_links = auto_fill_materials(parts, materials, lookup)
        material_agent.render(cards, doc_links, attachments.docs(), materials,
                              lambda m: thicknesses_for(lookup, m), apply_material)
    # Per-part angles are chosen on the cards, by the Nestaus switch.
    per_part = len(parts) > 1 and st.session_state.get(ANGLE_MODE) == PER_PART
    with headless(screen != BOM):
        products = _render_part_cards(parts, materials, lookup, per_part)
    if screen == BOM:
        return
    with headless(screen != NESTING):
        margin_pct, nest_mode, sheet = render_settings(
            key_prefix="dxf", separate_label="Laske jokainen osa erikseen")
    if not any(is_ready(p) for p in products):
        st.info(f"Valitse materiaali ja paksuus vähintään yhdelle osalle ({BOM}).")
        return

    render_nesting(products, len(parts), lookup, nest_mode, sheet, margin_pct,
                   prices=screen == PRICING)


def _render_part_cards(parts, materials: list[str], lookup: dict,
                       per_part_angles: bool = False) -> list[dict]:
    """One card per uploaded part; returns the configured product dicts. The
    loose parts come first, then each assembly with its cards in it. The
    nesting angle is on the cards only when it is chosen per part. A card
    draws its material on BOM, its work on PRICING."""
    title, clear = st.columns([6, 2], vertical_alignment="center")
    title.markdown("**Osat**")
    if len(parts) > 1:
        # Behind a popover: one stray click shouldn't drop every card's setup.
        with clear.popover("Poista kaikki", width="stretch"):
            st.button(f"Kyllä, poista kaikki {len(parts)} osaa", key="dxf_del_all",
                      type="primary", on_click=evict_all)
    drawing_of = assemblies.drawings()          # assembly id: its drawing's card
    if not is_headless():
        attachments.render_checked_count([fid for fid, _ in parts
                                          if fid not in drawing_of.values()])
    card_of = source_cards()
    index = {fid: idx for idx, (fid, _) in enumerate(parts)}
    per_assembly = per_unit(assemblies.current(card_of))  # card id: kpl in its assemblies
    doc_links = card_docs()
    products: dict[int, dict | None] = {}
    fill = _FillOffer()

    def draw(idxs: list[int], page_key: str) -> None:
        shown = {idxs[i] for i in _render_page_picker(len(idxs), page_key)}
        hidden = [parts[i][0] for i in idxs if i not in shown]
        keep(ids_in(hidden).search)
        for idx in idxs:
            fid, part = parts[idx]
            offer = idx in shown
            if offer:
                fill.slot = st.empty()  # above the card: where the question goes if it asks
            with headless(idx not in shown):  # hidden: priced, not drawn
                products[idx] = _render_part_config(fid, part, idx, materials, lookup,
                                                    per_part_angles, fill if offer else None,
                                                    doc_links.get(fid, []),
                                                    per_assembly.get(fid, 0))

    loose = [idx for idx, (fid, _) in enumerate(parts)
             if fid not in per_assembly and fid not in drawing_of.values()]
    if not loose:
        st.caption("Kaikki osat ovat kokoonpanoissa.")
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
    return [products[i] for i in sorted(products) if products[i] is not None]


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
        hdr[2].checkbox("Tarkastettu", key=attachments.CHECKED.format(fid))
        hdr[3].button("Poista", key=f"dxf_del_{fid}", on_click=evict, args=(fid,))
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
            return None
        with input_col:
            size = _checked_size(fid, dxf, report, hdr[1], _unit_reason(dxf, report, item, doc_ids))
        if not is_headless():
            with preview_col:
                _render_preview(report, model_bends(fid, report, item))
        if size is None:
            return None
        with input_col:
            material, thickness, qty = _render_part_inputs(fid, materials, lookup, per_assembly)
            if fill is not None:
                _offer_same_material(fid, idx, material, thickness, fill)
            angles = render_nesting_angles(fid) if per_part_angles else None
            work = render_work_inputs(fid, material=material, thickness=thickness, qty=qty,
                                      cut_length_m=report.cut_length_mm / 1000,
                                      bends=card_bends(report, item), threads=report.threads,
                                      fasteners=0 if in_assembly else card_fasteners(dxf, item),
                                      in_assembly=in_assembly)
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


def _render_part_inputs(fid: str, materials: list[str], lookup: dict,
                        per_assembly: int = 0) -> tuple:
    """Material, thickness and quantity; returns ``(material, thickness, qty)``.
    A part is nested for the largest batch size (in an assembly,
    ``per_assembly`` kpl in each), so its quantity is not typed.

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
        st.caption("Materiaali täytetty automaattisesti", help=note)
    batch = max(current_sizes())
    qty = max(per_assembly, 1) * batch
    st.markdown(f"Määrä: **{qty} kpl**",
                help=(f"{per_assembly} kpl / kokoonpano × " if per_assembly else "")
                + f"suurin eräkoko {batch}: levyt nestataan tälle määrälle. Eräkoot "
                  "muutetaan sivun yläosan Tarjous-kohdasta.")
    return material, thickness, qty


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
