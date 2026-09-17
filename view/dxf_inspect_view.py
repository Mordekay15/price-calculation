"""
view/dxf_inspect_view.py
========================
Sparrow — DXF inspection UI (Phase 6).

Renders the read-only inspection report produced by ``core/dxf_inspect.py`` for
each uploaded DXF file. This is a diagnostic view: it shows what the parser found
(units, entities, layers, contours, bounding box, holes, invalid geometry) so we
can confirm one real file is read correctly before building nesting on top of it.
It does not price or nest anything.
"""

from __future__ import annotations

import streamlit as st

from core.dxf_inspect import InspectionReport, SUPPORTED_TYPES, inspect_dxf


def render(uploaded) -> None:
    """Show an inspection report for each uploaded DXF file."""
    if not uploaded:
        st.info("Lataa vähintään yksi DXF-tiedosto tarkastettavaksi.")
        return

    for file in uploaded:
        report = inspect_dxf(file.getvalue(), file.name)
        _render_report(report)


def _render_report(report: InspectionReport) -> None:
    n_parts = len(report.parts)
    header = f"📐 {report.name} — {n_parts} osa{'' if n_parts == 1 else 'a'}"
    with st.expander(header, expanded=True):
        # ── Warnings & invalid geometry ────────────────────────────────────────
        for msg in report.invalid:
            st.error(f"⚠ {msg}")
        for msg in report.warnings:
            st.warning(msg)

        # ── Headline numbers ───────────────────────────────────────────────────
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Osat (suljetut)", n_parts)
        c2.metric("Reiät", len(report.holes))
        c3.metric("Avoimet ääriviivat", len(report.open_contours))
        unit = report.unit_label + (" *" if report.unit_assumed else "")
        c4.metric("Yksikkö", unit, help="* oletettu — piirustuksessa ei ollut $INSUNITS")

        box = report.bbox_mm
        if box is not None:
            c5, c6, c7 = st.columns(3)
            c5.metric("Leveys (mm)", f"{report.width_mm:.2f}")
            c6.metric("Korkeus (mm)", f"{report.height_mm:.2f}")
            c7.metric("Mittakaava", f"{report.mm_per_unit:g} mm/yksikkö")
            st.caption(
                f"Rajauslaatikko: x {box[0]:.2f}…{box[2]:.2f} mm, "
                f"y {box[1]:.2f}…{box[3]:.2f} mm"
            )
        else:
            st.info(
                "Piirustuksesta ei löytynyt yhtään suljettua osaa. Jos siinä on "
                "vain avoimia viivoja, ne ovat apuviivoja — ei osia."
            )

        # ── Parts detail ───────────────────────────────────────────────────────
        if report.parts:
            st.markdown("**Osat**")
            st.table([
                {
                    "#": i,
                    "Leveys (mm)": f"{c.width_mm:.2f}",
                    "Korkeus (mm)": f"{c.height_mm:.2f}",
                    "Pinta-ala (mm²)": f"{c.area_mm2:.1f}",
                    "Entiteetit": ", ".join(c.entity_types),
                    "Tasot": ", ".join(c.layers),
                    "Itseleikkaava": "kyllä" if c.self_intersects else "ei",
                }
                for i, c in enumerate(report.parts, 1)
            ])

        # ── Entity types ───────────────────────────────────────────────────────
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Entiteettityypit**")
            st.table([
                {
                    "Tyyppi": t,
                    "Määrä": n,
                    "Luettu": "✓" if t in SUPPORTED_TYPES else "— (ei vielä)",
                }
                for t, n in sorted(report.entity_counts.items())
            ] or [{"Tyyppi": "—", "Määrä": 0, "Luettu": ""}])
        with colB:
            st.markdown("**Tasot (layers)**")
            st.table([
                {"Taso": layer, "Entiteettejä": n}
                for layer, n in sorted(report.layer_counts.items())
            ] or [{"Taso": "—", "Entiteettejä": 0}])
