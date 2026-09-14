"""
view/sidebar.py
===============
The sidebar: a single PDF upload slot that auto-routes to the right supplier,
a status line per supplier, and the manual copper price.

`render()` draws the whole sidebar and returns the merged price data that the
calculator consumes. Copper is always merged in, so the calculator works even
before any supplier PDF has been uploaded.
"""

import streamlit as st
from core import price_store
from core.price_store import SUPPLIERS, Supplier
from core.price_parser.detect import detect_supplier, is_empty
from core.copper import (
    build_copper_section,
    COPPER_PRICE_MIN,
    COPPER_PRICE_MAX,
    COPPER_PRICE_STEP,
)

_PLACEHOLDER_SUPPLIER = "— Valitse toimittaja —"

# Session-state keys.
_SEEN   = "upload_handled_file_id"   # file_id of the upload already saved
_RESULT = "upload_last_result"       # {file_id, supplier_key, filename}


def render() -> dict:
    merged: dict = {}

    with st.sidebar:
        st.header("Hinnastot")
        st.caption(
            "Lataa hinnasto PDF-muodossa — toimittaja tunnistetaan "
            "automaattisesti."
        )

        _render_uploader()

        st.divider()
        for supplier in SUPPLIERS:
            stored = price_store.load(supplier)
            _render_status(supplier, stored)
            if stored:
                merged.update(stored["data"])

        st.divider()
        copper_price_kg = _render_copper()

    merged.update(build_copper_section(copper_price_kg))
    return merged


# ── Upload ────────────────────────────────────────────────────────────────────

def _render_uploader() -> None:
    uploaded = st.file_uploader(
        "Lataa hinnasto (PDF)",
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
        key = detect_supplier(file_bytes)

    if key is None:
        st.warning(
            "Toimittajaa ei tunnistettu tästä PDF:stä. Valitse toimittaja itse."
        )
        key = _ask_supplier("manual_route")
        if key is None:
            return

    _parse_and_save(price_store.by_key(key), file_bytes, uploaded.name, uploaded.file_id)


def _parse_and_save(
    supplier: Supplier,
    file_bytes: bytes,
    filename: str,
    file_id: str,
) -> None:
    """Parse with the chosen supplier's parser and persist, unless it came back empty."""
    with st.spinner(f"Käsitellään {supplier.label} PDF..."):
        parsed = supplier.parser(file_bytes)

    if is_empty(parsed):
        st.error(
            f"**{supplier.label}**-jäsennys ei löytänyt yhtään riviä. "
            "PDF saattaa olla toiselta toimittajalta tai sen rakenne on "
            "muuttunut. Hintoja ei tallennettu."
        )
        return

    price_store.save(supplier, parsed, filename)
    st.session_state[_SEEN] = file_id
    st.session_state[_RESULT] = {
        "file_id":      file_id,
        "supplier_key": supplier.key,
        "filename":     filename,
    }
    st.rerun()


def _render_correction(uploaded) -> None:
    """After a successful save, let the user reroute if detection guessed wrong."""
    result = st.session_state.get(_RESULT) or {}
    if result.get("file_id") != uploaded.file_id:
        return

    saved = price_store.by_key(result["supplier_key"])
    st.success(f"Tunnistettu **{saved.label}** — hinnat tallennettu.")

    with st.expander("Väärä toimittaja?"):
        chosen_key = _ask_supplier("correct_route", exclude=saved.key)
        if chosen_key is None:
            return
        if st.button("Tallenna uudelleen", key="confirm_reroute"):
            # Drop the misrouted data before writing it to the right supplier.
            price_store.delete(saved)
            _parse_and_save(
                price_store.by_key(chosen_key),
                uploaded.getvalue(),
                result["filename"],
                uploaded.file_id,
            )


def _ask_supplier(widget_key: str, exclude: str | None = None) -> str | None:
    """Selectbox of supplier labels. Returns a supplier key, or None if unpicked."""
    options = [s for s in SUPPLIERS if s.key != exclude]
    labels  = [_PLACEHOLDER_SUPPLIER] + [s.label for s in options]
    picked  = st.selectbox("Toimittaja", labels, index=0, key=widget_key)
    if picked == _PLACEHOLDER_SUPPLIER:
        return None
    return next(s.key for s in options if s.label == picked)


# ── Status ────────────────────────────────────────────────────────────────────

def _render_status(supplier: Supplier, stored: dict | None) -> None:
    if stored:
        st.markdown(
            f"**{supplier.label}** · {stored['source_file']}  \n"
            f"<span style='color:#64748b;font-size:12px;'>"
            f"Päivitetty {stored['updated_at']}</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"**{supplier.label}** · "
            f"<span style='color:#94a3b8;'>ei hinnastoa</span>",
            unsafe_allow_html=True,
        )


# ── Copper ────────────────────────────────────────────────────────────────────

def _render_copper() -> float | None:
    """
    Copper carries no list price; the user sets the price per kilo here. The
    input starts empty so no price exists until the user enters one.
    """
    st.markdown("**Kupari**")
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