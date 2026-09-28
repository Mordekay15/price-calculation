"""
view/dxf_part_view.py
=====================
The DXF part cards: one card per uploaded file.

sync_store() reads each uploaded file once with ``core.dxf.read_dxf`` (cached
in session state, pruned when a file is removed) and keeps upload order.
render_part_config() draws a card — the layer picker, a preview of the part,
its measured size and the material / thickness / quantity inputs — and returns
the product dict the pricing uses. A part that cannot be priced shows the
reasons instead and returns None.

The card and the pricing share the same ``DxfReport`` (the part built from the
chosen layers), so what the card shows is exactly what Sparrow nests.
"""

import streamlit as st

from core.dxf import DxfFile, DxfReport, read_dxf
from view.product_view import render_material_thickness

_STORE   = "dxf_store"         # {file_id: DxfFile}
_REPORTS = "dxf_part_reports"  # {(file_id, layers): DxfReport}
_CONFIG  = "dxf_part_config"   # {file_id: {"material", "thickness"}}


def sync_store(uploaded) -> list[tuple[str, DxfFile]]:
    """Read newly uploaded files once, drop removed ones, keep upload order."""
    store: dict = st.session_state.setdefault(_STORE, {})
    uploaded = uploaded or []
    current_ids = {u.file_id for u in uploaded}
    for fid in [f for f in store if f not in current_ids]:
        _evict(fid)

    files = []
    for up in uploaded:
        if up.file_id not in store:
            store[up.file_id] = read_dxf(up.getvalue(), up.name)
        files.append((up.file_id, store[up.file_id]))
    return files


def _evict(fid: str) -> None:
    """Drop everything kept for a removed file, including its widget state."""
    st.session_state.get(_STORE, {}).pop(fid, None)
    st.session_state.get(_CONFIG, {}).pop(fid, None)
    reports: dict = st.session_state.get(_REPORTS, {})
    for key in [k for k in reports if k[0] == fid]:
        del reports[key]
    for key in (f"dxf_mat_{fid}", f"dxf_th_{fid}", f"dxf_th_{fid}_disabled",
                f"dxf_q_{fid}", f"dxf_layers_{fid}"):
        st.session_state.pop(key, None)


def render_part_config(
    fid: str,
    dxf: DxfFile,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card; return its product dict, or None if not priceable."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1}** · {dxf.name}")

        # Text found in the drawing (Mat=…, Thk=…) — shown to cross-check the
        # material choice, never nested.
        if dxf.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(dxf.texts))
        if dxf.unit_from_text:
            st.caption(f"Yksikkö luettu piirustuksen tekstistä ({dxf.unit_label}).")

        layers = _render_layer_picker(fid, dxf)
        report = _part(fid, dxf, layers)

        preview = _preview_svg(report)
        if preview:
            st.markdown(preview, unsafe_allow_html=True)
        if report.dropped:
            st.caption(
                f"Osan ulkopuolelta ohitettiin {len(report.dropped)} kuviota "
                "(esim. lisäkuvat tai irralliset viivat) — harmaalla esikatselussa."
            )

        if report.problems:
            hdr[1].markdown(":red[ei hinnoiteltavissa]")
            st.error(
                "**Tätä osaa ei voi vielä hinnoitella:**\n\n"
                + "\n".join(f"- {p}" for p in report.problems)
            )
            return None

        width = round(report.outline.width_mm, 1)
        height = round(report.outline.height_mm, 1)
        hdr[1].markdown(f":gray[{width:g} × {height:g} mm · {report.unit_label}]")

        # Material + thickness — persisted per file and shared with the manual
        # calculator cards. Seed the selectboxes from the stored choice, then
        # write the current choice back into it.
        cfg = st.session_state.setdefault(_CONFIG, {}).setdefault(
            fid, {"material": None, "thickness": None})
        material, thickness = render_material_thickness(
            materials, lookup,
            mat_key=f"dxf_mat_{fid}", thick_key=f"dxf_th_{fid}",
            mat_default=cfg["material"], thick_default=cfg["thickness"],
        )
        cfg["material"] = material
        cfg["thickness"] = thickness
        qty = int(st.number_input("Määrä (kpl)", min_value=1, value=1, step=1,
                                  key=f"dxf_q_{fid}"))

    return {
        "id":        fid,
        "name":      dxf.name,
        "material":  material,
        "thickness": thickness,
        "width":     width,
        "height":    height,
        "qty":       qty,
        "layers":    None if layers is None else tuple(sorted(layers)),
        "report":    report,
    }


def _part(fid: str, dxf: DxfFile, layers: set[str] | None) -> DxfReport:
    """The part for this layer choice, memoised (building it scans every point)."""
    cache: dict = st.session_state.setdefault(_REPORTS, {})
    key = (fid, None if layers is None else tuple(sorted(layers)))
    if key not in cache:
        cache[key] = dxf.part(layers)
    return cache[key]


def _render_layer_picker(fid: str, dxf: DxfFile) -> set[str] | None:
    """Layer multiselect, shown when a drawing has more than one layer.

    Frame / title / text / dimension / bend / info layers are left out by
    default. Returns the chosen layers, or None for the default choice.
    """
    avail = dxf.available_layers()
    if len(avail) <= 1:
        return None
    suggested = dxf.suggested_layers()
    sizes = dxf.layer_sizes()

    def label(name: str) -> str:
        n, w, h = sizes.get(name, (0, 0, 0))
        return f"{name}  ·  {n} obj  ·  {w:.0f}×{h:.0f} mm"

    chosen = set(st.multiselect(
        "Leikattavat tasot (layers)",
        options=avail,
        default=suggested,
        format_func=label,
        key=f"dxf_layers_{fid}",
        help="Vain osan leikattavat tasot. Kehys, otsikko, mitat, tekstit, "
             "taivutusviivat ja info-tasot jätetään oletuksena pois — lisää tai "
             "poista tasoja ja katso esikatselusta, että vain osa jää.",
    ))
    hidden = [n for n in avail if n not in suggested]
    if hidden:
        st.caption("Jätetty oletuksena pois: " + ", ".join(hidden))
    return chosen


def _preview_svg(report: DxfReport, px: int = 260) -> str:
    """Small preview of the part (drawing Y flipped).

    The part is filled (holes cut out), reference lines are dashed, open lines
    inside the part are red, and geometry dropped outside the part is grey.
    """
    rings = ([report.outline.points] if report.outline else []) + [h.points for h in report.holes]
    groups = (rings, report.reference_lines, report.open_lines, report.dropped)
    pts = [p for group in groups for ring in group for p in ring]
    if not pts:
        return ""
    min_x = min(x for x, _ in pts)
    min_y = min(y for _, y in pts)
    w = max(x for x, _ in pts) - min_x
    h = max(y for _, y in pts) - min_y
    if w <= 0 or h <= 0:
        return ""
    stroke = max(0.5, max(w, h) / 300)

    def d(lines, close):
        return " ".join(
            "M " + " L ".join(f"{x - min_x:.1f} {h - (y - min_y):.1f}" for x, y in line)
            + (" Z" if close else "")
            for line in lines if len(line) >= 2
        )

    scale = px / max(w, h)
    out = [
        f'<svg width="{w * scale:.0f}" height="{h * scale:.0f}" '
        f'viewBox="{-stroke} {-stroke} {w + 2 * stroke:.1f} {h + 2 * stroke:.1f}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;'
        f'max-width:100%;height:auto;margin:4px 0 2px;">'
    ]
    if report.dropped:
        out.append(f'<path d="{d(report.dropped, False)}" fill="none" stroke="#cbd5e1" '
                   f'stroke-width="{stroke:.2f}"/>')
    if rings:
        out.append(f'<path d="{d(rings, True)}" fill="#3b82f6" fill-opacity="0.12" '
                   f'fill-rule="evenodd" stroke="#2563eb" stroke-width="{stroke:.2f}"/>')
    if report.reference_lines:
        out.append(f'<path d="{d(report.reference_lines, False)}" fill="none" '
                   f'stroke="#475569" stroke-width="{stroke * 0.7:.2f}" '
                   f'stroke-dasharray="{stroke * 4:.1f} {stroke * 3:.1f}"/>')
    if report.open_lines:
        out.append(f'<path d="{d(report.open_lines, False)}" fill="none" stroke="#dc2626" '
                   f'stroke-width="{stroke * 1.5:.2f}"/>')
    out.append("</svg>")
    return "".join(out)
