"""
Stremet Price Tool — Streamlit App

Each supplier has its own named upload slot in the sidebar. Parsed price data
is saved to per-supplier JSON files so it survives browser refreshes and server
restarts. Re-upload only when the monthly price list changes.
"""

import json
import datetime
import pathlib

import streamlit as st
from core.parser import parse_stremet_pdf, parse_tibnor_pdf
from view import calculator

# Each supplier: (label, json path, parser function).
SUPPLIERS = {
    "stremet": {
        "label":  "Tata Steel / Stremet",
        "path":   pathlib.Path("price_data.json"),
        "parser": parse_stremet_pdf,
    },
    "tibnor": {
        "label":  "Tibnor",
        "path":   pathlib.Path("price_data_tibnor.json"),
        "parser": parse_tibnor_pdf,
    },
}

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stremet Price Tool",
    layout="wide",
)

st.title("Stremet Price Tool")

# ── Persistent storage helpers ────────────────────────────────────────────────

@st.cache_resource
def _load_stored_data_cached(path_str: str, mtime: float) -> dict | None:
    """Load JSON keyed by (path, mtime) so the cache auto-invalidates on save."""
    with open(path_str, "r", encoding="utf-8") as f:
        return json.load(f)


def load_stored_data(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    return _load_stored_data_cached(str(path), path.stat().st_mtime)


def save_data(path: pathlib.Path, data: dict, filename: str) -> dict:
    payload = {
        "data": data,
        "source_file": filename,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


# ── Sidebar: one upload slot per supplier ─────────────────────────────────────

merged_data: dict = {}

with st.sidebar:
    st.header("Price lists")
    st.caption("Upload each supplier's monthly PDF separately.")

    for key, cfg in SUPPLIERS.items():
        st.divider()
        st.markdown(f"**{cfg['label']}**")

        stored = load_stored_data(cfg["path"])
        if stored:
            st.success(
                f"Loaded: **{stored['source_file']}**\n\n"
                f"Updated: {stored['updated_at']}"
            )

        uploaded = st.file_uploader(
            f"Upload new {cfg['label']} PDF" if stored else f"Upload {cfg['label']} PDF",
            type="pdf",
            key=f"upload_{key}",
        )

        # st.file_uploader keeps the file across reruns; track the file_id so
        # we only parse + save once per upload (otherwise updated_at ticks).
        seen_key = f"processed_upload_{key}"
        if uploaded and st.session_state.get(seen_key) != uploaded.file_id:
            with st.spinner(f"Parsing {cfg['label']} PDF..."):
                parsed = cfg["parser"](uploaded.read())
                stored = save_data(cfg["path"], parsed, uploaded.name)
            st.session_state[seen_key] = uploaded.file_id
            st.success(f"Saved {cfg['label']} prices from **{uploaded.name}**.")
            st.rerun()

        if stored:
            merged_data.update(stored["data"])

# ── Guard: nothing to show yet ────────────────────────────────────────────────

if not merged_data:
    st.info("No price data found. Upload a PDF in the sidebar to get started.")
    st.stop()

# ── Main content ──────────────────────────────────────────────────────────────

calculator.render(merged_data)
