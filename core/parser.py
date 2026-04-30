"""
core/parser.py
==============
Extracts all pricing tables from a Tata Steel / Stremet PDF.

To add support for a new PDF layout:
  - Add a new parse_* function below
  - Call it from parse_pdf() based on page count or a header keyword
"""

import io
import pdfplumber
import streamlit as st


# ── Low-level helpers ─────────────────────────────────────────────────────────

def clean(value: str | None) -> str:
    """Strip whitespace and newlines from a cell value."""
    return (value or "").replace("\n", " ").strip()


def to_float(value: str | None) -> float | None:
    """Convert a cell string like '1 234' or '1234,5' to float, or None."""
    v = clean(value).replace(" ", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


# ── Page-level parsers ────────────────────────────────────────────────────────

def parse_forecast(page) -> list[dict]:
    """Page 2 — quantity forecast table."""
    rows = []
    tables = page.extract_tables()
    if not tables:
        return rows
    for row in tables[0][1:]:           # skip header
        cells = clean(row[0]).split()
        if len(cells) >= 2 and not cells[0].isdigit():
            rows.append({
                "Materiaali":  cells[0],
                "+1kk (tn)":   to_float(cells[1]) if len(cells) > 1 else None,
                "+2kk (tn)":   to_float(cells[2]) if len(cells) > 2 else None,
                "+3kk (tn)":   to_float(cells[3]) if len(cells) > 3 else None,
                "Summa (tn)":  to_float(cells[4]) if len(cells) > 4 else None,
            })
    return rows


def parse_thin_sheets(table: list) -> list[dict]:
    """Table 0 on page 3 — cold-rolled, hot-dip galvanised, electro-galvanised."""
    rows = []
    for row in table[1:]:
        thickness = clean(row[0])
        if not thickness:
            continue
        rows.append({
            "Paksuus (mm)":                                      thickness,
            "Kylmävalssattu DC01 | 1000x2000":                   to_float(row[1]),
            "Kylmävalssattu DC01 | 1250x2500/1500x3000":         to_float(row[2]),
            "Kuumasinkitty Z275 | 1000x2000":                    to_float(row[3]),
            "Kuumasinkitty Z275 | 1250x2500/1500x3000":          to_float(row[4]),
            "Sähkösinkitty ZE | 1000x2000":                      to_float(row[5]),
            "Sähkösinkitty ZE | 1250x2500/1500x3000":            to_float(row[6]),
        })
    return rows


def parse_thick_sheets(table: list) -> list[dict]:
    """Table 1 on page 3 — S355MC and S650MC structural steel."""
    rows = []
    for row in table[1:]:
        thickness = clean(row[0])
        if not thickness:
            continue
        rows.append({
            "Paksuus (mm)":                             thickness,
            "Kuumavalssattu S355MC P+O | 1250x2500":    to_float(row[1]),
            "Kuumavalssattu S355MC P+O | 1500x3000":    to_float(row[2]),
            "Kuumavalssattu S355MC | 1500x3000":        to_float(row[3]),
            "Kuumavalssattu S650MC P+O | 1500x3000":    to_float(row[4]),
            "Kuumavalssattu S650MC | 1500x3000":        to_float(row[5]),
        })
    return rows


def parse_surcharges(table: list) -> list[dict]:
    """Table 2 on page 3 — lisäveloitukset."""
    rows = []
    for row in table:
        desc = clean(row[0])
        val  = clean(row[1]) if len(row) > 1 else ""
        if desc and desc != "LISÄVELOITUKSET":
            rows.append({"Kuvaus": desc, "€/tn": val})
    return rows


def parse_special(table: list) -> list[dict]:
    """Table 3 on page 3 — korvamerkitty varastoitava nimike."""
    rows = []
    for row in table[1:]:
        thickness = clean(row[0])
        price     = to_float(row[1]) if len(row) > 1 else None
        if thickness:
            rows.append({
                "Paksuus (mm)":                        thickness,
                "Kuumasinkitty DX51D+Z100MAC (€/tn)":  price,
            })
    return rows


# ── Main entry point ──────────────────────────────────────────────────────────

@st.cache_data(show_spinner="Parsing PDF…")
def parse_pdf(file_bytes: bytes) -> dict:
    """
    Parse a Stremet / Tata Steel price list PDF.

    Returns a dict with keys:
        thin, thick, forecast, surcharges, special
    Each value is a list of row dicts (ready for st.dataframe or csv export).
    """
    result = {
        "thin":       [],
        "thick":      [],
        "forecast":   [],
        "surcharges": [],
        "special":    [],
    }

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        num_pages = len(pdf.pages)

        if num_pages >= 2:
            result["forecast"] = parse_forecast(pdf.pages[1])

        if num_pages >= 3:
            tables = pdf.pages[2].extract_tables()
            if len(tables) > 0:
                result["thin"]       = parse_thin_sheets(tables[0])
            if len(tables) > 1:
                result["thick"]      = parse_thick_sheets(tables[1])
            if len(tables) > 2:
                result["surcharges"] = parse_surcharges(tables[2])
            if len(tables) > 3:
                result["special"]    = parse_special(tables[3])

    return result
