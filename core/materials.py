"""Reading materials from drawings and proposing catalogue materials, pure
(no Streamlit).

1. ``extract_hints`` finds material grades and thicknesses in a text: a PDF
   title block (its Material field, ``material_field``, wins) or the DXF's
   notes.
2. ``propose`` turns each part's hints into one proposal: a catalogue
   material from the price lists and one of its thicknesses, the grade
   matched to the catalogue names. Hints that disagree (the PDF says 2.5 mm, the
   STEP model 2 mm) are flagged as a conflict to check.

Nothing is written to the parts here: the page fills in the proposals that
have nothing to check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.pricing import parse_thickness_mm

# ── Hints from text ───────────────────────────────────────────────────────────

# family: the catalogue word(s) it maps to; grade: what tells it apart.
_GRADES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(?:1\.4404|1\.4401|aisi\s*316l?|316l?)\b", re.I), "HST", "316"),
    (re.compile(r"\b(?:1\.4301|1\.4307|aisi\s*304l?|304l?)\b", re.I), "RST", "304"),
    (re.compile(r"\b1\.4016\b", re.I), "RST", "1.4016"),
    (re.compile(r"\b1\.4509\b", re.I), "RST", "1.4509"),
    (re.compile(r"\b(?:haponkest\w*|acid[- ]?proof)\b", re.I), "HST", ""),
    (re.compile(r"\b(?:rst|ruostumato\w*|stainless(?:\s+steel)?|inox)\b", re.I), "RST", ""),
    (re.compile(r"\bhst\b", re.I), "HST", ""),
    (re.compile(r"\b(?:en\s*aw[- ]?)?(5754|5083|6082|6060|1050)\b", re.I), "ALUMIINI", r"\1"),
    (re.compile(r"\b(?:alumiini\w*|alumin(?:i)?um|alu)\b", re.I), "ALUMIINI", ""),
    (re.compile(r"\b(?:kupari|copper|cu-?etp)\b", re.I), "KUPARI", ""),
    (re.compile(r"\b(?:messinki|brass)\b", re.I), "MESSINKI", ""),
    # The grade may run into the standard, "S355MCEN10149-2", when a PDF's
    # text comes out without spaces.
    (re.compile(r"\bs\s?(235|275|355|420|460|500|650|700)\s?(mc|jr|j0|j2|j2\+n|ml|n)?(?!\d)", re.I),
     "S", r"S\1"),
    # Hot-dip galvanised: DX51D+Z275, HDG, kuumasinkitty, KU-SI. The grade is
    # the zinc coating (Z275) when one is given.
    (re.compile(r"\bdx5[1-4]d(?:\s*\+\s*(z\s?\d{2,3}))?", re.I), "SINK", r"\1"),
    (re.compile(r"\b(?:hdg|hot[- ]?dip(?:ped)?(?:\s+galvani[sz]ed)?|galvani[sz]ed|"
                r"kuumasinki\w*|sinkit\w*|sendzimir|ku-?si)\b", re.I), "SINK", ""),
    (re.compile(r"\b(dc0[1-6]|dd1[1-4])\b", re.I), "KY", r"\1"),
]

_THICKNESS = [
    re.compile(r"\b(?:t|s|thk|thickness|paksuus|vahvuus|pl)\s*[=:]?\s*(\d+(?:[.,]\d+)?)\s*(?:mm)?",
               re.I),
    re.compile(r"(\d+(?:[.,]\d+)?)\s*mm\b", re.I),
]


_COATING = re.compile(r"\b(z\d{2,3})\b", re.I)


@dataclass(frozen=True)
class Hint:
    """A material or thickness found in one source. ``family`` is ``S``
    (structural steel), ``KY`` (cold-rolled), ``SINK`` (hot-dip galvanised),
    ``RST``, ``HST``, ``ALUMIINI``,
    ``KUPARI`` or ``MESSINKI``; ``grade`` its grade (``S355``, ``304``…)."""

    source: str                       # e.g. "100325.pdf, s. 1", "Tarjouspyyntö"
    raw: str                          # the text it was read from
    family: str | None = None
    grade: str = ""
    thickness_mm: float | None = None
    field: bool = False               # read from a drawing's Material field


def extract_hints(text: str, source: str) -> list[Hint]:
    """The material hints in ``text``, line by line: one hint per line that
    names a grade or a thickness (a thickness alone is kept too)."""
    hints = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        family, grade = None, ""
        for pattern, fam, grade_tpl in _GRADES:
            m = pattern.search(line)
            if m:
                family = fam
                grade = m.expand(grade_tpl).upper().replace(" ", "") if grade_tpl else ""
                if family == "SINK" and not grade:
                    coat = _COATING.search(line)
                    grade = coat.group(1).upper() if coat else ""
                break
        thickness = _thickness(line)
        # A bare "2 mm" with no material word is too common (dimensions):
        # keep a thickness only with a grade or a thickness keyword.
        if thickness is not None and family is None and not _THICKNESS[0].search(line):
            thickness = None
        if family or thickness is not None:
            hints.append(Hint(source, line[:120], family, grade, thickness))
    return hints


def _thickness(line: str) -> float | None:
    """The first sheet-thickness-sized value after a keyword (or before
    "mm"): ``S355MC PL3x520x400`` is 3, not the 355 of "S355"."""
    for pattern in _THICKNESS:
        for m in pattern.finditer(line):
            value = float(m.group(1).replace(",", "."))
            if 0.3 <= value <= 60:
                return value
    return None


# The title block's material field: "Material:", "Materiaali", "Werkstoff".
_MATERIAL_LABEL = re.compile(r"^(?:material|materiaali|materiaal|werkstoff|matl|mat)\.?(?::(.*))?$",
                             re.I)


def material_field(words: list[dict]) -> str:
    """The text in a drawing's Material field, "" when there is none.
    ``words`` are pdfplumber words (``text``, ``x0``, ``x1``, ``top``,
    ``bottom``). The value is right of the label on its row (or run into it,
    ``Material:HDG``), or else on the first row under it, up to the next
    label (a word ending in ":") of the label's row: in ``Material:
    Dimensions:`` over ``S355MC PL3x520x400`` the material is S355MC."""
    for label in words:
        m = _MATERIAL_LABEL.match(label["text"])
        if not m:
            continue
        glued = m.group(1) or ""          # "Material:HDG", the value run into the label
        height = max(label["bottom"] - label["top"], 4.0)

        def same_row(w, ref=label):
            return abs(w["top"] - ref["top"]) <= height * 0.6

        ends = [w["x0"] for w in words if w is not label and same_row(w)
                and w["x0"] > label["x1"] and w["text"].endswith(":")]
        end = min(ends, default=float("inf"))
        right = [w for w in words if w is not label and same_row(w)
                 and label["x1"] <= w["x0"] < end]
        if right or glued:
            return " ".join([glued, *(w["text"] for w in sorted(right, key=lambda w: w["x0"]))]
                            ).strip()
        below = [w for w in words if label["bottom"] < w["top"] <= label["bottom"] + 4 * height
                 and label["x0"] - 3 <= w["x0"] < end]
        if below:
            first = min(below, key=lambda w: w["top"])
            row = [w for w in below if same_row(w, first)]
            return " ".join(w["text"] for w in sorted(row, key=lambda w: w["x0"]))
    return ""


def pdf_material_fields(data: bytes, max_pages: int = 3) -> list[tuple[int, str]]:
    """``[(page number, Material field text)]`` of a PDF's first pages that
    have one (see ``material_field``)."""
    import io

    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            found = [(i + 1, material_field(page.extract_words()))
                     for i, page in enumerate(pdf.pages[:max_pages])]
    except Exception:  # noqa: BLE001 - any unreadable PDF just gives nothing
        return []
    return [(page, text) for page, text in found if text]


def pdf_text(data: bytes, max_pages: int = 3) -> list[tuple[int, str]]:
    """``[(page number, text)]`` of a PDF's first pages; [] when unreadable."""
    import io

    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [(i + 1, page.extract_text() or "") for i, page in
                    enumerate(pdf.pages[:max_pages])]
    except Exception:  # noqa: BLE001 - any unreadable PDF just gives no text
        return []


# ── Proposals ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Proposal:
    """What to set on one part and why."""

    part_id: str
    part: str
    material: str | None
    thickness: str | None
    evidence: tuple[Hint, ...] = ()
    conflict: str = ""                # why the sources disagree, "" when they don't


def match_material(hint: Hint, materials: list[str]) -> list[str]:
    """Catalogue materials that fit a hint, best first."""
    if hint.family is None:
        return []
    names = {m: m.upper() for m in materials}

    def has(word: str, name: str) -> bool:
        return re.search(rf"(?<![A-Z0-9]){re.escape(word.upper())}", name) is not None

    if hint.family == "S":
        grade = hint.grade[:4]                    # S355 of S355MC
        hits = [m for m, n in names.items() if has(grade, n)]
        # Prefer the exact grade with its suffix, then the shortest name.
        return sorted(hits, key=lambda m: (hint.grade not in names[m], len(m)))
    if hint.family == "SINK":
        hits = [m for m, n in names.items()
                if "SINK" in n or "KU-SI" in n or re.search(r"DX5[1-4]D", n)]
        hits = [m for m in hits if "SÄHKÖ" not in names[m] and " ZE" not in names[m]] or hits
        if hint.grade:
            exact = [m for m in hits if hint.grade in names[m].replace(" ", "")]
            hits = exact or hits
        return sorted(hits, key=len)
    if hint.family == "KY":
        return sorted([m for m, n in names.items() if has(hint.grade, n)], key=len)
    if hint.family in ("RST", "HST"):
        hits = [m for m, n in names.items() if has(hint.family, n)]
        if not hits and hint.family == "HST":
            hits = [m for m, n in names.items() if has("RST", n)]
        if hint.grade.startswith("1.4"):
            exact = [m for m in hits if hint.grade in m]
            hits = exact or hits
        # Plain 2B finish first for the usual grades.
        return sorted(hits, key=lambda m: ("2B" not in names[m], len(m)))
    if hint.family == "ALUMIINI":
        hits = [m for m, n in names.items() if "ALUMIINI" in n or has("ALU", n)]
        if hint.grade:
            exact = [m for m in hits if hint.grade in m]
            hits = exact or hits
        return sorted(hits, key=len)
    return sorted([m for m, n in names.items() if hint.family in n], key=len)


def match_thickness(thickness_mm: float | None, available: list[str]) -> str | None:
    """The available thickness label equal to ``thickness_mm`` (± 0.05 mm)."""
    if thickness_mm is None:
        return None
    return next((label for label in available
                 if (mm := parse_thickness_mm(label)) is not None
                 and abs(mm - thickness_mm) <= 0.05), None)


def propose(parts: dict[str, tuple[str, list[Hint]]], materials: list[str],
            thicknesses_for) -> list[Proposal]:
    """One proposal per part. ``parts`` maps a part id to ``(name, hints from
    its own files)``; ``thicknesses_for(material)`` lists a catalogue
    material's thicknesses."""
    return [_propose_one(pid, name, hints, materials, thicknesses_for)
            for pid, (name, hints) in parts.items()]


def _propose_one(pid, name, hints, materials, thicknesses_for) -> Proposal:
    # A drawing's Material field names the part's own material: other lines
    # (a screw's "A2", a bracket's "S235" in a note) don't count then. The
    # hint with a grade first: "DX51D+Z275" says more than "HDG STEEL".
    named = [h for h in hints if h.family]
    named = [h for h in named if h.field] or named
    with_family = sorted(named, key=lambda h: not h.grade)
    with_thickness = [h for h in hints if h.thickness_mm is not None]
    conflicts = []
    families = {(h.family, h.grade) for h in with_family}
    grades = {g for _, g in families if g}
    if len({f for f, _ in families}) > 1 or len(grades) > 1:
        conflicts.append("materiaali: " + " / ".join(
            sorted({f"{h.grade or h.family} ({h.source})" for h in with_family})))
    thick_values = {round(h.thickness_mm, 2) for h in with_thickness}
    if len(thick_values) > 1:
        conflicts.append("paksuus: " + " / ".join(
            sorted({f"{h.thickness_mm:g} mm ({h.source})" for h in with_thickness})))

    material = None
    if with_family:
        candidates = match_material(with_family[0], materials)
        material = candidates[0] if candidates else None
    thickness = None
    if material and with_thickness:
        thickness = match_thickness(with_thickness[0].thickness_mm, thicknesses_for(material))
        if thickness is None:
            conflicts.append(f"paksuutta {with_thickness[0].thickness_mm:g} mm ei ole "
                             f"hinnastossa materiaalille {material}")
    evidence = tuple(with_family[:2] + [h for h in with_thickness[:2] if h not in with_family])
    return Proposal(pid, name, material, thickness, evidence, "; ".join(conflicts))
