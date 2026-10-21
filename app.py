"""Stremet Price Tool — Streamlit entry point: page setup, the sidebar (price
data in), the quote header, then the menu: the manual calculator and the
DXF tab's three screens (see ``view.dxf_tab``)."""

from contextlib import nullcontext

import streamlit as st

from view import calculator_tab, dxf_tab, quote, sidebar

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)

st.title("Stremet Price Tool")

price_data = sidebar.render()
quote.render_header()

# Only the open DXF screen runs drawn: the menu reruns the page on a click.
tab_calc, *screens = st.tabs(["Hintalaskuri", *dxf_tab.SCREENS], key="menu",
                             on_change="rerun")
with tab_calc:
    calculator_tab.render(price_data)
open_screen = next((s for s, tab in zip(dxf_tab.SCREENS, screens) if tab.open), None)
with screens[dxf_tab.SCREENS.index(open_screen)] if open_screen else nullcontext():
    dxf_tab.render(price_data, open_screen)
