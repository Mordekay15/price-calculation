"""The quote's data model: what is kept from every dropped file and what is
built from it (see "Data model" in the README).

Five layers, data flowing one way:

1. ``InputFile``: every dropped file, and every file inside a ZIP.
2. Readers, one per file type (``core.readers``). Each returns a
   ``ReadResult``: the ``FlatShape``s the file holds and the ``Fact``s it
   states. A DWG is only converted (``DwgConversion``); its DXF goes through
   the DXF reader.
3. ``FlatShape`` and ``Fact``: geometry only, and everything else a file says.
4. ``Part``: one per part number, where all files of a part meet
   (``PartSource``), each value a ``Value`` (auto, where from, override).
   ``Assembly``: one per part number, a tree of parts and subassemblies.
5. Nesting and price, computed from layer 4 and the rates.

Pure: no Streamlit, no I/O beyond the ``load`` callables given in."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from core.geometry import bbox_wh, length, net_area

Point = tuple[float, float]
T = TypeVar("T")


# ── 1 · Files ─────────────────────────────────────────────────────────────────

FILE_KINDS = ("zip", "dxf", "dwg", "step", "pdf")


def file_kind(name: str) -> str | None:
    """The kind of a file by its extension, None for any other file (left
    out unread)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    ext = "step" if ext == "stp" else ext
    return ext if ext in FILE_KINDS else None


@dataclass
class InputFile:
    """One dropped file, or one file found in a ZIP. Its bytes are not
    copied: ``load`` reads them from the upload or the ZIP when asked."""

    id: str
    name: str
    kind: str                                   # see FILE_KINDS
    load: Callable[[], bytes] = field(repr=False, compare=False)
    order: int = 0                              # position among the session's files
    folder: str = ""                            # path inside the ZIP, "" when dropped alone
    size_bytes: int = 0
    status: str = "read"                        # read | failed | waiting


# ── 2 · Readers' own records ──────────────────────────────────────────────────

@dataclass
class DwgConversion:
    """A DWG turned into a DXF, and how. ``dxf`` is None when it failed;
    ``problems`` then say why (in Finnish, for the card)."""

    file_id: str
    converter: str                              # oda | libredwg | none
    dxf: Callable[[], bytes] | None = field(default=None, repr=False, compare=False)
    dxf_kind: str = "full"                      # full | minimal
    problems: list[str] = field(default_factory=list)


# ── 3 · Shapes and facts ──────────────────────────────────────────────────────

@dataclass
class FlatBend:
    """One bend of a flat part. From STEP every field is known; from a DXF
    only that there is one (and its line when drawn)."""

    line: tuple[Point, Point] | None = None
    angle_deg: float | None = None
    inner_radius_mm: float | None = None
    up: bool | None = None


@dataclass
class FlatShape:
    """One flat part from one file, in mm: what the laser would cut.
    Geometry only: no material, no price."""

    origin: str                                 # the reader's source id (file id, or file id|body key)
    outline: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    open_cuts: list[list[Point]] = field(default_factory=list)
    markings: list[list[Point]] = field(default_factory=list)   # drawn, never cut
    bends: list[FlatBend] = field(default_factory=list)
    threads: int = 0
    outside_count: int = 0                      # shapes outside the part on its drawing
    layers_used: list[str] | None = None        # None from STEP
    problems: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return bbox_wh(self.outline) if self.outline else (0.0, 0.0)

    @property
    def net_area_mm2(self) -> float:
        return net_area(self.outline, self.holes) if self.outline else 0.0

    @property
    def cut_length_mm(self) -> float:
        """Outline, every hole and every open cut; a hole that is only a cut
        line (a sliver) is cut once, not round."""
        from core.dxf import is_sliver

        if not self.outline:
            return 0.0
        return (sum(length(c, closed=True) / (2 if is_sliver(c) else 1)
                    for c in (self.outline, *self.holes))
                + sum(length(c) for c in self.open_cuts))

    @property
    def priceable(self) -> bool:
        return bool(self.outline) and not self.problems


FACT_KINDS = ("material", "thickness", "part_number", "revision", "bom_qty",
              "fastener", "called_out", "bought_body")


@dataclass(frozen=True)
class Fact:
    """One thing a file states that is not a shape. ``value`` by kind:

    - material: ``{"family", "grade"}``   - thickness: mm (float)
    - part_number / revision / called_out: str
    - bom_qty: ``{"part_key", "kpl"}``    - fastener / bought_body: ``{"name", "kpl"}``

    ``strength``: ``field`` (a PDF Material field) beats ``text``;
    ``measured`` is a STEP thickness."""

    kind: str
    value: object
    file_id: str
    raw: str = ""
    page: int | None = None
    strength: str = "text"                      # field | text | measured
    body: str | None = None                     # the STEP body a fact is about


@dataclass
class ReadResult:
    """What a reader hands on: the shapes found and the facts stated, plus
    file-level problems. ``record`` is the reader's own (``DwgConversion``,
    ``core.dxf.DxfFile``, ``core.step.StepModel``) for the views that need it."""

    file_id: str
    shapes: list[FlatShape] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    is_drawing: bool = False                    # a drawing sheet, not a flat part
    is_assembly: bool = False
    record: object = field(default=None, repr=False, compare=False)

    def of(self, kind: str) -> list[Fact]:
        return [f for f in self.facts if f.kind == kind]


# ── 4 · Parts and assemblies ──────────────────────────────────────────────────

@dataclass
class Value(Generic[T]):
    """A card value: what the files say (``auto``, with where from), what
    was typed (``user``, wins when set), and why the files disagree."""

    auto: T | None = None
    source: str = ""                            # e.g. "ITM-064555.pdf, s. 1: 6 PCS"
    conflict: str = ""
    user: T | None = None

    @property
    def value(self) -> T | None:
        return self.user if self.user is not None else self.auto


@dataclass
class BoughtPart:
    """One row of Ostetut osat: bought, and put in. ``kpl`` is per owner:
    per part for a part's fasteners, per assembly for an assembly's."""

    name: str
    kpl: Value[int]
    eur: Value[float]
    fit_eur: Value[float]
    source: str = ""                            # "Mistä": the line it was read from, "" by hand

    @property
    def unit_eur(self) -> float:
        """One piece, bought and put in."""
        return float(self.eur.value or 0.0) + float(self.fit_eur.value or 0.0)

    @property
    def total_eur(self) -> float:
        """All of them, per owner."""
        return int(self.kpl.value or 0) * self.unit_eur


@dataclass
class PartSource:
    """One file on a part's card: a DXF, a DWG, a STEP body or a PDF.
    Those with a shape compete to be priced (``score``, ``note``)."""

    source_id: str                              # file id, or STEP file id|body key
    file_id: str
    linked_by: str                              # part_number | text | folder | hand
    shape: FlatShape | None = None
    score: float = 0.0
    note: str = ""
    hidden: bool = False
    name: str = ""                              # as on the card: the file's, or the body's
    valid: bool = True                          # its shape can be priced


@dataclass
class Child:
    """A part or a subassembly in an assembly, × kpl."""

    ref: str                                    # Part id or Assembly part number
    kpl: Value[int]
    is_assembly: bool = False


@dataclass
class Assembly:
    """One per part number, however many files describe it."""

    part_number: str
    name: str = ""
    sources: list[tuple[str, str]] = field(default_factory=list)   # (kind, file id)
    children: list[Child] = field(default_factory=list)
    bought: list[BoughtPart] = field(default_factory=list)
