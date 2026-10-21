"""Supporting files on the DXF tab: PDF drawings (and STEP models) dropped
with the DXFs are linked to the part cards by part number
(``core.linking``), can be relinked by hand, and show on their card as a
preview of the drawing. Each card also gets a "Tarkastettu" tick, counted
above the cards."""

from __future__ import annotations

import io

import streamlit as st

from core.linking import link_by_text, link_files
from view.common import render_page_picker

DOCS = "dxf_docs"            # {doc id: {"name", "size", "kind", "load"}}
_MANUAL = "dxf_doc_links"    # {doc id: part id or "" (= no part)}, set by hand
_PREVIEWS = "dxf_doc_previews"  # {doc id: PNG bytes of page 1}
TEXTS = "dxf_doc_texts"      # {doc id: [(page, text)]}, see core.materials.pdf_text
CHECKED = "dxf_checked_{}"   # widget key of a card's "Tarkastettu" tick

DOC_TYPES = {"pdf": "pdf", "step": "step", "stp": "step"}


def is_doc(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in DOC_TYPES


def add_doc(file_id: str, name: str, data: bytes, load=None) -> None:
    """Keep a dropped PDF or STEP file. With ``load`` (a file of a ZIP or a
    folder, read again when needed) its bytes aren't kept: a big RFQ's
    drawings and models are hundreds of MB."""
    st.session_state.setdefault(DOCS, {})[file_id] = {
        "name": name, "size": len(data), "kind": DOC_TYPES[name.rsplit(".", 1)[-1].lower()],
        "load": load or (lambda: data)}


def data(doc_id: str) -> bytes:
    """A kept file's bytes."""
    return docs()[doc_id]["load"]()


def remove_doc(doc_id: str) -> None:
    st.session_state.get(DOCS, {}).pop(doc_id, None)
    st.session_state.get(_MANUAL, {}).pop(doc_id, None)
    st.session_state.get(_PREVIEWS, {}).pop(doc_id, None)
    st.session_state.get(TEXTS, {}).pop(doc_id, None)


def docs() -> dict[str, dict]:
    return st.session_state.get(DOCS, {})


def _linkable() -> dict[str, dict]:
    """The files that link to cards: PDFs, and STEP files that couldn't be
    read as models (a model's parts are on the cards themselves, see
    ``view.bom``)."""
    from view import bom

    return {d: doc for d, doc in docs().items() if d not in bom.models()}


def links(parts: dict[str, str | list[str]], card_folders: dict[str, set[str]] | None = None,
          doc_folders: dict[str, str] | None = None) -> dict[str, list[str]]:
    """``{part id: [doc id, …]}``: the automatic links by part number, with
    the links set by hand on top. ``parts`` maps a card's id to its file name,
    or to the names of all its files. A PDF from a dropped ZIP that neither
    links goes to the one card whose files are in its folder
    (``card_folders``: card -> folders of its files, ``doc_folders``: doc ->
    its folder)."""
    all_docs = _linkable()
    names = {f"{pid}\0{i}": n for pid, ns in parts.items()
             for i, n in enumerate([ns] if isinstance(ns, str) else ns)}
    by_name = link_files(names, {d: doc["name"] for d, doc in all_docs.items()})
    named = {d for ids in by_name.values() for d in ids}
    unnamed = {d: _text(d, doc) for d, doc in all_docs.items()
               if d not in named and doc["kind"] == "pdf"}
    auto: dict[str, list[str]] = {}
    for found in (by_name, link_by_text(names, unnamed, lambda k: k.split("\0", 1)[0])):
        for key, ids in found.items():
            pid = key.split("\0", 1)[0]
            auto.setdefault(pid, []).extend(d for d in ids if d not in auto.get(pid, []))
    linked = {d for ids in auto.values() for d in ids}
    for d in unnamed:
        folder = (doc_folders or {}).get(d)
        cards = [c for c, fs in (card_folders or {}).items() if folder and folder in fs]
        if d not in linked and len(cards) == 1:
            auto.setdefault(cards[0], []).append(d)
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


def text(doc_id: str) -> str:
    """A linked PDF's text ("" for other files)."""
    doc = docs().get(doc_id)
    return _text(doc_id, doc) if doc is not None and doc["kind"] == "pdf" else ""


def pages(doc_id: str, doc: dict) -> list[tuple[int, str]]:
    """A PDF's first pages, ``[(page, text)]``, read once for the card links
    and the material proposals."""
    from core.materials import pdf_text

    cache = st.session_state.setdefault(TEXTS, {})
    if doc_id not in cache:
        cache[doc_id] = pdf_text(doc["load"]())
    return cache[doc_id]


def _text(doc_id: str, doc: dict) -> str:
    return "\n".join(text for _, text in pages(doc_id, doc))


_ROWS = 30   # files listed at once: a big RFQ has hundreds, each with a part picker


def doc_list_title(parts: dict[str, str]) -> tuple[str, int] | None:
    """The supporting files' tab label and how many are on no part; None
    when there are none."""
    all_docs = _linkable()
    if not all_docs:
        return None
    linked = {d for ids in links(parts).values() for d in ids}
    n_unlinked = sum(1 for d in all_docs if d not in linked)
    return f"Liitetiedostot ({len(all_docs)})", n_unlinked


def render_doc_list(parts: dict[str, str], labels: dict[str, str]) -> None:
    """The supporting files: which part each is linked to, changeable by
    hand, and a remove button."""
    all_docs = _linkable()
    if not all_docs:
        return
    linked = {d: pid for pid, ids in links(parts).items() for d in ids}
    options = ["", *parts]
    manual = st.session_state.setdefault(_MANUAL, {})
    shown = [d for d in all_docs if d not in linked]
    n_linked = len(all_docs) - len(shown)
    if n_linked and st.toggle(f"Näytä myös osiin linkitetyt ({n_linked})",
                              value=len(all_docs) <= _ROWS, key="dxf_doc_show_linked"):
        shown = list(all_docs)
    rows = render_page_picker(len(shown), _ROWS, key="dxf_doc_page",
                              label="Näytettävät tiedostot", suffix=f" / {len(shown)}")
    shown = shown[rows.start:rows.stop]
    for d in shown:
        doc = all_docs[d]
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


def render_card_docs(card_id: str, doc_ids: list[str]) -> None:
    """A card's linked drawings: the first page of each PDF, folded. One file
    can be on several cards (numbered pieces), so the widget keys name the card."""
    all_docs = docs()
    doc_ids = [d for d in doc_ids if d in all_docs]
    if not doc_ids:
        return
    names = ", ".join(all_docs[d]["name"] for d in doc_ids)
    with st.expander(f"Piirustukset: {names}"):
        for d in doc_ids:
            doc = all_docs[d]
            if doc["kind"] == "pdf":
                png = _preview(d, doc["load"])
                if png:
                    st.image(png, caption=doc["name"], width="stretch")
                else:
                    st.caption(f"{doc['name']}: esikatselua ei voitu tehdä.")
            else:
                _step_preview(card_id, d)
            st.download_button(f"Avaa {doc['name']}", data=doc["load"](),
                               file_name=doc["name"], key=f"dxf_doc_dl_{card_id}_{d}",
                               mime="application/pdf" if doc["kind"] == "pdf"
                               else "application/step")


def _step_preview(card_id: str, doc_id: str) -> None:
    from view import bom

    part = bom.single_part(doc_id)
    if part is not None and st.toggle(f"Näytä 3D: {part.name}", key=f"dxf_doc_3d_{card_id}_{doc_id}"):
        bom.render_3d(lambda: bom.shapes(doc_id)[part.key], f"{card_id}:{doc_id}")


def _preview(doc_id: str, load) -> bytes | None:
    cache = st.session_state.setdefault(_PREVIEWS, {})
    if doc_id not in cache:
        cache[doc_id] = pdf_first_page_png(load())
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
