"""
core/dxf.py
===========
Read an uploaded DXF drawing and turn it into a nestable "part".

Each DXF file is treated as one product. We extract every *cuttable* outline as
a flat list of (x, y) points (curves — arcs, circles, splines — are flattened
into short line segments), measure the part's bounding box, and convert
everything to millimetres.

Only real geometry counts. Annotation entities — dimension text like Mat="…",
Thk=…, Un="…", leaders, dimensions — are skipped, so they never inflate the
size or clutter the drawing. Geometry is also kept grouped by CAD *layer*, so
the UI can drop non-part layers (borders, bend lines, construction lines) when a
drawing has more than one.

For now the nesting itself packs by bounding box (see core/nesting.py), so the
polylines are used for *drawing the real shape*; the bbox width/height drive the
packing. Keeping the true outline here means shape-aware (interlocking) nesting
can be added later without re-parsing.

The parser is deliberately tolerant: a malformed or empty DXF returns a part
with a warning rather than raising, so the Streamlit page never dies on a bad
upload.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import ezdxf
from ezdxf import disassemble, recover

# Curve flattening tolerance (drawing units). Arcs/splines are approximated by
# line segments no further than this from the true curve. 0.2 units ≈ 0.2 mm on
# a mm drawing — smooth enough to look right, cheap enough to render fast.
_FLATTENING_DISTANCE = 0.2

# Entity types that are annotation, not part geometry. These are dropped so a
# "Mat=…"/"Thk=…" label or a dimension never becomes part of the shape or its
# bounding box.
_ANNOTATION_TYPES = {
    "TEXT", "MTEXT", "ATTRIB", "ATTDEF",
    "DIMENSION", "ARC_DIMENSION",
    "LEADER", "MLEADER", "MULTILEADER",
    "TOLERANCE", "WIPEOUT",
}

# DXF $INSUNITS header code -> millimetres-per-unit. Covers the units CAD tools
# actually emit for sheet metal; anything else falls back to "assume mm".
# https://ezdxf.readthedocs.io/en/stable/concepts/units.html
_UNITS_TO_MM: dict[int, float] = {
    1: 25.4,     # inches
    2: 304.8,    # feet
    4: 1.0,      # millimetres
    5: 10.0,     # centimetres
    6: 1000.0,   # metres
    8: 0.0254,   # microinches
    9: 0.0254,   # mils
    10: 914.4,   # yards
    13: 1e-6,    # nanometres
    14: 0.01,    # decimetres
}

_UNIT_LABELS: dict[int, str] = {
    0: "yksiköttä",
    1: "tuuma",
    2: "jalka",
    4: "mm",
    5: "cm",
    6: "m",
}

# Unit tokens that show up in "Un=…" style annotation text, mapped to the
# $INSUNITS code we treat them as. Lets us recover the unit when the header is
# missing but the drawing spells it out in a label.
_TEXT_UNIT_TO_CODE: dict[str, int] = {
    "mm": 4, "millimeter": 4, "millimetre": 4, "millimeters": 4, "millimetres": 4,
    "cm": 5, "centimeter": 5, "centimetre": 5,
    "m": 6, "meter": 6, "metre": 6,
    "in": 1, "inch": 1, "inches": 1, '"': 1,
}

# Matches labels like:  Mat="AlMg3"   Thk=6   Un="mm"
_KV_RE = re.compile(r'([A-Za-z][\w]*)\s*=\s*"?([^"\r\n]+?)"?\s*$')

# Layer-name fragments that mark a layer as annotation / drawing furniture rather
# than the cuttable part — used to pre-select only the part layers by default.
# Matched case-insensitively as substrings of the layer name.
_ANNOTATION_LAYER_KEYWORDS = (
    "dim", "dimension", "text", "note", "format", "frame", "border",
    "title", "hatch", "bom", "table", "symbol", "axis", "axes", "csys",
    "defpoint", "sketch", "draft", "weld", "cosm", "thread", "gdt",
    "tol", "mark", "label", "detail", "section", "balloon", "leader",
)


def _is_annotation_layer(name: str) -> bool:
    low = (name or "").lower()
    return any(kw in low for kw in _ANNOTATION_LAYER_KEYWORDS)


@dataclass
class DxfPart:
    """One parsed DXF file, ready to nest and draw.

    `layers` holds the cuttable geometry grouped by CAD layer, in millimetres,
    with annotation entities already removed. Call `build()` to collapse a chosen
    set of layers into a normalised outline plus bounding-box size.
    """

    name: str
    layers: dict[str, list[list[tuple[float, float]]]] = field(default_factory=dict)
    unit_code: int = 0
    unit_from_text: bool = False   # True if the unit was recovered from a label
    texts: list[str] = field(default_factory=list)     # raw annotation strings
    warnings: list[str] = field(default_factory=list)

    @property
    def unit_label(self) -> str:
        return _UNIT_LABELS.get(self.unit_code, f"koodi {self.unit_code}")

    def available_layers(self) -> list[str]:
        """Layer names that actually carry geometry, sorted."""
        return sorted(name for name, polys in self.layers.items() if polys)

    def suggested_layers(self) -> list[str]:
        """Layers to select by default — part geometry, minus annotation layers.

        Falls back to every geometry layer when name heuristics can't tell them
        apart (so we never hide everything).
        """
        avail = self.available_layers()
        part = [n for n in avail if not _is_annotation_layer(n)]
        return part or avail

    def layer_sizes(self) -> dict[str, tuple[int, float, float]]:
        """Per-layer (entity count, bbox width mm, bbox height mm)."""
        out: dict[str, tuple[int, float, float]] = {}
        for name in self.available_layers():
            polys = self.layers[name]
            xs = [x for poly in polys for x, _ in poly]
            ys = [y for poly in polys for _, y in poly]
            out[name] = (len(polys), max(xs) - min(xs), max(ys) - min(ys))
        return out

    def build(self, layers: set[str] | None = None) -> "DxfGeometry":
        """Collapse the selected layers into a drawable, measured outline.

        `layers=None` uses every geometry layer. Points are translated so the
        part sits at the origin (0..width / 0..height).
        """
        chosen = self.available_layers() if layers is None else [
            n for n in self.available_layers() if n in layers
        ]
        polys = [poly for name in chosen for poly in self.layers.get(name, [])]
        if not polys:
            return DxfGeometry(polylines=[], width=0.0, height=0.0)

        xs = [x for poly in polys for x, _ in poly]
        ys = [y for poly in polys for _, y in poly]
        min_x, min_y = min(xs), min(ys)
        max_x, max_y = max(xs), max(ys)
        normalised = [
            [(x - min_x, y - min_y) for x, y in poly] for poly in polys
        ]
        return DxfGeometry(
            polylines=normalised,
            width=max_x - min_x,
            height=max_y - min_y,
        )

    @property
    def is_empty(self) -> bool:
        return not any(self.layers.values())


@dataclass
class DxfGeometry:
    """The outline + measured size for one chosen set of layers."""

    polylines: list[list[tuple[float, float]]]
    width: float
    height: float

    @property
    def outline_area_mm2(self) -> float:
        """Shoelace area of the closed outlines (holes subtract via winding)."""
        total = 0.0
        for poly in self.polylines:
            if len(poly) < 3:
                continue
            s = 0.0
            for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
                s += x1 * y2 - x2 * y1
            total += abs(s) / 2.0
        return total


def _unit_factor(unit_code: int) -> float:
    """mm per drawing unit; unknown/unitless codes assume mm (factor 1.0)."""
    return _UNITS_TO_MM.get(unit_code, 1.0)


def _extract_texts(msp) -> list[str]:
    """Collect annotation strings (TEXT/MTEXT) for display and unit recovery."""
    texts: list[str] = []
    for entity in msp:
        dxftype = entity.dxftype()
        try:
            if dxftype == "TEXT":
                s = entity.dxf.text
            elif dxftype == "MTEXT":
                s = entity.plain_text()
            else:
                continue
        except Exception:
            continue
        s = (s or "").strip()
        if s:
            texts.append(s)
    return texts


def _unit_from_texts(texts: list[str]) -> int | None:
    """Read a 'Un=…' style unit label out of the annotation text, if present."""
    for raw in texts:
        m = _KV_RE.match(raw.strip())
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip().lower()
        if key in ("un", "unit", "units", "yksikko", "yksikkö"):
            code = _TEXT_UNIT_TO_CODE.get(val)
            if code is not None:
                return code
    return None


def _extract_layers(msp, factor: float) -> dict[str, list[list[tuple[float, float]]]]:
    """Flatten geometry entities into per-layer point lists (in mm).

    recursive_decompose explodes block INSERTs into their component entities;
    annotation entities are skipped; to_primitives turns each remaining entity
    into a flattened primitive whose vertices() yields points along the
    (curve-approximated) path.
    """
    entities = [
        e for e in disassemble.recursive_decompose(msp)
        if e.dxftype() not in _ANNOTATION_TYPES
    ]
    layers: dict[str, list[list[tuple[float, float]]]] = {}
    for primitive in disassemble.to_primitives(
        entities, max_flattening_distance=_FLATTENING_DISTANCE
    ):
        entity = primitive.entity
        if entity.dxftype() in _ANNOTATION_TYPES:
            continue
        try:
            pts = [(float(v.x) * factor, float(v.y) * factor) for v in primitive.vertices()]
        except Exception:
            continue
        if len(pts) < 2:
            continue
        layer = getattr(entity.dxf, "layer", "0") or "0"
        layers.setdefault(layer, []).append(pts)
    return layers


def _read_doc(data: bytes):
    """Open DXF bytes tolerantly. Returns (doc, recover_warnings)."""
    # recover.read repairs common structural problems and reads both ASCII and
    # binary DXF from a binary stream.
    doc, auditor = recover.read(io.BytesIO(data))
    warnings: list[str] = []
    if auditor.has_errors:
        warnings.append(
            f"DXF-tiedostossa oli {len(auditor.errors)} rakennevirhettä, "
            "jotka korjattiin automaattisesti."
        )
    return doc, warnings


def parse_dxf(data: bytes, name: str) -> DxfPart:
    """Parse one DXF file's bytes into a DxfPart.

    Never raises for bad content: parse failures come back as a DxfPart with an
    empty geometry and a warning describing the problem.
    """
    try:
        doc, warnings = _read_doc(data)
    except (ezdxf.DXFError, IOError, ValueError, IndexError) as exc:
        return DxfPart(name=name, warnings=[f"DXF:ää ei voitu lukea: {exc}"])
    except Exception as exc:  # noqa: BLE001 — keep the app alive on anything
        return DxfPart(name=name, warnings=[f"Odottamaton virhe DXF:ää luettaessa: {exc}"])

    msp = doc.modelspace()
    texts = _extract_texts(msp)

    unit_code = int(getattr(doc, "units", 0) or 0)
    unit_from_text = False
    if unit_code == 0:
        recovered = _unit_from_texts(texts)
        if recovered is not None:
            unit_code = recovered
            unit_from_text = True

    factor = _unit_factor(unit_code)
    layers = _extract_layers(msp, factor)

    if not any(layers.values()):
        warnings.append(
            "Piirustuksesta ei löytynyt leikattavaa geometriaa (viivoja, "
            "kaaria tai polylinejä). Tarkista, että osa on mallitilassa "
            "(modelspace)."
        )
        return DxfPart(
            name=name, layers=layers, unit_code=unit_code,
            unit_from_text=unit_from_text, texts=texts, warnings=warnings,
        )

    if unit_code == 0:
        warnings.append(
            "Piirustuksessa ei ollut yksikkötietoa ($INSUNITS) — mitat "
            "tulkitaan millimetreinä. Tarkista koko alta."
        )
    elif unit_from_text:
        warnings.append(
            f"Yksikkö luettu piirustuksen tekstistä ({_UNIT_LABELS.get(unit_code, unit_code)})."
        )

    return DxfPart(
        name=name,
        layers=layers,
        unit_code=unit_code,
        unit_from_text=unit_from_text,
        texts=texts,
        warnings=warnings,
    )
