"""
views/tables.py
===============
Read-only table views with CSV download.

To add a new table view:
  - Add a new render_* function following the same pattern
  - Call it from app.py in a new st.tab
"""

import streamlit as st
from core.export import rows_to_csv_bytes


def _table_section(rows: list[dict], csv_filename: str) -> None:
    """Shared pattern: show dataframe + download button."""
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇ Lataa CSV",
            rows_to_csv_bytes(rows),
            csv_filename,
            "text/csv",
        )
    else:
        st.info("Tässä osiossa ei ole tietoja.")


def render_thin_sheets(data: dict) -> None:
    st.subheader("Ohutlevyhinnat  (€/tn, alv 0%)")
    _table_section(data["thin"], "hinnat_ohutlevy.csv")


def render_thick_sheets(data: dict) -> None:
    st.subheader("Paksulevyhinnat  (€/tn, alv 0%)")
    _table_section(data["thick"], "hinnat_paksulevy.csv")


def render_forecast(data: dict) -> None:
    st.subheader("Määräennuste  (tonnia)")
    _table_section(data["forecast"], "maaraennuste.csv")


def render_surcharges(data: dict) -> None:
    st.subheader("Lisäveloitukset ja erikoistuotteet")
    if data["surcharges"]:
        st.markdown("**Lisäveloitukset**")
        _table_section(data["surcharges"], "lisaveloitukset.csv")
    if data["special"]:
        st.markdown("**Korvamerkitty varastoitava nimike**")
        _table_section(data["special"], "korvamerkitty.csv")
    if not data["surcharges"] and not data["special"]:
        st.info("Lisäveloitustietoja ei löytynyt.")
