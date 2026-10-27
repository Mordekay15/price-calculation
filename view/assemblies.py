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

Its cards are drawn inside it, apart from the loose parts.

Session keys: ``ASSEMBLIES`` holds ``{assembly id: {"name", "members":
{source id: kpl}, "bought": [row], "auto": STEP file id, …}}``. A
member is stored by a source id of its card (see ``core.items``), so it
stays on the right card when the card's files are regrouped."""

from __future__ import annotations

import re
from dataclasses import fields
from collections.abc import Callable

from core.assemblies import Assembly, AssemblyWork, BoughtPart
from core.items import NO_BLANK
from core.linking import base_key, callouts
from core.work import FASTENER_INSERT_EUR
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
           remove_card: Callable[[str], None]) -> None:
    """The assemblies, apart from the loose parts: each one's cards (drawn by
    ``draw_cards(assembly id, {card id: kpl / kokoonpano})``), bought parts and
    assembly work. Removing an assembly removes its cards and drawing
    (``remove_card``) and its STEP file."""
    store = _store()
    if not store:
        return
    st.divider()
    st.markdown(f"**Kokoonpanot ({len(store)})**",
                help="Kokoonpano on yksi tarjousrivi: sen osat (kortit), ostetut osat ja "
                     "kokoamistyö. Eräkoko tarkoittaa kokoonpanojen määrää, ja jokainen osa "
                     "hinnoitellaan määrällä eräkoko × kpl / kokoonpano. STEP-kokoonpano "
                     "tehdään tänne automaattisesti.")
    for aid in list(store):
        _render_one(aid, store[aid], card_of, draw_cards, remove_card)


_ORIGIN = {
    "auto": "Tehty STEP-mallista: osat ja määrät mallin rakenteesta.",
    "drawing": "Tehty kokoonpanopiirustuksesta: osat ovat piirustuksessa mainitut "
               "osanumerot, 1 kpl / kokoonpano kukin. Tarkista määrät piirustuksesta.",
}


def _render_one(aid: str, a: dict, card_of: dict[str, str],
                draw_cards: Callable[[str, dict[str, int]], None],
                remove_card: Callable[[str], None]) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
        a["name"] = c1.text_input("Kokoonpano", value=a["name"], key=f"asm_name_{aid}",
                                  help=_ORIGIN.get(next((k for k in _ORIGIN if a.get(k)), "")))
        c2.button("Poista", key=f"asm_del_{aid}",
                  on_click=undoable(_remove, f"kokoonpano {a['name']}"),
                  args=(aid, card_of, remove_card),
                  help="Poistaa kokoonpanon ja sen osat (kortit, piirustuksen ja STEP-mallin).")
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
