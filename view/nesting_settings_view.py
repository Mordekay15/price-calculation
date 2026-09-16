"""
view/nesting_settings_view.py
=============================
Nesting settings for the calculator: the "Sijoittelutapa" mode toggle plus the
two spacing inputs, "Rankaväli" (spacing between pieces) and "Pitkän sivun
kynsirainan leveys" (long-side clamp zone).

render_nesting_settings() returns ``(nest_mode, rankavali_mm,
long_side_clamp_mm)``; view/calculator.py feeds these into grouping and the
sheet-usage pricing.
"""

import streamlit as st


def render_nesting_settings() -> tuple[str, int, int]:
    """Render the nesting mode and spacing inputs.

    Returns ``(nest_mode, rankavali_mm, long_side_clamp_mm)``.
    """
    nest_mode = st.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": "Laske jokainen tuote erikseen",
        }[v],
        horizontal=True,
        key="calc_nest_mode",
        help=(
            "Yhdistettynä saman materiaalin ja paksuuden tuotteet sijoitellaan "
            "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
            "tuotteelle lasketaan oma levytarpeensa."
        ),
    )

    rankavali_mm = int(st.number_input(
        "Rankaväli (mm)",
        min_value=0,
        value=0,
        step=1,
        key="calc_rankavali_mm",
        help=(
            "Kappaleiden välinen rankaväli (leikkausvara). Lisätään jokaisen "
            "kappaleen leveyteen ja korkeuteen sijoittelussa, jotta vierekkäiset "
            "kappaleet pysyvät tämän etäisyyden päässä toisistaan."
        ),
    ))

    long_side_clamp_mm = int(st.number_input(
        "Pitkän sivun kynsirainan leveys (mm)",
        min_value=0,
        value=0,
        step=1,
        key="calc_long_side_clamp_mm",
        help=(
            "Kynsiraina on levyn pitkän sivun reunavyöhyke, johon koneen kynnet "
            "tarttuvat — aluetta ei voi käyttää kappaleiden sijoitteluun. "
            "Levy ostetaan silti täysikokoisena, joten paino ja hinta lasketaan "
            "bruttomitoista."
        ),
    ))

    return nest_mode, rankavali_mm, long_side_clamp_mm
