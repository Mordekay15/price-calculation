"""
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

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stremet Price Tool",
    page_icon="🏗️",
    layout="wide",
)

st.title("🏗️ Stremet Price Tool")
st.caption("Upload a Tata Steel / Stremet PDF price list to get started.")

# ── File upload ───────────────────────────────────────────────────────────────

uploaded = st.file_uploader("Upload PDF price list", type="pdf")

if not uploaded:
    st.info("Upload a PDF to continue.")
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
