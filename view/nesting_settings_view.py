"""
view/nesting_settings_view.py
=============================
Nesting settings shared by the calculator and the DXF nesting page: the
"Sijoittelutapa" mode toggle plus the two spacing inputs, "Rankaväli" (spacing
between pieces) and "Pitkän sivun kynsirainan leveys" (long-side clamp zone).

render_nesting_settings() returns ``(nest_mode, rankavali_mm,
long_side_clamp_mm)``. ``key_prefix`` keeps the two tabs' widget keys distinct
(Streamlit renders both tab bodies on every run). The label/help overrides let
each page keep its own wording — the calculator talks about "tuote", the DXF
page about "osa" — while the widget scaffolding lives here once. The DXF page
renders its own extra options (nesting algorithm, angles) after this.
"""

import streamlit as st

_NEST_HELP = (
    "Yhdistettynä saman materiaalin ja paksuuden tuotteet sijoitellaan "
    "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
    "tuotteelle lasketaan oma levytarpeensa."
)
_RANKAVALI_HELP = (
    "Kappaleiden välinen rankaväli (leikkausvara). Lisätään jokaisen "
    "kappaleen leveyteen ja korkeuteen sijoittelussa, jotta vierekkäiset "
    "kappaleet pysyvät tämän etäisyyden päässä toisistaan."
)
_CLAMP_HELP = (
    "Kynsiraina on levyn pitkän sivun reunavyöhyke, johon koneen kynnet "
    "tarttuvat — aluetta ei voi käyttää kappaleiden sijoitteluun. "
    "Levy ostetaan silti täysikokoisena, joten paino ja hinta lasketaan "
    "bruttomitoista."
)


def render_nesting_settings(
    *,
    key_prefix: str = "calc",
    separate_label: str = "Laske jokainen tuote erikseen"
) -> tuple[str, int, int]:
    """Render the nesting mode and spacing inputs.

    Returns ``(nest_mode, rankavali_mm, long_side_clamp_mm)``. Callers pass a
    ``key_prefix`` ("calc" / "dxf") to avoid widget-key collisions and may
    override the "separate" option label and the help texts.
    """
    nest_mode = st.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": separate_label,
        }[v],
        horizontal=True,
        key=f"{key_prefix}_nest_mode",
        help=_NEST_HELP,
    )

    rankavali_mm = int(st.number_input(
        "Rankaväli (mm)",
        min_value=0,
        value=0,
        step=1,
        key=f"{key_prefix}_rankavali_mm",
        help=_RANKAVALI_HELP,
    ))

    long_side_clamp_mm = int(st.number_input(
        "Pitkän sivun kynsirainan leveys (mm)",
        min_value=0,
        value=0,
        step=1,
        key=f"{key_prefix}_long_side_clamp_mm",
        help=_CLAMP_HELP,
    ))

    return nest_mode, rankavali_mm, long_side_clamp_mm
