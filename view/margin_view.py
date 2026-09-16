"""
view/margin_view.py
===================
The material margin ("Materiaalin kate") input, shared by the calculator and
the DXF nesting page.

render_margin() returns the chosen margin as a percentage. Each page passes its
own ``key`` so the two margin inputs keep independent state — Streamlit renders
both tab bodies on every run, so a shared key would collide.
"""

import streamlit as st


def render_margin(key: str) -> float:
    """Render the margin input and return the chosen percentage."""
    return st.number_input(
        "Materiaalin kate (%)",
        min_value=0.0,
        value=15.0,
        step=0.5,
        key=key,
    )
