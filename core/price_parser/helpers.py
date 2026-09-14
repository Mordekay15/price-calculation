"""
core/parser/helpers.py
=====================
Helpers shared by every supplier parser: cell cleaning, number conversion and
thickness-range expansion.
"""

import re


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