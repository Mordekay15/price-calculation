"""The DXF tab's files and cards: what was dropped, read once into the
session, and grouped into one card per part.

``ingest`` reads each dropped file once (a DWG is converted to DXF first, a
ZIP opened, a STEP model unfolded) and empties the drop area; ``sync``
groups the files into cards and chooses each card's shape (``core.items``).
Each card is a ``core.model.Part`` (``PARTS``): its files, the one priced,
its material and thickness. It keeps them while files join or leave it, and
``evict`` removes it with everything kept for it.
The card itself is drawn by ``view.dxf_tab``."""

import copy
import re
import time
from types import SimpleNamespace

from core.archive import ArchiveError, is_zip, unpack
from core.dxf import DxfFile, DxfReport
from core.items import STEP_CHOICE, Source, choose, group_sources, score_dxf
from core.linking import base_key
from core.linking import bom_quantities as _bom_quantities
from core.model import InputFile, Part, PartSource, file_kind
from core.step import available as step_available
from core.step import bends_on
from view import assemblies, attachments, bom, files, material_agent
from view.common import thicknesses_for
from view.ui import ids_in, st
from view.work import AUTO_SHOWN, PART_BOUGHT

STORE   = "dxf_store"         # {card id: DxfFile}: each card's chosen shape
REPORTS = "dxf_part_reports"  # {card id: DxfReport}
PARTS   = "dxf_parts"         # {card id: Part}: each card's files and values
_HIDDEN  = "dxf_hidden"        # model parts removed from the cards
_SRC_REPORTS = "dxf_source_reports"  # {source id: DxfReport}, for choosing
_BEND_LINES = "dxf_bend_lines"  # {(card, shape, flat): STEP bend lines on the DXF}
_INBOX   = "dxf_inbox"         # bumped to give the uploader a fresh, empty key
FILL_ASKED = "dxf_fill_asked" # the "same material for the others?" question is answered
_DUPES   = "dxf_dupes"         # (names of dropped files already on a card, warn until)
_DUPE_WARNING_S = 15           # how long that warning stays
_SKIPPED = "dxf_skipped"       # names of the last drop's files that aren't used
_BIG_STEP_BYTES = 5 << 20        # a STEP file this big waits, in a drop of many files
_USED = ("dxf", "dwg", "pdf", "step", "stp")
_SKIPPED_SHOWN = 12            # names listed in the skipped line


# Streamlit's uploader already takes dropped files, but it looks like a plain
# button. Make it a tall dashed drop zone that says files can be dropped on it.
_DROPZONE_CSS = """
<style>
[class*="st-key-dxf_uploader"] [data-testid="stFileUploaderDropzone"] {
    min-height: 9rem;
    border: 2px dashed rgba(128, 128, 128, 0.6);
    justify-content: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
}
[class*="st-key-dxf_uploader"] [data-testid="stFileUploaderDropzone"]::before {
    content: "Vedä ja pudota DXF-, DWG-, PDF-, STEP- tai ZIP-tiedostot tähän";
    width: 100%;
    text-align: center;
    font-weight: 600;
}
</style>
"""


def render_drop_area() -> list[tuple[str, DxfFile]]:
    """The drop area; returns every part read so far, in upload order."""
    st.html(_DROPZONE_CSS)
    uploaded = st.file_uploader(
        "Lataa DXF-, DWG-, PDF- tai STEP-tiedostot tai ZIP",
        accept_multiple_files=True,
        key=f"dxf_uploader_{st.session_state.get(_INBOX, 0)}",
        help="Vedä tiedostot alueelle tai valitse ne koneelta. Voit ladata useita "
             "tiedostoja kerralla. Saman osanumeron tiedostot (DXF, DWG, STEP, PDF) "
             "kootaan yhdelle osakortille, ja paras muoto valitaan niistä "
             "automaattisesti; kortin Poista-painike poistaa osan. DWG-tiedosto "
             "muunnetaan ensin DXF:ksi (ODA File Converter tai LibreDWG). Muut "
             "tiedostot (esim. Creon .prt- ja .drw-tiedostot) ohitetaan. ZIP-tiedosto "
             "puretaan: jokainen kansio on yleensä yksi osa, ja kansio auttaa "
             "yhdistämään tiedostot oikeaan osaan.",
    )
    if ingest(list(uploaded or [])):
        st.rerun()  # redraw with the drop area empty
    parts = sync()
    sync_assemblies()
    if not step_available() and any(bom.is_step(d["name"]) for d in attachments.docs().values()):
        st.warning("STEP-malleja ei voi lukea tällä palvelimella: Open CASCADE (cadquery-ocp) "
                   "ei lataudu, yleensä koska libGL puuttuu (packages.txt). Osat hinnoitellaan "
                   "DXF:stä ja piirustuksista ilman STEP-mallia.")
    skipped = st.session_state.get(_SKIPPED)
    if skipped:
        more = len(skipped) - _SKIPPED_SHOWN
        st.caption(f"Ohitettiin {len(skipped)} tiedostoa",
                   help="Ei tarvita hinnoitteluun: " + ", ".join(skipped[:_SKIPPED_SHOWN])
                        + (f" ja {more} muuta" if more > 0 else ""))
    render_waiting(None)
    _warn_duplicates(parts)
    labels = {fid: f"#{i + 1} · {dxf.name}" for i, (fid, dxf) in enumerate(parts)}
    _render_files(labels)
    return parts


def _render_files(labels: dict[str, str]) -> None:
    """One folded row under the drop area, a tab each: the supporting files
    and the STEP models. Open by itself when a file is on no part."""
    names = _card_names()
    docs, models = attachments.doc_list_title(names), bom.bom_title()
    tabs = [t for t in (docs and docs[0], models) if t]
    if not tabs:
        return
    n_unlinked = docs[1] if docs else 0
    title = "Tiedostot · " + " · ".join(tabs) + (
        f" · :orange[{n_unlinked} ilman osaa]" if n_unlinked else "")
    with st.expander(title, expanded=bool(n_unlinked)):
        # Keyed by its tabs: a new tab (files dropped) opens the first one again.
        boxes = iter(st.tabs(tabs, default=tabs[0], key="dxf_files_tab:" + "|".join(tabs)))
        if docs:
            with next(boxes):
                attachments.render_doc_list(names, labels)
        if models:
            with next(boxes):
                bom.render_bom({sid: labels[cid] for cid, part in parts().items()
                                for sid in part.members if cid in labels}, resync)


def _waiting(doc_ids=None) -> list[str]:
    """The big STEP files not read yet (of ``doc_ids``, or all)."""
    waiting = {fid for fid, f in files.of_kind("step").items() if f.status == "waiting"}
    return [d for d in (waiting if doc_ids is None else doc_ids) if d in waiting]


def _read_waiting(doc_ids: list[str]) -> None:
    """Read the waiting STEP models: their parts, bends and thickness join
    the cards."""
    for d in doc_ids:
        f = files.get(d)
        bom.add_model(d, f.name, f.load())
        f.status = "read"
    sync()


def render_waiting(doc_ids: list[str] | None) -> None:
    """A large drop's big STEP models are read only when asked, here for all
    of them (``doc_ids`` None) or on the card they belong to."""
    waiting = _waiting(doc_ids)
    if not waiting:
        return
    mb = sum(files.get(d).size_bytes for d in waiting) / (1 << 20)
    if doc_ids is None:
        label = f"Lue isot STEP-mallit ({len(waiting)} kpl, {mb:.0f} Mt)"
        text = (f"{len(waiting)} isoa STEP-mallia odottaa, jotta lataus olisi nopeampi. "
                "Ne voi lukea kortilla yksi kerrallaan tai kaikki kerralla.")
        key = "dxf_read_waiting"
    else:
        label = "Lue 3D-malli: " + ", ".join(files.get(d).name for d in waiting) + f" ({mb:.0f} Mt)"
        text = ("Iso STEP-malli luetaan vasta pyydettäessä (kestää hetken). Siitä tulevat "
                "taivutukset, paksuus ja mitan tarkistus.")
        key = f"dxf_read_waiting_{'_'.join(waiting)}"
    c1, c2 = st.columns([3, 2], vertical_alignment="center")
    c1.caption(text)
    c2.button(label, key=key, on_click=_read_waiting, args=(waiting,))


def _warn_duplicates(parts: list[tuple[str, DxfFile]]) -> None:
    """The warning about files dropped again, for ``_DUPE_WARNING_S`` seconds
    after the drop. It is drawn in a fragment that reruns when the time is up,
    so it goes away on its own, without a click."""
    names, until = st.session_state.get(_DUPES, ([], 0.0))
    left = until - time.time()
    if not names or left <= 0:
        st.session_state.pop(_DUPES, None)
        return
    st.fragment(_duplicate_warning, run_every=left + 0.1)(names, until, parts)


def _duplicate_warning(names: list[str], until: float,
                       parts: list[tuple[str, DxfFile]]) -> None:
    """Name each dropped file that was already a card, and that card's number;
    nothing once the time is up."""
    if time.time() >= until:
        st.session_state.pop(_DUPES, None)
        return
    idx = {dxf.name: i for i, (_, dxf) in enumerate(parts)}
    lines = [f"- **{n}** (osa #{idx[n] + 1})" if n in idx else f"- **{n}**"
             for n in names]
    st.warning(
        ("Tiedosto on jo lisätty, joten sitä ei lisätty uudelleen:"
         if len(names) == 1 else
         "Tiedostot on jo lisätty, joten niitä ei lisätty uudelleen:")
        + "\n\n" + "\n".join(lines)
        + "\n\nJos tiedosto on muuttunut, poista vanha osa ensin ja lataa se sitten uudelleen."
    )



def ingest(uploaded) -> bool:
    """Move newly dropped files into the session, in upload order, and empty
    the drop area (Streamlit can't remove one file from an uploader, only
    start a new one). A file whose name was dropped already is not added
    again; its name goes to ``_DUPES`` for the warning (a drop without one
    clears it). True when something was dropped."""
    known = files.inputs()
    dropped = [up for up in uploaded or [] if up.file_id not in known]
    names = {(f.folder, f.name) for f in known.values()}
    dupes, skipped = [], []
    todo = _unzipped(dropped, skipped)
    bar = st.progress(0.0) if len(todo) > 5 else None
    for n, up in enumerate(todo):
        if bar is not None:
            bar.progress(n / len(todo), text=f"Luetaan tiedostoja {n + 1} / {len(todo)}: "
                                             f"{up.name}")
        folder = getattr(up, "folder", "")
        if not _is_used(up.name):
            skipped.append(up.name)     # e.g. Creo's part.prt.1, part.drw.1
            continue
        if (folder, up.name) in names:
            dupes.append(up.name)
            continue
        data = up.getvalue()
        # A file of a ZIP is read from it again when needed; a file dropped on
        # its own is kept only when it is read later (a PDF or STEP model).
        again = up.getvalue if hasattr(up, "folder") else None
        f = InputFile(up.file_id, up.name, file_kind(up.name), again, folder=folder,
                      size_bytes=len(data))
        if f.kind in ("pdf", "step"):
            attachments.add_doc(f, data)
            if f.kind == "step" and step_available():
                if len(todo) > 5 and len(data) > _BIG_STEP_BYTES:
                    f.status = "waiting"
                else:
                    bom.add_model(up.file_id, up.name, data)
        else:
            add_drawing(f, data)
        names.add((folder, up.name))
    if bar is not None:
        bar.empty()
    if dropped:
        sync()
        _release_memory()
        st.session_state[_INBOX] = st.session_state.get(_INBOX, 0) + 1
        st.session_state[_DUPES] = (dupes, time.time() + _DUPE_WARNING_S)
        st.session_state[_SKIPPED] = skipped
    if len(dropped) > len(dupes) + len(skipped):
        st.session_state.pop(FILL_ASKED, None)  # new empty parts: ask again
    return bool(dropped)


def add_drawing(f: InputFile, data: bytes) -> None:
    """A dropped DXF or DWG: read now by its reader (``core.readers``), the
    cards need its part."""
    files.add(f)
    load, f.load = f.load, (lambda: data)
    files.result(f.id)
    f.load = load
    f.status = "read" if files.result(f.id).shapes else "failed"


def drawings() -> dict[str, DxfFile]:
    """Every dropped DXF/DWG as its reader read it, in drop order."""
    out = {}
    for fid in files.of_kind("dxf", "dwg"):
        res = files.result(fid)
        if res is not None:
            out[fid] = res.record[1] if isinstance(res.record, tuple) else res.record
    return out


def _release_memory() -> None:
    """Give memory freed while reading back to the system (Linux, e.g. a
    hosted app): Python keeps it otherwise, and the host counts it."""
    import ctypes
    import gc

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass    # not glibc (Windows, macOS)


def _is_used(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in _USED


def _unzipped(dropped, skipped: list[str]) -> list:
    """The dropped files with every ZIP opened: its files, folder by folder,
    each carrying its ``folder`` (see ``core.archive``). A ZIP that can't be
    opened goes to ``skipped``."""
    out = []
    for up in dropped:
        if not is_zip(up.name):
            out.append(up)
            continue
        try:
            entries = unpack(up.getvalue(), _is_used, skipped)
        except ArchiveError:
            skipped.append(f"{up.name} (ei voitu avata)")
            continue
        out += [SimpleNamespace(file_id=f"{up.file_id}/{i}", name=e.name, folder=e.folder,
                                getvalue=e.load)
                for i, e in enumerate(entries)]
    return out


# ── Parts: one card per part number ───────────────────────────────────────────

def parts() -> dict[str, Part]:
    """The cards, ``{card id: Part}``."""
    return st.session_state.get(PARTS, {})


def part(cid: str) -> Part | None:
    return parts().get(cid)


def sync() -> list[tuple[str, DxfFile]]:
    """Group the dropped files into cards and choose each card's shape.
    A card's id is its first file's id, so it keeps its values while files
    are added; a card that a drawing joins passes its values on."""
    ss = st.session_state
    dwgs = drawings()
    reports: dict = ss.setdefault(_SRC_REPORTS, {})
    old = parts()

    def shape(sid: str) -> DxfFile:
        return dwgs[sid] if sid in dwgs else bom.flat_file(sid)

    def report(sid: str) -> DxfReport:
        if sid not in reports:
            reports[sid] = shape(sid).part()
        return reports[sid]

    sources = ([Source(fid, d.name, "dxf", list(d.texts), folder=folder_of(fid))
                for fid, d in dwgs.items()]
               + bom.sources(ss.setdefault(_HIDDEN, set()), folder_of))
    linked: dict[str, str] = {}
    groups = group_sources(sources, lambda sid: sid in dwgs and files.result(sid).is_drawing,
                           _pdf_texts(), linked)
    store, new = {}, {}
    for members in groups:
        cid = members[0]
        was = old.get(cid) or next((old[m] for m in members if m in old), None)
        models = [bom.source(m) for m in members if bom.is_source(m)]
        blank = models[0][2].blank_mm if models and models[0][2] is not None else None
        scores = {sid: (score_dxf(report(sid), dwgs[sid].unit_guessed, dwgs[sid].texts, blank)
                        if sid in dwgs else STEP_CHOICE) for sid in members}
        by_user = was.chosen_by_user if was is not None else None
        chosen = choose(scores, by_user) or members[0]
        if cid in old and old[cid].chosen != chosen:
            ss.get(REPORTS, {}).pop(cid, None)
        store[cid] = shape(chosen)
        sources = [PartSource(m, shape(m).name, linked.get(m, "part_number"), scores[m])
                   for m in members]
        if was is None:
            new[cid] = Part(cid, sources, chosen, blank)
        else:   # the same Part goes on: its material, thickness and widget keys
            if any(p is was for p in new.values()):     # a card split in two
                was = copy.deepcopy(was)
            was.id, was.sources, was.chosen, was.blank = cid, sources, chosen, blank
            new[cid] = was
    ss[STORE], ss[PARTS] = store, new
    # A card gone (removed, or joined to another when a drawing came) leaves
    # no Ostetut osat behind.
    bought = ss.get(PART_BOUGHT, {})
    for cid in [c for c in bought if c not in store]:
        del bought[cid]
        ss.pop(f"work_bought_{cid}", None)
    return list(store.items())


def _pdf_texts():
    """The text of the dropped PDFs of a part number, read only when asked
    (a drawing whose title block may name the part it belongs to)."""
    by_key: dict[str, list[str]] = {}
    for d, doc in attachments.docs().items():
        if doc["kind"] == "pdf":
            by_key.setdefault(base_key(doc["name"]), []).append(d)
    return lambda key: "\n".join(attachments.text(d) for d in by_key.get(key, []))


def resync() -> None:
    """After the models changed (K factor, a model removed): unfold and
    choose again."""
    st.session_state.pop(_BEND_LINES, None)
    st.session_state.get(_SRC_REPORTS, {}).clear()
    st.session_state.get(REPORTS, {}).clear()
    sync()


def sync_assemblies() -> None:
    """The assemblies made from the dropped STEP models and assembly drawings."""
    assemblies.sync_from_models()
    links, items = card_docs(), parts()

    def facts(cid: str) -> list:
        """What the readers read from the card's drawings and its PDFs."""
        ids = [m for m in items[cid].members if files.get(m) is not None
               and files.get(m).kind in ("dxf", "dwg")]
        ids += [d for d in links.get(cid, []) if files.get(d) is not None
                and files.get(d).kind == "pdf"]
        return [f for fid in ids for f in files.result(fid).facts]
    assemblies.sync_from_drawings(items, facts)
    # A part's own fasteners stay on its card (its Ostetut osat); only an
    # assembly drawing's notes are its assembly's.
    drawn = assemblies.drawings().values()
    assemblies.sync_drawing_fasteners(
        {cid: card_fastener_lines(cid, items.get(cid), links.get(cid, []))[0] for cid in drawn},
        lambda cid: items[cid].material.value if cid in items else None)


def card_fastener_lines(cid: str, item: Part | None,
                        docs: list[str]) -> tuple[list[tuple[str, int]], str]:
    """The fasteners the card's drawings call out, ``(name, kpl / osa)``, and
    the file they were read from: of its PDFs and files, the one that asks
    for the most (a flat and its drawing repeat the same note)."""
    ids = [*docs, *(item.members if item else [cid])]
    sources = []
    for fid in dict.fromkeys(ids):
        f = files.get(fid)
        if f is None or f.kind not in ("pdf", "dxf", "dwg"):
            continue
        res = files.result(fid)
        sources.append(([(x.value["name"], x.value["kpl"]) for x in res.of("fastener")], f.name))
    best = max(sources, key=lambda s: sum(n for _, n in s[0]), default=([], ""))
    return (best if best[0] else ([], ""))


def source_cards() -> dict[str, str]:
    """Every file's card: ``{source id: card id}``."""
    return {sid: cid for cid, p in parts().items() for sid in p.members}


def folder_of(sid: str) -> str:
    """The ZIP folder a file came in ("" when dropped on its own); a STEP
    model's part is in its file's folder."""
    return files.folder(sid)


def card_docs() -> dict[str, list[str]]:
    """Each card's PDFs (see ``attachments.links``)."""
    card_folders = {cid: {f for f in map(folder_of, p.members) if f}
                    for cid, p in parts().items()}
    return attachments.links(_card_names(), card_folders,
                             {fid: f.folder for fid, f in files.inputs().items() if f.folder})


# A PDF named as a parts list: read for its quantities even when it links to
# no card.
_BOM_NAME = re.compile(r"bom|osaluettelo|kappaleluettelo|parts?[\s_\-]?list|st[üu]ckliste",
                       re.IGNORECASE)


def bom_quantities() -> dict[str, tuple[int, list[str]]]:
    """The quantity each card's BOMs ask for, ``{card id: (kpl, [BOM
    names])}``, from the PDF reader's BOM rows (see
    ``core.linking.bom_quantities``). The PDFs are the ones already read and
    the ones named as a parts list."""
    rows = {f.name: [(x.value["part_key"], x.value["kpl"])
                     for x in files.result(fid).of("bom_qty")]
            for fid, f in files.of_kind("pdf").items()
            if files.is_read(fid) or _BOM_NAME.search(f.name)}
    return _bom_quantities(_card_names(), rows)


def _card_names() -> dict[str, list[str]]:
    """Every card's file names (for linking the PDFs)."""
    return {cid: list(p.names.values()) for cid, p in parts().items()}


_MATERIAL_KINDS = ("material", "thickness")


def material_facts(cid: str, docs: list[str]) -> list:
    """What the card's files say about its material and thickness: its
    drawings' texts, its PDFs (``docs``), its STEP bodies' measured sheet."""
    p = part(cid)
    out = []
    for sid in dict.fromkeys([*(p.members if p else [cid]), *docs]):
        fid, _, body = sid.partition("|")
        f = files.get(fid)
        if f is None or f.kind not in ("dxf", "dwg", "pdf", "step") or (
                f.kind == "step" and not files.is_read(fid)):
            continue
        out += [x for x in files.result(fid).facts
                if x.kind in _MATERIAL_KINDS and (x.body or "") == body]
    return out


def auto_fill_materials(materials: list[str], lookup: dict) -> None:
    """Fill the empty cards' material and thickness from their own files (see
    ``material_agent.auto_fill``)."""
    links = card_docs()
    material_agent.auto_fill(parts(), lambda cid: material_facts(cid, links.get(cid, [])),
                             lambda fid: files.get(fid).name,
                             materials, lambda m: thicknesses_for(lookup, m))


def step_flats(item: Part | None) -> list:
    """The unfolded STEP parts on a card."""
    if item is None:
        return []
    return [src[2] for src in map(bom.source, item.members)
            if src is not None and src[2] is not None and src[2].is_sheet]


def card_bends(report: DxfReport, item: Part | None) -> int:
    """The bends of a part: the STEP model's when the card has one (a flat
    DXF often has no bend lines), else the DXF's bend lines."""
    flats = step_flats(item)
    return len(flats[0].bends) if flats else report.bends


def card_forms(item: Part | None) -> int:
    """The pressed forms (louvres, beads) of the card's STEP model: not
    counted as bends."""
    flats = step_flats(item)
    return flats[0].forms if flats else 0


def evict(fid: str) -> None:
    """A card's "Poista" button: drop the card's files (its PDFs too, unless
    another card has them) and everything kept for it, including its widget
    state."""
    ss = st.session_state
    item = part(fid)
    doc_links = card_docs()
    for sid in (item.members if item else [fid]):
        if files.get(sid) is not None and files.get(sid).kind in ("dxf", "dwg"):
            files.remove(sid)
        ss.get(_SRC_REPORTS, {}).pop(sid, None)
        src = bom.source(sid)
        if src is not None:
            model_id = sid.split("|", 1)[0]
            if len(src[0]["model"].parts) == 1:
                bom.remove_model(model_id)
            else:
                ss.setdefault(_HIDDEN, set()).add(sid)
    others = {d for cid, ids in doc_links.items() if cid != fid for d in ids}
    for d in doc_links.get(fid, []):
        if d not in others:
            attachments.remove_doc(d)
    ss.get(STORE, {}).pop(fid, None)
    ss.get(REPORTS, {}).pop(fid, None)
    for key in (*(material_keys(item) if item else ()), f"dxf_unit_ok_{fid}",
                f"dxf_src_{fid}",
                f"dxf_turn_{fid}"):
        ss.pop(key, None)
    shown = ss.get(AUTO_SHOWN, {})
    for key in [k for k in shown if ids_in([fid]).search(k)]:
        del shown[key]
    sync()


def evict_all() -> None:
    """The "Poista kaikki" button: every card's "Poista" at once."""
    for fid in list(st.session_state.get(STORE, {})):
        evict(fid)
    for doc_id in list(attachments.docs()):
        attachments.remove_doc(doc_id)
    for key in (bom.MODELS, *files.KEYS, PARTS, _HIDDEN, _SRC_REPORTS, FILL_ASKED,
                _DUPES, _SKIPPED,   # these warnings name files that are gone
                _BEND_LINES,
                AUTO_SHOWN, PART_BOUGHT, *material_agent.KEYS, *assemblies.KEYS):
        st.session_state.pop(key, None)


def model_bends(fid: str, report: DxfReport, item: Part | None) -> list | None:
    """The card's STEP bend lines on its DXF (a flat DXF often has no bend
    lines of its own), worked out once per shape."""
    flats = step_flats(item)
    if not flats or report.bends or report.outline is None:
        return None
    cache = st.session_state.setdefault(_BEND_LINES, {})
    key = (fid, item.chosen, id(flats[0]))
    if key not in cache:
        cache[key] = bends_on(flats[0], [report.outline.points]
                              + [h.points for h in report.holes])
    return cache[key]


def material_keys(p: Part) -> tuple[str, str, str]:
    """The material, thickness and disabled-thickness selectbox keys of a card.
    ``Part.version`` is bumped to give them new keys: a fresh selectbox starts
    from the Part's values. (Setting or deleting a widget's value doesn't
    stick — the browser sends its old value back, and Streamlit warns.)"""
    suffix = f"_{p.version}" if p.version else ""
    return (f"dxf_mat_{p.id}{suffix}", f"dxf_th_{p.id}{suffix}",
            f"dxf_th_{p.id}{suffix}_disabled")


def empty_cards() -> list[str]:
    """The cards with no material or thickness yet."""
    return [cid for cid, p in parts().items()
            if not (p.material.value and p.thickness.value)]


def fill_material(material: str | None, thickness: str | None) -> None:
    """Answer the question: give every empty card the material and thickness
    (none for "Ei") and stop asking. The empty cards are found when the button
    is clicked, so a card filled meanwhile keeps its own choice. Their
    selectboxes get new keys (see ``material_keys``), so they start from the
    Part's values."""
    for cid in empty_cards() if material else []:
        apply_material(cid, material, thickness)
    st.session_state[FILL_ASKED] = True


def apply_material(cid: str, material: str, thickness: str | None) -> None:
    """Set one card's material and thickness by hand; its selectboxes get new
    keys so they start from the new choice."""
    p = part(cid)
    if p is None:
        return
    for key in material_keys(p):
        st.session_state.pop(key, None)
    p.material.set(material)
    p.thickness.set(thickness)
    p.version += 1


def card_report(fid: str, dxf: DxfFile) -> DxfReport:
    """The part from the default cut layers, memoised (building it scans every
    point)."""
    cache: dict = st.session_state.setdefault(REPORTS, {})
    if fid not in cache:
        cache[fid] = dxf.part()
    return cache[fid]
