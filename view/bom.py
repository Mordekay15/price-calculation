"""STEP models on the DXF tab: a dropped STEP file is read into its BOM
(``core.step``) and every sheet-metal part is unfolded. Each unfolded part
is a candidate shape for the card of its part number (``core.items``): the
card of its DXF, or a card of its own. A 3D preview shows the model, and the
cards carry the model's quantities.

Session keys: ``MODELS`` holds ``{file id: {"name", "model", "flats",
"cards"}}``, where ``flats`` maps a part key to its ``FlatPart`` and
``cards`` to its flat ``DxfFile``. A model's part has the source id
``"{file id}|{part key}"``."""

from __future__ import annotations

import streamlit as st

from core import step
from core.dxf import read_dxf
from core.items import Source
from core.linking import base_key

MODELS = "dxf_step_models"
K_FACTOR = "dxf_step_k"
_SEP = "|"


def is_step(name: str) -> bool:
    return name.rsplit(".", 1)[-1].lower() in ("step", "stp")


def add_model(file_id: str, name: str, data: bytes) -> step.StepModel:
    """Read a dropped STEP file and unfold its parts. The 3D bodies are let
    go after unfolding when the file is kept (``attachments``): they are read
    again for a 3D view or a new K factor (see ``shapes``)."""
    from view import attachments

    model = step.read_step(data, name)
    k = st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)
    st.session_state.setdefault(MODELS, {})[file_id] = {
        "name": name, "model": model,
        "flats": {p.key: step.unfold(p.shape, k) for p in model.parts}}
    if file_id in attachments.docs():
        for p in model.parts:
            p.shape = None
    return model


def shapes(file_id: str) -> dict:
    """A model's 3D bodies by part key, read again from its file if they
    were let go."""
    from view import attachments

    parts = models()[file_id]["model"].parts
    if all(p.shape is not None for p in parts):
        return {p.key: p.shape for p in parts}
    again = step.read_step(attachments.data(file_id), models()[file_id]["name"])
    return {p.key: p.shape for p in again.parts}


def models() -> dict[str, dict]:
    return st.session_state.get(MODELS, {})


def is_assembly(file_id: str) -> bool:
    """A STEP file with more than one part, or several of one: shown in the
    BOM, not as a drawing linked to one card."""
    m = models().get(file_id)
    return m is not None and (len(m["model"].parts) != 1 or m["model"].parts[0].qty != 1)


def card_id(file_id: str, key: str) -> str:
    """The source id of a model's part (see ``core.items``)."""
    return f"{file_id}{_SEP}{key}"


def is_source(source_id: str) -> bool:
    return _SEP in source_id


def source(source_id: str) -> tuple[dict, step.StepPart, step.FlatPart] | None:
    """The model, part and flat of a model's part; None for a DXF."""
    if _SEP not in source_id:
        return None
    file_id, key = source_id.split(_SEP, 1)
    m = models().get(file_id)
    if m is None:
        return None
    part = next((p for p in m["model"].parts if p.key == key), None)
    return (m, part, m["flats"].get(key)) if part else None


def sources(hidden: set[str], folder_of=lambda sid: "") -> list[Source]:
    """Every sheet part of every model, as a candidate for the cards (the
    parts removed from the cards left out). ``folder_of(source id)`` is the
    ZIP folder its file came in."""
    out = []
    for file_id, m in models().items():
        for p in m["model"].parts:
            sid = card_id(file_id, p.key)
            flat = m["flats"].get(p.key)
            if sid in hidden or flat is None or not flat.is_sheet:
                continue
            out.append(Source(sid, _file_name(m, p), "step", [], _keys(m, p), folder_of(sid)))
    return out


def flat_file(source_id: str):
    """A model part's flat pattern as a ``DxfFile`` (made once per K factor)."""
    m, part, flat = source(source_id)
    cache = m.setdefault("cards", {})
    if part.key not in cache:
        cache[part.key] = read_dxf(step.flat_dxf(flat, part.name), _file_name(m, part))
    return cache[part.key]


def _file_name(m: dict, part: step.StepPart) -> str:
    """How a model's part is named on its card: the file's own name for a
    file of one part, else the part's name in the assembly."""
    if len(m["model"].parts) == 1:
        return m["name"]
    return f"{step.safe_name(part.name)}.step"


def _keys(m: dict, part: step.StepPart) -> set[str]:
    """The part numbers a model's part goes by: its name in the model and,
    for a file of one part, the file's name (``ITM-072558_A.stp`` whose body
    is named "Part1")."""
    keys = {base_key(part.name)}
    if len(m["model"].parts) == 1:
        keys.add(base_key(m["name"]))
    return keys


def refold(k: float) -> None:
    """Unfold every model again with a new K factor."""
    for file_id, m in models().items():
        m["flats"] = {key: step.unfold(shape, k) for key, shape in shapes(file_id).items()}
        m.pop("cards", None)


def remove_model(file_id: str) -> None:
    from view import attachments

    models().pop(file_id, None)
    attachments.remove_doc(file_id)


# ── BOM ───────────────────────────────────────────────────────────────────────

def render_bom(card_of: dict[str, str], on_change) -> None:
    """The models' BOMs, folded under the drop area: every part's quantity,
    what it is (sheet: thickness, bends, flat size; or why not) and the card
    it is on (``card_of``: source id -> card label). ``on_change()`` runs
    after the K factor or the models change."""
    all_models = models()
    if not all_models:
        return
    n_parts = sum(len(m["model"].parts) for m in all_models.values())
    with st.expander(f"STEP-mallit ({len(all_models)}) · {n_parts} osaa"):
        k = st.number_input(
            "K-kerroin", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
            value=float(st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)),
            key="dxf_step_k_input",
            help="Neutraaliakselin paikka taivutuksessa: avattu pituus = suorat osat + "
                 "kulma × (sisäsäde + K × paksuus). Tyypillisesti 0,33–0,5.")
        if abs(k - st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)) > 1e-9:
            st.session_state[K_FACTOR] = k
            refold(k)
            on_change()
            st.rerun()
        for file_id, m in list(all_models.items()):
            model = m["model"]
            c1, c2 = st.columns([6, 1], vertical_alignment="center")
            c1.markdown(f"🧊 **{m['name']}**")
            if c2.button("Poista", key=f"dxf_step_del_{file_id}"):
                remove_model(file_id)
                on_change()
                st.rerun()
            for problem in model.problems:
                st.warning(problem)
            rows = []
            for row in model.bom:
                rows.append({"Nimike": "\u2003" * row.level + row.name,
                             "Kpl / ylempi": row.qty, "Kpl yhteensä": row.total,
                             **_part_columns(file_id, m, row, card_of)})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            sheets = [p for p in model.parts if m["flats"][p.key].is_sheet]
            if model.parts:
                _render_preview_picker(file_id, m, sheets or model.parts)


def _part_columns(file_id, m, row, card_of) -> dict:
    if row.part_key is None:
        return {"Tyyppi": "kokoonpano", "Levy": "", "Kortti": ""}
    flat = m["flats"].get(row.part_key)
    if flat is None or not flat.is_sheet:
        return {"Tyyppi": "ei levyosa", "Levy": flat.reason if flat else "", "Kortti": "—"}
    w, h = flat.blank_mm or (0.0, 0.0)
    sheet = (f"t {flat.thickness_mm:g} mm · {len(flat.bends)} taiv. · "
             f"aihio {w:.0f} × {h:.0f} mm")
    return {"Tyyppi": "levyosa", "Levy": sheet,
            "Kortti": card_of.get(card_id(file_id, row.part_key), "poistettu")}


def _render_preview_picker(file_id: str, m: dict, parts: list[step.StepPart]) -> None:
    names = {p.key: p.name for p in parts}
    if st.toggle("Näytä 3D", key=f"dxf_step_3d_{file_id}"):
        key = st.selectbox("Osa", list(names), format_func=names.get,
                           key=f"dxf_step_pick_{file_id}")
        render_3d(lambda: shapes(file_id)[key], f"{file_id}:{key}")


# ── 3D and the card ───────────────────────────────────────────────────────────

def render_3d(shape, cache_key: str) -> None:
    """The body as a shaded 3D view you can turn (plotly). ``shape()`` gives
    the body (read only when its mesh isn't made yet)."""
    try:
        import plotly.graph_objects as go
    except ImportError:
        st.caption("3D-esikatseluun tarvitaan plotly-paketti.")
        return
    cache = st.session_state.setdefault("dxf_step_meshes", {})
    if cache_key not in cache:
        cache[cache_key] = step.mesh(shape())
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


def render_card_model(card: str, source_ids: list[str]) -> None:
    """On a card with a model's part: where it came from, the unfolding's
    facts and the 3D view."""
    for sid in source_ids:
        src = source(sid)
        if src is None:
            continue
        m, part, flat = src
        bends = ", ".join(f"{b.angle_deg:.0f}° r{b.inner_radius_mm:g}" for b in flat.bends)
        with st.expander(f"STEP: {m['name']} · {part.qty} kpl mallissa"):
            k = st.session_state.get(K_FACTOR, step.DEFAULT_K_FACTOR)
            st.caption(f"Avattu tasoaihioksi: paksuus {flat.thickness_mm:g} mm, "
                       f"{len(flat.bends)} taivutusta{': ' + bends if bends else ''} "
                       f"(K = {k:.2f}).")
            if st.toggle("Näytä 3D", key=f"dxf_step_card_3d_{card}_{sid}"):
                file_id = sid.split(_SEP, 1)[0]
                render_3d(lambda: shapes(file_id)[part.key], sid)


def single_part(file_id: str) -> step.StepPart | None:
    """The one part of a part-level STEP file (for its 3D view on a card)."""
    m = models().get(file_id)
    if m is None or len(m["model"].parts) != 1:
        return None
    return m["model"].parts[0]
