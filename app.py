"""
Stremet Price Tool — Streamlit App

Entry point. Page setup, then sidebar (price data in) → the two tabs.
Supplier upload slots and persistence live in view/sidebar.py and
core/price_store.py; the tabs live in view/calculator_tab.py and
view/dxf_tab.py.
"""

import streamlit as st

from view import calculator_tab, dxf_tab, sidebar

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)

st.title("Stremet Price Tool")

price_data = sidebar.render()

tab_calc, tab_dxf = st.tabs(["Hintalaskuri", "DXF-nestaus"])
with tab_calc:
    calculator_tab.render(price_data)
with tab_dxf:
    dxf_tab.render(price_data)
