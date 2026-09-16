"""
Stremet Price Tool — Streamlit App

Entry point. Page setup, then sidebar (price data in) → calculator (UI out).
Supplier upload slots and persistence live in view/sidebar.py and
core/price_store.py; the calculator lives in view/calculator.py.
"""

import streamlit as st
from view import calculator, dxf_nesting, sidebar

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)

st.title("Stremet Price Tool")

price_records = sidebar.render()

calc_tab, dxf_tab = st.tabs(["Hintalaskuri", "DXF-nestaus"])
with calc_tab:
    calculator.render(price_records)
with dxf_tab:
    dxf_nesting.render(price_records)