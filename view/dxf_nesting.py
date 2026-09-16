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

from core import true_nesting as tn
from core.calculator import (
    build_lookup,
    calculate,
    density_for_material,
    get_materials,
    get_sizes_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)
from core.copper import COPPER_MATERIAL
from core.dxf import parse_dxf
from core.nesting import parse_size
from core.sheet_usage import _fmt_m
from view.margin_view import render_margin
from view.nesting_settings_view import render_nesting_settings
from view.pieces_summary_view import render_pieces_summary
from view.product_view import render_material_thickness
from view.sheet_usage_view import _PRODUCT_PALETTE, _render_breakdown, render_group

_STORE = "dxf_store"   # session_state key: {file_id: DxfPart}

# Rotation presets for tight nesting: label -> angles tried per piece.
_ANGLE_PRESETS: dict[str, tuple[float, ...]] = {
    "0 / 90°":               (0.0, 90.0),
    "0 / 45 / 90 / 135°":    (0.0, 45.0, 90.0, 135.0),
    "30° välein":            (0.0, 30.0, 60.0, 90.0, 120.0, 150.0),
    "15° välein":            tuple(float(a) for a in range(0, 180, 15)),
}
_QUALITY_RES = {"Normaali (nopeampi)": 3.0, "Tarkka (hitaampi)": 2.0}


@st.cache_data(show_spinner=False)
def _run_nest(parts_key, sheet_w, sheet_h, res, angles, kerf_mm, clamp_mm):
    """Cached wrapper around the tight nester (keyed by geometry, not price)."""
    parts = [
        {"idx": idx, "polylines": [list(poly) for poly in polys], "qty": qty}
        for (idx, qty, polys) in parts_key
    ]
    return tn.nest(
        parts, sheet_w, sheet_h,
        res=res, angles=angles, kerf_mm=kerf_mm, long_side_clamp_mm=clamp_mm,
    )


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

    margin_pct = render_margin("dxf_margin_pct") #getting kate

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
    nest_mode, rankavali_mm, long_side_clamp_mm = render_nesting_settings(
        key_prefix="dxf",
        separate_label="Laske jokainen osa erikseen")

    pack_mode = st.radio(
        "Nestaustapa",
        options=("box", "tight"),
        format_func=lambda v: {
            "box":   "Laatikkonestaus (nopea)",
            "tight": "Tiivis muotonestaus (tarkka)",
        }[v],
        horizontal=True,
        key="dxf_pack_mode",
        help=(
            "Laatikkonestaus sijoittelee osat suorakulmion mukaan (nopea). "
            "Tiivis muotonestaus sijoittelee osien todelliset muodot niin, että "
            "ne lomittuvat toistensa lovien sisään — vähemmän hukkaa, mutta "
            "laskenta kestää hetken."
        ),
    )

    angles = (0.0, 90.0)
    res = 3.0
    if pack_mode == "tight":
        tcols = st.columns(2)
        angle_label = tcols[0].selectbox(
            "Sallitut kiertokulmat",
            options=list(_ANGLE_PRESETS.keys()),
            index=1,
            key="dxf_angles",
            help="Enemmän kulmia = tiiviimpi tulos mutta hitaampi laskenta.",
        )
        angles = _ANGLE_PRESETS[angle_label]
        quality_label = tcols[1].selectbox(
            "Laskennan tarkkuus",
            options=list(_QUALITY_RES.keys()),
            index=0,
            key="dxf_quality",
        )
        res = _QUALITY_RES[quality_label]

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
    st.markdown("**Levyn käyttö**")

    if pack_mode == "tight":
        total_pieces = sum(int(p["qty"]) for p in ready)
        if total_pieces > 120:
            st.warning(
                f"Osia on {total_pieces} kpl — tiivis muotonestaus voi kestää "
                "kymmeniä sekunteja. Voit pienentää määrää tai valita "
                "laatikkonestauksen."
            )

    grand_total_eur = 0.0
    any_priced = False
    cheapest_prices: dict[str, float] = {}
    for group_key, group_prods in groups.items():
        material, thickness = group_key[0], group_key[1]
        if pack_mode == "tight":
            cheapest_eur, cheapest_ppt = _render_tight_group(
                lookup=lookup,
                material=material,
                thickness=thickness,
                products=group_prods,
                margin_pct=margin_pct,
                long_side_clamp_mm=long_side_clamp_mm,
                rankavali_mm=rankavali_mm,
                angles=angles,
                res=res,
            )
        else:
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

    render_pieces_summary(ready, cheapest_prices, from_dxf=True)


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
        # so a frame / dimensions / text / bend lines can be dropped. Annotation
        # layers are hidden by default; the size of each layer is shown to help.
        avail_layers = part.available_layers()
        if len(avail_layers) > 1:
            sizes = part.layer_sizes()
            suggested = part.suggested_layers()

            def _label(name: str) -> str:
                n, w, h = sizes.get(name, (0, 0, 0))
                return f"{name}  ·  {n} obj  ·  {w:.0f}×{h:.0f} mm"

            selected_layers = set(st.multiselect(
                "Tasot (layers) mukaan sijoitteluun",
                options=avail_layers,
                default=suggested,
                format_func=_label,
                key=f"dxf_layers_{fid}",
                help="Vain osan leikattavat tasot. Mitat, tekstit, kehys ja "
                     "muut ei-osatasot on piilotettu oletuksena — lisää tai "
                     "poista tasoja ja katso esikatselusta, että vain osa jää.",
            ))
            hidden = [n for n in avail_layers if n not in suggested]
            if hidden:
                st.caption("Piilotettu oletuksena (todennäköisesti mitat/teksti/"
                           "kehys): " + ", ".join(hidden))
        else:
            selected_layers = None  # all

        main_only = st.checkbox(
            "Vain pääkappale (poista irralliset lisäkuvat)",
            value=True,
            key=f"dxf_mainonly_{fid}",
            help="Pitää suurimman yhtenäisen kappaleen ja sen reiät/aukot, mutta "
                 "poistaa erilliset apukuvat ja yksityiskohdat, jotka ovat osan "
                 "ulkopuolella.",
        )

        geom = part.build(selected_layers, main_only=main_only)
        if geom.width <= 0 or geom.height <= 0:
            st.warning("Valituilla tasoilla ei ole geometriaa. Valitse tasoja uudelleen.")
            return None

        hdr[1].markdown(
            f":gray[{geom.width:g} × {geom.height:g} mm · {part.unit_label}]"
        )

        # Live preview so the user can confirm only the product is left.
        st.markdown(_preview_svg(geom.polylines, geom.width, geom.height),
                    unsafe_allow_html=True)

        # Material + thickness — shared with the manual calculator cards.
        material, thickness = render_material_thickness(
            materials, lookup,
            mat_key=f"dxf_mat_{fid}", thick_key=f"dxf_th_{fid}",
        )

        # Detected size drives the widget defaults; the key includes the layer
        # signature so changing layers reseeds the numbers to the new geometry.
        det_w = round(geom.width, 1)
        det_h = round(geom.height, 1)
        sig = ("-".join(sorted(selected_layers)) if selected_layers else "all") \
            + ("-main" if main_only else "-full")
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


def _preview_svg(polylines, w_mm: float, h_mm: float, px: int = 260) -> str:
    """Small preview of the currently-selected geometry (drawing Y flipped)."""
    if not polylines or w_mm <= 0 or h_mm <= 0:
        return ""
    scale = px / max(w_mm, h_mm)
    segs = []
    for poly in polylines:
        if len(poly) < 2:
            continue
        segs.append("M " + " L ".join(f"{x:.1f} {h_mm - y:.1f}" for x, y in poly))
    return (
        f'<svg width="{w_mm * scale:.0f}" height="{h_mm * scale:.0f}" '
        f'viewBox="0 0 {w_mm:.0f} {h_mm:.0f}" preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;'
        f'max-width:100%;height:auto;margin:4px 0 2px;">'
        f'<path d="{" ".join(segs)}" fill="#3b82f6" fill-opacity="0.12" '
        f'fill-rule="evenodd" stroke="#2563eb" stroke-width="{max(0.5, w_mm/300):.2f}"/>'
        f'</svg>'
    )


def _scaled_polylines(polylines, det_w, det_h, new_w, new_h):
    """Scale outline points if the user overrode the detected size."""
    if not polylines:
        return polylines
    sx = new_w / det_w if det_w else 1.0
    sy = new_h / det_h if det_h else 1.0
    if abs(sx - 1.0) < 1e-9 and abs(sy - 1.0) < 1e-9:
        return polylines
    return [[(x * sx, y * sy) for x, y in poly] for poly in polylines]


# ── Tight (shape-aware) nesting ─────────────────────────────────────────────────

def _render_tight_group(
    lookup: dict,
    material: str,
    thickness: str,
    products: list[dict],
    margin_pct: float,
    long_side_clamp_mm: int,
    rankavali_mm: int,
    angles: tuple[float, ...],
    res: float,
) -> tuple[float | None, float | None]:
    """Compare priced sheet sizes using shape-aware nesting for one group."""
    thickness_mm = parse_thickness_mm(thickness)
    if thickness_mm is None:
        return None, None
    n_pieces = sum(int(p["qty"]) for p in products)
    if n_pieces == 0:
        return None, None

    st.markdown(f"**{material}** · **{thickness} mm**")

    candidates = []
    for size_label in get_sizes_for_material(lookup, material):
        price = lookup.get((thickness, f"{material} | {size_label}"))
        if price is None:
            continue
        for w_mm, h_mm in parse_size(size_label):
            candidates.append((size_label, w_mm, h_mm, price))
    if not candidates:
        if material == COPPER_MATERIAL:
            st.info("Aseta kuparin hinta (€/kg) sivupalkista, niin levylaskenta tulee näkyviin.")
        else:
            st.info("Tälle yhdistelmälle ei ole levykohtaista hinnoittelua.")
        return None, None

    pieces_kg = sum(
        piece_weight_kg(p["width"], p["height"], thickness_mm, material) * p["qty"]
        for p in products
    )

    parts_key = tuple(
        (i, int(p["qty"]),
         tuple(tuple((round(x, 3), round(y, 3)) for x, y in poly) for poly in p["_polylines"]))
        for i, p in enumerate(products)
    )

    rows = []
    with st.spinner("Lasketaan tiivistä muotonestausta…"):
        for _size_label, sw, sh, price in candidates:
            result = _run_nest(
                parts_key, sw, sh, res, tuple(angles),
                float(rankavali_mm), int(long_side_clamp_mm),
            )
            sheets_needed = len(result.sheets)
            has_fail = result.failed > 0 or sheets_needed == 0
            used_area = sum(s.used_area for s in result.sheets)
            util = used_area / (sw * sh * sheets_needed) if sheets_needed else 0.0
            sheet_weight_kg = sw * sh * thickness_mm * density_for_material(material)
            sheet_kg = sheet_weight_kg * sheets_needed
            billable_kg = sheet_kg
            calc = calculate(price, billable_kg / 1000, margin_pct=margin_pct)
            adjusted_ppt = calc["after_margin"]
            total_eur = calc["total"]
            cost_per_pc = round(total_eur / n_pieces, 2) if (not has_fail and n_pieces) else ""
            bill_rate_ppt = adjusted_ppt * (sheet_kg / pieces_kg) if pieces_kg else adjusted_ppt
            rows.append({
                "Levykoko":          f"{_fmt_m(sw)} × {_fmt_m(sh)} m",
                "Hinta (€/tn)":      f"{adjusted_ppt:,.2f}",
                "Tarvittavat levyt": "" if has_fail else sheets_needed,
                "Käyttöaste":        "" if has_fail else f"{util * 100:.1f} %",
                "Levyn kg":          "" if has_fail else round(sheet_kg, 2),
                "Yhteensä €":        "" if has_fail else round(total_eur, 2),
                "€/kpl":             cost_per_pc,
                "_total":       total_eur,
                "_ppt":         bill_rate_ppt,
                "_failed":      result.failed,
                "_util":        util,
                "_sheets":      result.sheets,
                "_sw":          sw,
                "_sh":          sh,
                "_breakdown": {
                    "sw": sw, "sh": sh, "base_ppt": price, "adjusted_ppt": adjusted_ppt,
                    "sheet_weight_kg": sheet_weight_kg, "sheets_needed": sheets_needed,
                    "sheet_kg": sheet_kg, "pieces_kg": pieces_kg, "billable_kg": billable_kg,
                    "total_eur": total_eur, "cost_per_pc": cost_per_pc,
                },
            })

    valid = [i for i, r in enumerate(rows) if r["_failed"] == 0]
    cheapest_idx = (
        min(valid, key=lambda i: (rows[i]["_total"], -rows[i]["_util"])) if valid else None
    )
    for i, r in enumerate(rows):
        r["Paras"] = "🚫" if r["_failed"] > 0 else ("◀ edullisin" if i == cheapest_idx else "")

    display_rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    select_key = f"tight_select::{material}::{thickness}::" + "-".join(str(p["id"]) for p in products)
    event = st.dataframe(
        display_rows, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row", key=select_key,
    )

    selected_idx = cheapest_idx
    sel_rows = list(getattr(event.selection, "rows", []) or [])
    if sel_rows:
        idx = sel_rows[0]
        if 0 <= idx < len(rows) and rows[idx]["_failed"] == 0:
            selected_idx = idx
        else:
            st.warning(f"**{rows[idx]['Levykoko']}** ei kelpaa — osia ei saatu mahtumaan.")
    if selected_idx is None:
        st.warning("Osia ei saatu mahtumaan millekään levykoolle.")
        return None, None

    active = rows[selected_idx]
    if cheapest_idx is not None:
        c = rows[cheapest_idx]
        if selected_idx != cheapest_idx:
            st.info(
                f"Valittu: **{active['Levykoko']}** "
                f"(+{active['_total'] - c['_total']:,.2f} € vs. {c['Levykoko']})."
            )
        else:
            st.success(
                f"Edullisin: **{c['Levykoko']}** — {c['Tarvittavat levyt']} levyä, "
                f"käyttöaste {c['Käyttöaste']}."
            )

    avg = active["_total"] / n_pieces if n_pieces else 0.0
    mc = st.columns([2, 1, 1, 1])
    mc[0].metric("Materiaalikulu €/kpl (ka.)", f"{avg:,.2f} €")
    mc[1].metric("Yhteensä €", f"{active['_total']:,.2f}")
    mc[2].metric("Levyjä", str(active["Tarvittavat levyt"]))
    mc[3].metric("Käyttöaste", active["Käyttöaste"] or "—")

    if len(products) >= 2:
        brk = []
        for prod in products:
            one_kg = piece_weight_kg(prod["width"], prod["height"], thickness_mm, material)
            unit = one_kg * active["_ppt"] / 1000
            brk.append({
                "Osa": prod.get("name") or f"#{prod['_global_idx'] + 1}",
                "Mitat (mm)": f"{prod['width']:g} × {prod['height']:g}",
                "Määrä (kpl)": prod["qty"],
                "€/kpl": f"{unit:,.2f}",
                "Yhteensä €": f"{unit * prod['qty']:,.2f}",
            })
        st.caption("Materiaalikulu osittain:")
        st.dataframe(brk, use_container_width=True, hide_index=True)

    _render_tight_layout(active["_sheets"], active["_sw"], active["_sh"],
                         products, long_side_clamp_mm)
    _render_breakdown(material, thickness, thickness_mm, margin_pct, n_pieces, active["_breakdown"])
    return active["_total"], active["_ppt"]


def _render_tight_layout(sheets, sheet_w, sheet_h, products, long_side_clamp_mm):
    if not sheets:
        return
    st.markdown(f"**Sijoittelu** — {len(sheets)} levyä")

    legend_bits = []
    for prod in products:
        color = _PRODUCT_PALETTE[prod["_global_idx"] % len(_PRODUCT_PALETTE)]
        dims = f"{prod['width']:g}×{prod['height']:g}"
        name = prod.get("name")
        label = f"{name} ({dims})" if name else f"Tuote #{prod['_global_idx'] + 1} ({dims})"
        legend_bits.append(
            f'<span style="display:inline-flex;align-items:center;margin-right:14px;'
            f'font-size:13px;"><span style="display:inline-block;width:14px;height:14px;'
            f'background:{color};opacity:0.55;border:2px solid {color};margin-right:6px;'
            f'border-radius:2px;"></span>{label}</span>'
        )
    st.markdown(f'<div style="margin-bottom:8px;">{"".join(legend_bits)}</div>', unsafe_allow_html=True)

    target_px = 460
    scale = target_px / max(sheet_w, sheet_h)
    cols_per_row = min(4, len(sheets))
    for row_start in range(0, len(sheets), cols_per_row):
        row = sheets[row_start:row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for ci, sheet in enumerate(row):
            with cols[ci]:
                st.markdown(
                    f"**Levy {row_start + ci + 1}** · käyttöaste {sheet.utilization * 100:.1f} %"
                )
                st.markdown(
                    _tight_sheet_svg(sheet, sheet_w, sheet_h, sheet_w * scale,
                                     sheet_h * scale, products, long_side_clamp_mm),
                    unsafe_allow_html=True,
                )


def _tight_sheet_svg(sheet, sheet_w, sheet_h, px_w, px_h, products, clamp):
    parts = [
        f'<svg width="{px_w:.0f}" height="{px_h:.0f}" viewBox="0 0 {sheet_w} {sheet_h}" '
        f'preserveAspectRatio="xMidYMid meet" style="background:#f8fafc;border:2px solid '
        f'#475569;border-radius:4px;display:block;max-width:100%;height:auto;">'
    ]
    # Claw strip (unusable), matching where the nester blocked the sheet.
    if clamp > 0:
        if sheet_w >= sheet_h:
            parts.append(f'<rect x="0" y="0" width="{sheet_w}" height="{clamp}" '
                         f'fill="#cbd5e1" fill-opacity="0.5"/>')
        else:
            parts.append(f'<rect x="{sheet_w - clamp}" y="0" width="{clamp}" height="{sheet_h}" '
                         f'fill="#cbd5e1" fill-opacity="0.5"/>')

    for placed in sheet.placed:
        prod = products[placed.part_idx]
        gidx = prod["_global_idx"]
        color = _PRODUCT_PALETTE[gidx % len(_PRODUCT_PALETTE)]
        segs = []
        for poly in placed.polylines:
            if len(poly) < 2:
                continue
            d = "M " + " L ".join(
                f"{placed.x_mm + x:.1f} {sheet_h - (placed.y_mm + y):.1f}" for x, y in poly
            ) + " Z"
            segs.append(d)
        parts.append(
            f'<path d="{" ".join(segs)}" fill="{color}" fill-opacity="0.55" '
            f'fill-rule="evenodd" stroke="{color}" stroke-width="4" stroke-linejoin="round"/>'
        )
        cx = placed.x_mm + placed.w_mm / 2
        cy = sheet_h - (placed.y_mm + placed.h_mm / 2)
        fs = max(36.0, min(placed.w_mm, placed.h_mm) / 5.5)
        parts.append(
            f'<text x="{cx:.0f}" y="{cy:.0f}" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="sans-serif" font-size="{fs:.0f}" font-weight="700" '
            f'fill="#0f172a">#{gidx + 1}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
