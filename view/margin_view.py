"""
view/margin_view.py
===================
The material margin ("Materiaalin kate") input for the calculator.

render_margin() returns the chosen margin as a percentage; view/calculator.py
passes it on to the sheet-usage pricing.
"""

import streamlit as st


def render_margin() -> float:
    """Render the margin input and return the chosen percentage."""
    return st.number_input(
        "Materiaalin kate (%)",
        min_value=0.0,
        value=15.0,
        step=0.5,
        key="calc_margin_pct",
    )
