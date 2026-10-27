"""Every SVG drawing: the sheet layouts of both tabs (rectangles, Sparrow's real
shapes), the edge-gap diagram and the DXF part preview on a card."""

import streamlit as st

from core.dxf import DxfReport, is_slit
from core.geometry import rotate_translate
from core.sheet_cost import SheetOption, fmt_m, product_label, utilization


# ── Shared pieces ─────────────────────────────────────────────

# Distinct, accessible colours per part / product index. Cycles for >10.
_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#6366f1",
]
_TARGET_PX = 460  # longest side of one drawn sheet


def _colour(i: int) -> str:
    return _PALETTE[i % len(_PALETTE)]


def _render_legend(items: list[tuple[int, str]]) -> None:
    """One coloured swatch + label per ``(colour_index, label)``."""
    bits = [
        f'<span style="display:inline-flex;align-items:center;'
        f'margin-right:14px;font-size:13px;">'
        f'<span style="display:inline-block;width:14px;height:14px;'
        f'background:{_colour(i)};opacity:0.55;border:2px solid {_colour(i)};'
        f'margin-right:6px;border-radius:2px;"></span>{label}</span>'
        for i, label in items
    ]
    st.markdown(f'<div style="margin-bottom:8px;">{"".join(bits)}</div>',
                unsafe_allow_html=True)


def _render_sheet_grid(cards: list[tuple[str, str]]) -> None:
    """Draw ``(caption, svg)`` cards, up to four per row."""
    per_row = min(4, len(cards))
    for start in range(0, len(cards), per_row):
        cols = st.columns(per_row)
        for col, (caption, svg) in zip(cols, cards[start:start + per_row]):
            with col:
                st.markdown(caption)
                st.markdown(svg, unsafe_allow_html=True)


def _render_layouts(packing, used_area, sheet_svg, start=(1, 1), scale=None,
                    option: SheetOption | None = None) -> tuple[int, int]:
    """One card per distinct sheet layout (a program); identical sheets are
    drawn once with a "×N" count and a sheet-number range (e.g. "Ohjelma 1 ·
    Levy 1–4 · ×4").

    ``used_area(sheet)`` is the real part area on one sheet and
    ``sheet_svg(sheet, scale)`` draws it. The ``option``'s last sheet, if
    charged only in part, shows the cut and the remnant. Numbering starts at ``start``
    (program, sheet); returns where the next size's numbering goes on.
    """
    sw, sh = packing.draw_w, packing.draw_h
    scale = scale or _TARGET_PX / max(sw, sh)
    cards = []
    n, first = start[0] - 1, start[1]
    for i, (n, sheet) in enumerate(enumerate(packing.sheets, start=start[0])):
        last = first + sheet.count - 1
        sheets = f"Levy {first}" if sheet.count == 1 else f"Levy {first}–{last} · ×{sheet.count}"
        label = f"Ohjelma {n} · {sheets}"
        first = last + 1
        svg = sheet_svg(sheet, scale)
        share = 1.0
        if option is not None and option.partial_index == i and option.last_share < 1:
            share = option.last_share
            svg = svg[:-len("</svg>")] + _remnant(packing, share) + "</svg>"
            label += f" · veloitetaan {share * 100:.0f} %"
        cards.append((
            f"**{label}** · käyttöaste "
            f"{utilization(used_area(sheet), sw * share, sh) * 100:.1f} %",
            svg,
        ))
    _render_sheet_grid(cards)
    return n + 1, first


def _remnant(packing, share: float) -> str:
    """The part of a sheet not charged: hatched, behind a dashed cut line at
    ``share`` of its length, labelled as the remnant."""
    w, h = packing.draw_w, packing.draw_h
    cut = w * share
    sw = max(w, h) / 300.0
    return (
        f'<defs><pattern id="remnant" width="{sw * 12:.1f}" height="{sw * 12:.1f}" '
        f'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<line x1="0" y1="0" x2="0" y2="{sw * 12:.1f}" stroke="#64748b" '
        f'stroke-width="{sw * 2:.1f}" stroke-opacity="0.35"/></pattern></defs>'
        f'<rect x="{cut:.1f}" y="0" width="{w - cut:.1f}" height="{h}" fill="url(#remnant)"/>'
        f'<line x1="{cut:.1f}" y1="0" x2="{cut:.1f}" y2="{h}" stroke="#dc2626" '
        f'stroke-width="{sw * 2:.1f}" stroke-dasharray="{sw * 8:.1f} {sw * 5:.1f}"/>'
        f'<text x="{(cut + w) / 2:.1f}" y="{h / 2:.1f}" text-anchor="middle" '
        f'dominant-baseline="central" font-family="sans-serif" font-size="{max(w, h) / 30:.0f}" '
        f'fill="#475569">jäännöslevy {100 - share * 100:.0f} %</text>'
    )


def _render_each_size(option: SheetOption, render) -> None:
    """The chosen option's layouts: ``render(sized, scale, start)`` for its
    size, or for each size of a plan cut from several, under the size's name,
    drawn to one scale and numbered on."""
    parts = option.mix or [option]
    scale = _TARGET_PX / max(max(o.packing.draw_w, o.packing.draw_h) for o in parts)
    start = (1, 1)
    for o in parts:
        if option.mix:
            st.markdown(f"**{fmt_m(o.sw)} × {fmt_m(o.sh)} m** · "
                        f"{o.sheets_needed} levyä")
        start = render(o, scale, start)


def _svg_open(vb_w: float, vb_h: float, scale: float) -> str:
    return (
        f'<svg width="{vb_w * scale:.0f}" height="{vb_h * scale:.0f}" '
        f'viewBox="0 0 {vb_w} {vb_h}" preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:2px solid #475569;border-radius:4px;'
        f'display:block;max-width:100%;height:auto;">'
    )


def _unusable_border(packing) -> str:
    """The edge gaps greyed out: bought, but not usable for parts. Drawn as
    the sheet minus its usable area (even-odd fill)."""
    p = packing
    if (p.eff_w, p.eff_h) == (p.draw_w, p.draw_h):
        return ""
    return (f'<path d="M0 0H{p.draw_w}V{p.draw_h}H0Z '
            f'M{p.x0} {p.y0}H{p.x0 + p.eff_w}V{p.y0 + p.eff_h}H{p.x0}Z" '
            f'fill="#94a3b8" fill-opacity="0.45" fill-rule="evenodd"/>')


def _usable_group(packing) -> str:
    """Opens a group whose origin is the usable area's corner, where the
    packers' placements start."""
    return f'<g transform="translate({packing.x0} {packing.y0})">'


def edge_gaps_svg(edges_mm: tuple[int, int, int, int]) -> str:
    """A small schematic sheet (long side horizontal) with its edge gaps
    shaded and labelled, so the user can see which input is which edge. Not
    to scale: a band shows only whether a gap is set."""
    top, bottom, left, right = edges_mm
    x, y, w, h = 44, 22, 190, 96          # the sheet inside a 278 × 150 box

    def band(bx, by, bw, bh, opacity):
        return (f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" '
                f'fill="currentColor" fill-opacity="{opacity}"/>')

    t = 10                                # drawn band thickness
    out = ['<svg viewBox="0 0 278 150" width="100%" style="max-width:300px;'
           'color:inherit;font-family:sans-serif;font-size:11px;">']
    if top:
        out.append(band(x, y, w, t, 0.25))
    if bottom:
        out.append(band(x, y + h - t, w, t, 0.25))
    if left:
        out.append(band(x, y, t, h, 0.25))
    if right:
        out.append(band(x + w - t, y, t, h, 0.25))
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" '
               f'stroke="currentColor" stroke-width="1.5"/>')
    cy = y + h / 2
    out += [
        f'<text x="{x + w / 2}" y="{y - 7}" text-anchor="middle" fill="currentColor">Ylä {top}</text>',
        f'<text x="{x + w / 2}" y="{y + h + 16}" text-anchor="middle" fill="currentColor">'
        f'Ala {bottom}</text>',
        f'<text x="{x - 8}" y="{cy}" text-anchor="middle" fill="currentColor" '
        f'transform="rotate(-90 {x - 8} {cy})">Vasen {left}</text>',
        f'<text x="{x + w + 14}" y="{cy}" text-anchor="middle" fill="currentColor" '
        f'transform="rotate(90 {x + w + 14} {cy})">Oikea {right}</text>',
        f'<text x="{x + w / 2}" y="{cy + 4}" text-anchor="middle" fill="currentColor" '
        f'fill-opacity="0.6">osien alue</text>',
        "</svg>",
    ]
    return "".join(out)


# ── Manual calculator: rectangles ─────────────────────────────────────────────

def draw_rect_layout(option: SheetOption, products: list[dict], rankavali_mm: int) -> None:
    """Each sheet layout of the chosen size, each piece a labelled rectangle.

    ``products`` is the group's product list (placements index into it); each
    product's ``_global_idx`` picks its colour and ``#N`` label.
    """
    _render_legend([
        (p["_global_idx"], f"{product_label(p)} ({p['width']:g}×{p['height']:g})")
        for p in products
    ])

    def used_area(sheet):
        return sum(products[p.product_idx]["width"] * products[p.product_idx]["height"]
                   for p in sheet.placements)

    _render_each_size(option, lambda o, scale, start: _render_layouts(
        o.packing, used_area,
        lambda sheet, sc: _rect_sheet_svg(sheet, o.packing, sc, products, rankavali_mm),
        start, scale, o))


def _rect_sheet_svg(sheet, packing, scale, products, rankavali_mm) -> str:
    out = [_svg_open(packing.draw_w, packing.draw_h, scale), _unusable_border(packing),
           _usable_group(packing)]
    for pl in sheet.placements:
        prod = products[pl.product_idx]
        idx = prod["_global_idx"]
        # Strip the rankaväli padding so the drawn footprint is the real piece.
        dw = max(1, pl.w - rankavali_mm)
        dh = max(1, pl.h - rankavali_mm)
        out.append(
            f'<rect x="{pl.x}" y="{pl.y}" width="{dw}" height="{dh}" '
            f'fill="{_colour(idx)}" fill-opacity="0.55" '
            f'stroke="{_colour(idx)}" stroke-width="6"/>'
        )
        cx, cy = pl.x + dw / 2, pl.y + dh / 2
        # Font size scales with the smaller piece dimension so labels fit.
        fs = max(40.0, min(dw, dh) / 5.5)
        out.append(
            f'<text x="{cx}" y="{cy - fs * 0.1}" text-anchor="middle" '
            f'dominant-baseline="middle" font-family="sans-serif" '
            f'font-size="{fs:.0f}" font-weight="700" fill="#0f172a">#{idx + 1}</text>'
            f'<text x="{cx}" y="{cy + fs}" text-anchor="middle" '
            f'dominant-baseline="middle" font-family="sans-serif" '
            f'font-size="{fs * 0.65:.0f}" fill="#0f172a">'
            f'{prod["width"]:g}×{prod["height"]:g}</text>'
        )
    out.append("</g></svg>")
    return "".join(out)


# ── DXF tab: Sparrow's real shapes and the part preview ────────────────────────────────────────────

def draw_sparrow_layout(option: SheetOption, parts: list) -> None:
    """Each sheet layout of the chosen size with the parts' real shapes."""
    sheets = [s for o in option.mix or [option] for s in o.packing.sheets]

    used = sorted({pl.part_index for s in sheets for pl in s.placements})
    _render_legend([
        (i, f"{parts[i].part_id} ({parts[i].width_mm:g}×{parts[i].height_mm:g})"
            if i < len(parts) else f"#{i + 1}")
        for i in used
    ])
    # A part with a fixed nesting angle lies to the rolling direction.
    if any(not parts[i].turns for i in used if i < len(parts)):
        st.caption("Valssaussuunta → levyn pitkä sivu (kuvissa vaakasuunta)")

    _render_each_size(option, lambda o, scale, start: _render_layouts(
        o.packing, lambda sheet: sheet.used_area,
        lambda sheet, sc: _shape_sheet_svg(sheet, o.packing, sc), start, scale, o))


def _shape_sheet_svg(sheet, packing, scale) -> str:
    """Real placements (holes via even-odd). Sparrow's Y runs up from the
    bottom of the usable area, so it is flipped within that area's height."""
    sw, sh, eh = packing.draw_w, packing.draw_h, packing.eff_h
    out = [_svg_open(sw, sh, scale), _unusable_border(packing), _usable_group(packing)]

    stroke = max(sw, sh) / 400.0
    for pl in sheet.placements:
        colour = _colour(pl.part_index)
        d = " ".join(_ring_path(r, eh) for r in (pl.outer, *pl.holes))
        out.append(
            f'<path d="{d}" fill="{colour}" fill-opacity="0.55" fill-rule="evenodd" '
            f'stroke="{colour}" stroke-width="{stroke:.2f}" stroke-linejoin="round"/>'
        )
        # Bend / tangent / centre-mark lines — drawn dashed, never filled.
        for cline in pl.construction:
            if len(cline) < 2:
                continue
            cd = "M " + " L ".join(f"{x:.1f} {eh - y:.1f}" for x, y in cline)
            out.append(
                f'<path d="{cd}" fill="none" stroke="#0f172a" '
                f'stroke-width="{stroke * 0.6:.2f}" stroke-opacity="0.7" '
                f'stroke-dasharray="{stroke * 2.5:.1f} {stroke * 1.8:.1f}"/>'
            )
        cx = sum(p[0] for p in pl.outer) / len(pl.outer)
        ty = eh - sum(p[1] for p in pl.outer) / len(pl.outer)
        out.append(
            f'<text x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" font-family="sans-serif" '
            f'font-size="{max(sw, sh) / 45.0:.0f}" font-weight="700" fill="#0f172a">'
            f'#{pl.part_index + 1}</text>'
        )
    out.append("</g></svg>")
    return "".join(out)


def _ring_path(points, sh: float) -> str:
    """SVG path for a ring, flipping Y (CAD up → SVG down)."""
    if len(points) < 2:
        return ""
    return ("M " + " L ".join(f"{x:.1f} {sh - y:.1f}" for x, y in points) + " Z")


BEND_UP_COLOR, BEND_DOWN_COLOR = "#16a34a", "#ea580c"


def preview_svg(report: DxfReport, px: int = 260, bends: list | None = None) -> str:
    """Small preview of the part (drawing Y flipped).

    The part is filled (holes cut out), reference lines are dashed, slits
    (straight cuts inside the part) are drawn like the outline, other open
    lines inside the part are red, and geometry dropped outside it is grey.
    ``bends`` are bend lines from elsewhere (the STEP model), ``[(line, up)]``:
    dashed, green up and orange down.
    """
    rings = ([report.outline.points] if report.outline else []) + [h.points for h in report.holes]
    groups = (rings, report.reference_lines, report.open_lines, report.dropped)
    pts = [p for group in groups for ring in group for p in ring]
    if not pts:
        return ""
    min_x = min(x for x, _ in pts)
    min_y = min(y for _, y in pts)
    w = max(x for x, _ in pts) - min_x
    h = max(y for _, y in pts) - min_y
    if w <= 0 or h <= 0:
        return ""
    stroke = max(0.5, max(w, h) / 300)

    def d(lines, close):
        return " ".join(
            "M " + " L ".join(f"{x - min_x:.1f} {h - (y - min_y):.1f}" for x, y in line)
            + (" Z" if close else "")
            for line in lines if len(line) >= 2
        )

    scale = px / max(w, h)
    out = [
        f'<svg width="{w * scale:.0f}" height="{h * scale:.0f}" '
        f'viewBox="{-stroke} {-stroke} {w + 2 * stroke:.1f} {h + 2 * stroke:.1f}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;'
        f'max-width:100%;height:auto;margin:4px 0 2px;">'
    ]
    if report.dropped:
        out.append(f'<path d="{d(report.dropped, False)}" fill="none" stroke="#cbd5e1" '
                   f'stroke-width="{stroke:.2f}"/>')
    if rings:
        out.append(f'<path d="{d(rings, True)}" fill="#3b82f6" fill-opacity="0.12" '
                   f'fill-rule="evenodd" stroke="#2563eb" stroke-width="{stroke:.2f}"/>')
    if report.reference_lines:
        out.append(f'<path d="{d(report.reference_lines, False)}" fill="none" '
                   f'stroke="#475569" stroke-width="{stroke * 0.7:.2f}" '
                   f'stroke-dasharray="{stroke * 4:.1f} {stroke * 3:.1f}"/>')
    slits = [line for line in report.open_lines if is_slit(line)]
    gaps = [line for line in report.open_lines if not is_slit(line)]
    if slits:
        out.append(f'<path d="{d(slits, False)}" fill="none" stroke="#2563eb" '
                   f'stroke-width="{stroke:.2f}"/>')
    if gaps:
        out.append(f'<path d="{d(gaps, False)}" fill="none" stroke="#dc2626" '
                   f'stroke-width="{stroke * 1.5:.2f}"/>')
    for up in (True, False):
        lines = [line for line, u in bends or [] if u == up]
        if lines:
            out.append(f'<path d="{d(lines, False)}" fill="none" '
                       f'stroke="{BEND_UP_COLOR if up else BEND_DOWN_COLOR}" '
                       f'stroke-width="{stroke * 2.2:.2f}" '
                       f'stroke-dasharray="{stroke * 6:.1f} {stroke * 3:.1f}"/>')
    out.append("</svg>")
    return "".join(out)


# A part with a clear way round, for the nesting-angle tiles of the run row
# (no one part to show there).
_SAMPLE_PART = [[(0, 0), (100, 0), (100, 18), (32, 18), (32, 50), (0, 50)]]


def orientation_svg(rings: list | None, angle: int, on: bool, px: int = 96) -> str:
    """One nesting-angle tile: the part (``rings``: outline first, then holes;
    a sample part when None) turned by ``angle`` on a patch of sheet whose
    lines and top arrow run in the rolling direction (horizontal, as on the
    layouts).
    ``on``: the angle is allowed (part in colour), else greyed out."""
    rings = [rotate_translate(r, angle, (0.0, 0.0)) for r in rings or _SAMPLE_PART if len(r) >= 3]
    if not rings:
        return ""
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    min_x, min_y = min(xs), min(ys)
    w, h = max(xs) - min_x, max(ys) - min_y
    size = max(w, h)
    if size <= 0:
        return ""
    pad = size * 0.12
    side = size + 2 * pad
    ox, oy = pad + (size - w) / 2, pad + (size - h) / 2
    d = " ".join("M " + " L ".join(f"{x - min_x + ox:.1f} {oy + h - (y - min_y):.1f}"
                                   for x, y in r) + " Z" for r in rings)
    step = side / 9
    grain = " ".join(f"M 0 {i * step:.1f} H {side:.1f}" for i in range(1, 9))
    # The rolling direction, an arrow along the top edge.
    a, y = pad * 0.45, pad * 0.5
    arrow = (f"M {side * 0.6:.1f} {y:.1f} H {side - pad * 0.6:.1f} "
             f"M {side - pad * 0.6 - a:.1f} {y - a * 0.6:.1f} L {side - pad * 0.6:.1f} {y:.1f} "
             f"L {side - pad * 0.6 - a:.1f} {y + a * 0.6:.1f}")
    fill, line = ("#3b82f6", "#2563eb") if on else ("#cbd5e1", "#94a3b8")
    return (
        f'<svg width="{px}" height="{px}" viewBox="0 0 {side:.1f} {side:.1f}" '
        f'style="max-width:100%;height:auto;display:block;border-radius:6px;'
        f'border:2px {"solid #2563eb" if on else "dashed #cbd5e1"};'
        f'background:{"#eff6ff" if on else "#f8fafc"};opacity:{1 if on else 0.75};">'
        f'<path d="{grain}" stroke="#94a3b8" stroke-opacity="0.45" '
        f'stroke-width="{side / 200:.2f}"/>'
        f'<path d="{arrow}" fill="none" stroke="#64748b" stroke-width="{side / 90:.2f}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<path d="{d}" fill="{fill}" fill-opacity="{0.35 if on else 0.3}" '
        f'fill-rule="evenodd" stroke="{line}" stroke-width="{side / 90:.2f}" '
        f'stroke-linejoin="round"/></svg>'
    )
