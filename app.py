"""Stremet Price Tool — Streamlit entry point: page setup, the sidebar (price
data in), the quote bar (the logo, the Eräkoot switch and batch sizes), then
the menu: a quote from files, with its three numbered steps (see
``view.dxf_tab``), or the quick manual calculator."""

from contextlib import nullcontext

import streamlit as st

from view import brand, calculator_tab, dxf_tab, quote, sidebar

FROM_FILES, MANUAL = "Tarjous tiedostoista", "Pikalaskuri (käsin)"

st.set_page_config(
    page_title="Stremet Price Tool",
    page_icon=str(brand.ICON),
    layout="wide",
)
brand.apply()
dxf_tab.keep_inputs()   # before anything is drawn

price_data = sidebar.render()
quote.render_header(dxf_tab.clear_all)

# Two menus: a quote from files (DXF, STEP, PDF), with its steps, or the
# manual calculator. Only the open step runs drawn: a click reruns the page.
files, manual = st.tabs([FROM_FILES, MANUAL], key="menu", on_change="rerun")
with manual:
    calculator_tab.render(price_data)
open_screen = None
if files.open:
    with files:
        screens = st.tabs(dxf_tab.SCREENS, key=dxf_tab.MENU, on_change="rerun")
    open_screen = next(s for s, tab in zip(dxf_tab.SCREENS, screens) if tab.open)
with screens[dxf_tab.SCREENS.index(open_screen)] if open_screen else nullcontext():
    dxf_tab.render(price_data, open_screen)   # the totals show on Hinnoittelu
