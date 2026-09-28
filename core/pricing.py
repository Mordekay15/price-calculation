"""
core/pricing.py
===============
Price data and material facts — pure, no Streamlit, no I/O.

* ``build_lookup`` flattens the parsed price lists into
  ``(thickness, "Material | Size") -> €/tn``.
* Materials, sizes and thicknesses available in that lookup.
* Densities and piece weights.
* Copper: always selectable, priced per kilo by the user (sidebar).
"""

THICKNESS_KEY = "Paksuus (mm)"

# Material densities in kg/mm³. A 1 mm sheet of 1 m² weighs density × 1e6 kg,
# so the kg/m²·mm value equals the g/cm³ value.
DENSITIES_KG_PER_MM3 = {
    "steel":    8.0e-6,
    "alumiini": 2.7e-6,
    "kupari":   8.96e-6,
    "pvc":      2.2e-6,
}


def density_for_material(material: str | None) -> float:
    """Pick a density (kg/mm³) by matching keywords in the material name.

    Falls back to steel for anything unrecognised — covers RST/HST, all the
    Tata/Stremet and Tibnor steel grades (DC01, DX51D, S235, S355MC, etc.).
    """
    name = (material or "").lower()
    if "alumiini" in name:
        return DENSITIES_KG_PER_MM3["alumiini"]
    if "kupari" in name:
        return DENSITIES_KG_PER_MM3["kupari"]
    if "pvc" in name or "pleksi" in name:
        return DENSITIES_KG_PER_MM3["pvc"]
    return DENSITIES_KG_PER_MM3["steel"]


# ── Lookup builder ────────────────────────────────────────────────────────────

def build_lookup(data: dict) -> dict:
    """
    Flatten all price tables into a single dict:
        (thickness: str, product: str) -> price_eur_per_ton: float

    Picks up any section in `data` whose rows carry a 'Paksuus (mm)' column,
    so new supplier parsers plug in without changes here.
    """
    lookup = {}
    for section_rows in data.values():
        if not isinstance(section_rows, list):
            continue
        for row in section_rows:
            if not isinstance(row, dict):
                continue
            t = row.get(THICKNESS_KEY)
            if not t:
                continue
            for col, val in row.items():
                if col != THICKNESS_KEY and val is not None:
                    lookup[(t, col)] = val
    return lookup


# ── Sorting ───────────────────────────────────────────────────────────────────

def thickness_sort_key(t: str) -> float:
    """Sort thickness strings numerically, push non-numeric ones to the end."""
    try:
        return float(t.replace(",", ".").split("/")[0].split("x")[0]) if t and t[0].isdigit() else 999
    except (ValueError, IndexError):
        return 999


# ── Material / size helpers ───────────────────────────────────────────────────

def extract_material_and_size(product_label: str) -> tuple[str, str]:
    """Split 'Material | Size' label into (material, size). Size is '' when absent."""
    if " | " in product_label:
        mat, sz = product_label.split(" | ", 1)
        return mat.strip(), sz.strip()
    return product_label, ""


def get_materials(lookup: dict) -> list[str]:
    """Sorted list of unique material names."""
    return sorted({extract_material_and_size(lbl)[0] for (_, lbl) in lookup})


def get_sizes_for_material(lookup: dict, material: str) -> list[str]:
    """Sorted list of available sizes for a given material."""
    sizes = {
        extract_material_and_size(lbl)[1]
        for (_, lbl) in lookup
        if extract_material_and_size(lbl)[0] == material
    }
    sizes.discard("")
    return sorted(sizes)


def get_thicknesses_for_material(lookup: dict, material: str) -> list[str]:
    """Sorted thicknesses available for a material across all of its sizes."""
    thicks = {
        t for (t, lbl) in lookup
        if extract_material_and_size(lbl)[0] == material
    }
    return sorted(thicks, key=thickness_sort_key)


# ── Weight calculation ────────────────────────────────────────────────────────

def parse_thickness_mm(thickness_str: str) -> float | None:
    """Convert a thickness label like '0,7/0,75' or '1,25' to mm as float."""
    try:
        return float(thickness_str.replace(",", ".").split("/")[0].split("x")[0])
    except (ValueError, IndexError, AttributeError):
        return None


def piece_weight_kg(
    width_mm: float,
    height_mm: float,
    thickness_mm: float,
    material: str | None = None,
) -> float:
    """Weight of a rectangular plate in kg, using the material's density."""
    return width_mm * height_mm * thickness_mm * density_for_material(material)


# ── Copper ────────────────────────────────────────────────────────────────────
#
# Copper is always available, independent of any uploaded price list. It has
# no list price: the user sets €/kg (15.0–15.9) in the sidebar, and until then
# copper is selectable but unpriced. It is stocked in one sheet size across a
# fixed set of thicknesses (the supplier's "KUPARI" list, e.g. "0,5x1000x2000").

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
