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
from core.copper import (
    build_copper_section,
    COPPER_PRICE_MIN,
    COPPER_PRICE_MAX,
    COPPER_PRICE_STEP,
)
from view import calculator

# Each supplier: (label, json path, parser function).
SUPPLIERS = {
    "stremet": {
        "label":  "Tata Steel",
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
    st.header("Hinnastot")
    st.caption("Lataa kunkin toimittajan kuukausittainen PDF erikseen.")

    for key, cfg in SUPPLIERS.items():
        st.divider()
        st.markdown(f"**{cfg['label']}**")

        stored = load_stored_data(cfg["path"])
        if stored:
            st.success(
                f"Ladattu: **{stored['source_file']}**\n\n"
                f"Päivitetty: {stored['updated_at']}"
            )

        uploaded = st.file_uploader(
            f"Lataa uusi {cfg['label']} PDF" if stored else f"Lataa {cfg['label']} PDF",
            type="pdf",
            key=f"upload_{key}",
        )

        # st.file_uploader keeps the file across reruns; track the file_id so
        # we only parse + save once per upload (otherwise updated_at ticks).
        seen_key = f"processed_upload_{key}"
        if uploaded and st.session_state.get(seen_key) != uploaded.file_id:
            with st.spinner(f"Käsitellään {cfg['label']} PDF..."):
                parsed = cfg["parser"](uploaded.read())
                stored = save_data(cfg["path"], parsed, uploaded.name)
            st.session_state[seen_key] = uploaded.file_id
            st.success(f"Tallennettu {cfg['label']}-hinnat tiedostosta **{uploaded.name}**.")
            st.rerun()

        if stored:
            merged_data.update(stored["data"])

    # ── Copper: always available, no PDF needed ──────────────────────────────
    # Copper carries no list price; the user sets the price per kilo here. The
    # input starts empty so no price exists until the user enters one.
    st.divider()
    st.markdown("**Kupari**")
    copper_price_kg = st.number_input(
        "Kuparin hinta (€/kg)",
        min_value=COPPER_PRICE_MIN,
        max_value=COPPER_PRICE_MAX,
        value=None,
        step=COPPER_PRICE_STEP,
        key="copper_price_kg",
        placeholder=f"{COPPER_PRICE_MIN:g}–{COPPER_PRICE_MAX:g}",
        help=(
            "Aseta kuparin hinta per kilo (15,0–15,9 €/kg). Kupari on aina "
            "valittavissa, mutta sille ei lasketa hintaa ennen kuin syötät sen."
        ),
    )

# Copper is always merged in, so the calculator is always available even before
# any supplier PDF has been uploaded.
merged_data.update(build_copper_section(copper_price_kg))

# ── Main content ──────────────────────────────────────────────────────────────

calculator.render(merged_data)
