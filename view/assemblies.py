"""Assemblies on the DXF tab (``core.assemblies``): parts sold together as
one quote line.

- **From a STEP assembly.** A dropped STEP file of several parts becomes an
  assembly: its sheet parts are the cards they are on, with the model's
  quantities, and its other bodies (pins, screws, machined parts) are bought
  parts to give a price.
- **By hand.** "Uusi kokoonpano" makes an empty one; any card is ticked into
  it with its kpl / kokoonpano.

Session keys: ``ASSEMBLIES`` holds ``{assembly id: {"name", "members":
{source id: kpl}, "bought": [row], "auto": STEP file id or None, …}}``. A
member is stored by a source id of its card (see ``core.items``), so it
stays on the right card when the card's files are regrouped."""

from __future__ import annotations

import hashlib

import streamlit as st

from core.assemblies import Assembly, AssemblyWork, BoughtPart
from view import bom

ASSEMBLIES = "dxf_assemblies"
_SEEN = "dxf_assemblies_seen"      # STEP file ids already made into assemblies
KEYS = (ASSEMBLIES, _SEEN)

_NAME, _QTY, _EUR = "Nimike", "Kpl / kokoonpano", "€/kpl"
_IN, _PART = "Mukana", "Osa"


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
                bought.append({_NAME: p.name, _QTY: p.qty, _EUR: 0.0})
        store[f"step:{file_id}"] = {"name": m["name"].rsplit(".", 1)[0], "members": members,
                                    "bought": bought, "bought_seed": bought, "auto": file_id}


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
                                  float(r.get(_EUR) or 0.0))
                       for r in a.get("bought", []) if r.get(_NAME) or r.get(_EUR))
        work = AssemblyWork(**{f: float(st.session_state.get(f"asm_{f}_{aid}", d))
                               for f, d in _WORK_DEFAULTS.items()})
        out.append(Assembly(aid, a["name"], tuple(_cards_of(a, card_of).items()), bought,
                            work))
    return out


def membership(cid: str, card_of: dict[str, str]) -> list[tuple[str, int]]:
    """``[(assembly name, kpl / kokoonpano)]`` of a card."""
    return [(a["name"], n) for a in _store().values()
            for c, n in _cards_of(a, card_of).items() if c == cid]


def clear() -> None:
    for key in KEYS:
        st.session_state.pop(key, None)


# ── The panel ─────────────────────────────────────────────────────────────────

_WORK_DEFAULTS = {"fit_min": 0.0, "weld_m": 0.0, "setup_min": 0.0, "eur_per_h": 60.0,
                  "other_eur": 0.0}
_WORK_LABELS = {
    "fit_min": ("Kokoonpanotyö (min/kok.)", 1.0, "Osien kokoaminen yhdeksi kokoonpanoksi."),
    "weld_m": ("Hitsaus (m/kok.)", 0.1, "Hinnoitellaan sivupalkin hitsaushinnalla."),
    "setup_min": ("Asetus (min/erä)", 5.0, "Kerran erää kohden, jaetaan erän kokoonpanoille."),
    "eur_per_h": ("Työ (€/h)", 5.0, "Kokoonpanotyön ja asetuksen tuntihinta."),
    "other_eur": ("Muut (€/kok.)", 0.5, "Esim. pakkaus tai koko kokoonpanon maalaus."),
}


def render(labels: dict[str, str], card_of: dict[str, str]) -> None:
    """The assemblies: each one's parts (ticked from the cards, with their
    kpl / kokoonpano), bought parts and assembly work, and a button for a
    new one. ``labels`` are the cards' "#1 · name"."""
    store = _store()
    title = f"Kokoonpanot ({len(store)})" if store else "Kokoonpanot"
    with st.expander(title, expanded=bool(store)):
        st.caption("Kokoonpano on yksi tarjousrivi: sen osat (kortit), ostetut osat ja "
                   "kokoamistyö. Eräkoko tarkoittaa kokoonpanojen määrää, ja jokainen osa "
                   "hinnoitellaan määrällä eräkoko × kpl / kokoonpano. STEP-kokoonpano "
                   "tehdään tänne automaattisesti.")
        for aid in list(store):
            _render_one(aid, store[aid], labels, card_of)
        if st.button("Uusi kokoonpano", key="asm_new"):
            n = 1 + sum(aid.startswith("m") for aid in store)
            while f"m{n}" in store:
                n += 1
            store[f"m{n}"] = {"name": f"Kokoonpano {n}", "members": {}, "bought": [],
                              "bought_seed": [], "auto": None}
            st.rerun()


def _render_one(aid: str, a: dict, labels: dict[str, str], card_of: dict[str, str]) -> None:
    with st.container(border=True):
        c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
        a["name"] = c1.text_input("Kokoonpano", value=a["name"], key=f"asm_name_{aid}")
        if c2.button("Poista", key=f"asm_del_{aid}"):
            _store().pop(aid, None)
            st.rerun()
        if a.get("auto"):
            st.caption("Tehty STEP-mallista: osat ja määrät mallin rakenteesta.")

        # The parts: every card, ticked in or not. The editor's rows are seeded
        # once per set of cards (its key changes with them), and what it returns
        # is stored: feeding it back would apply the edits twice.
        members = _cards_of(a, card_of)
        cards = list(labels)
        digest = hashlib.sha1("\0".join(cards).encode()).hexdigest()[:8]
        if a.get("parts_digest") != digest:
            a["parts_digest"] = digest
            a["parts_v"] = a.get("parts_v", 0) + 1
            a["parts_seed"] = [{_IN: cid in members, _PART: labels[cid],
                                _QTY: members.get(cid, 1)} for cid in cards]
        rows = st.data_editor(
            a["parts_seed"], key=f"asm_parts_{aid}_{a['parts_v']}", hide_index=True,
            width="stretch", disabled=[_PART],
            column_config={_IN: st.column_config.CheckboxColumn(_IN, width="small"),
                           _QTY: st.column_config.NumberColumn(_QTY, min_value=1, step=1)})
        a["members"] = {cid: int(r[_QTY] or 1) for cid, r in zip(cards, rows) if r[_IN]}

        st.markdown("Ostetut osat", help="Valmiina ostettavat osat (ruuvit, tapit, "
                    "koneistetut osat…): kappalehinta × kpl / kokoonpano.")
        bought = st.data_editor(
            a.get("bought_seed", []), key=f"asm_bought_{aid}", num_rows="dynamic",
            hide_index=True, width="stretch",
            column_config={_NAME: st.column_config.TextColumn(_NAME),
                           _QTY: st.column_config.NumberColumn(_QTY, min_value=0, step=1),
                           _EUR: st.column_config.NumberColumn(_EUR, min_value=0.0,
                                                               step=0.05, format="%.2f")})
        a["bought"] = [dict(r) for r in bought]

        cols = st.columns(len(_WORK_LABELS))
        for col, (f, (label, step, help_)) in zip(cols, _WORK_LABELS.items()):
            col.number_input(label, min_value=0.0, step=step, help=help_,
                             value=float(st.session_state.get(f"asm_{f}_{aid}",
                                                              _WORK_DEFAULTS[f])),
                             key=f"asm_{f}_{aid}")
