"""What the user fixes by hand on a card whose drawing couldn't be read on
its own (see ``core.fixes``): the part chosen on the drawing sheet, and the
lines inside the part marked as not cut."""

from __future__ import annotations

from core import fixes
from core.dxf import is_unclosed_problem
from core.model import FlatShape, Part
from view import files
from view.ui import is_headless, st

_PICKED, _OPTION, _SHEET = "#16a34a", "#2563eb", "#94a3b8"
_HEIGHT_PX = 430


def view_source(shape: FlatShape, item: Part | None, shape_of) -> FlatShape | None:
    """The shape whose drawing sheet the card offers to choose the part
    from: the card's own, or while the card has nothing to price, the first
    of its files that has views."""
    if shape.views is not None:
        return shape
    if not shape.problems and shape.outline:
        return None
    members = (shape_of(m) for m in (item.members if item is not None else []))
    return next((s for s in members if s is not None and s.views is not None), None)


def _pick(sid: str, k: int | None) -> None:
    res = files.result(sid.split("|", 1)[0])
    if res is not None:
        fixes.pick_view(res, k)


def _picked_on_sheet(sid: str, key: str, traces: dict[int, int]) -> None:
    """The chart's selection: a click on a view's number or outline."""
    points = (st.session_state.get(key) or {}).get("selection", {}).get("points", [])
    hits = [traces[p["curve_number"]] for p in points if p.get("curve_number") in traces]
    if hits:
        _pick(sid, hits[0])


def _figure(shape: FlatShape):
    """The sheet with its views numbered; returns the figure and which view
    each selectable trace is (trace number: view)."""
    import plotly.graph_objects as go

    choice = shape.views
    fig = go.Figure()
    xs, ys = [], []
    for line in choice.sheet:
        xs += [x for x, _ in line] + [None]
        ys += [y for _, y in line] + [None]
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line={"color": _SHEET, "width": 1},
                             hoverinfo="skip"))
    traces: dict[int, int] = {}
    for k, ring in enumerate(choice.outlines):
        colour = _PICKED if k == choice.picked else _OPTION
        w, h = choice.options[k].size_mm
        label = f"{k + 1} · {w:.0f} × {h:.0f} mm"
        closed = [*ring, ring[0]]
        traces[len(fig.data)] = k
        fig.add_trace(go.Scatter(
            x=[x for x, _ in closed], y=[y for _, y in closed], mode="lines+markers",
            fill="toself", fillcolor=_rgba(colour, 0.30 if k == choice.picked else 0.10),
            line={"color": colour, "width": 3 if k == choice.picked else 1.5},
            marker={"size": 2, "color": colour}, name=label, text=label, hoverinfo="text"))
        # The number, at the outline's top left corner: what is clicked.
        x0, y1 = min(x for x, _ in ring), max(y for _, y in ring)
        at = min(ring, key=lambda p: (p[0] - x0) ** 2 + (p[1] - y1) ** 2)
        traces[len(fig.data)] = k
        fig.add_trace(go.Scatter(
            x=[at[0]], y=[at[1]], mode="markers+text", text=[str(k + 1)],
            textfont={"color": "white", "size": 13},
            marker={"size": 24, "color": colour, "line": {"color": "white", "width": 1.5}},
            hovertext=[label], hoverinfo="text"))
    fig.update_layout(
        height=_HEIGHT_PX, margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False,
        dragmode="pan", plot_bgcolor="#f8fafc", paper_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False}, yaxis={"visible": False, "scaleanchor": "x"})
    return fig, traces


def _rgba(colour: str, alpha: float) -> str:
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def render_view_picker(shape: FlatShape) -> None:
    """The drawing sheet with its views numbered: the user clicks a number
    (or presses its button) and that view becomes the part. Open while nothing is chosen, folded after."""
    if is_headless():
        return
    choice, sid = shape.views, shape.origin
    if choice.picked is None:
        st.warning(f"**Osaa ei tunnistettu automaattisesti.** Piirustuksessa on "
                   f"{len(choice.options)} mahdollista näkymää. Valitse niistä osan aihio: "
                   "napsauta sen numeroa kuvassa tai paina näkymän painiketta kuvan alla. "
                   "Kuvaa voi suurentaa hiiren rullalla ja siirtää vetämällä.")
        box = st.container()
    else:
        w, h = choice.options[choice.picked].size_mm
        box = st.expander(f"Osa valittu piirustuksesta itse: näkymä {choice.picked + 1} "
                          f"({w:.0f} × {h:.0f} mm). Vaihda", expanded=False)
    with box:
        try:
            fig, traces = _figure(shape)
        except ImportError:
            st.caption("Piirustuksen näyttämiseen tarvitaan plotly-paketti; valitse "
                       "näkymä painikkeista.")
        else:
            key = f"dxf_view_fig_{sid}"
            st.plotly_chart(fig, key=key, on_select=lambda: _picked_on_sheet(sid, key, traces),
                            selection_mode="points",
                            config={"displaylogo": False, "scrollZoom": True})
        cols = st.columns(4)
        for k, option in enumerate(choice.options):
            w, h = option.size_mm
            cols[k % 4].button(f"{k + 1} · {w:.0f} × {h:.0f} mm", key=f"dxf_view_pick_{sid}_{k}",
                               type="primary" if k == choice.picked else "secondary",
                               on_click=_pick, args=(sid, k), width="stretch")
        if choice.picked is not None:
            st.button("Poista valinta", key=f"dxf_view_pick_{sid}_none", on_click=_pick,
                      args=(sid, None))
        st.caption("Harmaalla piirustuksen muut viivat. Jos osaa ei ole näkymien joukossa, "
                   "lataa osan aukikeritty DXF.")


def not_cut_key(sid: str) -> str:
    return f"dxf_notcut_{sid}"


def apply_not_cut(shape: FlatShape) -> bool:
    """Mark the lines chosen on the card (``render_not_cut``) as not cut.
    Returns whether the shape changed."""
    marked = frozenset(st.session_state.get(not_cut_key(shape.origin)) or ())
    if marked == shape.not_cut or (shape.unclosed is None and not any(
            map(is_unclosed_problem, shape.problems))):
        return False
    fixes.set_not_cut(shape, marked)
    return True


def _toggle_on_part(key: str, fig_key: str, traces: dict[int, int]) -> None:
    """A click on a line's number in the picture: marked, or cut again."""
    points = (st.session_state.get(fig_key) or {}).get("selection", {}).get("points", [])
    marked = set(st.session_state.get(key) or ())
    for i in {traces[p["curve_number"]] for p in points if p.get("curve_number") in traces}:
        marked ^= {i}
    st.session_state[key] = sorted(marked)


def _mark_all(key: str, n: int) -> None:
    st.session_state[key] = list(range(n))


def _lines_figure(shape: FlatShape, lines: list[list]):
    """The part with its unclosed lines numbered: red while cut, grey once
    marked. Returns the figure and each clickable trace's line."""
    import plotly.graph_objects as go

    fig = go.Figure()
    for rings, colour, dash, close in (([shape.outline, *shape.holes], _OPTION, None, True),
                                       (shape.markings, _SHEET, "dot", False),
                                       (shape.open_cuts, _OPTION, None, False)):
        xs, ys = [], []
        for ring in rings:
            if any(ring is line for line in lines):
                continue
            pts = [*ring, ring[0]] if close else ring
            xs += [x for x, _ in pts] + [None]
            ys += [y for _, y in pts] + [None]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip",
                                 line={"color": colour, "width": 1, "dash": dash}))
    traces: dict[int, int] = {}
    for i, line in enumerate(lines):
        off = i in shape.not_cut
        colour = "#64748b" if off else "#dc2626"
        label = f"viiva {i + 1}: " + ("ei leikata" if off else "ei sulkeudu ääriviivaksi")
        fig.add_trace(go.Scatter(x=[x for x, _ in line], y=[y for _, y in line], mode="lines",
                                 line={"color": colour, "width": 3,
                                       "dash": "dot" if off else None}, hoverinfo="skip"))
        x, y = line[len(line) // 2] if len(line) > 2 else (
            (line[0][0] + line[-1][0]) / 2, (line[0][1] + line[-1][1]) / 2)
        traces[len(fig.data)] = i
        fig.add_trace(go.Scatter(
            x=[x], y=[y], mode="markers+text", text=[str(i + 1)],
            textfont={"color": "white", "size": 12}, hovertext=[label], hoverinfo="text",
            marker={"size": 22, "color": colour, "line": {"color": "white", "width": 1.5}}))
    fig.update_layout(
        height=320, margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False,
        dragmode="pan", plot_bgcolor="#f8fafc", paper_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False}, yaxis={"visible": False, "scaleanchor": "x"})
    return fig, traces


def render_not_cut(shape: FlatShape) -> None:
    """The lines inside the part that close into no outline, numbered on a
    picture that zooms and pans: each can be marked as a drawing mark (not cut) by a
    click on its number or by its pill; ``apply_not_cut`` then marks it."""
    if shape.unclosed is None and not any(map(is_unclosed_problem, shape.problems)):
        return
    lines = fixes.unclosed_lines(shape)
    if not lines:
        return
    key = not_cut_key(shape.origin)
    if not is_headless():
        try:
            fig, traces = _lines_figure(shape, lines)
        except ImportError:
            pass
        else:
            fig_key = f"dxf_view_lines_{shape.origin}"
            st.plotly_chart(fig, key=fig_key, selection_mode="points",
                            on_select=lambda: _toggle_on_part(key, fig_key, traces),
                            config={"displaylogo": False, "scrollZoom": True})
    st.pills(
        "Viivat, joita ei leikata", list(range(len(lines))), selection_mode="multi",
        format_func=lambda i: f"viiva {i + 1}", key=key,
        help="Punaiset, numeroidut viivat eivät sulkeudu ääriviivaksi. Jos viiva on "
             "vain piirrosmerkki (esim. keskiviiva tai kahteen kertaan piirretty reuna), "
             "valitse se tästä tai napsauta sen numeroa kuvassa: se piirretään, mutta "
             "sitä ei leikata eikä hinnoitella. Jos se on reiän ääriviiva, jossa on "
             "aukko, korjaa piirustus. Kuvaa voi suurentaa hiiren rullalla.")
    if len(lines) > 1 and len(shape.not_cut) < len(lines):
        st.button(f"Merkitse kaikki {len(lines)} viivaa: ei leikata",
                  key=f"dxf_view_all_{shape.origin}", on_click=_mark_all,
                  args=(key, len(lines)))


def _use_outer_edge(shape: FlatShape, on: bool) -> None:
    fixes.use_outer_edge(shape, on)


def _crossing_figure(shape: FlatShape):
    """The part as drawn with the places where its outline crosses itself
    ringed in red, and the outer edge that would be used in green."""
    import plotly.graph_objects as go

    outline, holes = shape.drawn_rings or (shape.outline, shape.holes)
    fig = go.Figure()

    def add(rings, colour, width, dash=None, fill=None):
        xs, ys = [], []
        for ring in rings:
            xs += [x for x, _ in ring] + [ring[0][0], None]
            ys += [y for _, y in ring] + [ring[0][1], None]
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", fill=fill,
                                 fillcolor=_rgba(colour, 0.10),
                                 line={"color": colour, "width": width, "dash": dash}))

    edges = [fixes.outer_edge(r) for r in (outline, *holes) if fixes.crossings(r)]
    add([e for e in edges if e], _PICKED, 5, fill="toself")
    add([outline, *holes], _OPTION, 1.5)
    marks = [p for r in (outline, *holes) for p in fixes.crossings(r)]
    fig.add_trace(go.Scatter(
        x=[x for x, _ in marks], y=[y for _, y in marks], mode="markers", hoverinfo="text",
        hovertext=["ääriviiva leikkaa itseään tässä"] * len(marks),
        marker={"size": 18, "color": "rgba(0,0,0,0)", "line": {"color": "#dc2626", "width": 3}}))
    fig.update_layout(
        height=360, margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False,
        dragmode="pan", plot_bgcolor="#f8fafc", paper_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False}, yaxis={"visible": False, "scaleanchor": "x"})
    return fig, len(marks)


def render_outer_edge(shape: FlatShape) -> None:
    """A part whose outline crosses itself, on a picture that zooms and
    pans: where it crosses, the outer edge that could be cut instead, and
    the choice to use it (or to go back to the drawing as it is)."""
    if is_headless() or not fixes.can_use_outer_edge(shape):
        return
    sid, used = shape.origin, shape.drawn_rings is not None
    w, h = shape.size_mm
    if used:
        box = st.expander(f"Ääriviiva leikkasi itseään: käytössä osan ulkoreuna "
                          f"({w:.1f} × {h:.1f} mm). Näytä tai peru", expanded=False)
    else:
        box = st.container()
    with box:
        try:
            fig, n = _crossing_figure(shape)
        except ImportError:
            n = 0
        else:
            st.plotly_chart(fig, key=f"dxf_view_cross_{sid}",
                            config={"displaylogo": False, "scrollZoom": True})
            st.caption(f"Sininen: ääriviiva piirustuksessa. Punaiset renkaat ({n}): kohdat, "
                       "joissa se leikkaa itseään (esim. reuna piirretty kahteen kertaan tai "
                       "taitettu laippa piirretty levyn päälle). Vihreä: ulkoreuna, joka "
                       "kiertää kaiken, minkä ääriviiva sulkee sisäänsä. Kuvaa voi suurentaa "
                       "hiiren rullalla.")
        if used:
            st.button("Peru: käytä ääriviivaa sellaisenaan", key=f"dxf_view_edge_off_{sid}",
                      on_click=_use_outer_edge, args=(shape, False))
        else:
            st.button("Käytä osan ulkoreunaa (vihreä)", key=f"dxf_view_edge_on_{sid}",
                      type="primary", on_click=_use_outer_edge, args=(shape, True))
