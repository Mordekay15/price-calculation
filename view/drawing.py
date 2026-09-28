"""Every SVG drawing: the sheet layouts of both tabs (rectangles, Sparrow's real
shapes) and the DXF part preview on a card."""

import streamlit as st

from core.dxf import DxfReport
from core.sheet_cost import SheetOption, fmt_m, utilization


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


def _svg_open(vb_w: float, vb_h: float, scale: float) -> str:
    return (
        f'<svg width="{vb_w * scale:.0f}" height="{vb_h * scale:.0f}" '
        f'viewBox="0 0 {vb_w} {vb_h}" preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:2px solid #475569;border-radius:4px;'
        f'display:block;max-width:100%;height:auto;">'
    )


def _clamp_strip(sw: float, sh: float, eff_w: float, eff_h: float) -> str:
    """Greyed-out clamp strip: bought, but not usable for parts."""
    out = ""
    if eff_w < sw:
        out += (f'<rect x="{eff_w}" y="0" width="{sw - eff_w}" height="{sh}" '
                f'fill="#cbd5e1" fill-opacity="0.5"/>')
    if eff_h < sh:
        out += (f'<rect x="0" y="{eff_h}" width="{sw}" height="{sh - eff_h}" '
                f'fill="#cbd5e1" fill-opacity="0.5"/>')
    return out


# ── Manual calculator: rectangles ─────────────────────────────────────────────

def draw_rect_layout(option: SheetOption, products: list[dict], rankavali_mm: int) -> None:
    """Every sheet of the chosen size, each piece a labelled rectangle.

    ``products`` is the group's product list (placements index into it); each
    product's ``_global_idx`` picks its colour and ``#N`` label.
    """
    packing = option.packing
    sheets = packing.sheets
    sw, sh = packing.draw_w, packing.draw_h
    st.markdown(f"**Sijoittelu** — {len(sheets)} levyä")
    _render_legend([
        (p["_global_idx"],
         f"{p.get('name') or 'Tuote #' + str(p['_global_idx'] + 1)} "
         f"({p['width']:g}×{p['height']:g})")
        for p in products
    ])

    scale = _TARGET_PX / max(sw, sh)
    cards = []
    for n, sheet in enumerate(sheets, start=1):
        part_area = sum(
            products[p.product_idx]["width"] * products[p.product_idx]["height"]
            for p in sheet.placements
        )
        cards.append((
            f"**Levy {n}** · käyttöaste {utilization(part_area, sw, sh) * 100:.1f} %",
            _rect_sheet_svg(sheet, sw, sh, packing.eff_w, packing.eff_h,
                            scale, products, rankavali_mm),
        ))
    _render_sheet_grid(cards)


def _rect_sheet_svg(sheet, sw, sh, eff_w, eff_h, scale, products, rankavali_mm) -> str:
    out = [_svg_open(sw, sh, scale), _clamp_strip(sw, sh, eff_w, eff_h)]
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
    out.append("</svg>")
    return "".join(out)


# ── DXF tab: Sparrow's real shapes and the part preview ────────────────────────────────────────────

def draw_sparrow_layout(option: SheetOption, parts: list, key: str) -> None:
    """One SVG per distinct sheet layout for the chosen size.

    Identical sheets are drawn once with a "×N" count and a sheet-number range
    (e.g. "Levy 1–4 · ×4"). The "turn" toggle shows the *other* orientation's
    real re-nest when Sparrow made one; otherwise (a square sheet, or parts free
    to turn 90°) it just rotates the picture. The price never changes.
    A combination of sizes is drawn size by size, without the toggle.
    """
    if option.combo:
        for sub in option.combo:
            st.markdown(f"**{fmt_m(sub.sw)} × {fmt_m(sub.sh)} m**")
            _draw_packing(sub.packing, parts, rotate=False)
        return
    alt = option.packing.alt
    rotate = st.checkbox(
        "Käännä levy 90°",
        value=False,
        key=f"dxf_su_rot::{key}",
        help="Näyttää Sparrown asettelun käännetylle levylle (sama levykoko ja "
             "hinta, eri sijoittelu). Neliölevyllä tai kun osat saa kääntää "
             "90°, vain kuva kääntyy.",
    )
    shown = option.packing
    if rotate and alt:
        shown = alt
        used = sum(s.used_area * s.count for s in alt.sheets)
        st.caption(
            f"Käännetty levy — Sparrow laski asettelun uudelleen: käyttöaste "
            f"{utilization(used, alt.draw_w, alt.draw_h, alt.sheets_needed) * 100:.1f} %, "
            f"{alt.sheets_needed} levyä."
        )
    _draw_packing(shown, parts, rotate=rotate and not alt)  # no re-nest: turn the picture


def _draw_packing(shown, parts: list, *, rotate: bool) -> None:
    """The legend and one card per distinct sheet layout of one packing."""
    sheets = shown.sheets
    sw, sh = shown.draw_w, shown.draw_h
    st.markdown(f"**Sijoittelu** — {sum(s.count for s in sheets)} levyä")

    used = sorted({pl.part_index for s in sheets for pl in s.placements})
    _render_legend([
        (i, f"{parts[i].part_id} ({parts[i].width_mm:g}×{parts[i].height_mm:g})"
            if i < len(parts) else f"#{i + 1}")
        for i in used
    ])

    scale = _TARGET_PX / max(sw, sh)
    cards = []
    first = 1
    for sheet in sheets:
        last = first + sheet.count - 1
        label = f"Levy {first}" if sheet.count == 1 else f"Levy {first}–{last} · ×{sheet.count}"
        first = last + 1
        cards.append((
            f"**{label}** · käyttöaste {utilization(sheet.used_area, sw, sh) * 100:.1f} %",
            _shape_sheet_svg(sheet, sw, sh, shown.eff_w, shown.eff_h, scale, rotate),
        ))
    _render_sheet_grid(cards)


def _shape_sheet_svg(sheet, sw, sh, eff_w, eff_h, scale, rotate: bool) -> str:
    """Real placements (holes via even-odd), Y flipped from CAD to SVG.

    ``rotate`` turns the *view* 90° by wrapping the content in a rotation group;
    part labels are counter-rotated so they stay upright.
    """
    vb_w, vb_h = (sh, sw) if rotate else (sw, sh)
    out = [_svg_open(vb_w, vb_h, scale)]
    if rotate:
        out.append(f'<g transform="translate({sh},0) rotate(90)">')
    out.append(_clamp_strip(sw, sh, eff_w, eff_h))

    stroke = max(sw, sh) / 400.0
    for pl in sheet.placements:
        colour = _colour(pl.part_index)
        d = " ".join(_ring_path(r, sh) for r in (pl.outer, *pl.holes))
        out.append(
            f'<path d="{d}" fill="{colour}" fill-opacity="0.55" fill-rule="evenodd" '
            f'stroke="{colour}" stroke-width="{stroke:.2f}" stroke-linejoin="round"/>'
        )
        # Bend / tangent / centre-mark lines — drawn dashed, never filled.
        for cline in pl.construction:
            if len(cline) < 2:
                continue
            cd = "M " + " L ".join(f"{x:.1f} {sh - y:.1f}" for x, y in cline)
            out.append(
                f'<path d="{cd}" fill="none" stroke="#0f172a" '
                f'stroke-width="{stroke * 0.6:.2f}" stroke-opacity="0.7" '
                f'stroke-dasharray="{stroke * 2.5:.1f} {stroke * 1.8:.1f}"/>'
            )
        cx = sum(p[0] for p in pl.outer) / len(pl.outer)
        ty = sh - sum(p[1] for p in pl.outer) / len(pl.outer)
        upright = f' transform="rotate(-90 {cx:.1f} {ty:.1f})"' if rotate else ""
        out.append(
            f'<text x="{cx:.1f}" y="{ty:.1f}"{upright} text-anchor="middle" '
            f'dominant-baseline="central" font-family="sans-serif" '
            f'font-size="{max(sw, sh) / 45.0:.0f}" font-weight="700" fill="#0f172a">'
            f'#{pl.part_index + 1}</text>'
        )
    if rotate:
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def _ring_path(points, sh: float) -> str:
    """SVG path for a ring, flipping Y (CAD up → SVG down)."""
    if len(points) < 2:
        return ""
    return ("M " + " L ".join(f"{x:.1f} {sh - y:.1f}" for x, y in points) + " Z")


def preview_svg(report: DxfReport, px: int = 260) -> str:
    """Small preview of the part (drawing Y flipped).

    The part is filled (holes cut out), reference lines are dashed, open lines
    inside the part are red, and geometry dropped outside the part is grey.
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
    if report.open_lines:
        out.append(f'<path d="{d(report.open_lines, False)}" fill="none" stroke="#dc2626" '
                   f'stroke-width="{stroke * 1.5:.2f}"/>')
    out.append("</svg>")
    return "".join(out)
