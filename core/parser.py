"""
core/parser.py
==============
Extracts pricing tables from supplier PDFs.

Each supplier has its own parser:
  - parse_stremet_pdf  → Tata Steel / Stremet monthly price list
  - parse_tibnor_pdf   → Tibnor / Stremet Oy monthly price list
"""

import io
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
                result["thin"]       = parse_thin_sheets(tables[0])
            if len(tables) > 1:
                result["thick"]      = parse_thick_sheets(tables[1])
            if len(tables) > 2:
                result["surcharges"] = parse_surcharges(tables[2])
            if len(tables) > 3:
                result["special"]    = parse_special(tables[3])

    return result


# Backwards-compatible alias.
parse_pdf = parse_stremet_pdf


# ── Tibnor — config ───────────────────────────────────────────────────────────

# Sheet sizes Tibnor stocks (from the page-1 free-text note).
TIBNOR_SIZES = ["1000x2000", "1250x2500", "1500x3000", "1500x6000", "1520x3020"]

# Page 1 — non-ferrous & stainless (€/kg, multiplied ×1000 to normalise to €/tn).
# Each tuple is (header_keyword_lowercase, output_label).
TIBNOR_NONFERROUS = [
    ("al.1050",  "Alumiini 1050"),
    ("al.5754",  "Alumiini 5754"),
    ("al.5005",  "Alumiini 5005+Kalv"),
    ("rst 2b",   "RST 2B"),
    ("rst 2k",   "RST 2K+pe"),
    ("hst 2b",   "HST 2B"),
    ("1.4016",   "RST 1.4016 2R"),
]

# Page 2 — main steel grades (€/1000kg = €/tn, no conversion needed).
TIBNOR_STEEL = [
    ("am o/i",   "KY-VA DC01 AM O/I"),
    ("z275",     "KU-SI DX51D+Z275"),
    ("ze 25",    "SÄ-SI DC01+ZE 25/25"),
    ("s650mc",   "S650MC Peitatty"),
    ("s235",     "S235 Peitatty"),
    ("s355mc",   "S355MC Peitatty"),
]

# Page 2 — sub-tables for special items.
TIBNOR_SPECIAL = [
    ("z100",     "KU-SI DX51D+Z100"),
    ("laser",    "LASER 355ML Plus"),
]


# ── Tibnor — generic table parser ─────────────────────────────────────────────

def _identify_columns(
    header_cells: list[str],
    products: list[tuple[str, str]],
) -> dict[int, str]:
    """Map column index → output label by matching header text (case-insensitive).

    Each header column may consist of several stacked rows; pass the joined
    text per column.
    """
    col_map: dict[int, str] = {}
    for idx, text in enumerate(header_cells):
        t = text.lower()
        if not t:
            continue
        for keyword, label in products:
            if keyword in t and idx not in col_map:
                col_map[idx] = label
                break
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


def _thickness_col_for(headers: list[str], data_col: int) -> int:
    """Return the thickness column that applies to a given data column.

    The Tibnor page-2 layout has two side-by-side tables, each with its own
    'mm' header. Walks back from `data_col` to the nearest 'mm' / 'paks'
    column; falls back to column 0 if none is found.
    """
    for i in range(data_col - 1, -1, -1):
        text = headers[i].lower() if i < len(headers) else ""
        if "mm" in text or "paks" in text:
            return i
    return 0


def _parse_tibnor_table(
    table: list,
    products: list[tuple[str, str]],
    sizes: list[str],
    price_multiplier: float = 1.0,
) -> list[dict]:
    """Parse one pdfplumber table into rows shaped like the Stremet parser.

    Each output row keys prices by 'Material | Size', one row per thickness.
    When the table contains side-by-side sub-tables (a second 'mm' header
    further right), each product reads its thickness from its own 'mm'
    anchor column.

    Skips the table entirely if no expected product header is found.
    """
    if not table or len(table) < 2:
        return []

    headers = _join_header_rows(table)
    col_map = _identify_columns(headers, products)
    if not col_map:
        return []

    thickness_cols = {c: _thickness_col_for(headers, c) for c in col_map}

    by_thickness: dict[str, dict] = {}
    for row in table:
        if not row:
            continue
        for data_col, label in col_map.items():
            if data_col >= len(row):
                continue
            t_col = thickness_cols[data_col]
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

    return result
