"""
core/copper.py
==============
Copper (kupari) is always available in the calculator, independent of any
uploaded supplier price lists. Unlike the PDF-sourced materials, copper has no
list price — the user sets the price per kilo (15.0–15.9 €/kg) themselves, and
until they do copper simply carries no price (but stays selectable so the tool
is always usable).

Copper is stocked in a single sheet size (1000×2000 mm) across a fixed set of
thicknesses, matching the supplier's "KUPARI" product list
(e.g. "0,5x1000x2000" = 0,5 mm thickness on a 1000×2000 mm sheet).
"""

from core.calculator import thickness_sort_key

# Material / product identity — mirrors the "Material | Size" label scheme the
# PDF parsers emit so copper flows through the exact same calculator pipeline.
COPPER_MATERIAL = "KUPARI"
COPPER_SIZE = "1000x2000"
COPPER_LABEL = f"{COPPER_MATERIAL} | {COPPER_SIZE}"

# Thicknesses (mm) copper is stocked in, in Finnish decimal-comma format to
# match the rest of the app (parse_thickness_mm / thickness_sort_key handle it).
COPPER_THICKNESSES = sorted(
    ["0,5", "0,8", "1", "1,5", "2", "3", "4", "5", "6"],
    key=thickness_sort_key,
)

# Price-scaler bounds for the per-kilo copper price (€/kg).
COPPER_PRICE_MIN = 15.0
COPPER_PRICE_MAX = 15.9
COPPER_PRICE_STEP = 0.1

# Section key used inside the merged price-data dict.
COPPER_SECTION_KEY = "kupari"


def build_copper_section(price_per_kg: float | None) -> dict:
    """Return a data section (same shape as the parsers') for copper.

    One row per thickness, keyed by the 'Material | Size' label. When no price
    has been set yet the price is None, so copper still appears as a selectable
    material but carries no per-sheet pricing — build_lookup skips None values.
    The per-kilo price is converted to €/tn (× 1000) so it shares the calculator
    pipeline with the PDF-sourced materials, which are all normalised to €/tn.
    """
    price_per_tn = price_per_kg * 1000 if price_per_kg else None
    rows = [
        {
            "Paksuus (mm)": t,
            COPPER_LABEL: price_per_tn,
        }
        for t in COPPER_THICKNESSES
    ]
    return {COPPER_SECTION_KEY: rows}
