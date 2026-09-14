"""
core/dxf.py
===========
Read an uploaded DXF drawing and turn it into a nestable "part".

Each DXF file is treated as one product. We extract every drawn outline as a
flat list of (x, y) points (curves — arcs, circles, splines — are flattened
into short line segments), measure the part's bounding box, and convert
everything to millimetres using the drawing's own unit header.

For now the nesting itself packs by bounding box (see core/nesting.py), so the
polylines are used for *drawing the real shape* on the sheet, while the bbox
width/height drive the packing. Keeping the true outline here means shape-aware
(interlocking) nesting can be added later without re-parsing.

The parser is deliberately tolerant: a malformed or empty DXF returns a part
with a warning rather than raising, so the Streamlit page never dies on a bad
upload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import ezdxf
from ezdxf import disassemble, recover

# Curve flattening tolerance (drawing units). Arcs/splines are approximated by
# line segments no further than this from the true curve. 0.2 units ≈ 0.2 mm on
# a mm drawing — smooth enough to look right, cheap enough to render fast.
_FLATTENING_DISTANCE = 0.2

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


@dataclass
class DxfPart:
    """One parsed DXF file, ready to nest and draw."""

    name: str
    polylines: list[list[tuple[float, float]]] = field(default_factory=list)
    width: float = 0.0            # bounding-box width in mm
    height: float = 0.0           # bounding-box height in mm
    unit_code: int = 0            # raw $INSUNITS value
    warnings: list[str] = field(default_factory=list)

    @property
    def unit_label(self) -> str:
        return _UNIT_LABELS.get(self.unit_code, f"koodi {self.unit_code}")

    @property
    def outline_area_mm2(self) -> float:
        """Signed-area (shoelace) sum of the closed outlines, absolute value.

        Only outlines with 3+ points count. This is the *filled* area of the
        drawn shapes — smaller than the bounding box for anything non-rectangular
        — and is handy for reporting how "solid" a part is.
        """
        total = 0.0
        for poly in self.polylines:
            if len(poly) < 3:
                continue
            s = 0.0
            for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
                s += x1 * y2 - x2 * y1
            total += abs(s) / 2.0
        return total

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0 or not self.polylines


def _unit_factor(unit_code: int) -> float:
    """mm per drawing unit; unknown/unitless codes assume mm (factor 1.0)."""
    return _UNITS_TO_MM.get(unit_code, 1.0)


def _extract_polylines(msp) -> list[list[tuple[float, float]]]:
    """Flatten every entity in the modelspace into 2D point lists.

    recursive_decompose explodes block INSERTs into their component entities,
    and to_primitives turns each entity into a flattened primitive whose
    vertices() yields points along the (curve-approximated) path.
    """
    polylines: list[list[tuple[float, float]]] = []
    entities = list(disassemble.recursive_decompose(msp))
    for primitive in disassemble.to_primitives(
        entities, max_flattening_distance=_FLATTENING_DISTANCE
    ):
        try:
            pts = [(float(v.x), float(v.y)) for v in primitive.vertices()]
        except Exception:
            continue
        if len(pts) >= 2:
            polylines.append(pts)
    return polylines


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
        return DxfPart(
            name=name,
            warnings=[f"DXF:ää ei voitu lukea: {exc}"],
        )
    except Exception as exc:  # noqa: BLE001 — keep the app alive on anything
        return DxfPart(
            name=name,
            warnings=[f"Odottamaton virhe DXF:ää luettaessa: {exc}"],
        )

    unit_code = int(getattr(doc, "units", 0) or 0)
    factor = _unit_factor(unit_code)

    msp = doc.modelspace()
    raw_polylines = _extract_polylines(msp)

    if not raw_polylines:
        warnings.append(
            "Piirustuksesta ei löytynyt geometriaa (viivoja, kaaria tai "
            "polylinejä). Tarkista, että osa on mallitilassa (modelspace)."
        )
        return DxfPart(name=name, unit_code=unit_code, warnings=warnings)

    # Bounding box across every point (in native drawing units).
    xs = [x for poly in raw_polylines for x, _ in poly]
    ys = [y for poly in raw_polylines for _, y in poly]
    min_x, min_y = min(xs), min(ys)
    max_x, max_y = max(xs), max(ys)

    # Normalise to the origin and convert to mm, so downstream code always works
    # in a 0..width / 0..height millimetre space.
    polylines = [
        [((x - min_x) * factor, (y - min_y) * factor) for x, y in poly]
        for poly in raw_polylines
    ]
    width = (max_x - min_x) * factor
    height = (max_y - min_y) * factor

    if unit_code == 0:
        warnings.append(
            "Piirustuksessa ei ollut yksikkötietoa ($INSUNITS) — mitat "
            "tulkitaan millimetreinä. Tarkista koko alta."
        )

    return DxfPart(
        name=name,
        polylines=polylines,
        width=width,
        height=height,
        unit_code=unit_code,
        warnings=warnings,
    )
