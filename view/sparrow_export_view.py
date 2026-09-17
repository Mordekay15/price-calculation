"""
view/sparrow_export_view.py
===========================
Sparrow — DXF → Sparrow instance export UI (Phase 7).

Takes the uploaded DXF files, converts each part into a Sparrow item
(``core/sparrow_input.py``), assembles one strip-packing instance, validates it
against the jagua-rs schema, and offers the JSON for download. The original DXF
files are kept separately — the JSON only references them by name, so Sparrow's
polygon (an approximation used for placement) never replaces the real geometry.
"""

from __future__ import annotations

import json

import streamlit as st

from core.sparrow_input import (
    build_instance,
    parts_from_dxf,
    validate_instance,
)

# Rotation presets offered in the UI → allowed_orientations (degrees).
# None means "continuous rotation" (allowed_orientations omitted from the item).
_ROTATION_PRESETS: dict[str, tuple[float, ...] | None] = {
    "0° / 90° / 180° / 270°": (0.0, 90.0, 180.0, 270.0),
    "0° / 180°": (0.0, 180.0),
    "Ei kiertoa (0°)": (0.0,),
    "Vapaa kierto": None,
}


def render(uploaded) -> None:
    """Render the Sparrow-instance builder for the uploaded DXF files."""
    if not uploaded:
        st.info("Lataa DXF-tiedostot yllä luodaksesi Sparrow-syötteen.")
        return

    c1, c2 = st.columns(2)
    strip_height = c1.number_input(
        "Levyn korkeus (strip_height, mm)",
        min_value=1.0,
        value=1000.0,
        step=50.0,
        key="sparrow_strip_height",
        help="Levyn kiinteä mitta (esim. levyn leveys). Sparrow minimoi pituuden.",
    )
    preset_label = c2.selectbox(
        "Sallitut kierrot",
        options=list(_ROTATION_PRESETS.keys()),
        key="sparrow_rotations",
        help="Kulmat, joissa osa saa sijoittua levylle.",
    )
    orientations = _ROTATION_PRESETS[preset_label]

    st.markdown("**Määrät (kpl / osa)**")
    quantities: dict[str, int] = {}
    for file in uploaded:
        quantities[file.name] = st.number_input(
            file.name,
            min_value=1,
            value=1,
            step=1,
            key=f"sparrow_qty_{file.name}",
        )

    # ── Build the instance ─────────────────────────────────────────────────────
    all_parts = []
    for file in uploaded:
        kwargs = {} if orientations is None else {"allowed_orientations": orientations}
        parts, _report = parts_from_dxf(
            file.getvalue(), file.name, int(quantities[file.name]), **kwargs
        )
        all_parts.extend(parts)

    if not all_parts:
        st.warning(
            "Ladatuista tiedostoista ei löytynyt yhtään suljettua osaa — "
            "Sparrow-syötettä ei voi luoda."
        )
        return

    instance_name = "stremet_" + "_".join(
        f.name.rsplit(".", 1)[0] for f in uploaded
    )[:60]
    instance = build_instance(
        all_parts, name=instance_name, strip_height=float(strip_height)
    )
    problems = validate_instance(instance)

    # ── Summary ────────────────────────────────────────────────────────────────
    total_demand = sum(int(p.quantity) for p in all_parts)
    m1, m2, m3 = st.columns(3)
    m1.metric("Osia (yksilöllisiä)", len(all_parts))
    m2.metric("Kappaleita yhteensä", total_demand)
    m3.metric("Levyn korkeus (mm)", f"{strip_height:g}")

    if problems:
        st.error("Sparrow-syöte EI ole kelvollinen:")
        for p in problems:
            st.write(f"• {p}")
    else:
        st.success("Sparrow-syöte on kelvollinen (jagua-rs -skeema).")

    st.table([
        {
            "part_id": p.part_id,
            "dxf": p.dxf,
            "kpl": p.quantity,
            "muoto": "polygon (+reiät)" if p.holes else "simple_polygon",
            "reiät": len(p.holes),
            "mitat (mm)": f"{p.width_mm:.1f} × {p.height_mm:.1f}",
        }
        for p in all_parts
    ])

    json_text = json.dumps(instance, indent=2)
    st.download_button(
        "Lataa Sparrow-syöte (JSON)",
        data=json_text,
        file_name=f"{instance_name}.json",
        mime="application/json",
        key="sparrow_download",
        disabled=bool(problems),
    )
    with st.expander("Näytä JSON"):
        st.code(json_text, language="json")
