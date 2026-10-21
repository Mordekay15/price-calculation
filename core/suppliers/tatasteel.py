"""Tata Steel monthly price list. Page 3 holds the tables: 0 thin sheets,
1 thick sheets, 3 the special stocked item (2, surcharges, is not used)."""

import io

import pdfplumber

from core.suppliers.cells import clean, expand_range_rows, to_float


# Each table's price columns, in order after the thickness column.
_THIN = (                       # table 0: cold-rolled, hot-dip and electro-galvanised
    "Kylmävalssattu DC01 | 1000x2000", "Kylmävalssattu DC01 | 1250x2500/1500x3000",
    "Kuumasinkitty Z275 | 1000x2000", "Kuumasinkitty Z275 | 1250x2500/1500x3000",
    "Sähkösinkitty ZE | 1000x2000", "Sähkösinkitty ZE | 1250x2500/1500x3000",
)
_THICK = (                      # table 1: S355MC and S650MC structural steel
    "Kuumavalssattu S355MC P+O | 1250x2500", "Kuumavalssattu S355MC P+O | 1500x3000",
    "Kuumavalssattu S355MC | 1500x3000", "Kuumavalssattu S650MC P+O | 1500x3000",
    "Kuumavalssattu S650MC | 1500x3000",
)
_SPECIAL = ("Kuumasinkitty DX51D+Z100MAC (€/tn)",)   # table 3: korvamerkitty varastoitava nimike


def parse_table(table: list, labels: tuple[str, ...]) -> list[dict]:
    """One row per thickness (the first column), the prices keyed by
    ``labels``; a missing cell has no price."""
    rows = []
    for row in table[1:]:
        thickness = clean(row[0])
        if thickness:
            rows.append({"Paksuus (mm)": thickness,
                         **{label: to_float(row[i]) if i < len(row) else None
                            for i, label in enumerate(labels, start=1)}})
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
                result["thin"] = expand_range_rows(parse_table(tables[0], _THIN))
            if len(tables) > 1:
                result["thick"] = expand_range_rows(parse_table(tables[1], _THICK))
            if len(tables) > 3:
                result["special"] = expand_range_rows(parse_table(tables[3], _SPECIAL))
    return result
