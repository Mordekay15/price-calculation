"""
view/common.py
==============
Page pieces shared by the manual calculator tab and the DXF tab.

  * ``materials_with_copper`` — the material list, copper always included
  * ``render_material_thickness`` — the material + thickness pickers on a card
  * ``render_margin`` / ``render_nesting_settings`` — the shared inputs
  * ``group_products`` — group ready products by material + thickness
  * ``render_groups`` / ``render_grand_total`` — the per-group sheet-usage loop
  * ``render_pieces_summary`` — the per-piece weight / cost table at the bottom

Streamlit renders both tab bodies on every run, so every widget here takes a
key (prefix) from the caller to keep the two tabs' state apart.
"""

import streamlit as st

from core.pricing import (
    COPPER_MATERIAL,
    COPPER_THICKNESSES,
    density_for_material,
    get_materials,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)


def materials_with_copper(lookup: dict) -> list[str]:
    """Sorted material names; copper is always selectable, priced or not."""
    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])
    return materials


# ── Shared inputs ─────────────────────────────────────────────────────────────

_PLACEHOLDER_MAT   = "— Valitse materiaali —"
_PLACEHOLDER_THICK = "— Valitse paksuus —"


def _thicknesses_for(lookup: dict, material: str | None) -> list[str]:
    """Thicknesses available for ``material`` (empty if none is picked)."""
    if material == COPPER_MATERIAL:
        # Copper's thicknesses are fixed and available even with no price.
        return COPPER_THICKNESSES
    if material is not None:
        return get_thicknesses_for_material(lookup, material)
    return []


def render_material_thickness(
    materials: list[str],
    lookup: dict,
    *,
    mat_key: str,
    thick_key: str,
    mat_default: str | None = None,
    thick_default: str | None = None,
) -> tuple[str | None, str | None]:
    """Render the shared material + thickness selectboxes.

    Used by both the manual calculator product cards and the DXF part cards so
    the two pages pick material/thickness identically (copper always
    selectable, the thickness box disabled until a material is chosen).
    ``mat_default`` / ``thick_default`` seed the initial selection — pass a
    card's stored values to keep its choice across reruns, or leave them None
    to start on the placeholder. Returns ``(material, thickness)``, each None
    when unset.
    """
    mat_opts = [_PLACEHOLDER_MAT] + materials
    mat_default = mat_default if mat_default in materials else _PLACEHOLDER_MAT
    mat_raw = st.selectbox(
        "Materiaali",
        mat_opts,
        index=mat_opts.index(mat_default),
        key=mat_key,
    )
    material = mat_raw if mat_raw != _PLACEHOLDER_MAT else None

    thicknesses = _thicknesses_for(lookup, material)
    if thicknesses:
        th_opts = [_PLACEHOLDER_THICK] + thicknesses
        th_default = thick_default if thick_default in thicknesses else _PLACEHOLDER_THICK
        th_raw = st.selectbox(
            "Paksuus (mm)",
            th_opts,
            index=th_opts.index(th_default),
            key=thick_key,
        )
        thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
    else:
        st.selectbox(
            "Paksuus (mm)", [_PLACEHOLDER_THICK], index=0,
            disabled=True, key=f"{thick_key}_disabled",
        )
        thickness = None

    return material, thickness


def render_margin(key: str) -> float:
    """The material margin ("Materiaalin kate") input, as a percentage."""
    return st.number_input(
        "Materiaalin kate (%)",
        min_value=0.0,
        value=15.0,
        step=0.5,
        key=key,
    )


_NEST_HELP = (
    "Yhdistettynä saman materiaalin ja paksuuden tuotteet sijoitellaan "
    "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
    "tuotteelle lasketaan oma levytarpeensa."
)
_RANKAVALI_HELP = (
    "Kappaleiden välinen rankaväli (leikkausvara). Lisätään jokaisen "
    "kappaleen leveyteen ja korkeuteen sijoittelussa, jotta vierekkäiset "
    "kappaleet pysyvät tämän etäisyyden päässä toisistaan."
)
_CLAMP_HELP = (
    "Kynsiraina on levyn pitkän sivun reunavyöhyke, johon koneen kynnet "
    "tarttuvat — aluetta ei voi käyttää kappaleiden sijoitteluun. "
    "Levy ostetaan silti täysikokoisena, joten paino ja hinta lasketaan "
    "bruttomitoista."
)


def render_nesting_settings(
    *,
    key_prefix: str = "calc",
    separate_label: str = "Laske jokainen tuote erikseen",
) -> tuple[str, int, int]:
    """The "Sijoittelutapa" toggle, rankaväli and the long-side clamp strip.

    Returns ``(nest_mode, rankavali_mm, long_side_clamp_mm)``; ``nest_mode`` is
    "combined" or "separate".
    """
    nest_mode = st.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": separate_label,
        }[v],
        horizontal=True,
        key=f"{key_prefix}_nest_mode",
        help=_NEST_HELP,
    )
    rankavali_mm = int(st.number_input(
        "Rankaväli (mm)", min_value=0, value=0, step=1,
        key=f"{key_prefix}_rankavali_mm", help=_RANKAVALI_HELP,
    ))
    long_side_clamp_mm = int(st.number_input(
        "Pitkän sivun kynsirainan leveys (mm)", min_value=0, value=0, step=1,
        key=f"{key_prefix}_long_side_clamp_mm", help=_CLAMP_HELP,
    ))
    return nest_mode, rankavali_mm, long_side_clamp_mm


# ── Grouping and the per-group loop ───────────────────────────────────────────

def is_ready(prod: dict) -> bool:
    """A product can be priced once it has a material, thickness and size."""
    return bool(prod["material"] and prod["thickness"]
                and prod["width"] > 0 and prod["height"] > 0)


def group_products(products: list[dict], nest_mode: str) -> dict[tuple, list[dict]]:
    """Group ready products by ``(material, thickness)``.

    In "separate" mode the product id joins the key, so each product is nested
    on its own sheets. Each grouped product gets ``_global_idx`` — its position
    in ``products`` — which picks its colour and ``#N`` label in the layout.
    """
    groups: dict[tuple, list[dict]] = {}
    for i, prod in enumerate(products):
        if not is_ready(prod):
            continue
        key = (prod["material"], prod["thickness"])
        if nest_mode == "separate":
            key += (prod["id"],)
        groups.setdefault(key, []).append({**prod, "_global_idx": i})
    return groups


def render_groups(groups: dict[tuple, list[dict]], render_one):
    """Render every group and collect the prices for the pieces summary.

    ``render_one(key, products)`` renders one group and returns
    ``(total_eur, price_per_tonne)`` — either may be None when unpriced — or
    None when the group has no result yet.

    Returns ``(price_by_product_id, grand_total_eur, missing)``: the price
    each product's pieces are billed at, the summed total (None when no group
    was priced), and whether any group had no result yet.
    """
    prices: dict[str, float] = {}
    grand_total = None
    missing = False
    for key, prods in groups.items():
        out = render_one(key, prods)
        if out is None:
            missing = True
            continue
        total, ppt = out
        if total is not None:
            grand_total = (grand_total or 0.0) + total
        if ppt is not None:
            for p in prods:
                prices[p["id"]] = ppt
    return prices, grand_total, missing


def render_grand_total(grand_total: float | None, n_groups: int) -> None:
    """The combined total, shown when more than one group was priced."""
    if grand_total is not None and n_groups > 1:
        st.divider()
        st.metric("Yhdistetty edullisin yhteissumma (€)", f"{grand_total:,.2f}")


# ── Pieces summary ────────────────────────────────────────────────────────────

def render_pieces_summary(
    products: list[dict],
    cheapest_prices: dict[str, float],
    *,
    from_dxf: bool = False,
    areas_mm2: dict[str, float] | None = None,
) -> None:
    """The per-piece summary table plus its weight / cost totals.

    ``cheapest_prices`` maps a product id to the price per tonne its pieces are
    billed at. With ``from_dxf`` the leading column is the part name and the
    section uses the "Osa" wording; otherwise a running piece number.

    ``areas_mm2`` maps a product id to its real cut area (mm², holes removed);
    manual rectangles have no entry and use width × height.
    """
    areas_mm2 = areas_mm2 or {}
    title        = "Osayhteenveto" if from_dxf else "Kappaleyhteenveto"
    weight_label = "Osien yhteispaino (kg)" if from_dxf else "Kappaleiden yhteispaino (kg)"

    table_rows      = []
    total_weight_kg = 0.0
    total_cost_eur  = 0.0
    for i, prod in enumerate(products):
        if prod["width"] <= 0 or prod["height"] <= 0:
            continue
        thickness_mm = parse_thickness_mm(prod["thickness"]) if prod["thickness"] else None
        if thickness_mm is None:
            continue
        net_area = areas_mm2.get(prod["id"])
        if net_area is not None:
            one_weight = net_area * thickness_mm * density_for_material(prod["material"])
        else:
            one_weight = piece_weight_kg(prod["width"], prod["height"], thickness_mm, prod["material"])
        batch_weight = one_weight * prod["qty"]
        total_weight_kg += batch_weight

        ppt          = cheapest_prices.get(prod["id"])
        price_per_kg = ppt / 1000 if ppt else None
        one_cost     = round(one_weight   * price_per_kg, 4) if price_per_kg else ""
        batch_cost   = round(batch_weight * price_per_kg, 2) if price_per_kg else ""
        if price_per_kg:
            total_cost_eur += batch_weight * price_per_kg

        lead_col = {"Osa": prod["name"]} if from_dxf else {"#": i + 1}
        table_rows.append({
            **lead_col,
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

    if not table_rows:
        return

    st.divider()
    st.markdown(f"**{title}**")
    m1, m2 = st.columns(2)
    m1.metric(weight_label, f"{total_weight_kg:.3f}")
    if total_cost_eur:
        m2.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost_eur:,.2f}")
    st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
    st.dataframe(table_rows, use_container_width=True, hide_index=True)
