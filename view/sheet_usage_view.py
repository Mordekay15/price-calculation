"""
view/sheet_usage_view.py
========================
Shared "sheet usage" UI: for one material+thickness group, compare every priced
sheet size, let the user override the cheapest pick, show headline metrics, a
per-product cost split, a nesting layout, and a price breakdown.

Both the manual calculator (view/calculator.py) and the DXF nesting section
(view/dxf_nesting.py) call render_group(), so pricing and layout look and behave
identically no matter how the products were entered. The only difference is the
drawing: a product that carries a real "_polylines" outline (a DXF part) is
drawn as its true shape; a plain rectangle product is drawn as a rectangle.

The heavy costing lives in core/sheet_usage.py (pure, no Streamlit); this module
is just the rendering around it.
"""

import streamlit as st

from core.calculator import (
    density_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.copper import COPPER_MATERIAL
from core.sheet_usage import cheapest_index, compute_options, _fmt_m


def render_group(
    lookup: dict,
    material: str,
    thickness: str,
    products: list[dict],
    margin_pct: float = 0.0,
    long_side_clamp_mm: int = 0,
    rankavali_mm: int = 0,
) -> tuple[float | None, float | None]:
    """Render one sheet-usage table for products sharing a material + thickness.

    The user can click a row to override the default cheapest pick; rows where
    pieces don't fit are ignored if clicked. Returns the (total_eur,
    price_per_tonne) of the active row, or (None, None) when no priced sheet
    size can fulfil the order.
    """
    thickness_mm = parse_thickness_mm(thickness)
    if thickness_mm is None:
        return None, None

    result = compute_options(
        lookup=lookup,
        material=material,
        thickness=thickness,
        thickness_mm=thickness_mm,
        products=products,
        margin_pct=margin_pct,
        long_side_clamp_mm=long_side_clamp_mm,
        rankavali_mm=rankavali_mm,
    )
    if not result["has_pieces"]:
        return None, None

    st.markdown(f"**{material}** · **{thickness} mm**")

    if not result["has_candidates"]:
        if material == COPPER_MATERIAL:
            st.info(
                "Aseta kuparin hinta (€/kg) sivupalkista, niin levylaskenta "
                "tulee näkyviin."
            )
        else:
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

    display_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in rows
    ]

    select_key_suffix = "-".join(str(p["id"]) for p in products)
    select_key = f"sheet_select::{material}::{thickness}::{select_key_suffix}"
    event = st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=select_key,
    )

    selected_idx = cheapest_idx
    user_overrode = False
    sel_rows = list(getattr(event.selection, "rows", []) or [])
    if sel_rows:
        idx = sel_rows[0]
        if 0 <= idx < len(rows) and rows[idx]["_failed"] == 0:
            selected_idx = idx
            user_overrode = idx != cheapest_idx
        else:
            st.warning(
                f"**{rows[idx]['Levykoko']}** on liian pieni — "
                f"{rows[idx]['_failed']} kpl ei mahdu. Käytetään edullisinta levykokoa."
            )

    if selected_idx is None:
        return None, None

    active = rows[selected_idx]
    cheapest = rows[cheapest_idx] if cheapest_idx is not None else None

    if user_overrode and cheapest is not None:
        delta = active["_total"] - cheapest["_total"]
        st.info(
            f"Valittu: **{active['Levykoko']}** — "
            f"{active['Tarvittavat levyt']} levyä "
            f"(+{delta:,.2f} € verrattuna edullisimpaan {cheapest['Levykoko']})."
        )
    elif cheapest is not None:
        st.success(
            f"Edullisin: **{cheapest['Levykoko']}** — "
            f"{cheapest['Tarvittavat levyt']} levyä, "
            f"käyttöaste {cheapest['Käyttöaste']}."
        )

    # ── Headline metrics: big "€/kpl" is the visual anchor ──────────────────
    avg_per_pc = active["_total"] / n_pieces if n_pieces else 0.0
    m_cols = st.columns([2, 1, 1, 1])
    m_cols[0].metric("Materiaalikulu €/kpl (ka.)", f"{avg_per_pc:,.2f} €")
    m_cols[1].metric("Yhteensä €", f"{active['_total']:,.2f}")
    m_cols[2].metric("Levyjä", str(active["Tarvittavat levyt"]))
    m_cols[3].metric("Käyttöaste", active["Käyttöaste"] or "—")

    # ── Per-product unit cost when the group mixes multiple products ────────
    if len(products) >= 2:
        breakdown_rows = []
        bill_rate_ppt = active["_ppt"]
        for prod in products:
            one_kg = piece_weight_kg(prod["width"], prod["height"], thickness_mm, material)
            unit_cost = one_kg * bill_rate_ppt / 1000
            breakdown_rows.append({
                "Tuote":       prod.get("name") or f"#{prod['_global_idx'] + 1}",
                "Mitat (mm)":  f"{prod['width']:g} × {prod['height']:g}",
                "Määrä (kpl)": prod["qty"],
                "kg/kpl":      round(one_kg, 3),
                "€/kpl":       f"{unit_cost:,.2f}",
                "Yhteensä €":  f"{unit_cost * prod['qty']:,.2f}",
            })
        st.caption("Materiaalikulu tuotteittain (sekanestaus):")
        st.dataframe(breakdown_rows, use_container_width=True, hide_index=True)

    # ── Nesting visualisation ────────────────────────────────────────────────
    _render_nesting(
        sheets=active["_sheets"],
        sheet_w=active["_sw"],
        sheet_h=active["_sh"],
        eff_w=active["_eff_w"],
        eff_h=active["_eff_h"],
        products=products,
        rankavali_mm=rankavali_mm,
    )

    _render_breakdown(
        material=material,
        thickness=thickness,
        thickness_mm=thickness_mm,
        margin_pct=margin_pct,
        n_pieces=n_pieces,
        data=active["_breakdown"],
    )

    return active["_total"], active["_ppt"]


# ── Price breakdown ────────────────────────────────────────────────────────────

def _render_breakdown(
    material: str,
    thickness: str,
    thickness_mm: float,
    margin_pct: float,
    n_pieces: int,
    data: dict,
) -> None:
    """Show a step-by-step table of how the cheapest sheet's price was calculated."""
    sw              = data["sw"]
    sh              = data["sh"]
    base_ppt        = data["base_ppt"]
    adjusted_ppt    = data["adjusted_ppt"]
    sheet_weight_kg = data["sheet_weight_kg"]
    sheets_needed   = data["sheets_needed"]
    sheet_kg        = data["sheet_kg"]
    pieces_kg       = data["pieces_kg"]
    billable_kg     = data["billable_kg"]
    total_eur       = data["total_eur"]
    cost_per_pc     = data["cost_per_pc"]

    density_kg_mm3 = density_for_material(material)
    density_g_cm3  = density_kg_mm3 * 1e6
    margin_factor  = 1 + margin_pct / 100

    steps = [
        {
            "Vaihe":    "1. Perushinta (hinnastosta)",
            "Laskenta": f"{material}, {thickness} mm",
            "Arvo":     f"{base_ppt:,.2f} €/tn",
        },
        {
            "Vaihe":    f"2. Lisää kate (+{margin_pct:g}%)",
            "Laskenta": f"{base_ppt:,.2f} × {margin_factor:.4f}",
            "Arvo":     f"{adjusted_ppt:,.2f} €/tn",
        },
        {
            "Vaihe":    "3. Materiaalin tiheys",
            "Laskenta": f"tiheys({material})",
            "Arvo":     f"{density_g_cm3:.2f} g/cm³",
        },
        {
            "Vaihe":    "4. Yhden levyn paino",
            "Laskenta": f"{sw} × {sh} × {thickness_mm:g} mm × {density_g_cm3:.2f} g/cm³",
            "Arvo":     f"{sheet_weight_kg:,.2f} kg",
        },
        {
            "Vaihe":    "5. Tarvittavat levyt (sijoittelusta)",
            "Laskenta": f"{n_pieces} kpl sijoitettu {_fmt_m(sw)} × {_fmt_m(sh)} m levylle",
            "Arvo":     f"{sheets_needed}",
        },
        {
            "Vaihe":    "6. Levyjen kokonaispaino",
            "Laskenta": f"{sheet_weight_kg:,.2f} × {sheets_needed}",
            "Arvo":     f"{sheet_kg:,.2f} kg",
        },
        {
            "Vaihe":    "7. Kappaleiden kokonaispaino",
            "Laskenta": "Σ (l × k × p × tiheys × määrä)",
            "Arvo":     f"{pieces_kg:,.2f} kg",
        },
        {
            "Vaihe":    "8. Laskutettava paino (koko levy)",
            "Laskenta": "levy kg",
            "Arvo":     f"{billable_kg:,.2f} kg",
        },
        {
            "Vaihe":    "9. Kokonaiskustannus",
            "Laskenta": f"{adjusted_ppt:,.2f} €/tn × {billable_kg:,.2f} kg / 1000",
            "Arvo":     f"{total_eur:,.2f} €",
        },
    ]
    if n_pieces and isinstance(cost_per_pc, (int, float)):
        steps.append({
            "Vaihe":    "10. Kustannus per kappale",
            "Laskenta": f"{total_eur:,.2f} € / {n_pieces} kpl",
            "Arvo":     f"{cost_per_pc:,.2f} €/kpl",
        })

    with st.expander("Näytä laskennan erittely"):
        st.dataframe(steps, use_container_width=True, hide_index=True)
        if pieces_kg:
            effective_ppt = adjusted_ppt * (sheet_kg / pieces_kg)
            st.caption(
                f"{sheet_kg:,.2f} kg levyä laskutetaan {pieces_kg:,.2f} kg "
                f"todellisille kappaleille. Kappaleyhteenveto-taulukko jakaa "
                f"tämän takaisin kappaleille painon mukaan käyttäen efektiivistä "
                f"hintaa {adjusted_ppt:,.2f} × ({sheet_kg:,.2f} / {pieces_kg:,.2f}) "
                f"= **{effective_ppt:,.2f} €/tn**."
            )


# ── Nesting visualisation ─────────────────────────────────────────────────────

# Distinct, accessible colors per product index. Cycles for >10 products.
_PRODUCT_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#6366f1",
]


def _render_nesting(
    sheets: list,
    sheet_w: int,
    sheet_h: int,
    eff_w: int,
    eff_h: int,
    products: list[dict],
    rankavali_mm: int,
) -> None:
    """Render an SVG layout of every piece on every sheet for the active size."""
    if not sheets:
        return

    st.markdown(f"**Sijoittelu** — {len(sheets)} levyä")

    # Legend: which color = which product. DXF parts show their filename;
    # manually entered products show "Tuote #N".
    legend_bits = []
    for prod in products:
        color = _PRODUCT_PALETTE[prod["_global_idx"] % len(_PRODUCT_PALETTE)]
        dims = f"{prod['width']:g}×{prod['height']:g}"
        name = prod.get("name")
        label = f"{name} ({dims})" if name else f"Tuote #{prod['_global_idx'] + 1} ({dims})"
        legend_bits.append(
            f'<span style="display:inline-flex;align-items:center;'
            f'margin-right:14px;font-size:13px;">'
            f'<span style="display:inline-block;width:14px;height:14px;'
            f'background:{color};opacity:0.55;border:2px solid {color};'
            f'margin-right:6px;border-radius:2px;"></span>'
            f"{label}</span>"
        )
    st.markdown(
        f'<div style="margin-bottom:8px;">{"".join(legend_bits)}</div>',
        unsafe_allow_html=True,
    )

    # Render up to four sheets per row to use horizontal space without dwarfing them.
    target_px = 460
    scale     = target_px / max(sheet_w, sheet_h)
    sw_px     = sheet_w * scale
    sh_px     = sheet_h * scale

    cols_per_row = min(4, len(sheets))
    for row_start in range(0, len(sheets), cols_per_row):
        row = sheets[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col_idx, sheet in enumerate(row):
            sheet_no = row_start + col_idx + 1
            with cols[col_idx]:
                st.markdown(
                    f"**Levy {sheet_no}** · käyttöaste "
                    f"{sheet.utilization * 100:.1f} %"
                )
                st.markdown(
                    _sheet_svg(sheet, sheet_w, sheet_h, eff_w, eff_h,
                               sw_px, sh_px, products, rankavali_mm),
                    unsafe_allow_html=True,
                )


def _sheet_svg(
    sheet,
    sheet_w: int,
    sheet_h: int,
    eff_w: int,
    eff_h: int,
    px_w: float,
    px_h: float,
    products: list[dict],
    rankavali_mm: int,
) -> str:
    """Build an SVG string for one sheet, with each placement labelled.

    A placement whose product carries a real "_polylines" outline is drawn as
    that outline (holes rendered via even-odd fill); everything else is drawn as
    a plain rectangle — so the manual calculator and the DXF section share this
    exact renderer.
    """
    parts = [
        f'<svg width="{px_w:.0f}" height="{px_h:.0f}" '
        f'viewBox="0 0 {sheet_w} {sheet_h}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:2px solid #475569;'
        f'border-radius:4px;display:block;max-width:100%;height:auto;">',
    ]

    # Greyed-out claw strip area (bought but unusable).
    if eff_w < sheet_w:
        parts.append(
            f'<rect x="{eff_w}" y="0" width="{sheet_w - eff_w}" '
            f'height="{sheet_h}" fill="#cbd5e1" fill-opacity="0.5"/>'
        )
    if eff_h < sheet_h:
        parts.append(
            f'<rect x="0" y="{eff_h}" width="{sheet_w}" '
            f'height="{sheet_h - eff_h}" fill="#cbd5e1" fill-opacity="0.5"/>'
        )

    for placement in sheet.placements:
        prod = products[placement.product_idx]
        global_idx = prod["_global_idx"]
        color = _PRODUCT_PALETTE[global_idx % len(_PRODUCT_PALETTE)]

        # Strip the rankaväli inflation so the drawn footprint matches the real
        # piece rather than the padded packing cell.
        gap = rankavali_mm
        dw = max(1, placement.w - gap)
        dh = max(1, placement.h - gap)

        # Display dims in input-space (un-rotated), so the label always reads
        # leveys × korkeus from the user's perspective.
        orig_w = prod["width"]
        orig_h = prod["height"]

        polylines = prod.get("_polylines")
        if polylines:
            parts.append(
                _shape_path(polylines, placement, orig_w, orig_h, color)
            )
        else:
            parts.append(
                f'<rect x="{placement.x}" y="{placement.y}" '
                f'width="{dw}" height="{dh}" '
                f'fill="{color}" fill-opacity="0.55" '
                f'stroke="{color}" stroke-width="6"/>'
            )

        cx = placement.x + dw / 2
        cy = placement.y + dh / 2
        # Font size scales with the smaller piece dimension so labels fit.
        font_size = max(40.0, min(dw, dh) / 5.5)
        parts.append(
            f'<text x="{cx}" y="{cy - font_size * 0.1}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="sans-serif" font-size="{font_size:.0f}" '
            f'font-weight="700" fill="#0f172a">#{global_idx + 1}</text>'
        )
        parts.append(
            f'<text x="{cx}" y="{cy + font_size}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-family="sans-serif" font-size="{font_size * 0.65:.0f}" '
            f'fill="#0f172a">{orig_w:g}×{orig_h:g}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _shape_path(
    polylines: list[list[tuple[float, float]]],
    placement,
    part_w: float,
    part_h: float,
    color: str,
) -> str:
    """Draw a DXF part's real outline inside its placement box.

    The part's polylines live in a 0..part_w / 0..part_h millimetre space with
    the CAD convention of Y pointing up; SVG has Y pointing down, so we flip Y.
    When the packer rotated the piece 90°, we rotate the outline to match.
    """
    ox, oy = placement.x, placement.y
    rotated = placement.rotated

    def tx(lx: float, ly: float) -> tuple[float, float]:
        if not rotated:
            return ox + lx, oy + (part_h - ly)
        # 90° turn: the placement footprint is part_h wide by part_w tall.
        return ox + ly, oy + lx

    segments = []
    for poly in polylines:
        if len(poly) < 2:
            continue
        pts = [tx(lx, ly) for lx, ly in poly]
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts) + " Z"
        segments.append(d)

    if not segments:
        return ""

    return (
        f'<path d="{" ".join(segments)}" '
        f'fill="{color}" fill-opacity="0.55" fill-rule="evenodd" '
        f'stroke="{color}" stroke-width="6" stroke-linejoin="round"/>'
    )
