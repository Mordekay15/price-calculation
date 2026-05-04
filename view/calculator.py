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

_PLACEHOLDER_MAT   = "— Select material —"
_PLACEHOLDER_THICK = "— Select thickness —"


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
        st.info("No price data available.")
        return

    st.subheader("Price calculator")
    materials = get_materials(lookup)

    margin_pct = st.number_input(
        "Material margin / kate (%)",
        min_value=10.0,
        max_value=20.0,
        value=15.0,
        step=0.5,
        key="calc_margin_pct",
    )

    _init_products()

    # ── Products ──────────────────────────────────────────────────────────────

    st.markdown("**Products**")

    to_delete = None
    for i, prod in enumerate(st.session_state.calc_products):
        pid = prod["id"]
        with st.container(border=True):
            hdr_cols = st.columns([6, 1])
            hdr_cols[0].markdown(f"**Product {i + 1}**")
            if len(st.session_state.calc_products) > 1:
                if hdr_cols[1].button("Remove", key=f"del_{pid}"):
                    to_delete = i

            mat_opts = [_PLACEHOLDER_MAT] + materials
            mat_default = prod["material"] if prod["material"] in materials else _PLACEHOLDER_MAT
            mat_raw = st.selectbox(
                "Material",
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
                    "Thickness (mm)",
                    th_opts,
                    index=th_opts.index(th_default),
                    key=f"th_{pid}",
                )
                thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
            else:
                st.selectbox("Thickness (mm)", [_PLACEHOLDER_THICK], index=0, disabled=True, key=f"th_{pid}_disabled")
                thickness = None

            inp_cols = st.columns(3)
            w = inp_cols[0].number_input(
                "Width (mm)", min_value=0.0, value=float(prod["width"]),
                step=10.0, key=f"w_{pid}",
            )
            h = inp_cols[1].number_input(
                "Height (mm)", min_value=0.0, value=float(prod["height"]),
                step=10.0, key=f"h_{pid}",
            )
            q = inp_cols[2].number_input(
                "Quantity (pcs)", min_value=1, value=int(prod["qty"]),
                step=1, key=f"q_{pid}",
            )

            prod["material"]  = material
            prod["thickness"] = thickness
            prod["width"]     = w
            prod["height"]    = h
            prod["qty"]       = q

    if st.button("+ Add product"):
        st.session_state.calc_products.append(_new_product())
        st.rerun()

    if to_delete is not None:
        st.session_state.calc_products.pop(to_delete)
        st.rerun()

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
        st.markdown("**Sheet usage** — which sheet size is cheapest")
        st.caption(
            "Pieces from different products that share the same material and "
            "thickness also share leftover space on the same sheet (rotation "
            "allowed). Packing is a greedy heuristic, so manual nesting may "
            "save a few % more."
        )

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
            )
            if cheapest_eur is not None:
                grand_total_eur += cheapest_eur
                any_priced = True
            if cheapest_ppt is not None:
                cheapest_prices[(material, thickness)] = cheapest_ppt

        if any_priced and len(groups) > 1:
            st.divider()
            st.metric("Combined cheapest total (€)", f"{grand_total_eur:,.2f}")

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
            "#":             i + 1,
            "Material":      prod["material"] or "",
            "Thickness":     prod["thickness"] or "",
            "Width (mm)":    prod["width"],
            "Height (mm)":   prod["height"],
            "Qty (pcs)":     prod["qty"],
            "kg/pc":         round(one_weight,   3),
            "Total kg":      round(batch_weight, 3),
            "€/pc":          one_cost,
            "Total €":       batch_cost,
        })

    if table_rows:
        st.divider()
        st.markdown("**Pieces summary**")
        m1, m2 = st.columns(2)
        m1.metric("Total piece weight (kg)", f"{total_weight_kg:.3f}")
        if total_cost_eur:
            m2.metric("Total material cost (€)", f"{total_cost_eur:,.2f}")
        st.caption("€/pc uses the cheapest available sheet size price × piece weight (no waste).")
        st.dataframe(table_rows, use_container_width=True, hide_index=True)


# ── Sheet usage comparison ────────────────────────────────────────────────────

def _fmt_m(mm: int) -> str:
    """Format a mm value as metres: 1000 -> '1.0', 1250 -> '1.25', 1500 -> '1.5'."""
    s = f"{mm / 1000:.2f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _render_sheet_usage_group(
    lookup: dict,
    material: str,
    thickness: str,
    products: list[dict],
    margin_pct: float = 0.0,
) -> tuple[float | None, float | None]:
    """
    Render one sheet-usage table for products sharing the same material and
    thickness. Returns (cheapest_total_eur, cheapest_price_per_tonne), both
    None when no priced sheet size can fulfil the order.
    """
    pieces = expand_products(products)
    if not pieces:
        return None, None

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
        st.info("No sheet-size pricing available for this combination.")
        return None, None

    rows = []
    for _size_label, sw, sh, price_per_tonne in candidates:
        sheets, failed  = pack(pieces, sw, sh, allow_rotation=True)
        summary         = summarise(sw, sh, sheets, len(failed))
        has_failures    = summary["failed_pieces"] > 0
        sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
        total_kg        = sheet_weight_kg * summary["sheets_needed"]
        result          = calculate(price_per_tonne, total_kg / 1000, margin_pct=margin_pct)
        adjusted_ppt    = result["after_margin"]
        total_eur       = result["total"]
        cost_per_pc     = round(total_eur / n_pieces, 2) if (not has_failures and n_pieces) else ""
        rows.append({
            "Sheet size":     f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
            "Price (€/tn)":   f"{adjusted_ppt:,.2f}",
            "Sheets needed":  "" if has_failures else summary["sheets_needed"],
            "Utilisation":    "" if has_failures else f"{summary['utilization'] * 100:.1f} %",
            "Sheet kg":       "" if has_failures else round(total_kg, 2),
            "Total €":        "" if has_failures else round(total_eur, 2),
            "€/pc":           cost_per_pc,
            "_total":         total_eur,
            "_ppt":           adjusted_ppt,
            "_failed":        summary["failed_pieces"],
        })

    valid_rows         = [r for r in rows if r["_failed"] == 0]
    cheapest_eur       = None
    cheapest_ppt       = None
    if valid_rows:
        cheapest     = min(valid_rows, key=lambda r: r["_total"])
        cheapest_eur = cheapest["_total"]
        cheapest_ppt = cheapest["_ppt"]
        for r in rows:
            r["Best"] = "◀" if r is cheapest else ""
        st.success(
            f"Cheapest: **{cheapest['Sheet size']}** — "
            f"{cheapest['Sheets needed']} sheet(s), "
            f"**{cheapest['€/pc']} €/pc**, "
            f"total **{cheapest['Total €']:,.2f} €** "
            f"at {cheapest['Utilisation']} utilisation."
        )
    else:
        for r in rows:
            r["Best"] = ""

    display_rows = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in rows
    ]
    st.dataframe(display_rows, use_container_width=True, hide_index=True)

    return cheapest_eur, cheapest_ppt
