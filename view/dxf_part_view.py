"""
view/dxf_part_view.py
=====================
The DXF part cards: one card per uploaded file.

sync_store() reads each uploaded file once with ``core.dxf.read_dxf`` (cached
in session state, pruned when a file is removed) and keeps upload order.
render_part_config() draws a card — the measured size, a preview, and the
material / thickness / quantity inputs — and returns the product dict the
pricing uses. A file that cannot be priced shows the reasons instead and
returns None.

The card and the pricing use the same read result, so the size and shape on
the card are exactly what Sparrow nests.
"""

import streamlit as st

from core.dxf import DxfReport, read_dxf
from view.product_view import render_material_thickness

_STORE  = "dxf_store"        # {file_id: DxfReport}
_CONFIG = "dxf_part_config"  # {file_id: {"material", "thickness"}}


def sync_store(uploaded) -> list[tuple[str, DxfReport]]:
    """Read newly uploaded files once, drop removed ones, keep upload order."""
    store: dict = st.session_state.setdefault(_STORE, {})
    uploaded = uploaded or []
    current_ids = {u.file_id for u in uploaded}
    for fid in [f for f in store if f not in current_ids]:
        _evict(fid)

    parts = []
    for up in uploaded:
        if up.file_id not in store:
            store[up.file_id] = read_dxf(up.getvalue(), up.name)
        parts.append((up.file_id, store[up.file_id]))
    return parts


def _evict(fid: str) -> None:
    """Drop everything kept for a removed file, including its widget state."""
    st.session_state.get(_STORE, {}).pop(fid, None)
    st.session_state.get(_CONFIG, {}).pop(fid, None)
    for key in (f"dxf_mat_{fid}", f"dxf_th_{fid}", f"dxf_th_{fid}_disabled", f"dxf_q_{fid}"):
        st.session_state.pop(key, None)


def render_part_config(
    fid: str,
    report: DxfReport,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card; return its product dict, or None if not priceable."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1}** · {report.name}")

        # Text found in the drawing (Mat=…, Thk=…) — shown to cross-check the
        # material choice, never nested.
        if report.texts:
            st.caption("Piirustuksen tekstit: " + " · ".join(report.texts))

        preview = _preview_svg(report)
        if preview:
            st.markdown(preview, unsafe_allow_html=True)

        if report.problems:
            hdr[1].markdown(":red[ei hinnoiteltavissa]")
            st.error(
                "**Tätä tiedostoa ei voi vielä hinnoitella:**\n\n"
                + "\n".join(f"- {p}" for p in report.problems)
            )
            return None

        part = report.part
        width, height = round(part.width_mm, 1), round(part.height_mm, 1)
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
        "name":      report.name,
        "material":  material,
        "thickness": thickness,
        "width":     width,
        "height":    height,
        "qty":       qty,
        "report":    report,
    }


def _preview_svg(report: DxfReport, px: int = 260) -> str:
    """Small preview of what was read (drawing Y flipped).

    Closed outlines are filled (holes cut out), open lines are red — they are
    what makes a file unpriceable — and bend lines are dashed.
    """
    closed = [c.points for c in report.contours if c.closed]
    open_ = [c.points for c in report.contours if not c.closed]
    bends = report.construction_lines
    pts = [p for ring in (*closed, *open_, *bends) for p in ring]
    if not pts:
        return ""
    min_x = min(x for x, _ in pts)
    min_y = min(y for _, y in pts)
    w = max(x for x, _ in pts) - min_x
    h = max(y for _, y in pts) - min_y
    if w <= 0 or h <= 0:
        return ""
    stroke = max(0.5, max(w, h) / 300)

    def d(rings, close):
        return " ".join(
            "M " + " L ".join(f"{x - min_x:.1f} {h - (y - min_y):.1f}" for x, y in ring)
            + (" Z" if close else "")
            for ring in rings if len(ring) >= 2
        )

    scale = px / max(w, h)
    out = [
        f'<svg width="{w * scale:.0f}" height="{h * scale:.0f}" '
        f'viewBox="{-stroke} {-stroke} {w + 2 * stroke:.1f} {h + 2 * stroke:.1f}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'style="background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;'
        f'max-width:100%;height:auto;margin:4px 0 2px;">'
    ]
    if closed:
        out.append(f'<path d="{d(closed, True)}" fill="#3b82f6" fill-opacity="0.12" '
                   f'fill-rule="evenodd" stroke="#2563eb" stroke-width="{stroke:.2f}"/>')
    if bends:
        out.append(f'<path d="{d(bends, False)}" fill="none" stroke="#475569" '
                   f'stroke-width="{stroke * 0.7:.2f}" '
                   f'stroke-dasharray="{stroke * 4:.1f} {stroke * 3:.1f}"/>')
    if open_:
        out.append(f'<path d="{d(open_, False)}" fill="none" stroke="#dc2626" '
                   f'stroke-width="{stroke * 1.5:.2f}"/>')
    out.append("</svg>")
    return "".join(out)
