"""
view/dxf_part_view.py
=====================
The input side of the DXF nesting page: turning uploaded DXF files into
configured product dicts.

sync_store() parses each uploaded file once (cached in session state) and keeps
upload order. render_part_config() draws one part's card — warnings, the layer
picker, the "main part only" toggle, a live preview, and the material /
thickness / size / quantity inputs — and returns the product dict the nesting
and pricing steps consume, or None when the part cannot be placed yet.

Performance
-----------
A parsed ``DxfPart`` never changes after ``parse_dxf``, yet Streamlit re-runs
this whole module on every widget interaction — including edits to unrelated
cards. Two things are therefore memoised per file in session state so a rerun
touches them at most once:

  - the *static* layer info (available / suggested layers and per-layer sizes),
    which is a pure function of the parsed geometry; and
  - the built geometry + its preview SVG, keyed by the chosen layers and the
    "main part only" flag — this is the expensive step (it runs the
    connected-component clustering and the flood-fill mask in
    ``core.dxf.extract_main_part``).

Both caches are pruned in sync_store() when a file is removed, so they never
outlive their part.
"""

import streamlit as st

from core.dxf import parse_dxf
from view.product_view import render_material_thickness

_STORE      = "dxf_store"        # {file_id: DxfPart}
_LAYER_INFO = "dxf_layer_info"   # {file_id: {"avail", "suggested", "sizes"}}
_GEOM_CACHE = "dxf_geom_cache"   # {(file_id, layers_key, main_only): (geom, svg)}
_CONFIG     = "dxf_part_config"  # {file_id: {"material", "thickness"}}


def sync_store(uploaded) -> list[tuple[str, object]]:
    """Parse newly uploaded files once, drop removed ones, keep upload order.

    Returns a list of (file_id, DxfPart). Parsing is cached per file_id in
    session_state so it does not re-run on every rerun; the derived layer-info
    and geometry caches are pruned in lock-step when a file goes away.
    """
    store: dict = st.session_state.setdefault(_STORE, {})
    uploaded = uploaded or []
    current_ids = {u.file_id for u in uploaded}

    stale = [fid for fid in store if fid not in current_ids]
    if stale:
        _evict(set(stale))

    parts: list[tuple[str, object]] = []
    for up in uploaded:
        if up.file_id not in store:
            store[up.file_id] = parse_dxf(up.getvalue(), up.name)
        parts.append((up.file_id, store[up.file_id]))
    return parts


def _evict(fids: set[str]) -> None:
    """Drop every cached artefact belonging to the given file ids."""
    store: dict = st.session_state.get(_STORE, {})
    layer_info: dict = st.session_state.get(_LAYER_INFO, {})
    geom_cache: dict = st.session_state.get(_GEOM_CACHE, {})
    configs: dict = st.session_state.get(_CONFIG, {})
    for fid in fids:
        store.pop(fid, None)
        layer_info.pop(fid, None)
        configs.pop(fid, None)
        # Also drop the material/thickness selectboxes' own keyed widget state
        # so nothing about a removed file lingers in session_state.
        for wk in (f"dxf_mat_{fid}", f"dxf_th_{fid}", f"dxf_th_{fid}_disabled"):
            st.session_state.pop(wk, None)
    for key in [k for k in geom_cache if k[0] in fids]:
        del geom_cache[key]


def _part_config(fid: str) -> dict:
    """The persisted material/thickness for one file — the single source of truth.

    Mirrors the manual calculator's ``calc_products`` pattern: the chosen
    material and thickness are stored explicitly (keyed by file id) rather than
    only living inside the selectbox widget keys, so a later pricing phase can
    read every part's choice from one place. Pruned in _evict on file removal.
    """
    configs: dict = st.session_state.setdefault(_CONFIG, {})
    return configs.setdefault(fid, {"material": None, "thickness": None})


def render_part_config(
    fid: str,
    part,
    idx: int,
    materials: list[str],
    lookup: dict,
) -> dict | None:
    """Draw one part's card and return a product dict for nesting (or None)."""
    with st.container(border=True):
        hdr = st.columns([6, 2])
        hdr[0].markdown(f"**#{idx + 1}**")

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

        selected_layers = _render_layer_picker(fid, part)

        main_only = st.checkbox(
            "Vain pääkappale (poista irralliset lisäkuvat)",
            value=True,
            key=f"dxf_mainonly_{fid}",
            help="Pitää suurimman yhtenäisen kappaleen ja sen reiät/aukot, mutta "
                 "poistaa erilliset apukuvat ja yksityiskohdat, jotka ovat osan "
                 "ulkopuolella.",
        )

        # Expensive step (clustering + flood-fill mask) — memoised per file and
        # per (layers, main_only) so unrelated reruns don't recompute it.
        geom, preview_svg, layers_key = _build_geometry(
            fid, part, selected_layers, main_only,
        )
        if geom.width <= 0 or geom.height <= 0:
            st.warning("Valituilla tasoilla ei ole geometriaa. Valitse tasoja uudelleen.")
            return None

        hdr[1].markdown(
            f":gray[{geom.width:g} × {geom.height:g} mm · {part.unit_label}]"
        )

        # Live preview so the user can confirm only the product is left.
        st.markdown(preview_svg, unsafe_allow_html=True)

        # Material + thickness — persisted per file (one source of truth) and
        # shared with the manual calculator cards. Seed the selectboxes from the
        # stored choice, then write the current choice back into it.
        cfg = _part_config(fid)
        material, thickness = render_material_thickness(
            materials, lookup,
            mat_key=f"dxf_mat_{fid}", thick_key=f"dxf_th_{fid}",
            mat_default=cfg["material"], thick_default=cfg["thickness"],
        )
        cfg["material"] = material
        cfg["thickness"] = thickness

        det_w, det_h = round(geom.width, 1), round(geom.height, 1)
        width, height, qty = _render_size_inputs(fid, det_w, det_h, layers_key, main_only)

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


def _layer_info(fid: str, part) -> dict:
    """Static per-file layer data (available / suggested layers, sizes).

    All three derive purely from the parsed geometry, which never changes, so
    they are computed once per file and reused across reruns instead of being
    recomputed (each scans every point) on every interaction.
    """
    cache: dict = st.session_state.setdefault(_LAYER_INFO, {})
    info = cache.get(fid)
    if info is None:
        info = {
            "avail":     part.available_layers(),
            "suggested": part.suggested_layers(),
            "sizes":     part.layer_sizes(),
        }
        cache[fid] = info
    return info


def _build_geometry(fid: str, part, selected_layers: set | None, main_only: bool):
    """Return (geometry, preview_svg, layers_key), memoised per file + choice.

    ``layers_key`` is the canonical, hashable form of the layer selection
    (``None`` = all layers), reused both as the cache key and to seed the size
    inputs' widget key.
    """
    layers_key = None if selected_layers is None else tuple(sorted(selected_layers))
    cache: dict = st.session_state.setdefault(_GEOM_CACHE, {})
    key = (fid, layers_key, main_only)
    cached = cache.get(key)
    if cached is None:
        geom = part.build(selected_layers, main_only=main_only)
        svg = _preview_svg(geom.polylines, geom.width, geom.height)
        cached = (geom, svg)
        cache[key] = cached
    geom, svg = cached
    return geom, svg, layers_key


def _render_layer_picker(fid: str, part) -> set | None:
    """Layer multiselect, shown only when a drawing has more than one geometry
    layer so a frame / dimensions / text / bend lines can be dropped. Annotation
    layers are hidden by default. Returns the selected layers, or None for all.
    """
    info = _layer_info(fid, part)
    avail_layers = info["avail"]
    if len(avail_layers) <= 1:
        return None

    sizes = info["sizes"]
    suggested = info["suggested"]

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
    return selected_layers


def _render_size_inputs(
    fid: str,
    det_w: float,
    det_h: float,
    layers_key: tuple[str, ...] | None,
    main_only: bool,
) -> tuple[float, float, int]:
    """Width / height / quantity inputs. Detected size seeds the defaults; the
    widget key includes the layer signature so changing layers reseeds the
    numbers to the new geometry. Returns ``(width, height, qty)``.
    """
    sig = ("-".join(layers_key) if layers_key else "all") \
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
    return width, height, qty


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
