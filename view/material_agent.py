"""Materials on the DXF tab: each card's material and thickness are read
from its own files (DXF texts, the linked PDFs' Material field and text, the
STEP model's sheet) and filled in when they agree (see ``core.materials``)."""

from __future__ import annotations

from dataclasses import replace

import streamlit as st

from core.materials import Hint, Proposal, extract_hints, load_aliases, propose
from view import attachments

_AUTO = "dxf_material_auto"          # {card id: what it was filled from (see auto_fill)}
_AUTO_NOTE = "dxf_material_auto_note"  # {card id: (material, thickness, sources)}
_CHECK = "dxf_material_check"        # {card id: why its files disagree}
AUTO_KEYS = (_AUTO, _AUTO_NOTE, _CHECK)


def auto_fill(parts: list[tuple[str, object]], doc_links: dict[str, list[str]],
              docs: dict[str, dict], materials: list[str], thicknesses_for, apply,
              filled) -> None:
    """Give the cards that have no material yet the one their own files name,
    without asking: a proposal with nothing to check is applied; when the
    files disagree the card is left empty and says why (``check_note``). A
    card is searched again only when its files change (so a material the
    user clears stays clear).
    ``filled(card id)`` says whether a card already has a material."""
    done = st.session_state.setdefault(_AUTO, {})
    todo = []
    for fid, dxf in parts:
        sig = (tuple(dxf.texts), tuple(doc_links.get(fid, [])),
               getattr(dxf, "step_thickness", None))
        if not filled(fid) and done.get(fid) != sig:
            done[fid] = sig
            todo.append((fid, dxf))
    if not todo:
        return
    notes = st.session_state.setdefault(_AUTO_NOTE, {})
    checks = st.session_state.setdefault(_CHECK, {})
    for p in _search(todo, doc_links, docs, materials, thicknesses_for):
        checks.pop(p.part_id, None)
        if p.material and not p.conflict:
            apply(p.part_id, p.material, p.thickness)
            notes[p.part_id] = (p.material, p.thickness,
                                "; ".join(f"{h.source}: {h.raw}" for h in p.evidence))
        elif p.conflict:
            checks[p.part_id] = p.conflict


def auto_note(fid: str, material: str | None, thickness: str | None) -> str | None:
    """The card's line saying where its material came from, while it is
    still the one filled in automatically."""
    note = st.session_state.get(_AUTO_NOTE, {}).get(fid)
    if not note or (note[0], note[1]) != (material, thickness):
        return None
    return note[2]


def check_note(fid: str) -> str | None:
    """Why the card's material wasn't filled in: its files disagree."""
    return st.session_state.get(_CHECK, {}).get(fid)


def _search(parts, doc_links, docs, materials, thicknesses_for) -> list[Proposal]:
    by_part = {}
    for fid, dxf in parts:
        hints = extract_hints("\n".join(dxf.texts), f"{dxf.name} (DXF)")
        step_t = getattr(dxf, "step_thickness", None)
        if step_t:
            hints.append(Hint("STEP-malli", f"levyn paksuus {step_t:g} mm",
                              thickness_mm=round(step_t, 2)))
        for d in doc_links.get(fid, []):
            doc = docs.get(d)
            if doc is None or doc["kind"] != "pdf":
                continue
            for page, text in attachments.pages(d, doc):
                hints += extract_hints(text, f"{doc['name']}, s. {page}")
            for page, text in attachments.material_fields(d, doc):
                hints += [replace(h, field=True) for h in
                          extract_hints(text, f"{doc['name']}, s. {page}, Material-kenttä")]
        by_part[fid] = (dxf.name, hints)
    return propose(by_part, [], materials, thicknesses_for, load_aliases())
