"""
Stremet Price Tool — Streamlit App

Entry point. Page setup, then sidebar (price data in) → calculator (UI out).
Supplier upload slots and persistence live in view/sidebar.py and
core/price_store.py; the calculator lives in view/calculator.py.
"""

import streamlit as st
from view import calculator, sidebar

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)

st.title("Stremet Price Tool")

price_data = sidebar.render()
calculator.render(price_data)