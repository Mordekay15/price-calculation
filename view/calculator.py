"""
view/calculator.py
==================
Price calculator tab.

Flow:
  1. User adds products. For each product they pick material, thickness,
     width × height (mm) and quantity.
  2. The "Sheet usage" section groups pieces by (material, thickness) and
     for each group compares every sheet size that has a price — sheets
     needed, utilisation, total cost — and highlights the cheapest option.
  3. The "Pieces summary" at the bottom lists every product with its
     per-piece and batch weight, plus the grand total.
"""

import uuid

import streamlit as st
from core.calculator import (
    build_lookup,
    calculate,
    density_for_material,
    get_materials,
    get_sizes_for_material,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.nesting import expand_products, pack, parse_size, summarise

_PLACEHOLDER_MAT   = "— Valitse materiaali —"
_PLACEHOLDER_THICK = "— Valitse paksuus —"


def _new_product() -> dict:
    return {
        "id":        uuid.uuid4().hex,
        "material":  None,
        "thickness": None,
        "width":     0.0,
        "height":    0.0,
        "qty":       1,
    }


def _init_products() -> None:
    if "calc_products" not in st.session_state:
        st.session_state.calc_products = [_new_product()]


def render(data: dict) -> None:
    lookup = build_lookup(data)

    if not lookup:
        st.info("Hintatietoja ei saatavilla.")
        return

    st.subheader("Hintalaskuri")
    materials = get_materials(lookup)

    margin_pct = st.number_input(
        "Materiaalin kate (%)",
        min_value=10.0,
        max_value=20.0,
        value=15.0,
        step=0.5,
        key="calc_margin_pct",
    )

    charge_mode = st.radio(
        "Veloitus asiakkaalta",
        ["Tilattu määrä", "Koko levy"],
        horizontal=True,
        key="calc_charge_mode",
        help=(
            "Tilattu määrä: laskuta vain toimitetut kappaleet (ei hukkaa). "
            "Koko levy: laskuta koko levy, hukkapalat mukaan lukien."
        ),
    )
    charge_full_sheet = charge_mode == "Koko levy"

    _init_products()

    # ── Products ──────────────────────────────────────────────────────────────

    st.markdown("**Tuotteet**")

    to_delete = None
    for i, prod in enumerate(st.session_state.calc_products):
        pid = prod["id"]
        with st.container(border=True):
            hdr_cols = st.columns([6, 1])
            hdr_cols[0].markdown(f"**Tuote {i + 1}**")
            if len(st.session_state.calc_products) > 1:
                if hdr_cols[1].button("Poista", key=f"del_{pid}"):
                    to_delete = i

            mat_opts = [_PLACEHOLDER_MAT] + materials
            mat_default = prod["material"] if prod["material"] in materials else _PLACEHOLDER_MAT
            mat_raw = st.selectbox(
                "Materiaali",
                mat_opts,
                index=mat_opts.index(mat_default),
                key=f"mat_{pid}",
            )
            material = mat_raw if mat_raw != _PLACEHOLDER_MAT else None

            if material is not None:
                thicknesses = get_thicknesses_for_material(lookup, material)
            else:
                thicknesses = []

            if thicknesses:
                th_opts = [_PLACEHOLDER_THICK] + thicknesses
                th_default = prod["thickness"] if prod["thickness"] in thicknesses else _PLACEHOLDER_THICK
                th_raw = st.selectbox(
                    "Paksuus (mm)",
                    th_opts,
                    index=th_opts.index(th_default),
                    key=f"th_{pid}",
                )
                thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
            else:
                st.selectbox("Paksuus (mm)", [_PLACEHOLDER_THICK], index=0, disabled=True, key=f"th_{pid}_disabled")
                thickness = None

            inp_cols = st.columns(3)
            w = inp_cols[0].number_input(
                "Leveys (mm)", min_value=0.0, value=float(prod["width"]),
                step=10.0, key=f"w_{pid}",
            )
            h = inp_cols[1].number_input(
                "Korkeus (mm)", min_value=0.0, value=float(prod["height"]),
                step=10.0, key=f"h_{pid}",
            )
            q = inp_cols[2].number_input(
                "Määrä (kpl)", min_value=1, value=int(prod["qty"]),
                step=1, key=f"q_{pid}",
            )

            prod["material"]  = material
            prod["thickness"] = thickness
            prod["width"]     = w
            prod["height"]    = h
            prod["qty"]       = q

    if st.button("+ Lisää tuote"):
        st.session_state.calc_products.append(_new_product())
        st.rerun()

    if to_delete is not None:
        st.session_state.calc_products.pop(to_delete)
        st.rerun()

    rankavali_mm = int(st.number_input(
        "Rankaväli (mm)",
        min_value=0,
        value=0,
        step=1,
        key="calc_rankavali_mm",
        help=(
            "Kappaleiden välinen rankaväli (leikkausvara). Lisätään jokaisen "
            "kappaleen leveyteen ja korkeuteen sijoittelussa, jotta vierekkäiset "
            "kappaleet pysyvät tämän etäisyyden päässä toisistaan."
        ),
    ))

    long_side_clamp_mm = int(st.number_input(
        "Pitkän sivun kynsirainan leveys (mm)",
        min_value=0,
        value=0,
        step=1,
        key="calc_long_side_clamp_mm",
        help=(
            "Kynsiraina on levyn pitkän sivun reunavyöhyke, johon koneen kynnet "
            "tarttuvat — aluetta ei voi käyttää kappaleiden sijoitteluun. "
            "Levy ostetaan silti täysikokoisena, joten paino ja hinta lasketaan "
            "bruttomitoista."
        ),
    ))

    # ── Sheet usage (per material + thickness group) ─────────────────────────

    products = st.session_state.calc_products
    groups: dict[tuple[str, str], list[dict]] = {}
    for prod in products:
        if not prod["material"] or not prod["thickness"]:
            continue
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        groups.setdefault((prod["material"], prod["thickness"]), []).append(prod)

    if groups:
        st.divider()
        st.markdown("**Levyn käyttö** — mikä levykoko on edullisin")

        grand_total_eur  = 0.0
        any_priced       = False
        cheapest_prices: dict[tuple[str, str], float] = {}
        for (material, thickness), group_prods in groups.items():
            cheapest_eur, cheapest_ppt = _render_sheet_usage_group(
                lookup=lookup,
                material=material,
                thickness=thickness,
                products=group_prods,
                margin_pct=margin_pct,
                charge_full_sheet=charge_full_sheet,
                long_side_clamp_mm=long_side_clamp_mm,
                rankavali_mm=rankavali_mm,
            )
            if cheapest_eur is not None:
                grand_total_eur += cheapest_eur
                any_priced = True
            if cheapest_ppt is not None:
                cheapest_prices[(material, thickness)] = cheapest_ppt

        if any_priced and len(groups) > 1:
            st.divider()
            st.metric("Yhdistetty edullisin yhteissumma (€)", f"{grand_total_eur:,.2f}")

    # ── Pieces summary (bottom of page) ──────────────────────────────────────

    table_rows      = []
    total_weight_kg = 0.0
    total_cost_eur  = 0.0
    for i, prod in enumerate(products):
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        thickness_mm = parse_thickness_mm(prod["thickness"]) if prod["thickness"] else None
        if thickness_mm is None:
            continue
        one_weight   = piece_weight_kg(prod["width"], prod["height"], thickness_mm, prod["material"])
        batch_weight = one_weight * prod["qty"]
        total_weight_kg += batch_weight

        ppt          = cheapest_prices.get((prod["material"], prod["thickness"]))
        price_per_kg = ppt / 1000 if ppt else None
        one_cost     = round(one_weight   * price_per_kg, 4) if price_per_kg else ""
        batch_cost   = round(batch_weight * price_per_kg, 2) if price_per_kg else ""
        if price_per_kg:
            total_cost_eur += batch_weight * price_per_kg

        table_rows.append({
            "#":              i + 1,
            "Materiaali":     prod["material"] or "",
            "Paksuus":        prod["thickness"] or "",
            "Leveys (mm)":    prod["width"],
            "Korkeus (mm)":   prod["height"],
            "Määrä (kpl)":    prod["qty"],
            "kg/kpl":         round(one_weight,   3),
            "Yhteensä kg":    round(batch_weight, 3),
            "€/kpl":          one_cost,
            "Yhteensä €":     batch_cost,
        })

    if table_rows:
        st.divider()
        st.markdown("**Kappaleyhteenveto**")
        m1, m2 = st.columns(2)
        m1.metric("Kappaleiden yhteispaino (kg)", f"{total_weight_kg:.3f}")
        if total_cost_eur:
            m2.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost_eur:,.2f}")
        if charge_full_sheet:
            st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
        else:
            st.caption("€/kpl käyttää edullisimman saatavilla olevan levykoon hintaa × kappalepaino (ei hukkaa).")
        st.dataframe(table_rows, use_container_width=True, hide_index=True)


# ── Sheet usage comparison ────────────────────────────────────────────────────

def _fmt_m(mm: int) -> str:
    """Format a mm value as metres: 1000 -> '1.0', 1250 -> '1.25', 1500 -> '1.5'."""
    s = f"{mm / 1000:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _render_breakdown(
    material: str,
    thickness: str,
    thickness_mm: float,
    margin_pct: float,
    charge_full_sheet: bool,
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
    mode_label     = "koko levy" if charge_full_sheet else "tilatut kappaleet"

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
            "Vaihe":    f"8. Laskutettava paino ({mode_label})",
            "Laskenta": "levy kg" if charge_full_sheet else "kappaleet kg",
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
        if charge_full_sheet and pieces_kg:
            effective_ppt = adjusted_ppt * (sheet_kg / pieces_kg)
            st.caption(
                f"**Koko levy** -tilassa {sheet_kg:,.2f} kg levyä laskutetaan "
                f"{pieces_kg:,.2f} kg todellisille kappaleille. Kappaleyhteenveto-"
                f"taulukko jakaa tämän takaisin kappaleille painon mukaan "
                f"käyttäen efektiivistä hintaa "
                f"{adjusted_ppt:,.2f} × ({sheet_kg:,.2f} / {pieces_kg:,.2f}) = "
                f"**{effective_ppt:,.2f} €/tn**."
            )


def _render_sheet_usage_group(
    lookup: dict,
    material: str,
    thickness: str,
    products: list[dict],
    margin_pct: float = 0.0,
    charge_full_sheet: bool = False,
    long_side_clamp_mm: int = 0,
    rankavali_mm: int = 0,
) -> tuple[float | None, float | None]:
    """
    Render one sheet-usage table for products sharing the same material and
    thickness. The user can click a row to override the default cheapest pick;
    rows where pieces don't fit are ignored if clicked. Returns the (total_eur,
    price_per_tonne) of the active row, or (None, None) when no priced sheet
    size can fulfil the order.
    """
    pieces = expand_products(products)
    if not pieces:
        return None, None

    # Inflate each piece by the rankaväli so the packer leaves a gap between
    # adjacent pieces. The packer still uses the gross sheet size; the long-
    # side claw strip (below) is applied separately to the sheet dims.
    if rankavali_mm > 0:
        pieces = [
            (p_idx, c_idx, w + rankavali_mm, h + rankavali_mm)
            for p_idx, c_idx, w, h in pieces
        ]

    thickness_mm = parse_thickness_mm(thickness)
    if thickness_mm is None:
        return None, None

    n_pieces = len(pieces)

    candidates: list[tuple[str, int, int, float]] = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))

    st.markdown(f"**{material}** · **{thickness} mm**")
    if not candidates:
        st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")
        return None, None

    pieces_kg = sum(
        piece_weight_kg(p["width"], p["height"], thickness_mm, material) * p["qty"]
        for p in products
    )

    rows = []
    for _size_label, sw, sh, price_per_tonne in candidates:
        # Long-side claw strip eats into the perpendicular (short) dimension.
        if sw >= sh:
            eff_w, eff_h = sw, max(0, sh - long_side_clamp_mm)
        else:
            eff_w, eff_h = max(0, sw - long_side_clamp_mm), sh
        sheets, failed  = pack(pieces, eff_w, eff_h, allow_rotation=True)
        summary         = summarise(sw, sh, sheets, len(failed))
        has_failures    = summary["failed_pieces"] > 0
        sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
        sheet_kg        = sheet_weight_kg * summary["sheets_needed"]
        billable_kg     = sheet_kg if charge_full_sheet else pieces_kg
        result          = calculate(price_per_tonne, billable_kg / 1000, margin_pct=margin_pct)
        adjusted_ppt    = result["after_margin"]
        total_eur       = result["total"]
        cost_per_pc     = round(total_eur / n_pieces, 2) if (not has_failures and n_pieces) else ""
        # Effective rate to apply against piece weight so the pieces-summary
        # totals add up to the sheet-usage total in both charge modes.
        bill_rate_ppt   = adjusted_ppt * (sheet_kg / pieces_kg) if (charge_full_sheet and pieces_kg) else adjusted_ppt
        rows.append({
            "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
            "Tarvittavat levyt": "" if has_failures else summary["sheets_needed"],
            "Käyttöaste":        "" if has_failures else f"{summary['utilization'] * 100:.1f} %",
            "Levyn kg":          "" if has_failures else round(sheet_kg, 2),
            "Laskutettava kg":   "" if has_failures else round(billable_kg, 2),
            "Yhteensä €":        "" if has_failures else round(total_eur, 2),
            "€/kpl":             cost_per_pc,
            "_total":         total_eur,
            "_ppt":           bill_rate_ppt,
            "_failed":        summary["failed_pieces"],
            "_breakdown": {
                "sw":              sw,
                "sh":              sh,
                "base_ppt":        price_per_tonne,
                "adjusted_ppt":    adjusted_ppt,
                "sheet_weight_kg": sheet_weight_kg,
                "sheets_needed":   summary["sheets_needed"],
                "sheet_kg":        sheet_kg,
                "pieces_kg":       pieces_kg,
                "billable_kg":     billable_kg,
                "total_eur":       total_eur,
                "cost_per_pc":     cost_per_pc,
            },
        })

    valid_indices = [i for i, r in enumerate(rows) if r["_failed"] == 0]
    cheapest_idx: int | None = (
        min(valid_indices, key=lambda i: rows[i]["_total"]) if valid_indices else None
    )

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

    select_key = f"sheet_select::{material}::{thickness}"
    st.caption("Klikkaa riviä valitaksesi levykoon. Liian pieniä levyjä (🚫) ei voi valita.")
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
            f"{active['Tarvittavat levyt']} levyä, "
            f"**{active['€/kpl']} €/kpl**, "
            f"yhteensä **{active['Yhteensä €']:,.2f} €** "
            f"(+{delta:,.2f} € verrattuna edullisimpaan {cheapest['Levykoko']})."
        )
    elif cheapest is not None:
        st.success(
            f"Edullisin: **{cheapest['Levykoko']}** — "
            f"{cheapest['Tarvittavat levyt']} levyä, "
            f"**{cheapest['€/kpl']} €/kpl**, "
            f"yhteensä **{cheapest['Yhteensä €']:,.2f} €**, "
            f"käyttöaste {cheapest['Käyttöaste']}."
        )

    _render_breakdown(
        material=material,
        thickness=thickness,
        thickness_mm=thickness_mm,
        margin_pct=margin_pct,
        charge_full_sheet=charge_full_sheet,
        n_pieces=n_pieces,
        data=active["_breakdown"],
    )

    return active["_total"], active["_ppt"]
