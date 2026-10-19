"""Material proposals on the DXF tab: read the material and thickness from
each part's drawings (DXF notes, linked PDFs) and the customer's request,
propose a catalogue material for every card, flag sources that disagree, and
apply only the proposals the user ticks (see ``core.materials``)."""

from __future__ import annotations

import streamlit as st

from core.materials import (
    HIGH,
    LOW,
    MEDIUM,
    Proposal,
    ask_claude,
    claude_available,
    extract_hints,
    load_aliases,
    pdf_text,
    propose,
    save_aliases,
)

_RESULT = "dxf_material_proposals"   # list[Proposal] from the last search
_PDF_TEXT = "dxf_doc_texts"          # {doc id: [(page, text)]}
_NOTE = "dxf_rfq_note"

_CONFIDENCE = {HIGH: "🟢 korkea", MEDIUM: "🟡 keskitaso", LOW: "🔴 matala"}


def render(parts: list[tuple[str, object]], doc_links: dict[str, list[str]],
           docs: dict[str, dict], materials: list[str], thicknesses_for, apply) -> None:
    """The proposals panel. ``parts`` are ``(card id, DxfFile)``, ``doc_links``
    the files linked to each card, ``apply(card id, material, thickness)``
    sets a card's choice."""
    if not parts:
        return
    result: list[Proposal] | None = st.session_state.get(_RESULT)
    n_conflicts = sum(bool(p.conflict) for p in result or [])
    title = "Materiaaliehdotukset"
    if result:
        title += f" · {len(result)} osaa" + (f", {n_conflicts} tarkistettavaa"
                                               if n_conflicts else "")
    with st.expander(title, expanded=bool(n_conflicts)):
        st.caption("Luetaan materiaali ja paksuus osien piirustuksista (DXF-tekstit ja "
                   "linkitetyt PDF:t) ja tarjouspyynnöstä. Mitään ei muuteta ennen kuin "
                   "valitset ehdotukset ja painat Käytä valitut.")
        st.text_area("Tarjouspyynnön teksti", key=_NOTE, height=90,
                     placeholder="Esim. \"DXF:t tulee S355 2 mm\"",
                     help="Käytetään osille, joiden omista tiedostoista ei löydy materiaalia "
                          "tai paksuutta.")
        c1, c2, _ = st.columns([2, 2, 3])
        if c1.button("Etsi materiaalit", key="dxf_find_materials", type="primary"):
            st.session_state[_RESULT] = _search(parts, doc_links, docs, materials,
                                                thicknesses_for)
            st.rerun()
        if result and claude_available() and any(
                p.material is None or p.thickness is None for p in result):
            if c2.button("Kysy tekoälyltä avoimet", key="dxf_ask_claude",
                         help="Lähettää avointen osien tekstit ja hinnaston materiaalit "
                              "Clauden API:lle. Vaatii API-avaimen."):
                try:
                    with st.spinner("Tekoäly lukee piirustuksia…"):
                        st.session_state[_RESULT] = ask_claude(
                            result, st.session_state.get(_NOTE, ""), materials,
                            thicknesses_for)
                    st.rerun()
                except Exception as e:  # noqa: BLE001 - shown on the page
                    st.error(f"Tekoälyä ei voitu käyttää: {e}")
        if result:
            _render_result(result, apply)


def _search(parts, doc_links, docs, materials, thicknesses_for) -> list[Proposal]:
    texts = st.session_state.setdefault(_PDF_TEXT, {})
    by_part = {}
    for fid, dxf in parts:
        hints = extract_hints("\n".join(dxf.texts), f"{dxf.name} (DXF)")
        for d in doc_links.get(fid, []):
            doc = docs.get(d)
            if doc is None or doc["kind"] != "pdf":
                continue
            if d not in texts:
                texts[d] = pdf_text(doc["data"])
            for page, text in texts[d]:
                hints += extract_hints(text, f"{doc['name']}, s. {page}")
        by_part[fid] = (dxf.name, hints)
    note = extract_hints(st.session_state.get(_NOTE, ""), "Tarjouspyyntö")
    return propose(by_part, note, materials, thicknesses_for, load_aliases())


def _render_result(result: list[Proposal], apply) -> None:
    conflicts = [p for p in result if p.conflict]
    if conflicts:
        st.markdown("**Tarkistettavat**")
        for p in conflicts:
            st.warning(f"**{p.part}**: {p.conflict}")
    rows = [{
        "Käytä": p.material is not None and not p.conflict,
        "Osa": p.part,
        "Materiaali": p.material or "",
        "Paksuus": p.thickness or "",
        "Varmuus": _CONFIDENCE.get(p.confidence, p.confidence),
        "Lähde": "; ".join(f"{h.source}: {h.raw}" for h in p.evidence) or "—",
        "Peruste": {"alias": "aiempi valinta", "säännöt": "säännöt",
                    "tekoäly": f"tekoäly: {p.note}"}.get(p.via, p.via),
    } for p in result]
    edited = st.data_editor(rows, hide_index=True, width="stretch", key="dxf_material_table",
                            disabled=["Osa", "Materiaali", "Paksuus", "Varmuus", "Lähde",
                                      "Peruste"])
    c1, c2 = st.columns([2, 3])
    remember = c2.checkbox("Muista valinnat seuraavaa kertaa varten", value=True,
                           key="dxf_material_remember",
                           help="Tallentaa löydetyn tekstin ja valitun materiaalin "
                                "aliastaulukkoon (material_aliases.json).")
    if c1.button("Käytä valitut", key="dxf_apply_materials"):
        aliases = {}
        for row, p in zip(edited, result):
            if row["Käytä"] and p.material:
                apply(p.part_id, p.material, p.thickness)
                if p.raw:
                    aliases[p.raw] = {"material": p.material, "thickness": p.thickness}
        if remember and aliases:
            save_aliases(aliases)
        st.session_state.pop(_RESULT, None)
        st.rerun()
