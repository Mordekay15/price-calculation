"""
core/parser/tibnor.py
=====================
Parses the Tibnor monthly price list PDF.

Tibnor's tables are less regular than Tata Steel's — headers wrap across
several rows, and page 2 holds two sub-tables side by side — so columns are
identified by header keyword with a positional fallback rather than by fixed
index. All prices are normalised to €/tn so they share the calculator pipeline.
"""

import io

import pdfplumber

from core.price_parser.helpers import clean, expand_range_rows, to_float


# ── Config ────────────────────────────────────────────────────────────────────

# Sheet sizes Tibnor stocks (from the page-1 free-text note).
TIBNOR_SIZES = ["1000x2000", "1250x2500", "1500x3000", "1500x6000", "1520x3020"]

# Each product: (col_index, header_keyword, output_label, thickness_col).
# col_index is the canonical position in the supplier's table; header_keyword
# lets us recover when pdfplumber shifts columns; thickness_col tells us where
# to read the thickness from (different for the side-by-side LASER sub-table).

# Page 1 — non-ferrous & stainless (€/kg, multiplied ×1000 to normalise to €/tn).
TIBNOR_NONFERROUS = [
    (1, "al.1050",  "Alumiini 1050",       0),
    (2, "al.5754",  "Alumiini 5754",       0),
    (3, "al.5005",  "Alumiini 5005+Kalv",  0),
    (4, "rst 2b",   "RST 2B",              0),
    (5, "rst 2k",   "RST 2K+pe",           0),
    (6, "hst 2b",   "HST 2B",              0),
    (7, "1.4016",   "RST 1.4016 2R",       0),
]

# Page 2 — main steel grades (€/1000kg = €/tn, no conversion needed).
TIBNOR_STEEL = [
    (1, "am o/i",   "KY-VA DC01 AM O/I",       0),
    (2, "z275",     "KU-SI DX51D+Z275",        0),
    (3, "ze 25",    "SÄ-SI DC01+ZE 25/25",     0),
    (4, "s650mc",   "S650MC Peitatty",         0),
    (5, "s235",     "S235 Peitatty",           0),
    (6, "s355mc",   "S355MC Peitatty",         0),
]

# Page 2 — sub-tables for special items. The two tables are side-by-side, so
# LASER reads its thickness from its own 'mm' column at index 2.
TIBNOR_SPECIAL = [
    (1, "z100",     "KU-SI DX51D+Z100",        0),
    (3, "laser",    "LASER 355ML Plus",        2),
]


# ── Generic table parser ──────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase and strip all whitespace — robust to multi-line / extra-space cells."""
    return "".join((text or "").lower().split())


def _identify_columns(
    header_cells: list[str],
    products: list[tuple[int, str, str, int]],
) -> dict[int, tuple[str, int]]:
    """Map data-column index → (output_label, thickness_col) by header keyword.

    Falls back to the canonical column index from the product config when
    keyword matching fails for a given product (e.g. headers rendered as
    page text outside the gridded table).
    """
    normalized = [_norm(c) for c in header_cells]

    col_map: dict[int, tuple[str, int]] = {}
    matched_labels: set[str] = set()

    # Pass 1: keyword match.
    for col_idx, kw, label, t_col in products:
        kw_n = _norm(kw)
        for idx, h in enumerate(normalized):
            if h and kw_n in h and idx not in col_map:
                col_map[idx] = (label, t_col)
                matched_labels.add(label)
                break

    # Pass 2: positional fallback for products that didn't keyword-match.
    for col_idx, _kw, label, t_col in products:
        if label in matched_labels:
            continue
        if col_idx in col_map:
            continue
        col_map[col_idx] = (label, t_col)

    return col_map


def _join_header_rows(table: list, max_header_rows: int = 3) -> list[str]:
    """Concatenate the first few rows of a table per column to handle multi-line headers."""
    if not table:
        return []
    n_cols = max((len(r) for r in table[:max_header_rows]), default=0)
    joined = [""] * n_cols
    for r in table[:max_header_rows]:
        for c in range(min(len(r), n_cols)):
            joined[c] = (joined[c] + " " + clean(r[c])).strip()
    return joined


def _table_max_cols(table: list) -> int:
    return max((len(r) for r in table), default=0)


def _parse_tibnor_table(
    table: list,
    products: list[tuple[int, str, str, int]],
    sizes: list[str],
    price_multiplier: float = 1.0,
) -> list[dict]:
    """Parse one pdfplumber table into rows shaped like the Tata Steel parser.

    Each output row keys prices by 'Material | Size', one row per thickness.
    Header columns are matched by keyword first, then by canonical position
    if the header text is missing or split across page text.
    """
    if not table:
        return []

    # Skip tables that obviously don't have enough columns to hold the products.
    needed_cols = max((c for c, *_ in products), default=0) + 1
    if _table_max_cols(table) < needed_cols:
        return []

    headers = _join_header_rows(table)
    col_map = _identify_columns(headers, products)
    if not col_map:
        return []

    by_thickness: dict[str, dict] = {}
    for row in table:
        if not row:
            continue
        for data_col, (label, t_col) in col_map.items():
            if data_col >= len(row):
                continue
            thickness = clean(row[t_col]) if t_col < len(row) else ""
            if not thickness or not thickness[0].isdigit():
                continue
            price = to_float(row[data_col])
            if price is None:
                continue
            price_per_tn = price * price_multiplier
            entry = by_thickness.setdefault(
                thickness, {"Paksuus (mm)": thickness}
            )
            for size in sizes:
                entry[f"{label} | {size}"] = price_per_tn

    return list(by_thickness.values())


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_tibnor_pdf(file_bytes: bytes) -> dict:
    """Parse a Tibnor price list PDF.

    Returns a dict with keys:
        tibnor_nonferrous, tibnor_steel, tibnor_special
    Each value is a list of row dicts in the same shape as parse_tatasteel_pdf.
    All prices are normalised to €/tn so they share the calculator pipeline.
    """
    result: dict = {
        "tibnor_nonferrous": [],
        "tibnor_steel":      [],
        "tibnor_special":    [],
    }

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        if len(pdf.pages) >= 1:
            for table in pdf.pages[0].extract_tables() or []:
                rows = _parse_tibnor_table(
                    table, TIBNOR_NONFERROUS, TIBNOR_SIZES,
                    price_multiplier=1000.0,   # €/kg → €/tn
                )
                result["tibnor_nonferrous"].extend(rows)

        if len(pdf.pages) >= 2:
            for table in pdf.pages[1].extract_tables() or []:
                steel_rows = _parse_tibnor_table(
                    table, TIBNOR_STEEL, TIBNOR_SIZES,
                    price_multiplier=1.0,
                )
                result["tibnor_steel"].extend(steel_rows)

                special_rows = _parse_tibnor_table(
                    table, TIBNOR_SPECIAL, TIBNOR_SIZES,
                    price_multiplier=1.0,
                )
                result["tibnor_special"].extend(special_rows)

    for key in ("tibnor_nonferrous", "tibnor_steel", "tibnor_special"):
        result[key] = expand_range_rows(result[key])

    return result