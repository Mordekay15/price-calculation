"""The "sheet usage" section of both tabs: for one material + thickness group,
the priced sheet sizes, the chosen size's metrics, its layout (drawn by the
caller) and the step-by-step price breakdown."""

import streamlit as st

from core.pricing import COPPER_MATERIAL, density_for_material
from core.sheet_cost import GroupCost, SheetOption, cheapest_index, fmt_m


def render_group(
    material: str,
    thickness: str,
    thickness_mm: float,
    result: GroupCost | None,
    *,
    margin_pct: float,
    key: str,
    draw_layout,
) -> tuple[float | None, float | None]:
    """Render one group's sheet-usage table. Returns ``(total_eur, bill_rate_ppt)``.

    ``draw_layout(option)`` draws the chosen sheet size's layout. Returns
    ``(None, None)`` when no priced sheet size can fulfil the order.
    """
    st.markdown(f"**{material}** · **{thickness} mm**")

    if result is None:
        if material == COPPER_MATERIAL:
            st.info(
                "Aseta kuparin hinta (€/kg) sivupalkista, niin levylaskenta "
                "tulee näkyviin."
            )
        else:
            st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")
        return None, None

    options = result.options
    n_pieces = result.n_pieces
    cheapest_idx = cheapest_index(options)

    event = st.dataframe(
        _table_rows(options, n_pieces, cheapest_idx),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )

    if cheapest_idx is None:
        st.warning("Osat eivät mahtuneet millekään hinnoitellulle levykoolle.")
        # Show why each size failed — a Sparrow error (e.g. a broken binary)
        # otherwise looks exactly like "the parts are too big".
        reasons = {o.reason for o in options if o.reason}
        if reasons:
            st.caption("Syy: " + " · ".join(sorted(reasons)))
        return None, None

    # Default to the cheapest; let the user click a (valid) row to override.
    selected_idx = cheapest_idx
    sel = list(getattr(event.selection, "rows", []) or [])
    if sel:
        idx = sel[0]
        if 0 <= idx < len(options) and options[idx].ok:
            selected_idx = idx
        else:
            st.warning(
                f"**{_size_label(options[idx])}** on liian pieni — osat eivät mahdu. "
                "Käytetään edullisinta levykokoa."
            )

    active = options[selected_idx]
    cheapest = options[cheapest_idx]
    if selected_idx != cheapest_idx:
        delta = active.total_eur - cheapest.total_eur
        st.info(
            f"Valittu: **{_size_label(active)}** — {active.sheets_needed} "
            f"levyä ({delta:+,.2f} € verrattuna edullisimpaan "
            f"{_size_label(cheapest)})."
        )
    else:
        st.success(
            f"Edullisin: **{_size_label(active)}** — {active.sheets_needed} "
            f"levyä, käyttöaste {active.utilization * 100:.1f} %."
        )

    # ── Headline metrics: big "€/kpl" is the visual anchor ──────────────────
    m = st.columns([2, 1, 1, 1])
    m[0].metric("Materiaalikulu €/kpl (ka.)", f"{active.total_eur / n_pieces:,.2f} €")
    m[1].metric("Yhteensä €", f"{active.total_eur:,.2f}")
    m[2].metric("Levyjä", str(active.sheets_needed))
    m[3].metric("Käyttöaste", f"{active.utilization * 100:.1f} %")

    draw_layout(active)

    _render_breakdown(material, thickness, thickness_mm, margin_pct, result, active)
    return active.total_eur, active.bill_rate_ppt


def _size_label(o: SheetOption) -> str:
    return f"{fmt_m(o.sw)} × {fmt_m(o.sh)} m"


def _table_rows(options: list[SheetOption], n_pieces: int, cheapest_idx: int | None) -> list[dict]:
    """The sheet-size table; a size the pieces don't fit keeps only its price."""
    rows = []
    for i, o in enumerate(options):
        row = {
            "Levykoko":          _size_label(o),
            "Hinta (€/tn)":      f"{o.adjusted_ppt:,.2f}",
            "Tarvittavat levyt": "",
            "Käyttöaste":        "",
            "Levyn kg":          "",
            "Yhteensä €":        "",
            "€/kpl":             "",
            "Paras":             "🚫",
        }
        if o.ok:
            row.update({
                "Tarvittavat levyt": o.sheets_needed,
                "Käyttöaste":        f"{o.utilization * 100:.1f} %",
                "Levyn kg":          round(o.sheet_kg, 2),
                "Yhteensä €":        round(o.total_eur, 2),
                "€/kpl":             round(o.total_eur / n_pieces, 2),
                "Paras":             "◀ edullisin" if i == cheapest_idx else "",
            })
        rows.append(row)
    return rows


def _render_breakdown(
    material: str,
    thickness: str,
    thickness_mm: float,
    margin_pct: float,
    result: GroupCost,
    o: SheetOption,
) -> None:
    """Show a step-by-step table of how the chosen sheet's price was calculated."""
    n_pieces = result.n_pieces
    pieces_kg = result.pieces_kg
    cost_per_pc = round(o.total_eur / n_pieces, 2)
    density_g_cm3 = density_for_material(material) * 1e6
    margin_factor = 1 + margin_pct / 100

    steps = [
        ("1. Perushinta (hinnastosta)",
         f"{material}, {thickness} mm",
         f"{o.base_ppt:,.2f} €/tn"),
        (f"2. Lisää kate (+{margin_pct:g}%)",
         f"{o.base_ppt:,.2f} × {margin_factor:.4f}",
         f"{o.adjusted_ppt:,.2f} €/tn"),
        ("3. Materiaalin tiheys",
         f"tiheys({material})",
         f"{density_g_cm3:.2f} g/cm³"),
        ("4. Yhden levyn paino",
         f"{o.sw} × {o.sh} × {thickness_mm:g} mm × {density_g_cm3:.2f} g/cm³",
         f"{o.sheet_weight_kg:,.2f} kg"),
        ("5. Tarvittavat levyt (sijoittelusta)",
         f"{n_pieces} kpl sijoitettu {_size_label(o)} levylle",
         f"{o.sheets_needed}"),
        ("6. Levyjen kokonaispaino",
         f"{o.sheet_weight_kg:,.2f} × {o.sheets_needed}",
         f"{o.sheet_kg:,.2f} kg"),
        ("7. Kappaleiden kokonaispaino",
         "Σ (pinta-ala × p × tiheys × määrä)",
         f"{pieces_kg:,.2f} kg"),
        ("8. Kokonaiskustannus",
         f"{o.adjusted_ppt:,.2f} €/tn × {o.sheet_kg:,.2f} kg / 1000",
         f"{o.total_eur:,.2f} €"),
        ("9. Kustannus per kappale",
         f"{o.total_eur:,.2f} € / {n_pieces} kpl",
         f"{cost_per_pc:,.2f} €/kpl"),
    ]

    with st.expander("Näytä laskennan erittely"):
        st.dataframe(
            [{"Vaihe": v, "Laskenta": calc, "Arvo": val} for v, calc, val in steps],
            width="stretch", hide_index=True,
        )
        if pieces_kg:
            st.caption(
                f"{o.sheet_kg:,.2f} kg levyä laskutetaan {pieces_kg:,.2f} kg "
                f"todellisille kappaleille. Kappaleyhteenveto-taulukko jakaa "
                f"tämän takaisin kappaleille painon mukaan käyttäen efektiivistä "
                f"hintaa {o.adjusted_ppt:,.2f} × ({o.sheet_kg:,.2f} / {pieces_kg:,.2f}) "
                f"= **{o.bill_rate_ppt:,.2f} €/tn**."
            )
