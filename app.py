"""
Parsed price data is saved to disk (price_data.json) so it persists across
sessions. Upload a new PDF only when the monthly price list is updated — the
upload widget lives in the sidebar.

Install & run:
    pip install streamlit pdfplumber
    streamlit run app.py
"""

import io
import csv
import json
import datetime
import pathlib
import pdfplumber
app.py
======
Entry point — only responsible for:
  1. Page config
  2. File upload
  3. Wiring tabs to views

To add a new tab:
  - Create a new view in views/
  - Import it here and add a tab
"""

import streamlit as st
from core.parser import parse_pdf
from view import calculator, tables

DATA_FILE = pathlib.Path("price_data.json")

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stremet Price Tool",
    page_icon="🏗️",
    layout="wide",
)

st.title("🏗️ Stremet Price Tool")

# ── File upload ───────────────────────────────────────────────────────────────

stored = load_stored_data()

if stored is None:
    st.info("No price data found. Upload a PDF in the sidebar to get started.")
    st.stop()

data = parse_pdf(uploaded.read())
st.success("PDF parsed successfully.")
st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_calc, tab_thin, tab_thick, tab_forecast, tab_extra = st.tabs([
    "🧮 Price calculator",
    "📋 Thin sheets",
    "📋 Thick sheets",
    "📦 Forecast",
    "➕ Surcharges",
])

with tab_calc:     calculator.render(data)
with tab_thin:     tables.render_thin_sheets(data)
with tab_thick:    tables.render_thick_sheets(data)
with tab_forecast: tables.render_forecast(data)
with tab_extra:    tables.render_surcharges(data)
