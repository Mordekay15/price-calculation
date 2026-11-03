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
from core.geometry import min_area_angle, rotate_translate
from core.linking import _QTY, base_key, callouts
from core.materials import extract_hints
from core.model import DwgConversion, Fact, FlatBend, FlatShape, ReadResult

_STRAIGHT_TOL_DEG = 0.5
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
    """The part the DXF reader built, as a FlatShape."""
    if report.outline is None:
        return FlatShape(origin, [], problems=list(report.problems) or ["ei ääriviivaa"])
    return FlatShape(
        origin=origin,
        outline=list(report.outline.points),
        holes=[list(h.points) for h in report.holes],
        open_cuts=[list(c) for c in report.open_lines],
        markings=[list(c) for c in report.reference_lines],
        bends=[FlatBend() for _ in range(report.bends)],
        threads=report.threads,
        outside_count=report.dropped_count,
        layers_used=layers,
        problems=list(report.problems),
    )


def _from_dxf_file(f: DxfFile, file_id: str, name: str, record=None) -> ReadResult:
    from core.items import looks_like_drawing

    report = f.part()
    text = "\n".join(f.texts)
    own = base_key(name)
    facts = [Fact("part_number", own, file_id, name)]
    facts += _material_facts(text, file_id)
    facts += [Fact("fastener", {"name": n, "kpl": k}, file_id, f"{n} X{k}KPL")
              for n, k in fastener_lines(f.texts)]
    facts += _callout_facts(text, own, file_id)
    shape = shape_from_report(report, file_id, f.suggested_layers() if f.pieces else None)
    result = ReadResult(file_id, shapes=[shape], facts=facts, problems=list(f.problems),
                        is_drawing=looks_like_drawing(report), record=record or f)
    result.is_assembly = result.is_drawing and len({f.value for f in result.of("called_out")}) >= 2
    return result


def read_dxf_file(data: bytes, file_id: str, name: str) -> ReadResult:
    """A DXF: its part as a FlatShape, and the facts in its texts."""
    return _from_dxf_file(read_dxf(data, name), file_id, name)


def convert_dwg(data: bytes, file_id: str) -> DwgConversion:
    """A DWG as a DXF (see ``core.dwg``). LibreDWG's minimal DXF is used when
    the DXF reader can't read its full one but can read the minimal one."""
    conv = DwgConversion(file_id, converter=(dwg.converter() or "none").lower())
    try:
        full = dwg.dwg_to_dxf(data)
    except dwg.ConversionError as exc:
        conv.problems = [f"{exc} {dwg.SAVE_AS_DXF}"]
        return conv
    out, kind = full, "full"
    probe = read_dxf(full, "probe.dxf")
    if probe.problems and not probe.pieces and conv.converter == "libredwg":
        try:
            minimal = dwg._with_units(dwg.dwg_to_dxf(data, minimal=True), full)
            if read_dxf(minimal, "probe.dxf").pieces:
                out, kind = minimal, "minimal"
        except dwg.ConversionError:
            pass
    conv.dxf, conv.dxf_kind = (lambda b=out: b), kind
    return conv


def read_dwg_file(data: bytes, file_id: str, name: str) -> ReadResult:
    """A DWG: converted, then read exactly like a DXF."""
    conv = convert_dwg(data, file_id)
    if conv.dxf is None:
        return ReadResult(file_id, problems=list(conv.problems), record=conv)
    f = read_dxf(conv.dxf(), name)
    if f.problems and not f.pieces:
        f.problems = [f"{p} {dwg.SAVE_AS_DXF}" for p in f.problems]
    result = _from_dxf_file(f, file_id, name, record=f)
    result.record = (conv, f)
    return result


# ── STEP ──────────────────────────────────────────────────────────────────────

def _laid_along_length(outline, holes, bends):
    """Turn a flat so it lies along its length, as the DXF reader does."""
    turn = min_area_angle(outline)
    off = turn % 180.0
    if min(off, 180.0 - off) <= _STRAIGHT_TOL_DEG:
        return outline, holes, bends

    def t(points):
        return rotate_translate(points, turn, (0.0, 0.0))
    turned = []
    for b in bends:
        a, c = t(list(b.line))
        turned.append(FlatBend((a, c), b.angle_deg, b.inner_radius_mm, b.up))
    return t(outline), [t(h) for h in holes], turned


def shape_from_flat(flat, origin: str) -> FlatShape:
    """A STEP body's unfolded flat as a FlatShape: bends with their angle,
    radius and direction, nothing guessed back from a drawing."""
    bends = [FlatBend(tuple(map(tuple, b.line)), b.angle_deg, b.inner_radius_mm, b.up)
             for b in flat.bends]
    outline, holes, bends = _laid_along_length(list(flat.outline),
                                               [list(h) for h in flat.holes], bends)
    return FlatShape(origin, outline, holes=holes,
                     markings=[list(b.line) for b in bends], bends=bends)


def read_step_file(data: bytes, file_id: str, name: str,
                   k_factor: float | None = None) -> ReadResult:
    """A STEP model: a FlatShape per sheet body, its thickness (measured),
    its quantities, and the bodies that aren't sheet (bought parts)."""
    from core import step

    model = step.read_step(data, name)
    k = step.DEFAULT_K_FACTOR if k_factor is None else k_factor
    result = ReadResult(file_id, problems=list(model.problems), record=model)
    for part in model.parts:
        sid = f"{file_id}|{part.key}"
        flat = step.unfold(part.shape, k) if part.shape is not None else None
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
    facts = [Fact("part_number", own, file_id, name)]
    for page, text in pdf_material_fields(data):
        facts += _material_facts(text, file_id, page, "field")
    for page, text in pages:
        facts += _material_facts(text, file_id, page)
        facts += _callout_facts(text, own, file_id, page)
        facts += _bom_facts(text, file_id, page, own)
    facts += [Fact("fastener", {"name": n, "kpl": k}, file_id, f"{n} {k} PCS")
              for n, k in pdf_fasteners(data)]
    result = ReadResult(file_id, facts=facts, record=pages)
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
