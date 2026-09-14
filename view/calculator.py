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
     A "Sijoittelutapa" toggle lets the user switch from this combined
     nesting to per-product nesting where each product gets its own sheet.
  3. The "Pieces summary" at the bottom lists every product with its
     per-piece and batch weight, plus the grand total.
"""

import uuid

import streamlit as st
from core.calculator import (
    build_lookup,
    get_materials,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.copper import COPPER_MATERIAL, COPPER_THICKNESSES
from view.sheet_usage_view import render_group

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

    st.subheader("Hintalaskuri")

    # Copper is always selectable, even before any supplier PDF is uploaded and
    # even before a copper price has been set (it simply carries no price yet).
    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])

    margin_pct = st.number_input(
        "Materiaalin kate (%)",
        min_value=0.0,
        value=15.0,
        step=0.5,
        key="calc_margin_pct",
    )

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

            if material == COPPER_MATERIAL:
                # Copper's thicknesses are fixed and available even with no price.
                thicknesses = COPPER_THICKNESSES
            elif material is not None:
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

    nest_mode = st.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": "Laske jokainen tuote erikseen",
        }[v],
        horizontal=True,
        key="calc_nest_mode",
        help=(
            "Yhdistettynä saman materiaalin ja paksuuden tuotteet sijoitellaan "
            "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
            "tuotteelle lasketaan oma levytarpeensa."
        ),
    )

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
    groups: dict[tuple, list[dict]] = {}
    for i, prod in enumerate(products):
        if not prod["material"] or not prod["thickness"]:
            continue
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        if nest_mode == "separate":
            group_key = (prod["material"], prod["thickness"], prod["id"])
        else:
            group_key = (prod["material"], prod["thickness"])
        groups.setdefault(group_key, []).append(
            {**prod, "_global_idx": i}
        )

    if groups:
        st.divider()
        st.markdown("**Levyn käyttö** — mikä levykoko on edullisin")

        grand_total_eur  = 0.0
        any_priced       = False
        cheapest_prices: dict[str, float] = {}
        for group_key, group_prods in groups.items():
            material, thickness = group_key[0], group_key[1]
            cheapest_eur, cheapest_ppt = render_group(
                lookup=lookup,
                material=material,
                thickness=thickness,
                products=group_prods,
                margin_pct=margin_pct,
                long_side_clamp_mm=long_side_clamp_mm,
                rankavali_mm=rankavali_mm,
            )
            if cheapest_eur is not None:
                grand_total_eur += cheapest_eur
                any_priced = True
            if cheapest_ppt is not None:
                for gp in group_prods:
                    cheapest_prices[gp["id"]] = cheapest_ppt

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

        ppt          = cheapest_prices.get(prod["id"])
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
        st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

