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
- **From a part's fasteners.** A part whose drawing calls out fasteners
  (ruuvit, mutterit, niitit…, e.g. "M5-PURISTEMUTTERIT X4KPL") is an
  assembly of itself and them: the fasteners are its bought parts, priced
  there and not on the card. In an assembly of several parts, its fasteners
  are that assembly's bought parts instead.
- **By hand.** Parts attached to each other (welded, riveted, screwed
  together) are joined into one with "Uusi kokoonpano".

Its cards are drawn inside it, apart from the loose parts.

Session keys: ``ASSEMBLIES`` holds ``{assembly id: {"name", "members":
{source id: kpl}, "bought": [row], "auto": STEP file id, "drawing": card
id, "fasteners": card id, "hand": True, "fasteners_of": [card id whose
fasteners are in "bought"], …}}``. A
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


def sync_from_models() -> None:
    """Make an assembly of every STEP assembly dropped since the last time,
    and drop the ones whose STEP file was removed."""
    store, seen = _store(), st.session_state.setdefault(_SEEN, set())
    models = bom.models()
    for aid, a in list(store.items()):
        if a.get("auto") and a["auto"] not in models:
            store.pop(aid)
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
        store[f"step:{file_id}"] = {"name": m["name"].rsplit(".", 1)[0], "members": members,
                                    "bought": bought, "bought_seed": bought, "auto": file_id}


def sync_from_drawings(items: dict, texts: Callable[[str], str]) -> None:
    """Make an assembly of every assembly drawing among the cards: ``items``
    are the cards (``core.items.Item`` by card id), ``texts(card id)`` the
    text of its drawings."""
    store = _store()
    keys = {cid: {base_key(n) for n in item.names.values()} for cid, item in items.items()}
    live = set()
    for cid, item in items.items():
        scores = item.scores.values()
        if any(c.valid for c in scores) or not any(c.note == NO_BLANK for c in scores):
            continue
        expects = [k for k in callouts(texts(cid)) if k not in keys[cid]]
        aid = f"drawing:{cid}"
        if not expects:
            continue
        live.add(aid)
        a = store.setdefault(aid, {"name": base_key(item.names[cid]).upper(), "members": {},
                                   "bought": [], "bought_seed": [], "drawing": cid})
        a["expects"] = expects
        found = {k: other for k in expects for other, ks in keys.items() if k in ks}
        a["members"] = {m: a["members"].get(m, 1) for m in found.values()}
        bought = {base_key(str(r.get(_NAME) or "")) for r in a.get("bought", [])}
        a["awaited"] = [k.upper() for k in expects if k not in found and k not in bought]
    for aid in [aid for aid, a in store.items() if a.get("drawing") and aid not in live]:
        store.pop(aid)


def _fastener_rows(lines: list[tuple[str, int]], per: int, material: str | None) -> list[dict]:
    return [{_NAME: name, _QTY: n * per, _EUR: fastener_price(material),
             _FIT: FASTENER_INSERT_EUR} for name, n in lines]


def sync_from_fasteners(lines: dict[str, list[tuple[str, int]]], names: dict[str, str],
                        card_of: dict[str, str],
                        material: Callable[[str], str | None] = lambda cid: None) -> None:
    """Make an assembly of every card whose drawing calls out fasteners:
    ``lines`` are each card's ``(name, kpl / osa)``, ``names`` its name. A
    card in an assembly of several parts puts its fasteners in that one
    (once, × its kpl / kokoonpano) and has none of its own."""
    store = _store()
    joined = {cid: (aid, n) for aid, a in store.items() if not a.get("fasteners")
              for cid, n in _cards_of(a, card_of).items()}
    # An assembly drawing's own notes are its assembly's fasteners.
    joined |= {a["drawing"]: (aid, 1) for aid, a in store.items() if a.get("drawing")}
    live = set()
    for cid, ls in lines.items():
        if not ls:
            continue
        if cid in joined:
            aid, n = joined[cid]
            a = store[aid]
            if cid not in a.setdefault("fasteners_of", []):
                a["fasteners_of"].append(cid)
                own = store.get(f"fasteners:{cid}")
                rows = ([{**r, _QTY: int(r.get(_QTY) or 0) * n} for r in own["bought"]]
                        if own else _fastener_rows(ls, n, material(cid)))
                a["bought"] = a["bought_seed"] = [*a.get("bought", []), *rows]
            continue
        aid = f"fasteners:{cid}"
        live.add(aid)
        if aid not in store:
            rows = _fastener_rows(ls, 1, material(cid))
            store[aid] = {"name": names.get(cid, cid).rsplit(".", 1)[0], "members": {cid: 1},
                          "bought": rows, "bought_seed": rows, "fasteners": cid}
    for aid in [aid for aid, a in store.items() if a.get("fasteners") and aid not in live]:
        store.pop(aid)


def join(cids: list[str], name: str) -> None:
    """"Uusi kokoonpano": parts attached to each other, 1 kpl each, as one
    assembly. Their fasteners (as edited) come with them."""
    if not cids:
        return
    store = _store()
    bought, of = [], []
    for cid in cids:
        own = store.pop(f"fasteners:{cid}", None)
        if own:
            bought += own.get("bought", [])
            of.append(cid)
    store[f"hand:{uuid.uuid4().hex[:8]}"] = {
        "name": name.strip() or "Kokoonpano", "members": {cid: 1 for cid in cids},
        "bought": bought, "bought_seed": bought, "hand": True, "fasteners_of": of}


def drawings() -> dict[str, str]:
    """``{assembly id: card id}`` of the assemblies made from a drawing: that
    card is the assembly's drawing, not one of its parts."""
    return {aid: a["drawing"] for aid, a in _store().items() if a.get("drawing")}


def _cards_of(a: dict, card_of: dict[str, str]) -> dict[str, int]:
    """An assembly's members by card id (members whose card is gone left
    out)."""
    out: dict[str, int] = {}
    for sid, n in a["members"].items():
        cid = card_of.get(sid)
        if cid is not None and n > 0:
            out[cid] = max(out.get(cid, 0), int(n))
    return out


def current(card_of: dict[str, str]) -> list[Assembly]:
    """The assemblies as priced: members by card id, bought parts, work."""
    out = []
    for aid, a in _store().items():
        bought = tuple(BoughtPart(str(r.get(_NAME) or ""), int(r.get(_QTY) or 0),
                                  float(r.get(_EUR) or 0.0), float(r.get(_FIT) or 0.0))
                       for r in a.get("bought", [])
                       if r.get(_NAME) or r.get(_EUR) or r.get(_FIT))
        work = AssemblyWork(**{f: float(st.session_state.get(f"asm_{f}_{aid}", d))
                               for f, d in _WORK_DEFAULTS.items()})
        out.append(Assembly(aid, a["name"], tuple(_cards_of(a, card_of).items()), bought,
                            work))
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
    for aid in list(store):
        _render_one(aid, store[aid], card_of, draw_cards, remove_card)


def _render_new(col, labels: dict[str, str]) -> None:
    """"Uusi kokoonpano": pick the parts attached to each other."""
    ss = st.session_state
    ss["asm_new_parts"] = [c for c in ss.get("asm_new_parts") or [] if c in labels]
    with col.popover("Uusi kokoonpano", width="stretch"):
        st.text_input("Nimi", key="asm_new_name")
        st.multiselect("Osat", list(labels), format_func=labels.get, key="asm_new_parts",
                       help="Toisiinsa kiinnitetyt osat (hitsatut, niitatut, ruuvatut), "
                            "1 kpl / kokoonpano kukin. Osien kiinnikkeet tulevat mukaan.")
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
    "fasteners": "Osa kiinnikkeineen: kiinnikkeet (ruuvit, mutterit…) luettu piirustuksen "
                 "teksteistä ostetuiksi osiksi. Tarkista määrät ja hinnat.",
    "hand": "Yhdistetty käsin: toisiinsa kiinnitetyt osat.",
}


def _render_one(aid: str, a: dict, card_of: dict[str, str],
                draw_cards: Callable[[str, dict[str, int]], None],
                remove_card: Callable[[str], None]) -> None:
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
        draw_cards(aid, _cards_of(a, card_of))
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
