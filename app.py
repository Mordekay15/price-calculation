"""
Stremet Price Tool — Streamlit App

Parsed price data is saved to price_data.json so it survives browser refreshes
and server restarts. Upload a new PDF only when the monthly price list changes;
the upload widget lives in the sidebar.

Install & run:
    pip install streamlit pdfplumber
    streamlit run app.py
"""

import json
import datetime
import pathlib

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

# ── Persistent storage helpers ────────────────────────────────────────────────

@st.cache_resource
def _load_stored_data_cached(mtime: float) -> dict | None:
    """Load JSON keyed by file mtime so the cache auto-invalidates on save."""
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_stored_data() -> dict | None:
    if not DATA_FILE.exists():
        return None
    return _load_stored_data_cached(DATA_FILE.stat().st_mtime)


def save_data(data: dict, filename: str) -> dict:
    payload = {
        "data": data,
        "source_file": filename,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload

# ── Sidebar: upload (only needed once a month) ────────────────────────────────

with st.sidebar:
    st.header("Price list")
    stored = load_stored_data()

    if stored:
        st.success(
            f"Loaded: **{stored['source_file']}**\n\n"
            f"Updated: {stored['updated_at']}"
        )

    uploaded = st.file_uploader(
        "Upload new PDF to replace" if stored else "Upload PDF price list",
        type="pdf",
    )

    if uploaded:
        with st.spinner("Parsing PDF… this may take a few seconds"):
            parsed = parse_pdf(uploaded.read())
            stored = save_data(parsed, uploaded.name)
        st.success(f"Saved price data from **{uploaded.name}**.")
        st.rerun()

# ── Guard: nothing to show yet ────────────────────────────────────────────────

if stored is None:
    st.info("No price data found. Upload a PDF in the sidebar to get started.")
    st.stop()

data = stored["data"]

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
