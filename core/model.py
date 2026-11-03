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
from typing import TYPE_CHECKING, Generic, TypeVar

from core.geometry import bbox_wh, length, net_area

if TYPE_CHECKING:
    from core.work import WorkInputs

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
    copied: ``load`` reads them from the upload or the ZIP when asked, and is
    None once a file dropped on its own is read (a DXF or DWG is read once;
    only what was read from it is kept)."""

    id: str
    name: str
    kind: str                                   # see FILE_KINDS
    load: Callable[[], bytes] | None = field(repr=False, compare=False)
    order: int = 0                              # position among the session's files
    folder: str = ""                            # path inside the ZIP, "" when dropped alone
    size_bytes: int = 0
    status: str = "read"                        # read | failed | waiting


# ── 2 · Readers' own records ──────────────────────────────────────────────────

@dataclass
class DwgConversion:
    """A DWG turned into a DXF, and how; ``problems`` say why it failed (in
    Finnish, for the card). The DXF itself isn't kept: it is read at once
    (``core.readers.read_dwg_file``)."""

    file_id: str
    converter: str                              # oda | libredwg | none
    dxf_kind: str = "full"                      # full | minimal
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


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
    """One flat part from one file, in mm: what the laser would cut, laid
    along its length. Geometry only: no material, no price. The cards, the
    nesting and the price all read the part from here."""

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
    name: str = ""                              # as on its card: the file's, or the STEP body's
    unit_label: str = "mm"                      # the drawing's unit
    unit_guessed: bool = False                  # the file didn't say its unit
    drawn_blank_mm: float = 0.0                 # a profile's blank as drawn, when unfolded

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

    - material: ``{"family", "grade", "thickness_mm"}`` (thickness None when
      the line gives none)                - thickness: mm (float)
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
    ``core.dxf.DxfFile``, ``(DwgConversion, DxfFile)``, ``(StepModel, {body key:
    FlatPart})``, a PDF's ``[(page, text)]``) for the views that need it."""

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
    """A card value: what the files say (``auto``, with where from, or why
    they disagree), and what was chosen by hand (``user``, wins when set;
    "" is a value cleared by hand)."""

    auto: T | None = None
    source: str = ""                            # e.g. "ITM-064555.pdf, s. 1: S235 t=3"
    conflict: str = ""                          # why the files disagree
    user: T | None = None

    @property
    def value(self) -> T | None:
        return (self.user or None) if self.user is not None else self.auto

    def set(self, value: T | None) -> None:
        """What the card's widget now holds: by hand unless it is the auto."""
        self.user = None if value == self.auto else ("" if value is None else value)

    def fill(self, value: T | None, source: str = "") -> None:
        """What the files say; a hand choice is dropped (the files spoke)."""
        self.auto, self.source, self.conflict, self.user = value, source, "", None


@dataclass(frozen=True)
class Choice:
    """How a part's file scored as the shape to price."""

    score: float
    note: str                                   # why, in Finnish, for the card
    valid: bool = True


@dataclass
class PartSource:
    """One file on a part's card: a DXF/DWG, or a body of a STEP model, how it
    came onto the card, and how it scored as the shape to price."""

    source_id: str                              # file id, or STEP file id|body key
    name: str                                   # as on the card
    linked_by: str                              # part_number | text | folder
    choice: Choice


@dataclass(frozen=True)
class BoughtPart:
    """One row of Ostetut osat: bought, and put in. ``qty`` is per owner: per
    piece for a part's own (its fasteners), per assembly for an assembly's."""

    name: str
    qty: int
    eur: float = 0.0                            # one piece
    fit_eur: float = 0.0                        # putting one piece in (e.g. pressing a PEM nut)
    source: str = ""                            # "Mistä": the file it was read from, "" by hand

    @property
    def unit(self) -> float:
        """One piece, bought and put in."""
        return self.eur + self.fit_eur


@dataclass
class Part:
    """One card: one part number, however many files describe it. Its files
    in drop order, the one priced (``chosen``; ``chosen_by_user`` when picked
    by hand), the STEP blank's size, and its material and thickness."""

    id: str                                     # its first file's id
    sources: list[PartSource]
    chosen: str
    blank: tuple[float, float] | None = None    # mm
    chosen_by_user: str | None = None
    material: Value[str] = field(default_factory=Value)
    thickness: Value[str] = field(default_factory=Value)
    qty: Value[int] = field(default_factory=Value)  # to nest: BOM or batch × kpl, or typed
    work: WorkInputs | None = None              # its Työvaiheet as set on the card
    bought: list[BoughtPart] = field(default_factory=list)   # its own: fasteners, per piece
    bought_lines: list | None = None            # the drawing's lines ``bought`` was read from
    version: int = 0                            # bumped to give its widgets new keys

    @property
    def members(self) -> list[str]:
        return [s.source_id for s in self.sources]

    @property
    def scores(self) -> dict[str, Choice]:
        return {s.source_id: s.choice for s in self.sources}

    @property
    def names(self) -> dict[str, str]:
        return {s.source_id: s.name for s in self.sources}


@dataclass(frozen=True)
class AssemblyWork:
    """Putting one assembly together."""

    fit_min: float = 0.0                        # fitting, minutes per assembly
    weld_m: float = 0.0                         # welding, metres per assembly
    setup_min: float = 0.0                      # minutes per batch
    eur_per_h: float = 0.0                      # fitting and setup
    other_eur: float = 0.0                      # per assembly (packing, painting the whole…)


@dataclass
class Assembly:
    """One per part number, however many files describe it: a STEP model of
    several bodies, an assembly drawing, or parts joined by hand. It goes
    when none of them is left (``described``).

    ``parts`` are its cards × kpl / kokoonpano, a part sitting in one place
    (one in a subassembly is not also in its parent), ``children`` its
    subassemblies × kpl; ``core.assemblies`` prices them."""

    id: str                                     # its part number, or "hand:…"
    name: str
    parts: dict[str, int] = field(default_factory=dict)       # card id → kpl
    bought: list[BoughtPart] = field(default_factory=list)
    work: AssemblyWork = AssemblyWork()
    children: dict[str, int] = field(default_factory=dict)    # assembly id → kpl
    # Where it comes from:
    step_file: str | None = None                # the STEP model's file id
    members: dict[str, int] = field(default_factory=dict)     # source id → kpl: the
    #   model's sheet bodies, or the cards joined by hand
    step_bought: list[str] = field(default_factory=list)      # the model's bodies in bought
    drawing: str | None = None                  # the card of its assembly drawing
    drawn: dict[str, int] = field(default_factory=dict)       # card id → kpl it calls out
    awaited: list[str] = field(default_factory=list)          # called out, not dropped
    fasteners_read: bool = False                # its drawing's notes are in bought
    by_hand: bool = False

    @property
    def described(self) -> bool:
        return bool(self.step_file or self.drawing or self.by_hand)
