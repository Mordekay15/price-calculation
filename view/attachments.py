"""Supporting files on the DXF tab: PDF drawings (and STEP models) dropped
with the DXFs are linked to the part cards by part number
(``core.linking``), can be relinked by hand, and show on their card as a
preview of the drawing."""

from __future__ import annotations

import io

import streamlit as st

from core.linking import link_by_text, link_files
from core.model import InputFile
from view import files
from view.common import render_page_picker

_MANUAL = "dxf_doc_links"    # {doc id: part id or "" (= no part)}, set by hand
_PREVIEWS = "dxf_doc_previews"  # {doc id: PNG bytes of page 1}



def add_doc(f: InputFile, data: bytes) -> None:
    """Keep a dropped PDF or STEP file (``view.files``). A file of a ZIP is
    read from it again when needed (``f.load``), so its bytes aren't kept: a
    big RFQ's drawings and models are hundreds of MB."""
    if f.load is None:
        f.load = lambda: data
    f.size_bytes = f.size_bytes or len(data)
    files.add(f)


def data(doc_id: str) -> bytes:
    """A kept file's bytes."""
    return files.get(doc_id).load()


def remove_doc(doc_id: str) -> None:
    files.remove(doc_id)
    st.session_state.get(_MANUAL, {}).pop(doc_id, None)
    st.session_state.get(_PREVIEWS, {}).pop(doc_id, None)


def docs() -> dict[str, InputFile]:
    """The PDF and STEP files, ``{id: InputFile}``."""
    return files.of_kind("pdf", "step")


def _linkable() -> dict[str, InputFile]:
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
    by_name = link_files(names, {d: doc.name for d, doc in all_docs.items()})
    named = {d for ids in by_name.values() for d in ids}
    # An assembly drawing names its parts' numbers: it is no part's drawing.
    unnamed = {d: text(d) for d, doc in all_docs.items()
               if d not in named and doc.kind == "pdf" and not _is_assembly(d)}
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


def _is_assembly(doc_id: str) -> bool:
    """The PDF reader found the PDF to be an assembly drawing."""
    res = files.result(doc_id)
    return res is not None and any(f.value["verdict"] == "assembly"
                                   for f in res.of("assembly"))


def text(doc_id: str) -> str:
    """A PDF's text as the PDF reader read it ("" for other files)."""
    f = files.get(doc_id)
    if f is None or f.kind != "pdf":
        return ""
    res = files.result(doc_id)
    return "\n".join(t for _, t in res.record[0]) if res is not None and res.record else ""


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
        hint = assembly_hint(d)
        c1.markdown(f"{'📄' if doc.kind == 'pdf' else '🧊'} {doc.name}"
                    + (f"  \n:gray[{hint}]" if hint else ""))
        current = linked.get(d, "")
        key = f"dxf_doc_link_{d}_{current}"
        if st.session_state.get(key) not in options:   # seeded, not a default: kept keys
            st.session_state[key] = current if current in options else ""
        choice = c2.selectbox(
            "Osa", options, key=key, label_visibility="collapsed",
            format_func=lambda pid: "— ei osaa —" if not pid else labels.get(pid, pid))
        if choice != current:
            manual[d] = choice
            st.rerun()
        c3.button("Poista", key=f"dxf_doc_del_{d}", on_click=remove_doc, args=(d,))


def assembly_hint(doc_id: str) -> str:
    """What the PDF reader thinks of a read PDF: an assembly drawing, or
    maybe one, and why (see ``core.pdf_assembly``); "" for a part or a
    file not read."""
    if not files.is_read(doc_id):
        return ""
    said = files.result(doc_id).of("assembly")
    if not said:
        return ""
    v = said[0].value
    if v["verdict"] == "assembly":
        return f"Kokoonpanopiirustus: {v['why']}"
    if v["verdict"] == "maybe":
        return f"Voi olla kokoonpano ({v['score']:+d} p, kokoonpano 4+): {v['why']}"
    return ""


def render_card_docs(card_id: str, doc_ids: list[str]) -> None:
    """A card's linked drawings: the first page of each PDF, folded. One file
    can be on several cards (numbered pieces), so the widget keys name the card."""
    all_docs = docs()
    doc_ids = [d for d in doc_ids if d in all_docs]
    if not doc_ids:
        return
    names = ", ".join(all_docs[d].name for d in doc_ids)
    with st.expander(f"Piirustukset: {names}"):
        for d in doc_ids:
            doc = all_docs[d]
            if doc.kind == "pdf":
                png = _preview(d, doc.load)
                if png:
                    st.image(png, caption=doc.name, width="stretch")
                else:
                    st.caption(f"{doc.name}: esikatselua ei voitu tehdä.")
            else:
                _step_preview(card_id, d)
            st.download_button(f"Avaa {doc.name}", data=doc.load(),
                               file_name=doc.name, key=f"dxf_doc_dl_{card_id}_{d}",
                               mime="application/pdf" if doc.kind == "pdf"
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
