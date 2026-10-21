"""Stremet Price Tool — Streamlit entry point: page setup, the sidebar (price
data in), the quote header, then the menu: the manual calculator, or
"Edistynyt" with the DXF tab's three screens (see ``view.dxf_tab``)."""

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

# Two menus: the basic calculator, or the DXF tab's screens under
# "Edistynyt". Only the open DXF screen runs drawn: a click reruns the page.
basic, advanced = st.tabs(["Peruslaskuri", "Edistynyt"], key="menu", on_change="rerun")
with basic:
    calculator_tab.render(price_data)
open_screen = None
if advanced.open:
    with advanced:
        screens = st.tabs(dxf_tab.SCREENS, key=dxf_tab.MENU, on_change="rerun")
    open_screen = next(s for s, tab in zip(dxf_tab.SCREENS, screens) if tab.open)
with screens[dxf_tab.SCREENS.index(open_screen)] if open_screen else nullcontext():
    dxf_tab.render(price_data, open_screen)
