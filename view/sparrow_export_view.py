"""
view/sparrow_export_view.py
===========================
Sparrow — export, run, and reconstruct UI (Phases 7–9).

Takes the uploaded DXF files and:
  * builds a Sparrow strip-packing instance + per-item source records
    (``core/sparrow_input.build_job``), validates it, offers the JSON (Phase 7),
  * runs Sparrow on it (``core/sparrow_runner``) and shows the layout (Phase 8),
  * reconstructs the production DXF from the solution by re-applying each
    placement to the *original* entities (``core/sparrow_reconstruct``), with a
    download and the sheet-bounds / placed-count checks (Phase 9).

The original DXF is always kept as the source of truth; Sparrow's polygon is only
used to compute placement.
"""

from __future__ import annotations

import json

import streamlit as st

from core.sparrow_input import build_job, validate_instance
from core.sparrow_preview import render_layout_svg
from core.sparrow_reconstruct import reconstruct_dxf
from core.sparrow_runner import RunStatus, find_executable, run_sparrow

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

    # ── Build the instance + per-item source records (Phase 7 + 9) ─────────────
    instance_name = "stremet_" + "_".join(
        f.name.rsplit(".", 1)[0] for f in uploaded
    )[:60]
    inputs = [
        (file.getvalue(), file.name, int(quantities[file.name]))
        for file in uploaded
    ]
    job_kwargs = {} if orientations is None else {"allowed_orientations": orientations}
    instance, sources = build_job(
        inputs, strip_height=float(strip_height), name=instance_name, **job_kwargs
    )

    if not sources:
        st.warning(
            "Ladatuista tiedostoista ei löytynyt yhtään suljettua osaa — "
            "Sparrow-syötettä ei voi luoda."
        )
        return

    problems = validate_instance(instance)

    # ── Summary ────────────────────────────────────────────────────────────────
    total_demand = sum(int(s.quantity) for s in sources)
    m1, m2, m3 = st.columns(3)
    m1.metric("Osia (yksilöllisiä)", len(sources))
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
            "id": item["id"],
            "part_id": item["part_id"],
            "dxf": item["dxf"],
            "kpl": item["demand"],
            "muoto": item["shape"]["type"],
        }
        for item in instance["items"]
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

    # ── Run Sparrow (Phase 8) ──────────────────────────────────────────────────
    st.markdown("**Aja Sparrow**")
    if problems:
        st.info("Korjaa syötteen virheet ennen ajoa.")
        return

    exe = find_executable()
    if exe is None:
        st.info(
            "Sparrow-suoritustiedostoa ei löytynyt tästä ympäristöstä. "
            "Aseta polku `SPARROW_BIN`-ympäristömuuttujaan ajaaksesi nestauksen."
        )
        return

    rc1, rc2, rc3 = st.columns(3)
    time_limit = rc1.number_input(
        "Aikaraja (s)", min_value=1, value=10, step=1, key="sparrow_time_limit"
    )
    seed = rc2.number_input(
        "Siemen (seed)", min_value=0, value=0, step=1, key="sparrow_seed"
    )
    preserve = rc3.checkbox(
        "Säilytä alkuperäiset entiteetit",
        value=True,
        key="sparrow_preserve",
        help="Päällä: tuotanto-DXF säilyttää kaaret, reiät ja tasot. "
             "Pois: vain Sparrow-polygonit (vain testiin).",
    )

    if st.button("Aja Sparrow", key="sparrow_run"):
        with st.spinner("Sparrow ajaa nestausta…"):
            result = run_sparrow(
                instance,
                executable=exe,
                time_limit_sec=int(time_limit),
                seed=int(seed),
            )
        st.session_state["sparrow_result"] = result
        # Phase 9: reconstruct the production DXF + a real-geometry preview
        recon = None
        preview_svg = None
        if result.ok and result.solution is not None:
            mode = "original" if preserve else "polygon"
            with st.spinner("Rakennetaan tuotanto-DXF…"):
                recon = reconstruct_dxf(result.solution, sources, mode=mode)
            preview_svg = render_layout_svg(result.solution, sources)
        st.session_state["sparrow_recon"] = recon
        st.session_state["sparrow_preview"] = preview_svg
        st.session_state["sparrow_name"] = instance_name

    result = st.session_state.get("sparrow_result")
    if result is not None:
        _render_run_result(result, st.session_state.get("sparrow_preview"))
        _render_reconstruction(
            st.session_state.get("sparrow_recon"),
            st.session_state.get("sparrow_name", instance_name),
        )


def _render_reconstruction(recon, name: str) -> None:
    """Show the Phase-9 reconstructed DXF: checks + download."""
    if recon is None:
        return
    st.markdown("**Tuotanto-DXF (rekonstruktio)**")

    if recon.ok:
        st.success(
            f"DXF rakennettu: {recon.placed_count} osaa, tasot "
            f"{', '.join(recon.layers) or '—'}."
        )
    else:
        st.error("DXF rakennettiin, mutta tarkistukset löysivät ongelmia:")

    if recon.placed_count != recon.requested_count:
        st.warning(
            f"Sijoitettu {recon.placed_count} ≠ pyydetty {recon.requested_count}."
        )
    for msg in recon.out_of_bounds:
        st.error(f"Levyn ulkopuolella — {msg}")
    for msg in recon.warnings:
        st.warning(msg)

    if recon.entity_counts:
        st.caption(
            "Entiteetit: "
            + ", ".join(f"{t}×{n}" for t, n in sorted(recon.entity_counts.items()))
            + f"  ·  tila: {recon.mode}"
        )

    st.download_button(
        "Lataa tuotanto-DXF",
        data=recon.dxf_bytes,
        file_name=f"final_{name}.dxf",
        mime="image/vnd.dxf",
        key="sparrow_dxf_download",
    )


def _render_run_result(result, preview_svg: str | None = None) -> None:
    """Show a SparrowResult: status, counts, stats, and the layout preview."""
    import streamlit.components.v1 as components

    if result.status is RunStatus.OK:
        st.success(result.message)
    else:
        st.error(f"[{result.status.value}] {result.message}")

    r1, r2, r3 = st.columns(3)
    r1.metric("Pyydetty (kpl)", result.total_requested)
    r2.metric("Sijoitettu (kpl)", result.total_placed)
    if result.density is not None:
        r3.metric("Täyttöaste", f"{result.density * 100:.1f} %")
    if result.strip_width is not None:
        st.caption(
            f"Levyn pituus (strip width): {result.strip_width:.2f} mm · "
            f"korkeus: {result.strip_height:g} mm"
        )

    # Main preview: real geometry with every hole visible.
    if preview_svg:
        st.caption("Asettelu todellisella geometrialla (reiät näkyvissä):")
        components.html(
            f'<div style="width:100%;overflow:auto;background:#fff">{preview_svg}</div>',
            height=460,
            scrolling=True,
        )
        # Sparrow's own preview (outline + collision surrogate) kept for reference.
        if result.svg:
            with st.expander("Sparrowin oma esikatselu (polygoni + törmäysympyrä)"):
                components.html(
                    f'<div style="width:100%;overflow:auto;background:#fff">{result.svg}</div>',
                    height=440,
                    scrolling=True,
                )
    elif result.svg:
        components.html(
            f'<div style="width:100%;overflow:auto;background:#fff">{result.svg}</div>',
            height=440,
            scrolling=True,
        )

    if result.stderr and result.status is not RunStatus.OK:
        with st.expander("Sparrowin virhetuloste"):
            st.code(result.stderr[:4000])
