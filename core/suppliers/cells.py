"""Cell helpers shared by the supplier parsers: cleaning, numbers, thickness ranges."""

import re


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


# Matches a clean integer range like '3-6' or '12-20'. We deliberately do not
# match ranges with trailing text (e.g. '3-6mm 1D') because such labels usually
# denote a different finish/quality and shouldn't be flattened to bare integers.
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _expand_thickness(thickness: str) -> list[str]:
    """'3-6' → ['3', '4', '5', '6']; anything else is returned unchanged."""
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
        if not isinstance(t, str):
            out.append(row)
            continue
        out.extend({**row, "Paksuus (mm)": new_t} for new_t in _expand_thickness(t))
    return out
