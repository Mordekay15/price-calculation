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

Nesting settings (``view/nesting_settings_view``) feed straight into Sparrow:
  * "Sijoittelutapa" groups the parts into separate Sparrow instances — one per
    (material, thickness) when combined, one per part when separate;
  * "Rankaväli" is passed as Sparrow's ``--min-item-separation`` (min gap
    between placed parts);
  * "Pitkän sivun kynsirainan leveys" is subtracted from the usable strip
    height so no part is placed in the clamp zone, which is then drawn on the
    layout preview.

The original DXF is always kept as the source of truth; Sparrow's polygon is only
used to compute placement.
"""

from __future__ import annotations

import json
import math

import streamlit as st

from core.sparrow_input import build_job, validate_instance
from core.sparrow_preview import render_layout_svg
from core.sparrow_reconstruct import reconstruct_dxf
from core.sparrow_runner import RunStatus, find_executable, run_sparrow
from view.nesting_settings_view import render_nesting_settings

# Rotation presets offered in the UI → allowed_orientations (degrees).
# None means "continuous rotation" (allowed_orientations omitted from the item).
_ROTATION_PRESETS: dict[str, tuple[float, ...] | None] = {
    "0° / 90° / 180° / 270°": (0.0, 90.0, 180.0, 270.0),
    "0° / 180°": (0.0, 180.0),
    "Ei kiertoa (0°)": (0.0,),
    "Vapaa kierto": None,
}


def render(uploaded, products: list[dict] | None = None) -> None:
    """Render the Sparrow-instance builder for the uploaded DXF files.

    Quantities, material and thickness come from the per-part cards
    (``products``, keyed by file id); a file with no card entry falls back to
    quantity 1 and no material.
    """
    if not uploaded:
        st.info("Lataa DXF-tiedostot yllä luodaksesi Sparrow-syötteen.")
        return

    meta_by_fid = {p["id"]: p for p in (products or [])}

    # ── Fixed sheet size + rotation ────────────────────────────────────────────
    # Sparrow is a *strip* packer: it keeps `strip_height` fixed and minimises
    # the length, so the sheet height is the hard constraint and the sheet width
    # is the length budget the result is checked against below.
    c1, c2, c3 = st.columns(3)
    sheet_width = c1.number_input(
        "Levyn leveys (mm)",
        min_value=1.0,
        value=2500.0,
        step=50.0,
        key="sparrow_sheet_width",
        help="Levyn pituus, jonka suuntaan osat sijoitellaan. Sparrow minimoi "
             "käytetyn pituuden; tämä on käytettävissä oleva enimmäispituus, "
             "johon tulosta verrataan.",
    )
    sheet_height = c2.number_input(
        "Levyn korkeus (mm)",
        min_value=1.0,
        value=1250.0,
        step=50.0,
        key="sparrow_strip_height",
        help="Levyn kiinteä mitta. Jokaisen osan on mahduttava tähän "
             "korkeuteen (miinus kynsiraina) jossakin sallitussa kierrossa.",
    )
    preset_label = c3.selectbox(
        "Sallitut kierrot",
        options=list(_ROTATION_PRESETS.keys()),
        key="sparrow_rotations",
        help="Kulmat, joissa osa saa sijoittua levylle.",
    )
    orientations = _ROTATION_PRESETS[preset_label]

    # Nesting mode + spacing on the left, the per-side sheet margins on the
    # right (clamp replaced by per-side margins).
    settings_col, margins_col = st.columns(2)
    with settings_col:
        nest_mode, rankavali_mm, _ = render_nesting_settings(
            key_prefix="sparrow",
            separate_label="Laske jokainen osa erikseen",
            show_clamp=False,
        )
    with margins_col:
        margins = _render_sheet_margins("sparrow", sheet_width, sheet_height)

    # Height margins (top+bottom) are a hard constraint: they shrink the usable
    # strip height Sparrow packs into. Length margins (left+right) shrink the
    # usable length the result is checked against below.
    usable_height = float(sheet_height) - margins["top"] - margins["bottom"]
    usable_length = float(sheet_width) - margins["left"] - margins["right"]
    if usable_height <= 0 or usable_length <= 0:
        st.error("Reunavälit ovat vähintään levyn koko — osille ei jää tilaa.")
        return
    if any(margins.values()):
        st.caption(
            f"Käytettävä alue osille: {usable_length:g} × {usable_height:g} mm "
            f"(reunoille varattu — ylä {margins['top']:g}, ala {margins['bottom']:g}, "
            f"vasen {margins['left']:g}, oikea {margins['right']:g} mm)."
        )

    # ── Group the parts into Sparrow instances ─────────────────────────────────
    groups = _group_uploaded(uploaded, meta_by_fid, nest_mode)
    multi = len(groups) > 1

    built: list[tuple] = []  # (gid, label, name, instance, sources)
    for gid, label, files in groups:
        inputs = [
            (f.getvalue(), f.name, int(meta_by_fid.get(f.file_id, {}).get("qty", 1)))
            for f in files
        ]
        name = _instance_name(files, gid)
        job_kwargs = {} if orientations is None else {"allowed_orientations": orientations}
        instance, sources = build_job(
            inputs, strip_height=usable_height, name=name, **job_kwargs
        )
        if multi:
            st.markdown(f"#### {label}")
        problems = _render_group_input(
            instance, sources, name, sheet_width, sheet_height, gid
        )
        if sources and not problems:
            built.append((gid, label, name, instance, sources))

    if not built:
        return

    # ── Run controls (shared by every group) ───────────────────────────────────
    st.divider()
    st.markdown("**Aja Sparrow**")

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
    # Rankaväli → Sparrow's minimum gap between placed parts.
    separation = float(rankavali_mm) if rankavali_mm > 0 else None

    if st.button("Aja Sparrow", key="sparrow_run"):
        for gid, label, name, instance, sources in built:
            with st.spinner(f"Sparrow ajaa nestausta ({label})…"):
                result = run_sparrow(
                    instance,
                    executable=exe,
                    time_limit_sec=int(time_limit),
                    seed=int(seed),
                    min_item_separation=separation,
                )
            recon = None
            preview_svg = None
            if result.ok and result.solution is not None:
                mode = "original" if preserve else "polygon"
                with st.spinner(f"Rakennetaan tuotanto-DXF ({label})…"):
                    recon = reconstruct_dxf(result.solution, sources, mode=mode)
                preview_svg = render_layout_svg(
                    result.solution, sources,
                    sheet_margins=margins, sheet_width=float(sheet_width),
                )
            st.session_state[f"sparrow_result_{gid}"] = result
            st.session_state[f"sparrow_recon_{gid}"] = recon
            st.session_state[f"sparrow_preview_{gid}"] = preview_svg
            st.session_state[f"sparrow_name_{gid}"] = name

    # ── Results per group ──────────────────────────────────────────────────────
    for gid, label, name, instance, sources in built:
        result = st.session_state.get(f"sparrow_result_{gid}")
        if result is None:
            continue
        if multi:
            st.markdown(f"#### {label} — tulos")
        _render_run_result(
            result,
            st.session_state.get(f"sparrow_preview_{gid}"),
            usable_length=float(usable_length),
        )
        _render_reconstruction(
            st.session_state.get(f"sparrow_recon_{gid}"),
            st.session_state.get(f"sparrow_name_{gid}", name),
            gid,
        )


# ── Grouping ─────────────────────────────────────────────────────────────────

def _group_uploaded(uploaded, meta_by_fid: dict, nest_mode: str):
    """Group uploaded files into Sparrow instances.

    ``combined`` → one group per (material, thickness) so only parts that can
    share a physical sheet are nested together; ``separate`` → one group per
    file. Returns ``[(gid, label, files), ...]`` in a stable order; ``gid`` is a
    session-key-safe id.
    """
    if nest_mode == "separate":
        return [(f"file_{f.file_id}", _stem(f.name), [f]) for f in uploaded]

    groups: dict = {}
    order: list = []
    for f in uploaded:
        meta = meta_by_fid.get(f.file_id, {})
        key = (meta.get("material"), meta.get("thickness"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    out = []
    for key in order:
        material, thickness = key
        label = f"{material or 'Ei materiaalia'} · {thickness or '—'} mm"
        gid = "grp_" + _safe_key(f"{material}__{thickness}")
        out.append((gid, label, groups[key]))
    return out


def _render_sheet_margins(
    key_prefix: str = "sparrow",
    sheet_width: float = 2500.0,
    sheet_height: float = 1250.0,
) -> dict[str, float]:
    """Per-side unusable sheet margins (mm), laid out like the sheet's edges.

    The centre shows a live diagram of the sheet: the margins as a red ring
    shrinking the blue usable area, in the sheet's real proportions. Returns
    ``{"top", "bottom", "left", "right"}``, all default 0. These edges cannot
    hold parts (clamp / gripper zones); the sheet is still bought full size, so
    weight and price come from the gross dimensions.
    """
    st.markdown("**Levyn reunavälit (mm)**")
    st.caption(
        "Reuna-alueet, joille ei sijoiteta osia (esim. kynsiraina/tarttujat). "
        "Levy ostetaan silti täysikokoisena."
    )

    def _edge(col, label: str, side: str) -> float:
        return float(col.number_input(
            label, min_value=0, value=0, step=1,
            key=f"{key_prefix}_margin_{side}",
        ))

    def _val(side: str) -> float:
        return float(st.session_state.get(f"{key_prefix}_margin_{side}", 0) or 0)

    top_row = st.columns([1, 2, 1])
    top = _edge(top_row[1], "Yläreuna", "top")

    mid_row = st.columns([1, 2, 1])
    left = _edge(mid_row[0], "Vasen", "left")
    # Live sheet diagram — reads the current values (committed to session_state
    # before the rerun) so it updates as the margins change.
    mid_row[1].markdown(
        _margin_preview_svg(
            sheet_width, sheet_height,
            _val("top"), _val("bottom"), _val("left"), _val("right"),
        ),
        unsafe_allow_html=True,
    )
    right = _edge(mid_row[2], "Oikea", "right")

    bot_row = st.columns([1, 2, 1])
    bottom = _edge(bot_row[1], "Alareuna", "bottom")

    return {"top": top, "bottom": bottom, "left": left, "right": right}


def _margin_preview_svg(
    sheet_w: float, sheet_h: float,
    top: float, bottom: float, left: float, right: float,
) -> str:
    """A small live diagram of the sheet: red margin ring around a blue usable
    area, in the sheet's real proportions. Illustrative only.
    """
    sheet_w = max(1.0, float(sheet_w))
    sheet_h = max(1.0, float(sheet_h))
    W = 240.0
    H = max(90.0, min(150.0, W * sheet_h / sheet_w))
    pad = 8.0
    ow, oh = W - 2 * pad, H - 2 * pad
    sx, sy = ow / sheet_w, oh / sheet_h

    li = min(max(0.0, left), sheet_w) * sx
    ri = min(max(0.0, right), sheet_w) * sx
    ti = min(max(0.0, top), sheet_h) * sy
    bi = min(max(0.0, bottom), sheet_h) * sy
    iw = max(4.0, ow - li - ri)
    ih = max(4.0, oh - ti - bi)
    ix = pad + li
    iy = pad + ti

    return (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" width="100%" '
        f'style="max-width:240px;height:auto;display:block;margin:4px auto 0">'
        f'<rect x="{pad:.1f}" y="{pad:.1f}" width="{ow:.1f}" height="{oh:.1f}" '
        f'rx="4" fill="#fecaca" fill-opacity="0.55" stroke="#94a3b8" '
        f'stroke-width="1.5"/>'
        f'<rect x="{ix:.1f}" y="{iy:.1f}" width="{iw:.1f}" height="{ih:.1f}" '
        f'rx="2" fill="#bfdbfe" stroke="#3b82f6" stroke-width="1.2"/>'
        f'<text x="{W / 2:.0f}" y="{pad + oh / 2:.0f}" text-anchor="middle" '
        f'dominant-baseline="central" font-family="sans-serif" font-size="12" '
        f'fill="#1e3a8a">Levy</text>'
        f'</svg>'
    )


def _safe_key(s: str) -> str:
    """Reduce a string to characters safe for a session-state key."""
    return "".join(ch if ch.isalnum() else "_" for ch in str(s))


def _stem(name: str) -> str:
    """File name without path or the .dxf extension."""
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base[:-4] if base.lower().endswith(".dxf") else base


def _instance_name(files, gid: str) -> str:
    """A Sparrow instance name built from the group's file stems."""
    stems = "_".join(_stem(f.name) for f in files)
    return ("stremet_" + stems)[:60] or gid


# ── Per-group input section (summary, validation, JSON download) ───────────────

def _render_group_input(
    instance: dict,
    sources: list,
    name: str,
    sheet_width: float,
    sheet_height: float,
    gid: str,
) -> list[str]:
    """Show one group's summary, validation and JSON download. Returns problems."""
    if not sources:
        st.warning(
            "Ladatuista tiedostoista ei löytynyt yhtään suljettua osaa — "
            "Sparrow-syötettä ei voi luoda."
        )
        return ["no parts"]

    problems = validate_instance(instance)

    total_demand = sum(int(s.quantity) for s in sources)
    m1, m2, m3 = st.columns(3)
    m1.metric("Osia (yksilöllisiä)", len(sources))
    m2.metric("Kappaleita yhteensä", total_demand)
    m3.metric("Levyn koko (mm)", f"{sheet_width:g} × {sheet_height:g}")

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
        file_name=f"{name}.json",
        mime="application/json",
        key=f"sparrow_download_{gid}",
        disabled=bool(problems),
    )
    with st.expander("Näytä JSON"):
        st.code(json_text, language="json")

    return problems


def _render_reconstruction(recon, name: str, gid: str) -> None:
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
        key=f"sparrow_dxf_download_{gid}",
    )


def _render_sheet_fit(
    used_length: float,
    strip_height: float | None,
    density: float | None,
    usable_length: float | None,
) -> None:
    """Compare the used strip length against the usable sheet length (budget).

    ``usable_length`` is the sheet length minus the left/right margins, and
    ``strip_height`` the usable height Sparrow packed into (sheet height minus
    top/bottom margins), so the fit is checked against the real usable area.
    """
    if not usable_length or usable_length <= 0 or strip_height is None:
        return
    if used_length <= usable_length + 1e-6:
        msg = (
            f"Mahtuu yhdelle levylle — käytetty {used_length:.1f} / "
            f"{usable_length:g} mm (käytettävä alue {usable_length:g} × "
            f"{strip_height:g} mm)."
        )
        if density is not None:
            # Exact one-sheet fill: placed area / usable area.
            sheet_fill = density * used_length / usable_length
            msg += f" Täyttöaste käytettävästä alueesta: {sheet_fill * 100:.1f} %."
        st.success(msg)
    else:
        sheets = math.ceil(used_length / usable_length - 1e-9)
        st.warning(
            f"Ei mahdu yhdelle levylle — jatkuva pituus {used_length:.1f} mm "
            f"≈ {sheets} levyä (käytettävä pituus {usable_length:g} mm/levy). "
            f"Arvio: osa voi jäädä levyjen rajalle."
        )


def _render_run_result(
    result, preview_svg: str | None = None, usable_length: float | None = None
) -> None:
    """Show a SparrowResult: status, counts, stats, and the layout preview.

    ``usable_length`` is the sheet length minus the left/right margins — the
    budget the used strip length is checked against, since Sparrow only
    minimises the length (fits one sheet, or an estimated sheet count — an
    estimate because a part may straddle a sheet boundary).
    """
    import streamlit.components.v1 as components

    if result.status is RunStatus.OK:
        st.success(result.message)
    else:
        st.error(f"[{result.status.value}] {result.message}")

    r1, r2, r3 = st.columns(3)
    r1.metric("Pyydetty (kpl)", result.total_requested)
    r2.metric("Sijoitettu (kpl)", result.total_placed)
    if result.density is not None:
        r3.metric("Täyttöaste (strip)", f"{result.density * 100:.1f} %")
    if result.strip_width is not None:
        used = result.strip_width
        st.caption(
            f"Käytetty pituus: {used:.1f} mm · korkeus: {result.strip_height:g} mm"
        )
        _render_sheet_fit(used, result.strip_height, result.density, usable_length)

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
