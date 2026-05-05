"""
core/parser.py
==============
Extracts pricing tables from supplier PDFs.

Each supplier has its own parser:
  - parse_stremet_pdf  → Tata Steel / Stremet monthly price list
  - parse_tibnor_pdf   → Tibnor / Stremet Oy monthly price list
"""

import io
import re

import pdfplumber


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


# ── Thickness range expansion ────────────────────────────────────────────────

# Matches a clean integer range like '3-6' or '12-20'. We deliberately do not
# match ranges with trailing text (e.g. '3-6mm 1D') because such labels usually
# denote a different finish/quality and shouldn't be flattened to bare integers.
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def expand_thickness(thickness: str) -> list[str]:
    """Expand 'N-M' thickness ranges into individual integer values.

    '3-6' → ['3', '4', '5', '6']. Anything that isn't a clean integer range
    (single value, decimal, slash-separated, range with suffix) is returned
    unchanged.
    """
    if not isinstance(thickness, str):
        return [thickness]
    m = _RANGE_RE.match(thickness)
    if not m:
        return [thickness]
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi < lo:
        return [thickness]
    return [str(n) for n in range(lo, hi + 1)]


def expand_range_rows(rows: list[dict]) -> list[dict]:
    """Duplicate each range-thickness row into one row per integer thickness.

    Rows whose 'Paksuus (mm)' is a range like '3-6' share the same prices
    across every thickness in the range, so we materialise one row per value
    so the calculator and lookup can find a price for e.g. '4 mm' directly.
    """
    out: list[dict] = []
    for row in rows:
        t = row.get("Paksuus (mm)")
        expanded = expand_thickness(t) if isinstance(t, str) else [t]
        if len(expanded) == 1:
            out.append(row)
            continue
        for new_t in expanded:
            out.append({**row, "Paksuus (mm)": new_t})
    return out


# ── Tata Steel / Stremet — page-level parsers ─────────────────────────────────

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


def parse_stremet_pdf(file_bytes: bytes) -> dict:
    """
    Parse a Tata Steel / Stremet price list PDF.

    Returns a dict with keys:
        thin, thick, forecast, surcharges, special
    Each value is a list of row dicts.
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
                result["thin"]       = expand_range_rows(parse_thin_sheets(tables[0]))
            if len(tables) > 1:
                result["thick"]      = expand_range_rows(parse_thick_sheets(tables[1]))
            if len(tables) > 2:
                result["surcharges"] = parse_surcharges(tables[2])
            if len(tables) > 3:
                result["special"]    = expand_range_rows(parse_special(tables[3]))

    return result


# Backwards-compatible alias.
parse_pdf = parse_stremet_pdf


# ── Tibnor — config ───────────────────────────────────────────────────────────

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


# ── Tibnor — generic table parser ─────────────────────────────────────────────

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
    """Parse one pdfplumber table into rows shaped like the Stremet parser.

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


# ── Tibnor — main entry point ─────────────────────────────────────────────────

def parse_tibnor_pdf(file_bytes: bytes) -> dict:
    """Parse a Tibnor / Stremet Oy price list PDF.

    Returns a dict with keys:
        tibnor_nonferrous, tibnor_steel, tibnor_special
    Each value is a list of row dicts in the same shape as parse_stremet_pdf.
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
