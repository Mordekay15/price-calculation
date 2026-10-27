"""Stremet Price Tool — Streamlit entry point: page setup, the sidebar (price
data in), the quote bar (the quote's name, batch sizes and live totals), then
the menu: a quote from files, with its three numbered steps (see
``view.dxf_tab``), or the quick manual calculator."""

from contextlib import nullcontext

import streamlit as st

from view import calculator_tab, dxf_tab, quote, sidebar

FROM_FILES, MANUAL = "Tarjous tiedostoista", "Pikalaskuri (käsin)"

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)
dxf_tab.keep_inputs()   # before anything is drawn

price_data = sidebar.render()
totals_slot = quote.render_header(dxf_tab.clear_all)

# Two menus: a quote from files (DXF, STEP, PDF), with its steps, or the
# manual calculator. Only the open step runs drawn: a click reruns the page.
files, manual = st.tabs([FROM_FILES, MANUAL], key="menu", on_change="rerun")
with manual:
    manual_totals = calculator_tab.render(price_data)
open_screen = None
if files.open:
    with files:
        screens = st.tabs(dxf_tab.SCREENS, key=dxf_tab.MENU, on_change="rerun")
    open_screen = next(s for s, tab in zip(dxf_tab.SCREENS, screens) if tab.open)
with screens[dxf_tab.SCREENS.index(open_screen)] if open_screen else nullcontext():
    files_totals = dxf_tab.render(price_data, open_screen)
quote.render_totals(totals_slot, files_totals if files.open else manual_totals)
