"""Materials on the DXF tab: each card's material and thickness come from
what the readers read from its own files (``core.model.Fact``: the DXF and
DWG texts, the linked PDFs' Material field and text, the STEP model's
measured sheet) and are filled in when they agree (see ``core.materials``).
What was filled in, from where, and why the files disagree is kept on the
card's ``Part`` (``Value.auto``, ``.source``, ``.conflict``)."""

from __future__ import annotations

from core.materials import Hint, propose
from core.model import Fact, Part
from view.ui import st

_SEARCHED = "dxf_material_searched"   # {card id: the facts it was last searched with}
KEYS = (_SEARCHED,)


def hints(facts: list[Fact], name_of) -> list[Hint]:
    """The facts about material and thickness, as ``core.materials`` reads
    them; ``name_of(file id)`` names where each came from."""
    out = []
    for f in facts:
        if f.strength == "measured":
            out.append(Hint("STEP-malli", f"levyn paksuus {f.value:g} mm", thickness_mm=f.value))
            continue
        where = name_of(f.file_id) + (" (DXF)" if f.page is None else f", s. {f.page}")
        where += ", Material-kenttä" if f.strength == "field" else ""
        field = f.strength == "field"
        if f.kind == "material":
            out.append(Hint(where, f.raw, f.value["family"], f.value["grade"],
                            f.value["thickness_mm"], field))
        elif f.kind == "thickness":
            out.append(Hint(where, f.raw, thickness_mm=f.value, field=field))
    return out


def _signature(facts: list[Fact]) -> tuple:
    return tuple((f.kind, f.file_id, f.raw, f.page, f.strength, f.body) for f in facts)


def auto_fill(cards: dict[str, Part], facts_of, name_of, materials: list[str],
              thicknesses_for) -> None:
    """Give the cards that have no material yet the one their own files name,
    without asking: a proposal with nothing to check is filled in
    (``Value.fill``, with where it came from); when the files disagree the
    card is left empty and says why (``Value.conflict``). A card is searched
    again only when its facts change (so a material cleared by hand stays
    clear). ``facts_of(card id)`` are the material and thickness facts of
    its files."""
    done = st.session_state.setdefault(_SEARCHED, {})
    todo = {}
    for cid, part in cards.items():
        facts = facts_of(cid)
        sig = _signature(facts)
        if not part.material.value and done.get(cid) != sig:
            done[cid] = sig
            todo[cid] = (part.names[part.chosen], hints(facts, name_of))
    for p in propose(todo, materials, thicknesses_for):
        part = cards[p.part_id]
        if p.material and not p.conflict:
            source = "; ".join(f"{h.source}: {h.raw}" for h in p.evidence)
            part.material.fill(p.material, source)
            part.thickness.fill(p.thickness, source)
            part.version += 1           # its selectboxes start from the new values
        else:
            part.material.conflict = p.conflict or ""
