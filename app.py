"""Stremet Price Tool — Streamlit entry point: page setup, the sidebar (price
data in), then the manual and DXF tabs."""

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
