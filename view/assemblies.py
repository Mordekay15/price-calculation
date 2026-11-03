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

Session keys: ``ASSEMBLIES`` holds ``{assembly id (part number, or "hand:…"):
{"name", "members": {source id: kpl} (STEP or by hand), "drawn": {card id:
kpl} (its drawing's), "kpl_from", "children": {assembly id: kpl}, "bought":
[row], "auto": STEP file id, "drawing": card id, "fasteners_read": True once
its drawing's notes are in "bought", "hand": True, …}}``. A
member is stored by a source id of its card (see ``core.items``), so it
stays on the right card when the card's files are regrouped."""

from __future__ import annotations

import re
import uuid
from dataclasses import fields
from collections.abc import Callable

from core.assemblies import Assembly, AssemblyWork, BoughtPart
from core.items import NO_BLANK
from core.linking import base_key, callouts
from core.work import FASTENER_INSERT_EUR, fastener_price
from view import bom
from view.ui import is_headless, st
from view.undo import undoable

ASSEMBLIES = "dxf_assemblies"
_SEEN = "dxf_assemblies_seen"      # STEP file ids already made into assemblies
KEYS = (ASSEMBLIES, _SEEN)

_NAME, _QTY, _EUR, _FIT = "Nimike", "Kpl / kokoonpano", "€/kpl", "Asennus (€/kpl)"
# Bought parts pressed or fitted into a sheet part: their putting in is priced
# at FASTENER_INSERT_EUR each by default.
_FASTENER = re.compile(r"PEM|MUTTER|RUUV|PULT|NIIT|HOLK|TAPP|NUT|SCREW|STUD|BOLT|RIVET|PIN",
                       re.IGNORECASE)


def _store() -> dict[str, dict]:
    return st.session_state.setdefault(ASSEMBLIES, {})


def _drop_source(aid: str, keys: tuple[str, ...]) -> None:
    """A source of an assembly is gone (its STEP file, or its drawing no
    longer an assembly drawing): what came from it goes, and the assembly
    with it when nothing else describes it."""
    store = _store()
    a = store[aid]
    for k in keys:
        a.pop(k, None)
    if not (a.get("auto") or a.get("drawing") or a.get("hand")):
        store.pop(aid)


def sync_from_models() -> None:
    """Make an assembly of every STEP assembly dropped since the last time,
    and drop the ones whose STEP file was removed. One assembly per part
    number: a STEP model with the number of an assembly drawing's assembly
    joins it (its parts and quantities come first)."""
    store, seen = _store(), st.session_state.setdefault(_SEEN, set())
    models = bom.models()
    for aid, a in list(store.items()):
        if a.get("auto") and a["auto"] not in models:
            names = set(a.get("step_names", ()))
            a["bought"] = a["bought_seed"] = [r for r in a.get("bought", [])
                                              if r.get(_NAME) not in names]
            _drop_source(aid, ("auto", "members", "step_names"))
    seen &= set(models)
    for file_id, m in models.items():
        if file_id in seen:
            continue
        seen.add(file_id)
        if not bom.is_assembly(file_id):
            continue
        members, bought = {}, []
        for p in m["model"].parts:
            flat = m["flats"].get(p.key)
            if flat is not None and flat.is_sheet:
                members[bom.card_id(file_id, p.key)] = p.qty
            else:
                bought.append({_NAME: p.name, _QTY: p.qty, _EUR: 0.0,
                               _FIT: FASTENER_INSERT_EUR if _FASTENER.search(p.name) else 0.0})
        name = m["name"].rsplit(".", 1)[0]
        a = store.setdefault(part_number(name), {"name": name, "bought": [],
                                                  "bought_seed": []})
        a.update(members=members, auto=file_id, step_names=[r[_NAME] for r in bought])
        a["bought"] = a["bought_seed"] = [*a.get("bought", []), *bought]


def part_number(name: str) -> str:
    """An assembly's id: its part number (a file's name without revision)."""
    return base_key(name.rsplit(".", 1)[0] if "." in name else name)


def sync_from_drawings(items: dict, texts: Callable[[str], str]) -> None:
    """Make an assembly of every assembly drawing among the cards: ``items``
    are the cards (``core.items.Item`` by card id), ``texts(card id)`` the
    text of its drawings. Each part number it calls out is a card in it, or
    a subassembly when that number is another assembly's; × the kpl its BOM
    lines give, else 1. One assembly per part number: a drawing of a STEP
    assembly's number joins it."""
    store = _store()
    keys = {cid: {base_key(n) for n in item.names.values()} for cid, item in items.items()}
    drawings = {}
    for cid, item in items.items():
        scores = item.scores.values()
        if any(c.valid for c in scores) or not any(c.note == NO_BLANK for c in scores):
            continue
        text = texts(cid)
        expects = [k for k in callouts(text) if k not in keys[cid]]
        if expects:
            drawings[cid] = (part_number(item.names[cid]), expects, _bom_kpl(text, keys[cid]))
    for aid, a in list(store.items()):
        if a.get("drawing") and a["drawing"] not in drawings:
            _drop_source(aid, ("drawing", "drawn", "children", "kpl_from", "expects",
                               "awaited", "fasteners_read"))
    numbers = {pn for pn, _, _ in drawings.values()} | set(store)
    for cid, (pn, expects, kpl) in drawings.items():
        a = store.setdefault(pn, {"name": pn.upper(), "bought": [], "bought_seed": []})
        a.update(drawing=cid, expects=expects)
        found = {k: other for k in expects for other, ks in keys.items()
                 if k in ks and k not in numbers}
        a["children"] = {k: kpl.get(k, 1) for k in expects if k in numbers and k != pn}
        a["drawn"] = {c: kpl.get(k, 1) for k, c in found.items()}
        a["kpl_from"] = {c: "BOM" if k in kpl else "piirustus" for k, c in found.items()}
        bought = {base_key(str(r.get(_NAME) or "")) for r in a.get("bought", [])}
        a["awaited"] = [k.upper() for k in expects
                        if k not in found and k not in a["children"] and k not in bought]


def _bom_kpl(text: str, own: set[str]) -> dict[str, int]:
    """The kpl the drawing's BOM lines give each part number."""
    from core.readers import _bom_facts

    out: dict[str, int] = {}
    for f in _bom_facts(text, "", 0, ""):
        key = f.value["part_key"]
        if key not in own:
            out.setdefault(key, f.value["kpl"])
    return out


def _fastener_rows(lines: list[tuple[str, int]], per: int, material: str | None) -> list[dict]:
    return [{_NAME: name, _QTY: n * per, _EUR: fastener_price(material),
             _FIT: FASTENER_INSERT_EUR} for name, n in lines]


def sync_drawing_fasteners(lines: dict[str, list[tuple[str, int]]],
                           material: Callable[[str], str | None] = lambda cid: None) -> None:
    """An assembly drawing's fastener notes (``lines`` by drawing card) are
    its assembly's bought parts, added once. A part's own fasteners are not
    here: they stay on its card (``view.work``), wherever the part goes."""
    for a in _store().values():
        cid = a.get("drawing")
        if cid and lines.get(cid) and not a.get("fasteners_read"):
            a["fasteners_read"] = True
            rows = _fastener_rows(lines[cid], 1, material(cid))
            a["bought"] = a["bought_seed"] = [*a.get("bought", []), *rows]


def join(cids: list[str], name: str) -> None:
    """"Uusi kokoonpano": parts attached to each other, 1 kpl each, as one
    assembly. Their fasteners stay on their cards."""
    if not cids:
        return
    _store()[f"hand:{uuid.uuid4().hex[:8]}"] = {
        "name": name.strip() or "Kokoonpano", "members": {cid: 1 for cid in cids},
        "bought": [], "bought_seed": [], "hand": True}


def drawings() -> dict[str, str]:
    """``{assembly id: card id}`` of the assemblies made from a drawing: that
    card is the assembly's drawing, not one of its parts."""
    return {aid: a["drawing"] for aid, a in _store().items() if a.get("drawing")}


def _cards_of(a: dict, card_of: dict[str, str]) -> dict[str, int]:
    """An assembly's own cards with their kpl (members whose card is gone
    left out): the STEP model's (or joined by hand) first, then the ones its
    drawing calls out that the model doesn't have."""
    out: dict[str, int] = {}
    for sid, n in a.get("members", {}).items():
        cid = card_of.get(sid)
        if cid is not None and n > 0:
            out[cid] = max(out.get(cid, 0), int(n))
    for cid, n in a.get("drawn", {}).items():
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
        for c in store[aid].get("children", {}):
            if c in store and c not in seen:
                out |= set(own[c]) | below(c, seen | {c})
        return out
    return {aid: {c: n for c, n in cards.items() if c not in below(aid, {aid})}
            for aid, cards in own.items()}


def current(card_of: dict[str, str]) -> list[Assembly]:
    """The assemblies as priced: members by card id, bought parts, work."""
    out = []
    cards = _tree_cards(card_of)
    for aid, a in _store().items():
        bought = tuple(BoughtPart(str(r.get(_NAME) or ""), int(r.get(_QTY) or 0),
                                  float(r.get(_EUR) or 0.0), float(r.get(_FIT) or 0.0))
                       for r in a.get("bought", [])
                       if r.get(_NAME) or r.get(_EUR) or r.get(_FIT))
        work = AssemblyWork(**{f: float(st.session_state.get(f"asm_{f}_{aid}", d))
                               for f, d in _WORK_DEFAULTS.items()})
        out.append(Assembly(aid, a["name"], tuple(cards[aid].items()), bought, work,
                            tuple((c, int(n)) for c, n in a.get("children", {}).items())))
    return out


# ── The panel ─────────────────────────────────────────────────────────────────

_WORK_DEFAULTS = {f.name: f.default for f in fields(AssemblyWork)}
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
    parents = {c: (b["name"], n) for b in store.values()
               for c, n in b.get("children", {}).items() if c in store}
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


_ORIGIN = {
    "auto": "Tehty STEP-mallista: osat ja määrät mallin rakenteesta.",
    "drawing": "Tehty kokoonpanopiirustuksesta: osat ovat piirustuksessa mainitut "
               "osanumerot, 1 kpl / kokoonpano kukin. Tarkista määrät piirustuksesta.",
    "hand": "Yhdistetty käsin: toisiinsa kiinnitetyt osat.",
}


def _render_one(aid: str, a: dict, cards: dict[str, int],
                draw_cards: Callable[[str, dict[str, int]], None],
                remove_card: Callable[[str], None], card_of: dict[str, str],
                parent: tuple[str, int] | None) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
        a["name"] = c1.text_input("Kokoonpano", value=a["name"], key=f"asm_name_{aid}",
                                  help=_ORIGIN.get(next((k for k in _ORIGIN if a.get(k)), "")))
        c2.button("Pura" if a.get("hand") else "Poista", key=f"asm_del_{aid}",
                  on_click=undoable(_remove, f"kokoonpano {a['name']}"),
                  args=(aid, card_of, remove_card),
                  help="Purkaa kokoonpanon: osat jäävät irto-osiksi." if a.get("hand") else
                  "Poistaa kokoonpanon ja sen osat (kortit, piirustuksen ja STEP-mallin).")
        if a.get("awaited"):
            st.warning("Piirustuksessa on osia, joita ei ole ladattu: "
                       + ", ".join(a["awaited"]) + ". Lataa niiden DXF- tai STEP-tiedostot, "
                       "tai lisää ne alle ostettuina osina.")
        if parent is not None:
            st.caption(f"Alikokoonpano: {parent[1]} kpl kokoonpanossa {parent[0]}, "
                       "hinnoitellaan siinä.")
        store = _store()
        if kids := {c: n for c, n in a.get("children", {}).items() if c in store}:
            st.caption("Alikokoonpanot: " + " · ".join(
                f"🧩 {store[c]['name']} × {n}" for c, n in kids.items()))
        draw_cards(aid, cards)
        _render_work(aid, a)


def _remove(aid: str, card_of: dict[str, str], remove_card: Callable[[str], None]) -> None:
    """The assembly's "Poista": it goes with its parts' cards (a card in
    another assembly too stays), its drawing and its STEP file."""
    store = _store()
    a = store.pop(aid, {})
    if a.get("hand"):       # only undone: its parts are loose again
        return
    others = {c for b in store.values() for c in _cards_of(b, card_of)}
    for cid in [*_cards_of(a, card_of), *filter(None, [a.get("drawing")])]:
        if cid not in others:
            remove_card(cid)
    if a.get("auto"):
        bom.remove_model(a["auto"])


def _render_work(aid: str, a: dict) -> None:
    """The bought parts and the assembly work."""
    st.markdown("Ostetut osat", help="Valmiina ostettavat osat (ruuvit, tapit, "
                "puristemutterit, koneistetut osat…): (€/kpl + asennus) × kpl / "
                "kokoonpano. €/kpl on ostohinta, Asennus (€/kpl) yhden kappaleen "
                "kiinnitys osaan, esim. puristemutterin puristus.")
    if is_headless():
        # Not drawn: the editor forgets its edits, so it starts from them.
        a["bought_seed"] = a.get("bought", a.get("bought_seed", []))
        return
    bought = st.data_editor(
        a.get("bought_seed", []), key=f"asm_bought_{aid}", num_rows="dynamic",
        hide_index=True, width="stretch",
        column_config={_NAME: st.column_config.TextColumn(_NAME),
                       _QTY: st.column_config.NumberColumn(_QTY, min_value=0, step=1),
                       _EUR: st.column_config.NumberColumn(_EUR, min_value=0.0,
                                                           step=0.05, format="%.2f"),
                       _FIT: st.column_config.NumberColumn(_FIT, min_value=0.0,
                                                           step=0.05, format="%.2f")})
    a["bought"] = [dict(r) for r in bought]

    cols = st.columns(len(_WORK_LABELS))
    for col, (f, (label, step, help_)) in zip(cols, _WORK_LABELS.items()):
        col.number_input(label, min_value=0.0, step=step, help=help_,
                         value=float(st.session_state.get(f"asm_{f}_{aid}",
                                                          _WORK_DEFAULTS[f])),
                         key=f"asm_{f}_{aid}")
