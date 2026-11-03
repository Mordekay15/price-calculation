"""One reader per file type (see ``core.model``). Each takes a file's bytes
and returns a ``ReadResult``: the ``FlatShape``s it holds and the ``Fact``s it
states. A reader does no merging and no pricing, so each can be tested with
one file alone.

- DXF  → ``read_dxf_file``: analyses the sheet and returns the part.
- DWG  → ``read_dwg_file``: converts (``DwgConversion``), then the DXF reader.
- STEP → ``read_step_file``: one FlatShape per sheet body, straight from the
  unfolded body (no DXF round trip), plus the BOM and the bought bodies.
- PDF  → ``read_pdf_file``: facts only."""

from __future__ import annotations

import re

from core import dwg
from core.dxf import DxfFile, DxfReport, fastener_lines, read_dxf
from core.linking import _QTY, base_key, callouts
from core.materials import extract_hints
from core.model import DwgConversion, Fact, FlatBend, FlatShape, ReadResult

# A part number in a BOM line: a word with five digits in a row, its
# revision (.A, -revB) taken off by ``base_key``.
_NUMBER = re.compile(r"(?<![\w.-])(?=[\w.-]*\d{5})[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*")
_ASSEMBLY_TITLE = re.compile(r"\b(assembly|assy|kokoonpano|zsb|zusammenbau)\b", re.IGNORECASE)


# ── Facts shared by the DXF and PDF readers ───────────────────────────────────

def _material_facts(text: str, file_id: str, page: int | None = None,
                    strength: str = "text") -> list[Fact]:
    out = []
    for h in extract_hints(text, file_id):
        if h.family:
            out.append(Fact("material", {"family": h.family, "grade": h.grade}, file_id,
                            h.raw, page, strength))
        if h.thickness_mm:
            out.append(Fact("thickness", float(h.thickness_mm), file_id, h.raw, page, strength))
    return out


def _callout_facts(text: str, own: str, file_id: str, page: int | None = None) -> list[Fact]:
    return [Fact("called_out", key, file_id, key, page)
            for key in callouts(text) if key != own]


# ── DXF and DWG ───────────────────────────────────────────────────────────────

def shape_from_report(report: DxfReport, origin: str,
                      layers: list[str] | None = None) -> FlatShape:
    """The part the DXF reader built, as a FlatShape. Its point lists are the
    report's own, not copies (the report is kept too, see ``ReadResult``)."""
    if report.outline is None:
        return FlatShape(origin, [], problems=list(report.problems) or ["ei ääriviivaa"])
    return FlatShape(
        origin=origin,
        outline=report.outline.points,
        holes=[h.points for h in report.holes],
        open_cuts=report.open_lines,
        markings=report.reference_lines,
        bends=[FlatBend() for _ in range(report.bends)],
        threads=report.threads,
        outside_count=report.dropped_count,
        layers_used=layers,
        problems=list(report.problems),
    )


def _from_dxf_file(f: DxfFile, file_id: str, name: str, record=None) -> ReadResult:
    from core.items import looks_like_drawing

    report = f.part()
    layers = f.suggested_layers() if f.pieces else None
    f.forget_lines()                    # the part is all that's used after (memory)
    text = "\n".join(f.texts)
    own = base_key(name)
    facts = [Fact("part_number", own, file_id, name)]
    facts += _material_facts(text, file_id)
    facts += [Fact("fastener", {"name": n, "kpl": k}, file_id, f"{n} X{k}KPL")
              for n, k in fastener_lines(f.texts)]
    facts += _callout_facts(text, own, file_id)
    result = ReadResult(file_id, shapes=[shape_from_report(report, file_id, layers)],
                        facts=facts, problems=list(f.problems),
                        is_drawing=looks_like_drawing(report),
                        record=f if record is None else record)
    result.is_assembly = result.is_drawing and len({f.value for f in result.of("called_out")}) >= 2
    return result


def read_dxf_file(data: bytes, file_id: str, name: str) -> ReadResult:
    """A DXF: its part as a FlatShape, and the facts in its texts."""
    return _from_dxf_file(read_dxf(data, name), file_id, name)


def read_dwg_file(data: bytes, file_id: str, name: str) -> ReadResult:
    """A DWG: converted (``DwgConversion``), then read exactly like a DXF.
    LibreDWG's minimal DXF is used when its full one can't be read but the
    minimal one can; the drawing's unit comes from the full one."""
    conv = DwgConversion(file_id, converter=(dwg.converter() or "none").lower())
    try:
        full = dwg.dwg_to_dxf(data)
    except dwg.ConversionError as exc:
        conv.problems = [f"{exc} {dwg.SAVE_AS_DXF}"]
        return ReadResult(file_id, problems=list(conv.problems),
                          record=(conv, DxfFile(name=name, problems=list(conv.problems))))
    f = read_dxf(full, name)
    if f.problems and not f.pieces and conv.converter == "libredwg":
        try:
            minimal = read_dxf(dwg._with_units(dwg.dwg_to_dxf(data, minimal=True), full), name)
        except dwg.ConversionError:
            minimal = None
        if minimal is not None and minimal.pieces:
            f, conv.dxf_kind = minimal, "minimal"
    if f.problems and not f.pieces:
        f.problems = [f"{p} {dwg.SAVE_AS_DXF}" for p in f.problems]
    return _from_dxf_file(f, file_id, name, record=(conv, f))


# ── STEP ──────────────────────────────────────────────────────────────────────

def shape_from_flat(flat, origin: str) -> FlatShape:
    """A STEP body's unfolded flat as a FlatShape: bends with their angle,
    radius and direction, nothing guessed back from a drawing."""
    bends = [FlatBend(tuple(map(tuple, b.line)), b.angle_deg, b.inner_radius_mm, b.up)
             for b in flat.bends]
    return FlatShape(origin, list(flat.outline), holes=[list(h) for h in flat.holes],
                     markings=[list(b.line) for b in bends], bends=bends)


def dxf_file_of(shape: FlatShape, name: str, thickness_mm: float | None = None) -> DxfFile:
    """A STEP body's FlatShape as the cards' ``DxfFile``, built straight from
    it: no DXF written and read back. Its part (``DxfFile.part``) is the
    shape itself, checked and laid along its length by the DXF reader's own
    steps; its lines stay on CUT / BEND_UP / BEND_DOWN, and its note gives the
    thickness to the material panel."""
    from core.dxf import Contour, _lay_along_length, _part_problems, _Piece
    from core.geometry import area
    from core.step import BEND_DOWN, BEND_UP

    bends = [(list(b.line), BEND_UP if b.up else BEND_DOWN)
             for b in shape.bends if b.line is not None]
    note = (f"{name.rsplit('.', 1)[0]} | t = {thickness_mm:g} mm | "
            f"{len(shape.bends)} taivutusta (STEP)" if thickness_mm else "")
    f = DxfFile(name, unit_label="mm", texts=[note] if note else [],
                pieces=[_Piece(list(c), True, "CUT", True) for c in (shape.outline, *shape.holes)]
                + [_Piece(line, False, layer, True) for line, layer in bends])
    report = DxfReport(name=name, unit_label="mm", problems=list(shape.problems),
                       bends=len(shape.bends), threads=shape.threads,
                       reference_lines=[line for line, _ in bends])
    if shape.outline:
        report.outline = Contour(list(shape.outline), True, abs(area(shape.outline)))
        report.holes = [Contour(list(h), True, abs(area(h)), 1) for h in shape.holes]
        report.problems += _part_problems(report)
        _lay_along_length(report)
    f._default = report
    return f


def read_step_file(data: bytes, file_id: str, name: str,
                   k_factor: float | None = None) -> ReadResult:
    """A STEP model: a FlatShape per sheet body, its thickness (measured),
    its quantities, and the bodies that aren't sheet (bought parts). Its
    ``record`` is the model and each body's unfolding, ``(StepModel, {body key:
    FlatPart})``, for the BOM view and the 3D preview."""
    from core import step

    model = step.read_step(data, name)
    k = step.DEFAULT_K_FACTOR if k_factor is None else k_factor
    flats = {p.key: step.unfold(p.shape, k) for p in model.parts}
    result = ReadResult(file_id, problems=list(model.problems), record=(model, flats))
    for part in model.parts:
        sid = f"{file_id}|{part.key}"
        flat = flats[part.key]
        key = base_key(part.name)
        if flat is not None and flat.is_sheet and flat.outline:
            result.shapes.append(shape_from_flat(flat, sid))
            result.facts.append(Fact("part_number", key, file_id, part.name, body=part.key))
            if flat.thickness_mm:
                result.facts.append(Fact("thickness", round(flat.thickness_mm, 2), file_id,
                                         f"t = {flat.thickness_mm:g} mm", strength="measured",
                                         body=part.key))
            result.facts.append(Fact("bom_qty", {"part_key": key, "kpl": part.qty}, file_id,
                                     f"{part.name} × {part.qty}", body=part.key))
        else:
            result.facts.append(Fact("bought_body", {"name": part.name, "kpl": part.qty},
                                     file_id, (flat.reason if flat else "") or part.name,
                                     body=part.key))
    result.is_assembly = len(model.parts) > 1
    return result


# ── PDF ───────────────────────────────────────────────────────────────────────

def _bom_facts(text: str, file_id: str, page: int, own: str) -> list[Fact]:
    """``1 ITM-064555.A Plate … 2 pcs``: a line naming one part number and a
    quantity with its unit."""
    out = []
    for line in text.splitlines():
        qty = _QTY.findall(line)
        numbers = {base_key(n) for n in _NUMBER.findall(line)} - {own}
        if qty and len(numbers) == 1:
            out.append(Fact("bom_qty", {"part_key": numbers.pop(), "kpl": int(qty[-1])},
                            file_id, line.strip(), page))
    return out


def read_pdf_file(data: bytes, file_id: str, name: str) -> ReadResult:
    """A PDF: no shape, only facts (material, part numbers, BOM lines,
    fastener callouts), and whether it is an assembly drawing."""
    from core.fasteners import pdf_fasteners
    from core.materials import pdf_material_fields, pdf_text

    own = base_key(name)
    pages = pdf_text(data)
    fields = pdf_material_fields(data)
    facts = [Fact("part_number", own, file_id, name)]
    for page, text in fields:
        facts += _material_facts(text, file_id, page, "field")
    for page, text in pages:
        facts += _material_facts(text, file_id, page)
        facts += _callout_facts(text, own, file_id, page)
        facts += _bom_facts(text, file_id, page, own)
    facts += [Fact("fastener", {"name": n, "kpl": k}, file_id, f"{n} {k} PCS")
              for n, k in pdf_fasteners(data)]
    result = ReadResult(file_id, facts=facts, record=(pages, fields))
    titled = any(_ASSEMBLY_TITLE.search(t) for _, t in pages)
    result.is_assembly = titled or len({f.value for f in result.of("called_out")}) >= 2
    if not pages:
        result.problems.append("PDF-tiedostoa ei voitu lukea.")
    return result


# ── Any file ──────────────────────────────────────────────────────────────────

READERS = {"dxf": read_dxf_file, "dwg": read_dwg_file, "step": read_step_file,
           "pdf": read_pdf_file}


def read_file(f) -> ReadResult:
    """Read an ``InputFile`` with the reader of its kind (a ZIP has none: it
    only yields InputFiles, see ``core.archive``)."""
    reader = READERS.get(f.kind)
    if reader is None:
        return ReadResult(f.id)
    return reader(f.load(), f.id, f.name)
