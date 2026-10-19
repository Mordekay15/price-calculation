"""Reading materials from drawings and proposing catalogue materials, pure
(no Streamlit).

1. ``extract_hints`` finds material grades and thicknesses in a text: a PDF
   title block, the DXF's notes or the customer's RFQ email
   ("DXFt tulee s355 2mm").
2. ``propose`` turns each part's hints into one proposal: a catalogue
   material from the price lists and one of its thicknesses. The shop's alias
   table (raw text → choice made before) wins; otherwise the grade is matched
   to the catalogue names. Hints that disagree (the PDF says 2.5 mm, the RFQ
   2 mm) are flagged as a conflict to check.
3. ``ask_claude`` (optional) proposes for the parts the rules left open, when
   the ``anthropic`` package and an API key are available.

Nothing is written to the parts here: the page applies the proposals the
user ticks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALIAS_FILE = ROOT / "material_aliases.json"

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
    (re.compile(r"\bs\s?(235|275|355|420|460|500|650|700)\s?(mc|jr|j0|j2|j2\+n|ml|n)?\b", re.I),
     "S", r"S\1"),
    (re.compile(r"\b(dc0[1-6]|dx5[1-4]d|dd1[1-4])\b", re.I), "KY", r"\1"),
]

_THICKNESS = [
    re.compile(r"\b(?:t|s|thk|thickness|paksuus|vahvuus|pl)\s*[=:]?\s*(\d+(?:[.,]\d+)?)\s*(?:mm)?",
               re.I),
    re.compile(r"(\d+(?:[.,]\d+)?)\s*mm\b", re.I),
]


@dataclass(frozen=True)
class Hint:
    """A material or thickness found in one source. ``family`` is ``S``
    (structural steel), ``KY`` (cold-rolled), ``RST``, ``HST``, ``ALUMIINI``,
    ``KUPARI`` or ``MESSINKI``; ``grade`` its grade (``S355``, ``304``…)."""

    source: str                       # e.g. "100325.pdf, s. 1", "Tarjouspyyntö"
    raw: str                          # the text it was read from
    family: str | None = None
    grade: str = ""
    thickness_mm: float | None = None


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
                grade = m.expand(grade_tpl).upper() if grade_tpl else ""
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
    for pattern in _THICKNESS:
        m = pattern.search(line)
        if m:
            value = float(m.group(1).replace(",", "."))
            if 0.3 <= value <= 60:
                return value
    return None


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

HIGH, MEDIUM, LOW = "korkea", "keskitaso", "matala"


@dataclass(frozen=True)
class Proposal:
    """What to set on one part, why, and how sure it is."""

    part_id: str
    part: str
    material: str | None
    thickness: str | None
    confidence: str = LOW
    evidence: tuple[Hint, ...] = ()
    conflict: str = ""                # why the sources disagree, "" when they don't
    via: str = "säännöt"              # "alias", "säännöt", "tekoäly"
    raw: str = ""                     # the text the alias table keys on
    note: str = ""                    # the AI's reason, when it chose


def normalize(raw: str) -> str:
    """The alias table's key for a raw text."""
    return re.sub(r"\s+", " ", raw.strip().lower())


def load_aliases(path: Path = ALIAS_FILE) -> dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_aliases(new: dict[str, dict], path: Path = ALIAS_FILE) -> None:
    """Add ``{raw: {"material", "thickness"}}`` to the alias table."""
    table = load_aliases(path)
    table.update({normalize(k): v for k, v in new.items()})
    path.write_text(json.dumps(table, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8")


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
    for label in available:
        try:
            if abs(float(label.replace(",", ".")) - thickness_mm) <= 0.05:
                return label
        except ValueError:
            continue
    return None


def propose(parts: dict[str, tuple[str, list[Hint]]], note_hints: list[Hint],
            materials: list[str], thicknesses_for, aliases: dict[str, dict] | None = None,
            ) -> list[Proposal]:
    """One proposal per part. ``parts`` maps a part id to ``(name, hints from
    its own files)``; ``note_hints`` come from the RFQ and apply to parts whose
    own files say nothing about a material or a thickness.
    ``thicknesses_for(material)`` lists a catalogue material's thicknesses."""
    aliases = aliases or {}
    out = []
    for pid, (name, own) in parts.items():
        hints = list(own)
        if not any(h.family for h in hints):
            hints += [h for h in note_hints if h.family]
        if not any(h.thickness_mm is not None for h in hints):
            hints += [h for h in note_hints if h.thickness_mm is not None and h not in hints]
        out.append(_propose_one(pid, name, hints, materials, thicknesses_for, aliases))
    return out


def _propose_one(pid, name, hints, materials, thicknesses_for, aliases) -> Proposal:
    for h in hints:
        alias = aliases.get(normalize(h.raw))
        if alias and alias.get("material") in materials:
            return Proposal(pid, name, alias["material"], alias.get("thickness"), HIGH,
                            (h,), via="alias", raw=h.raw)

    with_family = [h for h in hints if h.family]
    with_thickness = [h for h in hints if h.thickness_mm is not None]
    conflicts = []
    families = {(h.family, h.grade) for h in with_family}
    if len({f for f, _ in families}) > 1:
        conflicts.append("materiaali: " + " / ".join(
            sorted({f"{h.grade or h.family} ({h.source})" for h in with_family})))
    thick_values = {round(h.thickness_mm, 2) for h in with_thickness}
    if len(thick_values) > 1:
        conflicts.append("paksuus: " + " / ".join(
            sorted({f"{h.thickness_mm:g} mm ({h.source})" for h in with_thickness})))

    material = None
    candidates: list[str] = []
    if with_family:
        candidates = match_material(with_family[0], materials)
        material = candidates[0] if candidates else None
    thickness = None
    if material and with_thickness:
        thickness = match_thickness(with_thickness[0].thickness_mm, thicknesses_for(material))
        if thickness is None:
            conflicts.append(f"paksuutta {with_thickness[0].thickness_mm:g} mm ei ole "
                             f"hinnastossa materiaalille {material}")

    if material and thickness and not conflicts:
        confidence = HIGH if len(candidates) == 1 else MEDIUM
    elif material:
        confidence = MEDIUM if thickness else LOW
    else:
        confidence = LOW
    evidence = tuple(with_family[:2] + [h for h in with_thickness[:2] if h not in with_family])
    raw = with_family[0].raw if with_family else (with_thickness[0].raw if with_thickness else "")
    return Proposal(pid, name, material, thickness, confidence, evidence,
                    "; ".join(conflicts), raw=raw)


# ── Optional: ask Claude ──────────────────────────────────────────────────────

MODEL = "claude-opus-5-5"


def claude_available() -> bool:
    """True when the ``anthropic`` package is installed. Credentials come
    from the environment (``ANTHROPIC_API_KEY`` or a saved login)."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def ask_claude(proposals: list[Proposal], note: str, materials: list[str],
               thicknesses_for) -> list[Proposal]:
    """Ask Claude for the parts the rules left without a material or a
    thickness; the others come back as they were. Errors are raised to the
    caller (the page shows them)."""
    open_ = [p for p in proposals if p.material is None or p.thickness is None]
    if not open_:
        return proposals

    import anthropic
    from pydantic import BaseModel

    class Choice(BaseModel):
        part_id: str
        material: str | None
        thickness: str | None
        confidence: str
        reason: str

    class Answer(BaseModel):
        choices: list[Choice]

    catalogue = {m: thicknesses_for(m) for m in materials}
    parts = [{"part_id": p.part_id, "part": p.part,
              "texts": [f"{h.source}: {h.raw}" for h in p.evidence] or ["(ei tekstiä)"]}
             for p in open_]
    prompt = (
        "You map sheet-metal parts to a shop's material catalogue for a quote.\n"
        "For each part, pick one catalogue material (exact name from the catalogue) and "
        "one of its thicknesses (exact label), from the texts found in the part's "
        "drawings and the customer's request below. Use null when the texts don't tell. "
        "confidence is one of: korkea, keskitaso, matala. reason: one short sentence in "
        "Finnish naming the text you used.\n\n"
        f"Customer's request:\n{note or '(none)'}\n\n"
        f"Parts:\n{json.dumps(parts, ensure_ascii=False)}\n\n"
        f"Catalogue (material: thicknesses):\n{json.dumps(catalogue, ensure_ascii=False)}"
    )
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_format=Answer,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return proposals
    by_id = {c.part_id: c for c in response.parsed_output.choices}
    out = []
    for p in proposals:
        c = by_id.get(p.part_id)
        if p in open_ and c is not None and c.material in catalogue:
            thickness = c.thickness if c.thickness in catalogue[c.material] else None
            confidence = c.confidence if c.confidence in (HIGH, MEDIUM, LOW) else LOW
            p = replace(p, material=c.material, thickness=thickness, confidence=confidence,
                        note=c.reason, via="tekoäly")
        out.append(p)
    return out


@dataclass
class PartTexts:
    """The texts of one part's files, by source label."""

    texts: dict[str, str] = field(default_factory=dict)

    def hints(self) -> list[Hint]:
        return [h for source, text in self.texts.items() for h in extract_hints(text, source)]
