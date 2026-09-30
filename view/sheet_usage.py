"""The "sheet usage" section of both tabs: for one material + thickness group,
the chosen size's layout (drawn by the caller, with the parts' names and
colours) and its metrics first, then the priced sheet sizes to pick from and
the step-by-step price breakdown. A size can have two rows: the fewest sheets
and the fewest programs (see ``core.programs``)."""

import pandas as pd
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
    note=None,
) -> tuple[float | None, float | None]:
    """Render one group's sheet-usage table. Returns ``(total_eur, bill_rate_ppt)``.

    ``draw_layout(option)`` draws the chosen sheet size's layout; ``note()``,
    if given, draws extra notes first. Returns ``(None, None)`` when no priced
    sheet size can fulfil the order.
    """
    if note is not None:
        note()

    if result is None:
        _render_unpriced(material)
        return None, None

    options = result.options
    cheapest_idx = cheapest_index(options)

    # The layout and metrics go above the table but depend on the row picked
    # in it: reserve their place, draw the table, then fill it.
    answer = st.container()
    if len(options) > 1:
        st.caption("Valitse rivi vaihtaaksesi levykokoa.")
    event = st.dataframe(
        _styled_table(_table_rows(options, result.n_pieces, cheapest_idx), cheapest_idx),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )

    if cheapest_idx is None:
        _render_no_fit(options)
        return None, None

    active = options[_selected_index(event, options, cheapest_idx)]
    with answer:
        draw_layout(active)
        _render_summary(active, result.n_pieces)

    _render_breakdown(material, thickness, thickness_mm, margin_pct, result, active)
    return active.total_eur, active.bill_rate_ppt


def _render_unpriced(material: str) -> None:
    if material == COPPER_MATERIAL:
        st.info(
            "Aseta kuparin hinta (€/kg) sivupalkista, niin levylaskenta "
            "tulee näkyviin."
        )
    else:
        st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")


def _render_no_fit(options: list[SheetOption]) -> None:
    st.warning("Osat eivät mahtuneet millekään hinnoitellulle levykoolle.")
    # Show why each size failed — a Sparrow error (e.g. a broken binary)
    # otherwise looks exactly like "the parts are too big".
    reasons = {o.reason for o in options if o.reason}
    if reasons:
        st.caption("Syy: " + " · ".join(sorted(reasons)))


def _selected_index(event, options: list[SheetOption], cheapest_idx: int) -> int:
    """The clicked row if the pieces fit on it, else the cheapest."""
    sel = list(getattr(event.selection, "rows", []) or [])
    if not sel:
        return cheapest_idx
    idx = sel[0]
    if 0 <= idx < len(options) and options[idx].ok:
        return idx
    st.warning(
        f"**{_size_label(options[idx])}** on liian pieni — osat eivät mahdu. "
        "Käytetään edullisinta levykokoa."
    )
    return cheapest_idx


def _render_summary(active: SheetOption, n_pieces: int) -> None:
    """Headline metrics for the chosen size — big "€/kpl" is the visual anchor."""
    m = st.columns([2, 1, 1, 1, 1])
    m[0].metric("Materiaalikulu €/kpl (ka.)", f"{active.total_eur / n_pieces:,.2f} €")
    m[1].metric("Yhteensä €", f"{active.total_eur:,.2f}")
    m[2].metric("Levyjä", str(active.sheets_needed))
    m[3].metric("Ohjelmia", str(active.programs), help=_PROGRAMS_HELP)
    m[4].metric("Käyttöaste", f"{active.utilization * 100:.1f} %")


_PROGRAMS_HELP = (
    "Erilaisten levyjen määrä eli leikkausohjelmat, jotka suunnittelija tekee. "
    "Tuotanto ajaa saman ohjelman niin monta kertaa kuin levyjä tarvitaan."
)


def _size_label(o: SheetOption) -> str:
    return f"{fmt_m(o.sw)} × {fmt_m(o.sh)} m"


def _table_rows(options: list[SheetOption], n_pieces: int, cheapest_idx: int | None) -> list[dict]:
    """The sheet-size table; a size the pieces don't fit keeps only its price."""
    rows = []
    for i, o in enumerate(options):
        row = {
            "Levykoko":          _size_label(o),
            "Hinta (€/tn)":      f"{o.adjusted_ppt:,.2f}",
            # None, not "": a number column with a text cell can't be sent
            # to the browser as Arrow and Streamlit logs a traceback.
            "Tarvittavat levyt": None,
            "Ohjelmia":          None,
            "Käyttöaste":        "",
            "Levyn kg":          None,
            "Yhteensä €":        None,
            "€/kpl":             None,
            "Paras":             "ei mahdu",
        }
        if o.ok:
            row.update({
                "Tarvittavat levyt": o.sheets_needed,
                "Ohjelmia":          o.programs,
                "Käyttöaste":        f"{o.utilization * 100:.1f} %",
                "Levyn kg":          round(o.sheet_kg, 2),
                "Yhteensä €":        round(o.total_eur, 2),
                "€/kpl":             round(o.total_eur / n_pieces, 2),
                "Paras":             "◀ edullisin" if i == cheapest_idx else "",
            })
        rows.append(row)
    return rows


# Light enough to read in both themes; the "Paras" text says the same for
# anyone who can't tell the colour apart.
_CHEAPEST_ROW = "background-color: rgba(34, 197, 94, 0.22)"

# A Styler replaces Streamlit's own number display, so every number column
# gets its format here ("" for a size the pieces don't fit).
_NUMBER_FORMATS = {
    "Tarvittavat levyt": "{:.0f}",
    "Ohjelmia":          "{:.0f}",
    "Levyn kg":          "{:,.2f}",
    "Yhteensä €":        "{:,.2f}",
    "€/kpl":             "{:,.2f}",
}


def _styled_table(rows: list[dict], cheapest_idx: int | None):
    """The sheet-size table with the cheapest row coloured green."""
    def colour(row):
        return [_CHEAPEST_ROW if row.name == cheapest_idx else ""] * len(row)

    return (pd.DataFrame(rows).style
            .apply(colour, axis=1)
            .format(_NUMBER_FORMATS, na_rep=""))


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
         f"{n_pieces} kpl sijoitettu {_size_label(o)} levylle, "
         f"{o.programs} ohjelmalla",
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
                f"todellisille kappaleille. Sivun lopun taulukko jakaa "
                f"tämän takaisin kappaleille painon mukaan käyttäen efektiivistä "
                f"hintaa {o.adjusted_ppt:,.2f} × ({o.sheet_kg:,.2f} / {pieces_kg:,.2f}) "
                f"= **{o.bill_rate_ppt:,.2f} €/tn**."
            )
