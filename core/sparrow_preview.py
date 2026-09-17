"""
core/sparrow_preview.py
=======================
Sparrow — real-geometry layout preview.

Sparrow's own SVG draws each item as its bare outer polygon plus a
collision-detection surrogate (the big circle you see in the layout) — so the
holes are invisible. This module renders the *placed real geometry* instead:
every part's true outline with all its holes cut out, laid out on the strip
exactly where Sparrow placed it. Operators can then see all the holes.

It reuses the same placement transform as the reconstruction
(``p' = R(θ)·p + t``) and the flattened outline/holes from the inspector, so the
preview matches the production DXF.
"""

from __future__ import annotations

from core.sparrow_reconstruct import (
    _holes_for,
    _rotate_translate,
    read_placements,
    _strip_size,
)

# Colours (work on light and dark backgrounds; the sheet is drawn on white).
_SHEET_FILL = "#ffffff"
_SHEET_STROKE = "#94a3b8"
_LABEL = "#0f172a"

# Categorical palette — one colour per item (product type), so all copies of a
# part share a colour and different parts stand out. Chosen for good contrast on
# the white sheet and to stay distinct for colour-vision deficiencies; cycled
# with modulo when there are more items than colours.
_PALETTE = (
    "#1d4ed8",  # blue
    "#ea580c",  # orange
    "#059669",  # emerald
    "#db2777",  # pink
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#ca8a04",  # gold
    "#dc2626",  # red
    "#4d7c0f",  # olive
    "#be185d",  # magenta
)


def _colour_for(item_id: int) -> str:
    """Stable colour for one item id (product type)."""
    return _PALETTE[item_id % len(_PALETTE)]


def render_layout_svg(
    solution: dict,
    sources: list,
    *,
    margin_mm: float = 20.0,
    show_labels: bool = False,
) -> str:
    """Render the nested layout (parts + all holes) as an SVG string."""
    placements = read_placements(solution)
    strip_w, strip_h = _strip_size(solution)
    if strip_w is None or strip_h is None or not placements:
        return _empty_svg()

    # Build every placed part's outline + holes in model (mm) coordinates.
    shapes: list[dict] = []
    for pl in placements:
        if pl.item_id >= len(sources):
            continue
        src = sources[pl.item_id]
        part = src.report.parts[src.part_index]
        outer = _rotate_translate(part.points, pl.rotation_deg, pl.translation)
        holes = [
            _rotate_translate(h.points, pl.rotation_deg, pl.translation)
            for h in _holes_for(part, src.report)
        ]
        shapes.append({
            "outer": outer,
            "holes": holes,
            "label": src.part_id,
            "colour": _colour_for(pl.item_id),
            "centroid": _centroid(outer),
        })

    vb_w = strip_w + 2 * margin_mm
    vb_h = strip_h + 2 * margin_mm

    def fx(x: float) -> float:
        return x + margin_mm

    def fy(y: float) -> float:
        # flip Y so the preview reads the same way up as CAD
        return (strip_h - y) + margin_mm

    stroke = max(strip_w, strip_h) / 400.0  # scale line weight to the sheet
    parts_svg: list[str] = []

    # the strip / sheet
    parts_svg.append(
        f'<rect x="{fx(0):.3f}" y="{fy(strip_h):.3f}" '
        f'width="{strip_w:.3f}" height="{strip_h:.3f}" '
        f'fill="{_SHEET_FILL}" stroke="{_SHEET_STROKE}" '
        f'stroke-width="{stroke*1.5:.3f}" stroke-dasharray="{stroke*4:.2f} {stroke*4:.2f}"/>'
    )

    for sh in shapes:
        d = _ring_path(sh["outer"], fx, fy)
        for hole in sh["holes"]:
            d += " " + _ring_path(hole, fx, fy)
        colour = sh["colour"]
        parts_svg.append(
            f'<path d="{d}" fill="{colour}" fill-rule="evenodd" '
            f'fill-opacity="0.5" stroke="{colour}" '
            f'stroke-width="{stroke:.3f}" stroke-linejoin="round"/>'
        )
        if show_labels:
            cx, cy = sh["centroid"]
            fs = max(strip_w, strip_h) / 30.0
            parts_svg.append(
                f'<text x="{fx(cx):.2f}" y="{fy(cy):.2f}" fill="{_LABEL}" '
                f'font-family="sans-serif" font-size="{fs:.2f}" '
                f'text-anchor="middle" dominant-baseline="central" '
                f'opacity="0.75">{_escape(sh["label"])}</text>'
            )

    body = "\n".join(parts_svg)
    return (
        f'<svg viewBox="0 0 {vb_w:.3f} {vb_h:.3f}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;height:auto;background:#ffffff">\n{body}\n</svg>'
    )


def _ring_path(points, fx, fy) -> str:
    if not points:
        return ""
    cmds = [f'M {fx(points[0][0]):.3f},{fy(points[0][1]):.3f}']
    for x, y in points[1:]:
        cmds.append(f'L {fx(x):.3f},{fy(y):.3f}')
    cmds.append("Z")
    return " ".join(cmds)


def _centroid(points) -> tuple[float, float]:
    n = len(points)
    return sum(x for x, _ in points) / n, sum(y for _, y in points) / n


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _empty_svg() -> str:
    return (
        '<svg viewBox="0 0 100 40" xmlns="http://www.w3.org/2000/svg">'
        '<text x="50" y="22" text-anchor="middle" font-family="sans-serif" '
        'font-size="6" fill="#64748b">Ei asettelua näytettäväksi</text></svg>'
    )
