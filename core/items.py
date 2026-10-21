"""One card per item, pure (no Streamlit): the files dropped for one part
(its flat DXF, a DWG or DXF of the drawing, a STEP model) are grouped into
one item, and the best of them is chosen as the shape to price.

- **Grouping.** Files of one part number go together (``core.linking``:
  revisions and numbered pieces ignored, so ``3aua0000098622_1.dxf``,
  ``_2.dxf`` and ``3AUA0000098622.stp`` are one item). A file named
  otherwise (the drawing ``DRW-023179_A.dwg``) joins the item whose part
  number its texts name (``ITM-072558.A`` in the title block), when they
  name exactly one, or else the one other item in its folder of a dropped
  ZIP.
- **Choosing.** Every candidate shape is scored: a clean flat pattern beats a
  drawing sheet (a frame the size of an A-paper, other views and the title
  block around the part, many texts); a DXF whose size matches the STEP
  model's unfolded blank is confirmed by it. The STEP blank itself is clean
  by construction and is used when no DXF agrees with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.linking import base_key, link_by_text

# Paper sheets (mm, short × long): an outline this size is a drawing frame.
_PAPER = [(210, 297), (297, 420), (420, 594), (594, 841), (841, 1189)]
_PAPER_TOL = 0.03
_SIZE_TOL = 0.03          # a DXF within 3 % of the STEP blank is the same part
_MIN_PART_MM = 3.0        # narrower, among other shapes: a line, not a part
NO_BLANK = "piirustus, ei osan aihiota"


@dataclass
class Source:
    """One candidate file for an item: a DXF/DWG, or a part of a STEP model
    (``keys``: the part numbers it goes by, see ``view.bom``)."""

    id: str
    name: str
    kind: str                          # "dxf" or "step"
    texts: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    folder: str = ""                   # the ZIP folder it came in, "" if none

    def part_keys(self) -> set[str]:
        return self.keys or {base_key(self.name)}


def group_sources(sources: list[Source], is_drawing=lambda source_id: True
                  ) -> list[list[str]]:
    """The items: lists of source ids, each in drop order, the items in the
    order of their first file. A group joins another by its texts only when
    ``is_drawing`` says each of its files is a drawing (not a flat part of
    its own that merely mentions another part number)."""
    groups: dict[str, list[Source]] = {}
    for s in sources:
        keys = s.part_keys()
        key = next((k for k in sorted(keys) if k in groups), None) or sorted(keys)[0]
        groups.setdefault(key, []).append(s)

    # A group named otherwise whose texts name exactly one other group's part
    # number is that part's drawing: it joins it.
    names = {key: key for key in groups}
    for key, members in _drawings(groups, is_drawing):
        texts = "\n".join(t for s in members for t in s.texts)
        others = {k: n for k, n in names.items() if k != key and k in groups}
        found = link_by_text(others, {"x": texts})
        if found:
            target = next(iter(found))
            groups[target].extend(groups.pop(key))

    # A drawing still on its own joins the one other item in its folder (a
    # ZIP of item folders: DRW-023179_A.dwg in the folder of ITM-072558).
    for key, members in _drawings(groups, is_drawing):
        folders = {s.folder for s in members}
        folder = folders.pop() if len(folders) == 1 else ""
        others = [k for k, ms in groups.items()
                  if folder and k != key and any(s.folder == folder for s in ms)]
        if len(others) == 1:
            groups[others[0]].extend(groups.pop(key))

    order = {s.id: i for i, s in enumerate(sources)}
    items = [sorted((s.id for s in members), key=order.get) for members in groups.values()]
    return sorted(items, key=lambda ids: order[ids[0]])


def _drawings(groups: dict[str, list[Source]], is_drawing):
    """The groups still there whose every file is a drawing, ``(key,
    members)``, looked up one by one: a group may join another meanwhile."""
    for key in list(groups):
        members = groups.get(key)
        if members and all(s.kind != "step" and is_drawing(s.id) for s in members):
            yield key, members


@dataclass
class Choice:
    score: float
    note: str                          # why, in Finnish, for the card
    valid: bool = True


def score_dxf(report, unit_guessed: bool, texts: list[str],
              blank_mm: tuple[float, float] | None) -> Choice:
    """How much a DXF/DWG looks like the part's flat pattern (higher is
    better). ``report`` is its ``DxfReport``, ``blank_mm`` the STEP blank's
    size when the item has a model."""
    if report.problems or report.outline is None:
        return Choice(-1e9, "ei luettavaa ääriviivaa", valid=False)
    w, h = sorted((report.outline.width_mm, report.outline.height_mm))
    if w < _MIN_PART_MM and report.dropped_count:
        # A drawing sheet with no flat pattern on it (an assembly drawing):
        # the "part" found is one of its lines.
        return Choice(-1e9, NO_BLANK, valid=False)
    score, notes = 100.0, []
    if _is_paper(w, h):
        score -= 200
        notes.append("piirustuksen kehys")
    outside = report.dropped_count
    if outside:
        score -= 15 * min(outside, 10)
        notes.append(f"{outside} kuviota osan ulkopuolella")
    if texts:
        score -= 2 * min(len(texts), 20)
    if unit_guessed:
        score -= 10
        notes.append("mittayksikkö puuttuu")
    if blank_mm is not None:
        bw, bh = sorted(blank_mm)
        if abs(w - bw) <= _SIZE_TOL * bw and abs(h - bh) <= _SIZE_TOL * bh:
            score += 50
            notes.insert(0, "vastaa STEP-mallin aihiota")
        else:
            score -= 30
            notes.append("eri koko kuin STEP-mallin aihio")
    if not notes:
        notes.append("puhdas aihio")
    return Choice(score, ", ".join(notes))


# A STEP model's unfolded sheet part: clean by construction.
STEP_CHOICE = Choice(90.0, "avattu STEP-mallista")


def choose(scored: dict[str, Choice], override: str | None = None) -> str | None:
    """The source to price: the user's choice when it is still there and
    valid, else the best score (the first dropped on a tie)."""
    if override in scored and scored[override].valid:
        return override
    valid = [(c.score, -i, sid) for i, (sid, c) in enumerate(scored.items()) if c.valid]
    return max(valid)[2] if valid else None


def looks_like_drawing(report) -> bool:
    """A drawing sheet rather than a part: unreadable, a paper-size frame, or
    other views and the title block around the part."""
    if report.problems or report.outline is None:
        return True
    w, h = sorted((report.outline.width_mm, report.outline.height_mm))
    return _is_paper(w, h) or report.dropped_count >= 3


_NUMBER = re.compile(r"(?<![\d.,])\d{2,5}(?:[.,]\d{1,2})?(?![\d])")
_MIN_DIM = 10.0           # smaller sizes match stray numbers too easily


def confirm_unit(size_mm: tuple[float, float], blank_mm: tuple[float, float] | None = None,
                 pdf_texts: list[str] = ()) -> str | None:
    """Why a DXF's guessed unit is right, or None: its size (in mm, by the
    guess) is the STEP model's unfolded blank, or a dimension written on the
    part's PDF drawing. Returns the reason, in Finnish, for the card."""
    w, h = size_mm
    if blank_mm is not None:
        bw, bh = sorted(blank_mm)
        sw, sh = sorted(size_mm)
        if abs(sw - bw) <= _SIZE_TOL * bw and abs(sh - bh) <= _SIZE_TOL * bh:
            return "vastaa STEP-mallin aihiota"
    numbers = set()
    for text in pdf_texts:
        # Rotated text can come out reversed ("(393.3)" as ")3.393(").
        for t in (text, text[::-1]):
            numbers.update(float(n.replace(",", ".")) for n in _NUMBER.findall(t))
    found = [d for d in (w, h) if d >= _MIN_DIM
             and any(abs(d - n) <= max(0.15, 0.001 * d) for n in numbers)]
    if found:
        return "PDF-piirustuksen mitta " + " × ".join(f"{d:g}" for d in found) + " mm"
    return None


def _is_paper(w: float, h: float) -> bool:
    return any(abs(w - pw) <= _PAPER_TOL * pw and abs(h - ph) <= _PAPER_TOL * ph
               for pw, ph in _PAPER)
