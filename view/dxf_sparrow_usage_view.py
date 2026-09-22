"""
view/dxf_sparrow_usage_view.py
==============================
Sheet-usage UI for the DXF page, with the Sparrow nesting backend.

Same design as the manual calculator's sheet-usage view (compare every priced
sheet size, pick the cheapest that fits, show metrics, a per-sheet layout, and a
price breakdown) — but the nesting is done by Sparrow via
``core.sparrow_sheet_cost.compute_options_sparrow`` /
``core.sparrow_pack.greedy_fixed_sheets`` instead of the bounding-box packer.

Because each Sparrow run is comparatively slow, the caller computes the result
once (behind a button) and hands the finished ``result`` dict here to render.
This module owns the layout drawing (real placements: rotation + translation +
holes) and reuses the shared price-breakdown renderer.
"""

from __future__ import annotations

import streamlit as st

from core.sheet_usage import cheapest_index
from view.sheet_usage_view import _render_breakdown

# Distinct, accessible colours per part index. Cycles for many parts.
_PALETTE = [
    "#1d4ed8", "#ea580c", "#059669", "#db2777", "#7c3aed",
    "#0891b2", "#ca8a04", "#dc2626", "#4d7c0f", "#be185d",
]


def render_group_sparrow(
    material: str,
    thickness: str,
    thickness_mm: float,
    result: dict,
    parts: list,
    long_side_clamp_mm: int = 0,
    margin_pct: float = 0.0,
    key_suffix: str = "",
) -> tuple[float | None, float | None]:
    """Render one Sparrow-nested sheet-usage group. Returns (total_eur, ppt).

    The sheet-size table is selectable: clicking a row draws that sheet size's
    layout (and prices it) instead of the cheapest — every size's layout is
    already computed, so switching is instant.
    """
    st.markdown(f"**{material}** · **{thickness} mm**")

    if not result.get("has_pieces"):
        return None, None
    if not result.get("has_candidates"):
        st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")
        return None, None

    rows = result["rows"]
    n_pieces = result["n_pieces"]
    cheapest_idx = cheapest_index(rows)

    for i, r in enumerate(rows):
        if r["_failed"] > 0:
            r["Paras"] = "🚫"
        elif i == cheapest_idx:
            r["Paras"] = "◀ edullisin"
        else:
            r["Paras"] = ""

    display_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    event = st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"dxf_su_select::{key_suffix}",
    )

    if cheapest_idx is None:
        st.warning("Osat eivät mahtuneet millekään hinnoitellulle levykoolle.")
        return None, None

    # Default to the cheapest; let the user click a (valid) row to override.
    selected_idx = cheapest_idx
    user_overrode = False
    sel = list(getattr(event.selection, "rows", []) or [])
    if sel:
        idx = sel[0]
        if 0 <= idx < len(rows) and rows[idx]["_failed"] == 0:
            selected_idx = idx
            user_overrode = idx != cheapest_idx
        else:
            st.warning(
                f"**{rows[idx]['Levykoko']}** ei kelpaa — osat eivät mahdu. "
                "Näytetään edullisin."
            )

    active = rows[selected_idx]
    cheapest = rows[cheapest_idx]
    if user_overrode:
        delta = active["_total"] - cheapest["_total"]
        st.info(
            f"Valittu: **{active['Levykoko']}** — {active['Tarvittavat levyt']} "
            f"levyä ({'+' if delta >= 0 else ''}{delta:,.2f} € vs. edullisin "
            f"{cheapest['Levykoko']})."
        )
    else:
        st.success(
            f"Edullisin: **{active['Levykoko']}** — {active['Tarvittavat levyt']} "
            f"levyä, käyttöaste {active['Käyttöaste']}."
        )

    # ── Headline metrics ────────────────────────────────────────────────────
    avg_per_pc = active["_total"] / n_pieces if n_pieces else 0.0
    m = st.columns([2, 1, 1, 1])
    m[0].metric("Materiaalikulu €/kpl (ka.)", f"{avg_per_pc:,.2f} €")
    m[1].metric("Yhteensä €", f"{active['_total']:,.2f}")
    m[2].metric("Levyjä", str(active["Tarvittavat levyt"]))
    m[3].metric("Käyttöaste", active["Käyttöaste"] or "—")

    # ── Per-sheet Sparrow layout ────────────────────────────────────────────
    _render_layout(active, parts, key_suffix=key_suffix)

    _render_breakdown(
        material=material,
        thickness=thickness,
        thickness_mm=thickness_mm,
        margin_pct=margin_pct,
        n_pieces=n_pieces,
        data=active["_breakdown"],
    )

    return active["_total"], active["_ppt"]


def _render_layout(active: dict, parts: list, key_suffix: str = "") -> None:
    """Legend + one SVG per packed sheet for the cheapest sheet size."""
    sheets = active["_sheets"]
    if not sheets:
        return
    sw, sh = active["_sw"], active["_sh"]
    eff_w, eff_h = active["_eff_w"], active["_eff_h"]

    st.markdown(f"**Sijoittelu** — {len(sheets)} levyä")

    rotate = st.checkbox(
        "Käännä näkymä 90°",
        value=False,
        key=f"dxf_su_rot::{key_suffix}",
        help="Kääntää levyn esikatselun 90° — kapea korkea levy näkyy leveänä. "
             "Vain näkymä kääntyy, laskenta ja hinta pysyvät samoina.",
    )

    # Which part indices actually appear, for a compact legend.
    used_indices = sorted({pl.part_index for s in sheets for pl in s.placements})
    legend = []
    for idx in used_indices:
        part = parts[idx] if idx < len(parts) else None
        color = _PALETTE[idx % len(_PALETTE)]
        label = getattr(part, "part_id", f"#{idx + 1}")
        if part is not None:
            label += f" ({part.width_mm:g}×{part.height_mm:g})"
        legend.append(
            f'<span style="display:inline-flex;align-items:center;'
            f'margin-right:14px;font-size:13px;">'
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:{color};opacity:0.55;border:2px solid {color};'
            f'margin-right:6px;border-radius:2px;"></span>{label}</span>'
        )
    st.markdown(f'<div style="margin-bottom:8px;">{"".join(legend)}</div>',
                unsafe_allow_html=True)

    target_px = 460
    scale = target_px / max(sw, sh)

    cols_per_row = min(4, len(sheets))
    for row_start in range(0, len(sheets), cols_per_row):
        row = sheets[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col_idx, sheet in enumerate(row):
            with cols[col_idx]:
                st.markdown(
                    f"**Levy {row_start + col_idx + 1}** · käyttöaste "
                    f"{sheet.utilization * 100:.1f} %"
                )
                st.markdown(
                    _sheet_svg(sheet, sw, sh, eff_w, eff_h, scale, rotate),
                    unsafe_allow_html=True,
                )


def _sheet_svg(sheet, sw, sh, eff_w, eff_h, scale, rotate: bool = False) -> str:
    """SVG for one packed sheet: real placements (holes via even-odd), Y flipped.

    ``rotate`` turns the *view* 90° (a tall sheet shows landscape) by wrapping the
    content in a rotation group; the part labels are counter-rotated so they stay
    upright. Only the picture changes — the geometry, area and price do not.
    """
    # The content is drawn in the sw×sh space; rotating swaps the display box.
    vb_w, vb_h = (sh, sw) if rotate else (sw, sh)
    parts_svg = [
        f'<svg width="{vb_w * scale:.0f}" height="{vb_h * scale:.0f}" '
        f'viewBox="0 0 {vb_w} {vb_h}" preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:2px solid #475569;border-radius:4px;'
        f'display:block;max-width:100%;height:auto;">',
    ]
    if rotate:
        parts_svg.append(f'<g transform="translate({sh},0) rotate(90)">')

    # Greyed-out clamp strip (bought but unusable).
    if eff_w < sw:
        parts_svg.append(
            f'<rect x="{eff_w}" y="0" width="{sw - eff_w}" height="{sh}" '
            f'fill="#cbd5e1" fill-opacity="0.5"/>'
        )
    if eff_h < sh:
        parts_svg.append(
            f'<rect x="0" y="{eff_h}" width="{sw}" height="{sh - eff_h}" '
            f'fill="#cbd5e1" fill-opacity="0.5"/>'
        )

    stroke = max(sw, sh) / 400.0
    for pl in sheet.placements:
        color = _PALETTE[pl.part_index % len(_PALETTE)]
        d = _ring_path(pl.outer, sh)
        for hole in pl.holes:
            d += " " + _ring_path(hole, sh)
        parts_svg.append(
            f'<path d="{d}" fill="{color}" fill-opacity="0.55" fill-rule="evenodd" '
            f'stroke="{color}" stroke-width="{stroke:.2f}" stroke-linejoin="round"/>'
        )
        # Bend / tangent / centre-mark lines — drawn dashed, never filled.
        for cline in getattr(pl, "construction", []):
            if len(cline) < 2:
                continue
            cd = "M " + " L ".join(f"{x:.1f} {sh - y:.1f}" for x, y in cline)
            parts_svg.append(
                f'<path d="{cd}" fill="none" stroke="#0f172a" '
                f'stroke-width="{stroke * 0.6:.2f}" stroke-opacity="0.7" '
                f'stroke-dasharray="{stroke * 2.5:.1f} {stroke * 1.8:.1f}"/>'
            )
        xs = [p[0] for p in pl.outer]
        ys = [p[1] for p in pl.outer]
        cx = sum(xs) / len(xs)
        ty = sh - sum(ys) / len(ys)
        fs = max(sw, sh) / 45.0
        # Keep the label upright when the view is rotated.
        upright = f' transform="rotate(-90 {cx:.1f} {ty:.1f})"' if rotate else ""
        parts_svg.append(
            f'<text x="{cx:.1f}" y="{ty:.1f}"{upright} text-anchor="middle" '
            f'dominant-baseline="central" font-family="sans-serif" '
            f'font-size="{fs:.0f}" font-weight="700" fill="#0f172a">'
            f'#{pl.part_index + 1}</text>'
        )
    if rotate:
        parts_svg.append("</g>")
    parts_svg.append("</svg>")
    return "".join(parts_svg)


def _ring_path(points, sh: float) -> str:
    """SVG path for a ring, flipping Y (CAD up → SVG down)."""
    if len(points) < 2:
        return ""
    cmds = [f'M {points[0][0]:.1f} {sh - points[0][1]:.1f}']
    for x, y in points[1:]:
        cmds.append(f'L {x:.1f} {sh - y:.1f}')
    cmds.append("Z")
    return " ".join(cmds)
