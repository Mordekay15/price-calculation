"""The sidebar: one PDF upload slot routed to the right supplier, a status line
per saved price list, and the manual copper price. ``render`` returns the
merged price data (copper always included)."""

import streamlit as st

from core import suppliers
from core.suppliers import SUPPLIERS, Supplier
from core.pricing import (
    COPPER_PRICE_MAX,
    COPPER_PRICE_MIN,
    COPPER_PRICE_STEP,
    build_copper_section,
)

_PLACEHOLDER_SUPPLIER = "— Valitse toimittaja —"

# Shown when pdfplumber can't open the upload at all (corrupt, truncated or
# password-protected file). Without this guard the exception propagates out of
# the sidebar and Streamlit replaces the whole page with a traceback.
_UNREADABLE_PDF_MSG = (
    "PDF:ää ei voitu lukea. Tiedosto voi olla vioittunut, keskeneräinen tai "
    "salasanasuojattu. Tallenna hinnasto uudelleen PDF:ksi ja yritä uudestaan."
)

# Session-state keys.
_SEEN   = "upload_handled_file_id"   # file_id of the upload already saved
_RESULT = "upload_last_result"       # {file_id, supplier_key, filename}


def render() -> dict:
    merged: dict = {}

    with st.sidebar:
        st.header("Hinnastot")

        _render_uploader()

        stored_by_supplier = [
            (supplier, suppliers.load(supplier)) for supplier in SUPPLIERS
        ]
        # Only draw the status section (and its divider) when at least one
        # supplier has an uploaded price list — no empty "ei hinnastoa" rows.
        if any(stored for _, stored in stored_by_supplier):
            st.divider()
            for supplier, stored in stored_by_supplier:
                if stored:
                    _render_status(supplier, stored)
                    merged.update(stored["data"])

        st.divider()
        copper_price_kg = _render_copper()

    merged.update(build_copper_section(copper_price_kg))
    return merged


# ── Upload ────────────────────────────────────────────────────────────────────

def _render_uploader() -> None:
    uploaded = st.file_uploader(
        "Hinnasto (PDF)",
        type="pdf",
        key="upload_pricelist",
    )

    if uploaded is None:
        return

    # st.file_uploader keeps the file across reruns; only parse + save once per
    # upload, otherwise updated_at ticks on every interaction with the page.
    if st.session_state.get(_SEEN) == uploaded.file_id:
        _render_correction(uploaded)
        return

    # getvalue() (not read()) so the buffer stays readable across reruns.
    file_bytes = uploaded.getvalue()

    with st.spinner("Tunnistetaan toimittajaa..."):
        try:
            key = suppliers.detect_supplier(file_bytes)
        except Exception:
            st.error(_UNREADABLE_PDF_MSG)
            return

    if key is None:
        st.warning(
            "Toimittajaa ei tunnistettu tästä PDF:stä. Valitse toimittaja itse."
        )
        key = _ask_supplier("manual_route")
        if key is None:
            return

    _parse_and_save(suppliers.by_key(key), file_bytes, uploaded.name, uploaded.file_id)


def _parse_and_save(
    supplier: Supplier,
    file_bytes: bytes,
    filename: str,
    file_id: str,
) -> None:
    """Parse with the chosen supplier's parser and persist, unless it came back empty."""
    with st.spinner(f"Käsitellään {supplier.label} PDF..."):
        try:
            parsed = supplier.parser(file_bytes)
        except Exception:
            st.error(_UNREADABLE_PDF_MSG)
            return

    if suppliers.is_empty(parsed):
        st.error(
            f"**{supplier.label}**-jäsennys ei löytänyt yhtään riviä. "
            "PDF saattaa olla toiselta toimittajalta tai sen rakenne on "
            "muuttunut. Hintoja ei tallennettu."
        )
        return

    suppliers.save(supplier, parsed, filename)
    st.session_state[_SEEN] = file_id
    st.session_state[_RESULT] = {
        "file_id":      file_id,
        "supplier_key": supplier.key,
        "filename":     filename,
    }
    st.rerun()


def _render_correction(uploaded) -> None:
    """After a successful save, confirm which supplier the PDF was routed to."""
    result = st.session_state.get(_RESULT) or {}
    if result.get("file_id") != uploaded.file_id:
        return

    saved = suppliers.by_key(result["supplier_key"])
    st.success(f"Tunnistettu **{saved.label}** — hinnat tallennettu.")


def _ask_supplier(widget_key: str) -> str | None:
    """Selectbox of supplier labels. Returns a supplier key, or None if unpicked."""
    labels = [_PLACEHOLDER_SUPPLIER] + [s.label for s in SUPPLIERS]
    picked = st.selectbox("Toimittaja", labels, index=0, key=widget_key)
    if picked == _PLACEHOLDER_SUPPLIER:
        return None
    return next(s.key for s in SUPPLIERS if s.label == picked)


# ── Status ────────────────────────────────────────────────────────────────────

def _render_status(supplier: Supplier, stored: dict) -> None:
    st.markdown(f"**{supplier.label}** · {stored['source_file']}")
    st.caption(f"Päivitetty {stored['updated_at']}")


# ── Copper ────────────────────────────────────────────────────────────────────

def _render_copper() -> float | None:
    """
    Copper carries no list price; the user sets the price per kilo here. The
    input starts empty so no price exists until the user enters one.
    """
    return st.number_input(
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