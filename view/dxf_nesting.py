"""
view/dxf_nesting.py
===================
DXF nesting section.

The user uploads one or more DXF files — each file is one product. We parse the
real cuttable outline and bounding-box size from every drawing (core/dxf.py),
skipping annotation text/dimensions so nothing extra is nested. The user picks
material / thickness / quantity per part (and can drop non-part layers or
correct the size), then the parts run through the same sheet-usage costing and
layout as the manual calculator (view/sheet_usage_view.py) — except each piece
is drawn as its true DXF shape instead of a plain rectangle.

Nesting still packs by bounding box for now (see core/nesting.py); shape-aware
interlocking can be layered on later without changing this view.
"""

import streamlit as st

from core.calculator import (
    build_lookup,
    get_materials,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.copper import COPPER_MATERIAL, COPPER_THICKNESSES
from core.dxf import parse_dxf
from view.sheet_usage_view import render_group

_PLACEHOLDER_MAT   = "— Valitse materiaali —"
_PLACEHOLDER_THICK = "— Valitse paksuus —"

_STORE = "dxf_store"   # session_state key: {file_id: DxfPart}


def render(data: dict) -> None:
    lookup = build_lookup(data)

    st.subheader("DXF-nestaus")
    st.caption(
        "Lataa osat DXF-tiedostoina. Ohjelma lukee kunkin osan todellisen "
        "muodon ja mitat (tekstit ja mitoitukset ohitetaan) ja sijoittelee ne "
        "levylle mahdollisimman tehokkaasti."
    )

    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])

    margin_pct = st.number_input(
        "Materiaalin kate (%)",
        min_value=0.0, value=15.0, step=0.5, key="dxf_margin_pct",
    )

    # ── Upload ────────────────────────────────────────────────────────────────
    uploaded = st.file_uploader(
        "Lataa DXF-tiedostot",
        type="dxf",
        accept_multiple_files=True,
        key="dxf_uploader",
        help="Voit ladata useita tiedostoja kerralla. Jokainen tiedosto on yksi tuote.",
    )

    parts = _sync_store(uploaded)
    if not parts:
        st.info("Lataa vähintään yksi DXF-tiedosto aloittaaksesi.")
        return

    # ── Per-part configuration ─────────────────────────────────────────────────
    st.markdown("**Osat**")

    products: list[dict] = []
    for idx, (fid, part) in enumerate(parts):
        product = _render_part_config(fid, part, idx, materials, lookup)
        if product is not None:
            products.append(product)

    # ── Placement options (mirrors the manual calculator) ──────────────────────
    nest_mode = st.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": "Laske jokainen osa erikseen",
        }[v],
        horizontal=True,
        key="dxf_nest_mode",
        help=(
            "Yhdistettynä saman materiaalin ja paksuuden osat sijoitellaan "
            "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
            "osalle lasketaan oma levytarpeensa."
        ),
    )

    rankavali_mm = int(st.number_input(
        "Rankaväli (mm)",
        min_value=0, value=0, step=1, key="dxf_rankavali_mm",
        help=(
            "Kappaleiden välinen leikkausvara. Lisätään jokaisen kappaleen "
            "leveyteen ja korkeuteen sijoittelussa."
        ),
    ))

    long_side_clamp_mm = int(st.number_input(
        "Pitkän sivun kynsirainan leveys (mm)",
        min_value=0, value=0, step=1, key="dxf_long_side_clamp_mm",
        help=(
            "Kynsiraina on levyn pitkän sivun reunavyöhyke, johon koneen kynnet "
            "tarttuvat — aluetta ei voi käyttää sijoitteluun. Levy ostetaan silti "
            "täysikokoisena."
        ),
    ))

    # ── Group and render ───────────────────────────────────────────────────────
    ready = [
        p for p in products
        if p["material"] and p["thickness"] and p["width"] > 0 and p["height"] > 0
    ]
    if not ready:
        st.info("Valitse materiaali ja paksuus vähintään yhdelle osalle.")
        return

    groups: dict[tuple, list[dict]] = {}
    for prod in ready:
        if nest_mode == "separate":
            group_key = (prod["material"], prod["thickness"], prod["id"])
        else:
            group_key = (prod["material"], prod["thickness"])
        groups.setdefault(group_key, []).append(prod)

    st.divider()
    st.markdown("**Levyn käyttö** — mikä levykoko on edullisin")

    grand_total_eur = 0.0
    any_priced = False
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

    _render_parts_summary(ready, cheapest_prices)


# ── Upload store ────────────────────────────────────────────────────────────────

def _sync_store(uploaded) -> list[tuple[str, object]]:
    """Parse newly uploaded files once, drop removed ones, keep upload order.

    Returns a list of (file_id, DxfPart). Parsing is cached per file_id in
    session_state so it does not re-run on every rerun.
    """
    store: dict = st.session_state.setdefault(_STORE, {})
    uploaded = uploaded or []
    current_ids = {u.file_id for u in uploaded}

    for stale in [fid for fid in store if fid not in current_ids]:
        del store[stale]

    parts: list[tuple[str, object]] = []
    for up in uploaded:
        if up.file_id not in store:
            store[up.file_id] = parse_dxf(up.getvalue(), up.name)
        parts.append((up.file_id, store[up.file_id]))
    return parts


# ── Per-part UI ──────────────────────────────────────────────────────────────

def _render_part_config(
    fid: str,
    part,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card and return a product dict for nesting (or None)."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1} · {part.name}**")

        for w in part.warnings:
            st.warning(w)

        # Show any text found in the drawing (Mat=…, Thk=…, Un=…) so the user
        # can cross-check — but it is never nested.
        if part.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(part.texts))

        if part.is_empty:
            hdr[1].markdown(":red[ei geometriaa]")
            st.caption("Osaa ei voi sijoitella ennen kuin tiedostossa on geometriaa.")
            return None

        # Layer picker — only when a drawing has more than one geometry layer,
        # so borders / bend lines / construction lines can be dropped.
        avail_layers = part.available_layers()
        if len(avail_layers) > 1:
            selected_layers = set(st.multiselect(
                "Tasot (layers) mukaan sijoitteluun",
                options=avail_layers,
                default=avail_layers,
                key=f"dxf_layers_{fid}",
                help="Ota mukaan vain osan leikattavat tasot. Pois voi jättää "
                     "esim. kehyksen, taivutusviivat tai mitoitustasot.",
            ))
        else:
            selected_layers = None  # all

        geom = part.build(selected_layers)
        if geom.width <= 0 or geom.height <= 0:
            st.warning("Valituilla tasoilla ei ole geometriaa. Valitse tasoja uudelleen.")
            return None

        hdr[1].markdown(
            f":gray[{geom.width:g} × {geom.height:g} mm · {part.unit_label}]"
        )

        # Material + thickness (same pattern as the manual calculator).
        mat_opts = [_PLACEHOLDER_MAT] + materials
        mat_raw = st.selectbox("Materiaali", mat_opts, index=0, key=f"dxf_mat_{fid}")
        material = mat_raw if mat_raw != _PLACEHOLDER_MAT else None

        if material == COPPER_MATERIAL:
            thicknesses = COPPER_THICKNESSES
        elif material is not None:
            thicknesses = get_thicknesses_for_material(lookup, material)
        else:
            thicknesses = []

        if thicknesses:
            th_opts = [_PLACEHOLDER_THICK] + thicknesses
            th_raw = st.selectbox("Paksuus (mm)", th_opts, index=0, key=f"dxf_th_{fid}")
            thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
        else:
            st.selectbox(
                "Paksuus (mm)", [_PLACEHOLDER_THICK], index=0,
                disabled=True, key=f"dxf_th_{fid}_disabled",
            )
            thickness = None

        # Detected size drives the widget defaults; the key includes the layer
        # signature so changing layers reseeds the numbers to the new geometry.
        det_w = round(geom.width, 1)
        det_h = round(geom.height, 1)
        sig = "-".join(sorted(selected_layers)) if selected_layers else "all"
        cols = st.columns(3)
        width = cols[0].number_input(
            "Leveys (mm)", min_value=0.0, value=float(det_w), step=1.0,
            key=f"dxf_w_{fid}_{sig}",
            help="Luettu DXF:stä. Muokkaa, jos piirustuksen yksikkö oli väärä.",
        )
        height = cols[1].number_input(
            "Korkeus (mm)", min_value=0.0, value=float(det_h), step=1.0,
            key=f"dxf_h_{fid}_{sig}",
        )
        qty = int(cols[2].number_input(
            "Määrä (kpl)", min_value=1, value=1, step=1, key=f"dxf_q_{fid}",
        ))

    # If the user corrected the size, scale the outline to match so the drawing
    # stays consistent with the numbers driving the nest.
    polylines = _scaled_polylines(geom.polylines, det_w, det_h, width, height)

    return {
        "id":          fid,
        "name":        part.name,
        "material":    material,
        "thickness":   thickness,
        "width":       width,
        "height":      height,
        "qty":         qty,
        "_global_idx": idx,
        "_polylines":  polylines,
    }


def _scaled_polylines(polylines, det_w, det_h, new_w, new_h):
    """Scale outline points if the user overrode the detected size."""
    if not polylines:
        return polylines
    sx = new_w / det_w if det_w else 1.0
    sy = new_h / det_h if det_h else 1.0
    if abs(sx - 1.0) < 1e-9 and abs(sy - 1.0) < 1e-9:
        return polylines
    return [[(x * sx, y * sy) for x, y in poly] for poly in polylines]


# ── Parts summary ──────────────────────────────────────────────────────────────

def _render_parts_summary(products: list[dict], cheapest_prices: dict[str, float]) -> None:
    rows = []
    total_weight = 0.0
    total_cost = 0.0
    for prod in products:
        thickness_mm = parse_thickness_mm(prod["thickness"])
        if thickness_mm is None:
            continue
        one_kg   = piece_weight_kg(prod["width"], prod["height"], thickness_mm, prod["material"])
        batch_kg = one_kg * prod["qty"]
        total_weight += batch_kg

        ppt = cheapest_prices.get(prod["id"])
        per_kg = ppt / 1000 if ppt else None
        one_cost   = round(one_kg * per_kg, 4) if per_kg else ""
        batch_cost = round(batch_kg * per_kg, 2) if per_kg else ""
        if per_kg:
            total_cost += batch_kg * per_kg

        rows.append({
            "Osa":          prod["name"],
            "Materiaali":   prod["material"] or "",
            "Paksuus":      prod["thickness"] or "",
            "Leveys (mm)":  prod["width"],
            "Korkeus (mm)": prod["height"],
            "Määrä (kpl)":  prod["qty"],
            "kg/kpl":       round(one_kg, 3),
            "Yhteensä kg":  round(batch_kg, 3),
            "€/kpl":        one_cost,
            "Yhteensä €":   batch_cost,
        })

    if not rows:
        return

    st.divider()
    st.markdown("**Osayhteenveto**")
    m1, m2 = st.columns(2)
    m1.metric("Osien yhteispaino (kg)", f"{total_weight:.3f}")
    if total_cost:
        m2.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost:,.2f}")
    st.caption("€/kpl jakaa koko levyn kustannuksen kappaleiden kesken painon mukaan.")
    st.dataframe(rows, use_container_width=True, hide_index=True)
