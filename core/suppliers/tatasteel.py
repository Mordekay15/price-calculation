"""Tata Steel monthly price list. Page 3 holds the tables: 0 thin sheets,
1 thick sheets, 3 the special stocked item (2, surcharges, is not used)."""

import io

import pdfplumber

from core.suppliers.cells import clean, expand_range_rows, to_float


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

def parse_tatasteel_pdf(file_bytes: bytes) -> dict:
    """Parse a Tata Steel price list PDF into ``{"thin", "thick", "special"}``
    lists of row dicts."""
    result = {"thin": [], "thick": [], "special": []}
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        if len(pdf.pages) >= 3:
            tables = pdf.pages[2].extract_tables()
            if len(tables) > 0:
                result["thin"] = expand_range_rows(parse_thin_sheets(tables[0]))
            if len(tables) > 1:
                result["thick"] = expand_range_rows(parse_thick_sheets(tables[1]))
            if len(tables) > 3:
                result["special"] = expand_range_rows(parse_special(tables[3]))
    return result
