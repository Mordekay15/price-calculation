"""STEP models on the DXF tab: a dropped STEP file is read into its BOM
(``core.step``), every sheet-metal part is unfolded into a part card (unless
a DXF of the same part number is already on one), and a 3D preview shows the
model. The cards made from a model carry its quantities.

Session keys: ``MODELS`` holds ``{file id: {"name", "model", "flats"}}``,
where ``flats`` maps a part key to its ``FlatPart``. A card made from a
model has the id ``"{file id}|{part key}"``."""

from __future__ import annotations

import streamlit as st

from core import step
from core.dxf import read_dxf
from core.linking import part_key

MODELS = "dxf_step_models"
K_FACTOR = "dxf_step_k"
_SEP = "|"


def is_step(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in ("step", "stp")


def add_model(file_id: str, name: str, data: bytes) -> step.StepModel:
    """Read a dropped STEP file and unfold its parts."""
    model = step.read_step(data, name)
    k = st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)
    st.session_state.setdefault(MODELS, {})[file_id] = {
        "name": name, "model": model,
        "flats": {p.key: step.unfold(p.shape, k) for p in model.parts}}
    return model


def models() -> dict[str, dict]:
    return st.session_state.get(MODELS, {})


def is_assembly(file_id: str) -> bool:
    """A STEP file with more than one part, or several of one: shown in the
    BOM, not as a drawing linked to one card."""
    m = models().get(file_id)
    return m is not None and (len(m["model"].parts) != 1 or m["model"].parts[0].qty != 1)


def card_id(file_id: str, key: str) -> str:
    return f"{file_id}{_SEP}{key}"


def card_source(fid: str) -> tuple[dict, step.StepPart, step.FlatPart] | None:
    """The model, part and flat a card was made from; None for a DXF card."""
    if _SEP not in fid:
        return None
    file_id, key = fid.split(_SEP, 1)
    m = models().get(file_id)
    if m is None:
        return None
    part = next((p for p in m["model"].parts if p.key == key), None)
    return (m, part, m["flats"].get(key)) if part else None


def sync_cards(store: dict, config: dict) -> list[str]:
    """Add a card for every unfolded sheet part that has none yet and no DXF
    card of its part number; returns the new card ids. Its quantity is the
    model's total (seeded once: the card's input changes it after)."""
    dxf_keys = {part_key(d.name) for fid, d in store.items() if _SEP not in fid}
    new = []
    for file_id, m in models().items():
        for p in m["model"].parts:
            flat = m["flats"].get(p.key)
            cid = card_id(file_id, p.key)
            if (flat is None or not flat.is_sheet or cid in store
                    or _keys(m, p) & dxf_keys):
                continue
            store[cid] = _card_file(p, flat)
            config.setdefault(cid, {"material": None, "thickness": None})["qty"] = p.qty
            new.append(cid)
    return new


def replaced_by_dxf(store: dict) -> list[str]:
    """Cards made from a model whose part number now has a DXF card: the DXF
    is used instead."""
    dxf_keys = {part_key(d.name) for fid, d in store.items() if _SEP not in fid}
    out = []
    for fid in store:
        src = card_source(fid)
        if src is not None and _keys(src[0], src[1]) & dxf_keys:
            out.append(fid)
    return out


def _keys(m: dict, part: step.StepPart) -> set[str]:
    """The part numbers a model's part goes by: its name in the model and,
    for a file of one part, the file's name (``ITM-072558_A.stp`` whose body
    is named "Part1")."""
    keys = {part_key(part.name)}
    if len(m["model"].parts) == 1:
        keys.add(part_key(m["name"]))
    return keys


def _card_file(part: step.StepPart, flat: step.FlatPart):
    return read_dxf(step.flat_dxf(flat, part.name), f"{step.safe_name(part.name)}.step")


def refold(store: dict, reports: dict, k: float) -> None:
    """Unfold every model again with a new K factor and redraw its cards."""
    for file_id, m in models().items():
        m["flats"] = {p.key: step.unfold(p.shape, k) for p in m["model"].parts}
        for p in m["model"].parts:
            cid = card_id(file_id, p.key)
            if cid in store and m["flats"][p.key].is_sheet:
                store[cid] = _card_file(p, m["flats"][p.key])
                reports.pop(cid, None)


def remove_model(file_id: str, evict) -> None:
    """Drop a model and the cards made from it (``evict(card id)``)."""
    m = models().pop(file_id, None)
    if m is None:
        return
    for p in m["model"].parts:
        evict(card_id(file_id, p.key))


# ── BOM ───────────────────────────────────────────────────────────────────────

def render_bom(store: dict, reports: dict, labels: dict[str, str], evict) -> None:
    """The models' BOMs, folded under the drop area: every part's quantity,
    what it is (sheet: thickness, bends, flat size; or why not) and its card."""
    all_models = models()
    if not all_models:
        return
    n_parts = sum(len(m["model"].parts) for m in all_models.values())
    with st.expander(f"STEP-mallit ({len(all_models)}) · {n_parts} osaa", expanded=True):
        st.caption("Levyosat avataan tasoaihioiksi ja lisätään osakorteiksi mallin "
                   "määrillä. Jos osalle on jo DXF-kortti samalla osanumerolla, käytetään "
                   "DXF:ää. Muut kappaleet (esim. ostettavat tai koneistetut) näkyvät "
                   "listassa, mutta niitä ei hinnoitella.")
        k = st.number_input(
            "K-kerroin", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
            value=float(st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)),
            key="dxf_step_k_input",
            help="Neutraaliakselin paikka taivutuksessa: avattu pituus = suorat osat + "
                 "kulma × (sisäsäde + K × paksuus). Tyypillisesti 0,33–0,5.")
        if abs(k - st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)) > 1e-9:
            st.session_state[K_FACTOR] = k
            refold(store, reports, k)
            st.rerun()
        dxf_cards = {part_key(d.name): fid for fid, d in store.items() if _SEP not in fid}
        for file_id, m in list(all_models.items()):
            model = m["model"]
            c1, c2 = st.columns([6, 1], vertical_alignment="center")
            c1.markdown(f"🧊 **{m['name']}**")
            c2.button("Poista", key=f"dxf_step_del_{file_id}", on_click=remove_model,
                      args=(file_id, evict))
            for problem in model.problems:
                st.warning(problem)
            rows = []
            for row in model.bom:
                rows.append({"Nimike": " " * row.level + row.name,
                             "Kpl / ylempi": row.qty, "Kpl yhteensä": row.total,
                             **_part_columns(file_id, m, row, store, dxf_cards, labels)})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            sheets = [p for p in model.parts if m["flats"][p.key].is_sheet]
            if model.parts:
                _render_preview_picker(file_id, m, sheets or model.parts)


def _part_columns(file_id, m, row, store, dxf_cards, labels) -> dict:
    if row.part_key is None:
        return {"Tyyppi": "kokoonpano", "Levy": "", "Kortti": ""}
    flat = m["flats"].get(row.part_key)
    part = next(p for p in m["model"].parts if p.key == row.part_key)
    if flat is None or not flat.is_sheet:
        return {"Tyyppi": "ei levyosa", "Levy": flat.reason if flat else "", "Kortti": "—"}
    xs = [p[0] for p in flat.outline]
    ys = [p[1] for p in flat.outline]
    sheet = (f"t {flat.thickness_mm:g} mm · {len(flat.bends)} taiv. · "
             f"aihio {max(xs) - min(xs):.0f} × {max(ys) - min(ys):.0f} mm")
    cid = card_id(file_id, row.part_key)
    dxf_fid = next((dxf_cards[k] for k in _keys(m, part) if k in dxf_cards), None)
    if dxf_fid:
        card = "DXF: " + labels.get(dxf_fid, store[dxf_fid].name)
    else:
        card = labels.get(cid, "poistettu") if cid in store else "poistettu"
    return {"Tyyppi": "levyosa", "Levy": sheet, "Kortti": card}


def _render_preview_picker(file_id: str, m: dict, parts: list[step.StepPart]) -> None:
    names = {p.key: p.name for p in parts}
    if st.toggle("Näytä 3D", key=f"dxf_step_3d_{file_id}"):
        key = st.selectbox("Osa", list(names), format_func=names.get,
                           key=f"dxf_step_pick_{file_id}")
        part = next(p for p in parts if p.key == key)
        render_3d(part.shape, f"{file_id}:{key}")


# ── 3D and the card ───────────────────────────────────────────────────────────

def render_3d(shape, cache_key: str) -> None:
    """The body as a shaded 3D view you can turn (plotly)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        st.caption("3D-esikatseluun tarvitaan plotly-paketti.")
        return
    cache = st.session_state.setdefault("dxf_step_meshes", {})
    if cache_key not in cache:
        cache[cache_key] = step.mesh(shape)
    verts, tris = cache[cache_key]
    if not tris:
        st.caption("Mallista ei voitu tehdä 3D-näkymää.")
        return
    x, y, z = zip(*verts)
    i, j, k = zip(*tris)
    fig = go.Figure(go.Mesh3d(x=x, y=y, z=z, i=i, j=j, k=k, color="#9db4c8",
                              flatshading=True, lighting={"ambient": 0.45, "diffuse": 0.8}))
    fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 0, "b": 0},
                      scene={"aspectmode": "data", "xaxis": {"visible": False},
                             "yaxis": {"visible": False}, "zaxis": {"visible": False}})
    st.plotly_chart(fig, key=f"dxf_step_fig_{cache_key}")


def render_card_model(fid: str) -> None:
    """On a card made from a model: where it came from, the unfolding's facts
    and the 3D view."""
    src = card_source(fid)
    if src is None:
        return
    m, part, flat = src
    bends = ", ".join(f"{b.angle_deg:.0f}° r{b.inner_radius_mm:g}" for b in flat.bends)
    with st.expander(f"STEP: {m['name']} · {part.qty} kpl mallissa"):
        k = st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)
        st.caption(f"Avattu tasoaihioksi: paksuus {flat.thickness_mm:g} mm, "
                   f"{len(flat.bends)} taivutusta{': ' + bends if bends else ''} "
                   f"(K = {k:.2f}). Taivutusviivat näkyvät esikatselussa ohuina viivoina.")
        if st.toggle("Näytä 3D", key=f"dxf_step_card_3d_{fid}"):
            render_3d(part.shape, fid)


def single_part(file_id: str) -> step.StepPart | None:
    """The one part of a part-level STEP file (for its 3D view on a card)."""
    m = models().get(file_id)
    if m is None or len(m["model"].parts) != 1:
        return None
    return m["model"].parts[0]
