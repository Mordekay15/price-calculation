"""Supporting files on the DXF tab: PDF drawings (and STEP models) dropped
with the DXFs are linked to the part cards by part number
(``core.linking``), can be relinked by hand, and show on their card as a
preview of the drawing. Each card also gets a "Tarkastettu" tick, counted
above the cards."""

from __future__ import annotations

import io

import streamlit as st

from core.linking import link_files

DOCS = "dxf_docs"            # {doc id: {"name", "data", "kind"}}
_MANUAL = "dxf_doc_links"    # {doc id: part id or "" (= no part)}, set by hand
_PREVIEWS = "dxf_doc_previews"  # {doc id: PNG bytes of page 1}
CHECKED = "dxf_checked_{}"   # widget key of a card's "Tarkastettu" tick

DOC_TYPES = {"pdf": "pdf", "step": "step", "stp": "step"}


def is_doc(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in DOC_TYPES


def add_doc(file_id: str, name: str, data: bytes) -> None:
    st.session_state.setdefault(DOCS, {})[file_id] = {
        "name": name, "data": data, "kind": DOC_TYPES[name.rsplit(".", 1)[-1].lower()]}


def remove_doc(doc_id: str) -> None:
    st.session_state.get(DOCS, {}).pop(doc_id, None)
    st.session_state.get(_MANUAL, {}).pop(doc_id, None)
    st.session_state.get(_PREVIEWS, {}).pop(doc_id, None)


def docs() -> dict[str, dict]:
    return st.session_state.get(DOCS, {})


def links(parts: dict[str, str]) -> dict[str, list[str]]:
    """``{part id: [doc id, …]}``: the automatic links by part number, with
    the links set by hand on top. ``parts`` maps a card's id to its file name."""
    all_docs = docs()
    auto = link_files(parts, {d: doc["name"] for d, doc in all_docs.items()})
    manual = st.session_state.get(_MANUAL, {})
    out: dict[str, list[str]] = {}
    for pid, ids in auto.items():
        for d in ids:
            if d not in manual:
                out.setdefault(pid, []).append(d)
    for d, pid in manual.items():
        if pid and pid in parts and d in all_docs:
            out.setdefault(pid, []).append(d)
    return out


def render_doc_list(parts: dict[str, str], labels: dict[str, str]) -> None:
    """The supporting files, folded under the drop area: which part each is
    linked to, changeable by hand, and a remove button."""
    all_docs = docs()
    if not all_docs:
        return
    linked = {d: pid for pid, ids in links(parts).items() for d in ids}
    n_unlinked = sum(1 for d in all_docs if d not in linked)
    title = f"Liitetiedostot ({len(all_docs)})"
    if n_unlinked:
        title += f" · {n_unlinked} ilman osaa"
    with st.expander(title, expanded=bool(n_unlinked)):
        st.caption("PDF- ja STEP-tiedostot linkitetään osiin tiedostonimen osanumeron "
                   "mukaan (revisiotunnus, esim. -revA, ohitetaan). Vaihda osa tarvittaessa.")
        options = ["", *parts]
        manual = st.session_state.setdefault(_MANUAL, {})
        for d, doc in list(all_docs.items()):
            c1, c2, c3 = st.columns([3, 4, 1], vertical_alignment="center")
            c1.markdown(f"{'📄' if doc['kind'] == 'pdf' else '🧊'} {doc['name']}")
            current = linked.get(d, "")
            choice = c2.selectbox(
                "Osa", options, index=options.index(current) if current in options else 0,
                key=f"dxf_doc_link_{d}_{current}", label_visibility="collapsed",
                format_func=lambda pid: "— ei osaa —" if not pid else labels.get(pid, pid))
            if choice != current:
                manual[d] = choice
                st.rerun()
            c3.button("Poista", key=f"dxf_doc_del_{d}", on_click=remove_doc, args=(d,))


def render_card_docs(doc_ids: list[str]) -> None:
    """A card's linked drawings: the first page of each PDF, folded."""
    all_docs = docs()
    doc_ids = [d for d in doc_ids if d in all_docs]
    if not doc_ids:
        return
    names = ", ".join(all_docs[d]["name"] for d in doc_ids)
    with st.expander(f"Piirustukset: {names}"):
        for d in doc_ids:
            doc = all_docs[d]
            if doc["kind"] == "pdf":
                png = _preview(d, doc["data"])
                if png:
                    st.image(png, caption=doc["name"], width="stretch")
                else:
                    st.caption(f"{doc['name']}: esikatselua ei voitu tehdä.")
            st.download_button(f"Avaa {doc['name']}", data=doc["data"],
                               file_name=doc["name"], key=f"dxf_doc_dl_{d}",
                               mime="application/pdf" if doc["kind"] == "pdf"
                               else "application/step")


def _preview(doc_id: str, data: bytes) -> bytes | None:
    cache = st.session_state.setdefault(_PREVIEWS, {})
    if doc_id not in cache:
        cache[doc_id] = pdf_first_page_png(data)
    return cache[doc_id]


def pdf_first_page_png(data: bytes, scale: float = 1.5) -> bytes | None:
    """Page 1 of a PDF as PNG bytes; None when it can't be drawn."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        image = pdf[0].render(scale=scale).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a broken PDF just has no preview
        return None


def render_checked_count(part_ids: list[str]) -> None:
    """How many cards are ticked as checked."""
    if not part_ids:
        return
    done = sum(bool(st.session_state.get(CHECKED.format(p))) for p in part_ids)
    colour = "green" if done == len(part_ids) else "gray"
    st.caption(f":{colour}[Tarkastettu {done} / {len(part_ids)} osaa]")
