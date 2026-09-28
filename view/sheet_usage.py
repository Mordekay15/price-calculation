"""
view/sheet_usage.py
===================
The "sheet usage" section, shared by both tabs.

For one material + thickness group, ``render_group`` shows the priced sheet
sizes (from ``core.sheet_cost.compute_options``), lets the user click a row to
override the cheapest pick, and shows the headline metrics, the nesting layout
and the step-by-step price breakdown.

Only the layout drawing differs per tab, so the caller passes it in:

  * ``draw_rect_layout``    — manual calculator, pieces as labelled rectangles
  * ``draw_sparrow_layout`` — DXF tab, Sparrow's real shapes (with holes)
"""

import streamlit as st

from core.calculator import density_for_material, piece_weight_kg
from core.copper import COPPER_MATERIAL
from core.sheet_cost import cheapest_index, fmt_m, utilization


def render_group(
    material: str,
    thickness: str,
    thickness_mm: float,
    result: dict,
    *,
    margin_pct: float,
    key: str,
    draw_layout,
) -> tuple[float | None, float | None]:
    """Render one group's sheet-usage table. Returns ``(total_eur, ppt)``.

    ``draw_layout(active_row)`` draws the chosen sheet size's layout. Returns
    ``(None, None)`` when no priced sheet size can fulfil the order.
    """
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

    event = st.dataframe(
        [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )

    if cheapest_idx is None:
        st.warning("Osat eivät mahtuneet millekään hinnoitellulle levykoolle.")
        # Show why each size failed — a Sparrow error (e.g. a broken binary)
        # otherwise looks exactly like "the parts are too big".
        reasons = {r.get("_reason") for r in rows if r.get("_reason")}
        if reasons:
            st.caption("Syy: " + " · ".join(sorted(reasons)))
        return None, None

    # Default to the cheapest; let the user click a (valid) row to override.
    selected_idx = cheapest_idx
    sel = list(getattr(event.selection, "rows", []) or [])
    if sel:
        idx = sel[0]
        if 0 <= idx < len(rows) and rows[idx]["_failed"] == 0:
            selected_idx = idx
        else:
            st.warning(
                f"**{rows[idx]['Levykoko']}** on liian pieni — osat eivät mahdu. "
                "Käytetään edullisinta levykokoa."
            )

    active = rows[selected_idx]
    cheapest = rows[cheapest_idx]
    if selected_idx != cheapest_idx:
        delta = active["_total"] - cheapest["_total"]
        st.info(
            f"Valittu: **{active['Levykoko']}** — {active['Tarvittavat levyt']} "
            f"levyä ({delta:+,.2f} € verrattuna edullisimpaan "
            f"{cheapest['Levykoko']})."
        )
    else:
        st.success(
            f"Edullisin: **{active['Levykoko']}** — {active['Tarvittavat levyt']} "
            f"levyä, käyttöaste {active['Käyttöaste']}."
        )

    # ── Headline metrics: big "€/kpl" is the visual anchor ──────────────────
    m = st.columns([2, 1, 1, 1])
    m[0].metric("Materiaalikulu €/kpl (ka.)", f"{active['_total'] / n_pieces:,.2f} €")
    m[1].metric("Yhteensä €", f"{active['_total']:,.2f}")
    m[2].metric("Levyjä", str(active["Tarvittavat levyt"]))
    m[3].metric("Käyttöaste", active["Käyttöaste"] or "—")

    draw_layout(active)

    _render_breakdown(material, thickness, thickness_mm, margin_pct, n_pieces,
                      active["_breakdown"])
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
    """Show a step-by-step table of how the chosen sheet's price was calculated."""
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

    density_g_cm3 = density_for_material(material) * 1e6
    margin_factor = 1 + margin_pct / 100

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
            "Laskenta": f"{n_pieces} kpl sijoitettu {fmt_m(sw)} × {fmt_m(sh)} m levylle",
            "Arvo":     f"{sheets_needed}",
        },
        {
            "Vaihe":    "6. Levyjen kokonaispaino",
            "Laskenta": f"{sheet_weight_kg:,.2f} × {sheets_needed}",
            "Arvo":     f"{sheet_kg:,.2f} kg",
        },
        {
            "Vaihe":    "7. Kappaleiden kokonaispaino",
            "Laskenta": "Σ (pinta-ala × p × tiheys × määrä)",
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
        {
            "Vaihe":    "10. Kustannus per kappale",
            "Laskenta": f"{total_eur:,.2f} € / {n_pieces} kpl",
            "Arvo":     f"{cost_per_pc:,.2f} €/kpl",
        },
    ]

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


# ── Layout drawing: shared pieces ─────────────────────────────────────────────

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

def draw_rect_layout(active: dict, products: list[dict], rankavali_mm: int) -> None:
    """Every sheet of the chosen size, each piece a labelled rectangle.

    ``products`` is the group's product list (placements index into it); each
    product's ``_global_idx`` picks its colour and ``#N`` label.
    """
    sheets = active["_sheets"]
    sw, sh = active["_sw"], active["_sh"]
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
            _rect_sheet_svg(sheet, sw, sh, active["_eff_w"], active["_eff_h"],
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


def render_mix_costs(active: dict, products: list[dict], thickness_mm: float,
                     material: str) -> None:
    """Per-product unit cost when several products share the sheets."""
    if len(products) < 2:
        return
    rows = []
    for prod in products:
        one_kg = piece_weight_kg(prod["width"], prod["height"], thickness_mm, material)
        unit_cost = one_kg * active["_ppt"] / 1000
        rows.append({
            "Tuote":       prod.get("name") or f"#{prod['_global_idx'] + 1}",
            "Mitat (mm)":  f"{prod['width']:g} × {prod['height']:g}",
            "Määrä (kpl)": prod["qty"],
            "kg/kpl":      round(one_kg, 3),
            "€/kpl":       f"{unit_cost:,.2f}",
            "Yhteensä €":  f"{unit_cost * prod['qty']:,.2f}",
        })
    st.caption("Materiaalikulu tuotteittain (sekanestaus):")
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ── DXF tab: Sparrow's real shapes ────────────────────────────────────────────

def draw_sparrow_layout(active: dict, parts: list, key: str) -> None:
    """One SVG per distinct sheet layout for the chosen size.

    Identical sheets are drawn once with a "×N" count and a sheet-number range
    (e.g. "Levy 1–4 · ×4"). The "turn" toggle shows the *other* orientation's
    real re-nest when Sparrow made one; otherwise (a square sheet, or parts free
    to turn 90°) it just rotates the picture. The price never changes.
    """
    alt = active.get("_alt")
    rotate = st.checkbox(
        "Käännä levy 90°",
        value=False,
        key=f"dxf_su_rot::{key}",
        help="Näyttää Sparrown asettelun käännetylle levylle (sama levykoko ja "
             "hinta, eri sijoittelu). Neliölevyllä tai kun osat saa kääntää "
             "90°, vain kuva kääntyy.",
    )
    shown = alt if (rotate and alt) else active
    if shown is alt:
        st.caption(
            f"Käännetty levy — Sparrow laski asettelun uudelleen: käyttöaste "
            f"{alt['utilization'] * 100:.1f} %, {alt['sheets_needed']} levyä."
        )
    turn_picture = rotate and not alt  # no re-nest: rotate the picture only

    sheets = shown["_sheets"]
    sw, sh = shown["_sw"], shown["_sh"]
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
            _shape_sheet_svg(sheet, sw, sh, shown["_eff_w"], shown["_eff_h"],
                             scale, turn_picture),
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
        for cline in getattr(pl, "construction", []):
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
