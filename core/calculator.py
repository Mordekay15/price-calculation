"""
core/calculator.py
==================
Pure business logic — no Streamlit, no I/O.

Build a price lookup from parsed data and run calculations on it.
To add a new calculation type (e.g. weight-based, area-based):
  - Add a new function below and call it from the relevant view.
"""

THICKNESS_KEY = "Paksuus (mm)"


# ── Lookup builder ────────────────────────────────────────────────────────────

def build_lookup(data: dict) -> dict:
    """
    Flatten all price tables into a single dict:
        (thickness: str, product: str) -> price_eur_per_ton: float

    Works for any table that has a 'Paksuus (mm)' column.
    """
    lookup = {}
    for section in ("thin", "thick", "special"):
        for row in data.get(section, []):
            t = row.get(THICKNESS_KEY, "")
            for col, val in row.items():
                if col != THICKNESS_KEY and val is not None:
                    lookup[(t, col)] = val
    return lookup


# ── Sorting ───────────────────────────────────────────────────────────────────

def thickness_sort_key(t: str) -> float:
    """Sort thickness strings numerically, push non-numeric ones to the end."""
    try:
        return float(t.replace(",", ".").split("x")[0]) if t and t[0].isdigit() else 999
    except (ValueError, IndexError):
        return 999


def sorted_products(lookup: dict) -> list[str]:
    return sorted(set(label for (_, label) in lookup))


def sorted_thicknesses(lookup: dict, product: str | None = None) -> list[str]:
    """Return thicknesses, optionally filtered to those available for a product."""
    if product:
        items = [t for (t, p) in lookup if p == product]
    else:
        items = [t for (t, _) in lookup]
    return sorted(set(items), key=thickness_sort_key)


# ── Price calculation ─────────────────────────────────────────────────────────

def calculate(
    base_price: float,
    quantity_tn: float,
    margin_pct: float = 0.0,
    surcharge_per_tn: float = 0.0,
    fx_rate: float = 1.0,
) -> dict:
    """
    Calculate total cost with optional adjustments.

    Returns a dict with every intermediate step so the UI can
    display as much or as little detail as it wants.
    """
    after_margin    = base_price * (1 + margin_pct / 100)
    after_surcharge = after_margin + surcharge_per_tn
    after_fx        = after_surcharge * fx_rate
    total           = after_fx * quantity_tn

    return {
        "base_price":       base_price,
        "after_margin":     after_margin,
        "after_surcharge":  after_surcharge,
        "after_fx":         after_fx,
        "total":            total,
        "quantity_tn":      quantity_tn,
        "margin_pct":       margin_pct,
        "surcharge_per_tn": surcharge_per_tn,
        "fx_rate":          fx_rate,
    }


def compare_thicknesses(
    lookup: dict,
    product: str,
    quantity_tn: float,
    margin_pct: float = 0.0,
    surcharge_per_tn: float = 0.0,
    fx_rate: float = 1.0,
    selected_thickness: str | None = None,
) -> list[dict]:
    """
    Return a comparison table of all thicknesses for one product.
    Each row includes base price, adjusted price, and total cost.
    """
    rows = []
    for t in sorted_thicknesses(lookup, product):
        bp = lookup.get((t, product))
        if bp is None:
            continue
        result = calculate(bp, quantity_tn, margin_pct, surcharge_per_tn, fx_rate)
        rows.append({
            "Thickness":         t,
            "Base (€/tn)":       f"{result['base_price']:,.2f}",
            "Adjusted (€/tn)":   f"{result['after_fx']:,.2f}",
            f"Total ({quantity_tn} tn)": f"{result['total']:,.2f}",
            "":                  "◀" if t == selected_thickness else "",
        })
    return rows
