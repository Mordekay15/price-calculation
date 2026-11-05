"""Assemblies on the DXF tab (``core.assemblies``): parts sold together as
one quote line.

- **From a STEP assembly.** A dropped STEP file of several parts becomes an
  assembly: its sheet parts are the cards they are on, with the model's
  quantities, and its other bodies (pins, screws, machined parts) are bought
  parts to give a price.
- **From an assembly drawing.** A card that is only a drawing (no blank to
  cut, see ``core.items``) whose drawing calls out other part numbers
  becomes an assembly of the cards with those numbers, 1 kpl each (the
  drawing is shown in it, not among the cards); the ones
  neither dropped yet nor added as bought parts are listed as awaited.
- **Not from a part's fasteners.** The fasteners a part's drawing calls out
  (e.g. "M5-PURISTEMUTTERIT X4KPL") are the part's own Ostetut osat, on its
  card (``view.work``), and come with it into any assembly. Only an assembly
  drawing's own notes are its assembly's bought parts.
- **By hand.** Parts attached to each other (welded, riveted, screwed
  together) are joined into one with "Uusi kokoonpano".

Its cards are drawn inside it, apart from the loose parts.

One assembly per part number: a STEP model and an assembly drawing of the
same number are one assembly, its kpl the model's first, then the drawing's
BOM lines, then 1 for a part only called out. A number called out that is
another assembly's makes that one a subassembly (``children``), and a part
sits in one place: a card in a subassembly is not also in its parent.

``ASSEMBLIES`` holds ``{assembly id (part number, or "hand:…"): Assembly}``
(``core.model.Assembly``): where each comes from, its bought parts and work,
and its ``parts`` (set by ``current``). A STEP or hand member is stored by a
source id of its card (see ``core.items``), so it stays on the right card
when the card's files are regrouped."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace

from core.items import NO_BLANK
from core.linking import base_key
from core.model import Assembly
from view import bom, bought
from view.ui import is_headless, st
from view.undo import undoable

ASSEMBLIES = "dxf_assemblies"
_SEEN = "dxf_assemblies_seen"      # STEP file ids already made into assemblies
KEYS = (ASSEMBLIES, _SEEN)


def _store() -> dict[str, Assembly]:
    return st.session_state.setdefault(ASSEMBLIES, {})


def _drop_if_undescribed(aid: str) -> None:
    """A source of an assembly is gone (its STEP file, or its drawing no
    longer an assembly drawing): the assembly goes too when nothing else
    describes it."""
    if not _store()[aid].described:
        _store().pop(aid)
        bought.reset(_widget(aid))


def sync_from_models() -> None:
    """Make an assembly of every STEP assembly dropped since the last time,
    and drop the ones whose STEP file was removed. One assembly per part
    number: a STEP model with the number of an assembly drawing's assembly
    joins it (its parts and quantities come first)."""
    store, seen = _store(), st.session_state.setdefault(_SEEN, set())
    models = bom.models()
    for aid, a in list(store.items()):
        if a.step_file and a.step_file not in models:
            a.bought = [b for b in a.bought if b.name not in a.step_bought]
            bought.reset(_widget(aid))
            a.step_file, a.members, a.step_bought = None, {}, []
            _drop_if_undescribed(aid)
    seen &= set(models)
    for file_id, m in models.items():
        if file_id in seen:
            continue
        seen.add(file_id)
        if not bom.is_assembly(file_id):
            continue
        members, bodies = {}, []
        for p in m["model"].parts:
            flat = m["flats"].get(p.key)
            if flat is not None and flat.is_sheet:
                members[bom.card_id(file_id, p.key)] = p.qty
            else:
                bodies.append(bought.body_part(p.name, p.qty, m["name"]))
        name = m["name"].rsplit(".", 1)[0]
        aid = part_number(name)
        a = store.setdefault(aid, Assembly(aid, name))
        a.step_file, a.members, a.step_bought = file_id, members, [b.name for b in bodies]
        _add_bought(a, bodies)


def part_number(name: str) -> str:
    """An assembly's id: its part number (a file's name without revision)."""
    return base_key(name.rsplit(".", 1)[0] if "." in name else name)


def sync_from_drawings(items: dict, facts: Callable[[str], list],
                       pdfs: dict[str, tuple[str, list]] | None = None) -> None:
    """Make an assembly of every assembly drawing among the cards: ``items``
    are the cards (``core.model.Part`` by card id), ``facts(card id)`` what
    the readers read from its drawings (``core.model.Fact``: the part numbers
    it calls out, its BOM lines). Each part number it calls out is a card in it, or
    a subassembly when that number is another assembly's; × the kpl its BOM
    lines give, else 1. One assembly per part number: a drawing of a STEP
    assembly's number joins it.

    ``pdfs`` are the PDFs on no card, ``{doc id: (file name, facts)}``: one
    the PDF reader found to be an assembly drawing (its ``assembly`` fact,
    see ``core.pdf_assembly``) makes an assembly too, unless a card's drawing
    already makes that number's."""
    store = _store()
    keys = {cid: {base_key(n) for n in item.names.values()} for cid, item in items.items()}
    drawings = {}
    for cid, item in items.items():
        scores = item.scores.values()
        if any(c.valid for c in scores) or not any(c.note == NO_BLANK for c in scores):
            continue
        if found := _called(facts(cid), keys[cid]):
            drawings[cid] = (part_number(item.names[cid]), *found)
    on_cards = {pn for pn, _, _ in drawings.values()} | {k for ks in keys.values() for k in ks}
    from_pdfs = {}
    for doc, (name, said) in (pdfs or {}).items():
        pn = part_number(name)
        verdict = next((f.value["verdict"] for f in said if f.kind == "assembly"), None)
        if verdict == "assembly" and pn not in on_cards and (found := _called(said, {pn})):
            from_pdfs[doc] = (pn, *found)
    for aid, a in list(store.items()):
        if a.drawing and a.drawing not in drawings:
            a.drawing, a.drawn, a.children, a.awaited, a.fasteners_read = None, {}, {}, [], False
            _drop_if_undescribed(aid)
        elif a.drawing_pdf and a.drawing_pdf not in from_pdfs:
            a.drawing_pdf, a.fasteners_read = None, False
            if not a.drawing:
                a.drawn, a.children, a.awaited = {}, {}, []
            _drop_if_undescribed(aid)
    # The part numbers that are assemblies (one joined by hand has none).
    numbers = {pn for pn, _, _ in [*drawings.values(), *from_pdfs.values()]} | {
        aid for aid, a in store.items() if not a.by_hand}
    for cid, (pn, expects, kpl) in drawings.items():
        a = store.setdefault(pn, Assembly(pn, pn.upper()))
        a.drawing = cid
        _fill(a, expects, kpl, keys, numbers)
    for doc, (pn, expects, kpl) in from_pdfs.items():
        a = store.setdefault(pn, Assembly(pn, pn.upper()))
        a.drawing_pdf = doc
        _fill(a, expects, kpl, keys, numbers)


def _called(said: list, own: set[str]) -> tuple[list[str], dict[str, int]] | None:
    """The part numbers a drawing calls out (``own`` left out) and the kpl
    its BOM gives each; None when it calls out none."""
    expects = list(dict.fromkeys(f.value for f in said if f.kind == "called_out"
                                 and f.value not in own))
    kpl: dict[str, int] = {}
    for f in said:
        if f.kind == "bom_qty":
            kpl.setdefault(f.value["part_key"], f.value["kpl"])
    return (expects, kpl) if expects else None


def _fill(a: Assembly, expects: list[str], kpl: dict[str, int],
          keys: dict[str, set[str]], numbers: set[str]) -> None:
    """An assembly drawing's parts: the cards it calls out, the
    subassemblies, and the numbers not dropped yet (``awaited``)."""
    pn = a.id
    found = {k: other for k in expects for other, ks in keys.items()
             if k in ks and k not in numbers}
    a.children = {k: kpl.get(k, 1) for k in expects if k in numbers and k != pn}
    a.drawn = {c: kpl.get(k, 1) for k, c in found.items()}
    named = {base_key(b.name) for b in a.bought}
    a.awaited = [k.upper() for k in expects
                 if k not in found and k not in a.children and k not in named]


def _widget(aid: str) -> str:
    return f"asm_bought_{aid}"


def _add_bought(a: Assembly, parts: list) -> None:
    """Bought parts read from a file, added to an assembly's Ostetut osat
    (the ones typed in kept); its table starts again from them."""
    if parts:
        a.bought = [*a.bought, *parts]
        bought.reset(_widget(a.id))


def sync_drawing_fasteners(lines: dict[str, list[tuple[str, int]]],
                           material: Callable[[str], str | None] = lambda cid: None) -> None:
    """An assembly drawing's fastener notes (``lines`` by drawing card) are
    its assembly's bought parts, added once. A part's own fasteners are not
    here: they stay on its card (``view.work``), wherever the part goes."""
    for a in _store().values():
        cid = a.drawing or a.drawing_pdf
        if cid and lines.get(cid) and not a.fasteners_read:
            a.fasteners_read = True
            _add_bought(a, bought.fastener_parts(lines[cid], material(cid), "piirustus"))


def join(cids: list[str], name: str) -> None:
    """"Uusi kokoonpano": parts attached to each other, 1 kpl each, as one
    assembly. Their fasteners stay on their cards."""
    if not cids:
        return
    aid = f"hand:{uuid.uuid4().hex[:8]}"
    _store()[aid] = Assembly(aid, name.strip() or "Kokoonpano",
                             members={cid: 1 for cid in cids}, by_hand=True)


def drawings() -> dict[str, str]:
    """``{assembly id: card id}`` of the assemblies made from a drawing: that
    card is the assembly's drawing, not one of its parts."""
    return {aid: a.drawing for aid, a in _store().items() if a.drawing}


def pdf_drawings() -> dict[str, str]:
    """``{assembly id: PDF id}`` of the assemblies made from a PDF on no
    card (``sync_from_drawings``'s ``pdfs``)."""
    return {aid: a.drawing_pdf for aid, a in _store().items()
            if a.drawing_pdf and not a.drawing}


def _cards_of(a: Assembly, card_of: dict[str, str]) -> dict[str, int]:
    """An assembly's own cards with their kpl (members whose card is gone
    left out): the STEP model's (or joined by hand) first, then the ones its
    drawing calls out that the model doesn't have."""
    out: dict[str, int] = {}
    for sid, n in a.members.items():
        cid = card_of.get(sid)
        if cid is not None and n > 0:
            out[cid] = max(out.get(cid, 0), int(n))
    for cid, n in a.drawn.items():
        if cid in card_of.values() and n > 0:
            out.setdefault(cid, int(n))
    return out


def _tree_cards(card_of: dict[str, str]) -> dict[str, dict[str, int]]:
    """Every assembly's cards, a card in one of its subassemblies (at any
    depth) left out of it: a part sits in one place."""
    store = _store()
    own = {aid: _cards_of(a, card_of) for aid, a in store.items()}

    def below(aid: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        for c in store[aid].children:
            if c in store and c not in seen:
                out |= set(own[c]) | below(c, seen | {c})
        return out
    return {aid: {c: n for c, n in cards.items() if c not in below(aid, {aid})}
            for aid, cards in own.items()}


def current(card_of: dict[str, str]) -> list[Assembly]:
    """The assemblies, each with its ``parts`` (cards × kpl) as the cards now
    stand."""
    cards = _tree_cards(card_of)
    for aid, a in _store().items():
        a.parts = cards[aid]
    return list(_store().values())


# ── The panel ─────────────────────────────────────────────────────────────────

_WORK_LABELS = {
    "fit_min": ("Kokoonpanotyö (min/kok.)", 1.0, "Osien kokoaminen yhdeksi kokoonpanoksi."),
    "weld_m": ("Hitsaus (m/kok.)", 0.1, "Osien hitsaus yhteen. Hinnoitellaan sivupalkin "
                "hitsaushinnalla."),
    "setup_min": ("Asetus (min/erä)", 5.0, "Kokoamisen asetus kerran erää kohden, jaetaan erän "
                  "kokoonpanoille. Osien omat asetukset (laser, särmäys) ovat korteilla."),
    "eur_per_h": ("Työ (€/h)", 5.0, "Kokoonpanotyön ja asetuksen tuntihinta."),
    "other_eur": ("Muut (€/kok.)", 0.5, "Esim. pakkaus tai koko kokoonpanon maalaus."),
}


def render(card_of: dict[str, str], draw_cards: Callable[[str, dict[str, int]], None],
           remove_card: Callable[[str], None], labels: dict[str, str] | None = None) -> None:
    """The assemblies, apart from the loose parts: each one's cards (drawn by
    ``draw_cards(assembly id, {card id: kpl / kokoonpano})``), bought parts and
    assembly work. Removing an assembly removes its cards and drawing
    (``remove_card``) and its STEP file. ``labels`` ("#1 · name" by card id)
    are the parts "Uusi kokoonpano" can join."""
    store = _store()
    st.divider()
    title, new = st.columns([8, 2], vertical_alignment="center")
    if labels and len(labels) > 1 and not is_headless():
        _render_new(new, labels)
    if not store:
        return
    title.markdown(f"**Kokoonpanot ({len(store)})**",
                help="Kokoonpano on yksi tarjousrivi: sen osat (kortit), ostetut osat ja "
                     "kokoamistyö. Eräkoko tarkoittaa kokoonpanojen määrää, ja jokainen osa "
                     "hinnoitellaan määrällä eräkoko × kpl / kokoonpano. STEP-kokoonpano "
                     "tehdään tänne automaattisesti.")
    cards = _tree_cards(card_of)
    parents = {c: (b.name, n) for b in store.values()
               for c, n in b.children.items() if c in store}
    for aid in list(store):
        _render_one(aid, store[aid], cards[aid], draw_cards, remove_card, card_of,
                    parents.get(aid))


def _render_new(col, labels: dict[str, str]) -> None:
    """"Uusi kokoonpano": pick the parts attached to each other."""
    ss = st.session_state
    ss["asm_new_parts"] = [c for c in ss.get("asm_new_parts") or [] if c in labels]
    with col.popover("Uusi kokoonpano", width="stretch"):
        st.text_input("Nimi", key="asm_new_name")
        st.multiselect("Osat", list(labels), format_func=labels.get, key="asm_new_parts",
                       help="Toisiinsa kiinnitetyt osat (hitsatut, niitatut, ruuvatut), "
                            "1 kpl / kokoonpano kukin. Osien kiinnikkeet pysyvät osien korteilla.")
        st.button("Yhdistä kokoonpanoksi", key="asm_new_go", type="primary",
                  on_click=undoable(_join_picked, "uusi kokoonpano"))


def _join_picked() -> None:
    ss = st.session_state
    join(ss.get("asm_new_parts") or [], ss.get("asm_new_name") or "")
    ss["asm_new_parts"], ss["asm_new_name"] = [], ""


def _origin(a: Assembly) -> str | None:
    if a.step_file:
        return "Tehty STEP-mallista: osat ja määrät mallin rakenteesta."
    if a.drawing:
        return ("Tehty kokoonpanopiirustuksesta: osat ovat piirustuksessa mainitut "
                "osanumerot, 1 kpl / kokoonpano kukin. Tarkista määrät piirustuksesta.")
    if a.drawing_pdf:
        return ("Tehty kokoonpanopiirustuksen PDF:stä: osat ja määrät sen osaluettelosta "
                "tai mainituista osanumeroista. Tarkista määrät piirustuksesta.")
    return "Yhdistetty käsin: toisiinsa kiinnitetyt osat." if a.by_hand else None


def _render_one(aid: str, a: Assembly, cards: dict[str, int],
                draw_cards: Callable[[str, dict[str, int]], None],
                remove_card: Callable[[str], None], card_of: dict[str, str],
                parent: tuple[str, int] | None) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
        a.name = c1.text_input("Kokoonpano", value=a.name, key=f"asm_name_{aid}",
                               help=_origin(a))
        c2.button("Pura" if a.by_hand else "Poista", key=f"asm_del_{aid}",
                  on_click=undoable(_remove, f"kokoonpano {a.name}"),
                  args=(aid, card_of, remove_card),
                  help="Purkaa kokoonpanon: osat jäävät irto-osiksi." if a.by_hand else
                  "Poistaa kokoonpanon ja sen osat (kortit, piirustuksen ja STEP-mallin).")
        if a.awaited:
            st.warning("Piirustuksessa on osia, joita ei ole ladattu: "
                       + ", ".join(a.awaited) + ". Lataa niiden DXF- tai STEP-tiedostot, "
                       "tai lisää ne alle ostettuina osina.")
        if parent is not None:
            st.caption(f"Alikokoonpano: {parent[1]} kpl kokoonpanossa {parent[0]}, "
                       "hinnoitellaan siinä.")
        store = _store()
        if kids := {c: n for c, n in a.children.items() if c in store}:
            st.caption("Alikokoonpanot: " + " · ".join(
                f"🧩 {store[c].name} × {n}" for c, n in kids.items()))
        draw_cards(aid, cards)
        _render_work(aid, a)


def _remove(aid: str, card_of: dict[str, str], remove_card: Callable[[str], None]) -> None:
    """The assembly's "Poista": it goes with its parts' cards (a card in
    another assembly too stays), its drawing and its STEP file."""
    store = _store()
    a = store.pop(aid, None)
    bought.reset(_widget(aid))
    if a is None or a.by_hand:       # only undone: its parts are loose again
        return
    others = {c for b in store.values() for c in _cards_of(b, card_of)}
    for cid in [*_cards_of(a, card_of), *filter(None, [a.drawing])]:
        if cid not in others:
            remove_card(cid)
    if a.drawing_pdf and not a.drawing:
        from view import attachments

        attachments.remove_doc(a.drawing_pdf)
    if a.step_file:
        bom.remove_model(a.step_file)


def _render_work(aid: str, a: Assembly) -> None:
    """The bought parts and the assembly work, written back to the Assembly."""
    st.markdown("Ostetut osat", help=bought.HELP + " Kpl on kokoonpanoa kohden.")
    a.bought = bought.editor(a.bought, _widget(aid), "Kpl / kokoonpano")

    cols = st.columns(len(_WORK_LABELS))
    work = {}
    for col, (f, (label, step, help_)) in zip(cols, _WORK_LABELS.items()):
        work[f] = col.number_input(label, min_value=0.0, step=step, help=help_,
                                   value=float(getattr(a.work, f)), key=f"asm_{f}_{aid}")
    a.work = replace(a.work, **work)
