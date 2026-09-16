"""
core/models.py
==============
The domain model — the two things the rest of the app keeps re-deriving from
strings today, made explicit and defined once.

  - MaterialSpec : the *stable* facts about a material (density, display label,
                   the aliases used to recognise it in messy supplier text).
                   Reference data — rarely changes.
  - PriceRecord  : *one price, fully described* (supplier, material, finish,
                   size, thickness, price). The volatile fact — a new one is
                   produced every month from each supplier PDF.

This module is deliberately dependency-free (pure stdlib) and is the lowest
layer: parsers/normalizers/stores build on it, it depends on nothing above it.

NOTHING imports this yet — adding it changes no behavior. It exists so the
canonical shape is visible and testable before anything is wired onto it.

--------------------------------------------------------------------------------
Where these replace today's string-parsing
--------------------------------------------------------------------------------
Today a single price lives as a column-name → number pair inside a row, e.g.

    {"Paksuus (mm)": "3", "Kylmävalssattu DC01 | 1000x2000": 950.0}

so material / finish / size / unit / supplier are all buried in the column
string and in *which parser* produced it. Every consumer re-parses that:

    core/calculator.py  extract_material_and_size()  splits on " | "
    core/calculator.py  parse_thickness_mm()         splits on "/" and "x"
    core/calculator.py  density_for_material()        substring-guesses density
    core/parser.py      price * 1000                  €/kg → €/tn, per supplier
    core/copper.py      price_per_kg * 1000           €/kg → €/tn, again

PriceRecord holds those facts as fields; MaterialSpec holds the density/label
knowledge in one registry instead of scattered keyword checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ══════════════════════════════════════════════════════════════════════════════
# MaterialSpec — the reference ("dimension") data: what a material *is*
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MaterialSpec:
    """The stable properties of a material.

    `aliases` are lowercase substrings used to recognise this material inside
    a supplier's free-form product text (e.g. "kylmävalssattu dc01"). Matching
    is substring-based and case-insensitive, mirroring today's
    density_for_material() but returning the *whole* spec, not just a density.
    """
    code: str                       # stable identifier, e.g. "DC01", "KUPARI"
    label: str                      # human display name (Finnish), e.g. "Kupari"
    density_g_cm3: float            # g/cm³ — numerically equal to kg/(m²·mm)
    aliases: tuple[str, ...] = ()   # lowercase keywords for fuzzy matching

    @property
    def density_kg_per_mm3(self) -> float:
        """Density in kg/mm³, the unit the weight math uses.

        A 1 mm sheet of 1 m² weighs density_g_cm3 kg, so the conversion is
        simply g/cm³ × 1e-6 (same constant the calculator uses today).
        """
        return self.density_g_cm3 * 1e-6


# Base categories. Every steel grade in both suppliers' lists uses the same
# 8.0 g/cm³ the calculator applies today (see the comment in core/calculator.py:
# "Steel / RST / HST — 7.85 g/cm³" is superseded by the 8.0 value in use). RST
# (stainless) and HST therefore share the steel density on purpose.
STEEL = MaterialSpec(
    code="STEEL",
    label="Teräs",
    density_g_cm3=8.0,
    aliases=(),   # the default fallback — matched by category, not by keyword
)

# The concrete materials that appear across the current parsers. Ordered so the
# more specific keyword wins first when several could match the same text.
MATERIALS: tuple[MaterialSpec, ...] = (
    # ── Non-ferrous ───────────────────────────────────────────────────────────
    MaterialSpec("ALUMIINI", "Alumiini", 2.7, aliases=("alumiini", "al.")),
    MaterialSpec("KUPARI",   "Kupari",   8.96, aliases=("kupari",)),
    MaterialSpec("PVC",      "PVC",      2.2, aliases=("pvc", "pleksi")),

    # ── Stainless / acid-resistant (share steel density) ──────────────────────
    MaterialSpec("RST", "Ruostumaton teräs (RST)", 8.0, aliases=("rst", "1.4016")),
    MaterialSpec("HST", "Haponkestävä teräs (HST)", 8.0, aliases=("hst",)),

    # ── Carbon / structural steel grades (Tata Steel + Tibnor) ────────────────
    MaterialSpec("DC01",   "Kylmävalssattu DC01",     8.0, aliases=("dc01", "kylmävalssattu", "ky-va")),
    MaterialSpec("DX51D",  "Kuumasinkitty DX51D",     8.0, aliases=("dx51d", "kuumasinkitty", "ku-si", "z275", "z100")),
    MaterialSpec("ZE",     "Sähkösinkitty ZE",        8.0, aliases=("sähkösinkitty", "sä-si", " ze", "ze ")),
    MaterialSpec("S235",   "S235",                    8.0, aliases=("s235",)),
    MaterialSpec("S355MC", "Kuumavalssattu S355MC",   8.0, aliases=("s355mc",)),
    MaterialSpec("S650MC", "Kuumavalssattu S650MC",   8.0, aliases=("s650mc",)),
    MaterialSpec("LASER",  "LASER 355ML Plus",        8.0, aliases=("laser",)),
)

# Fast lookup by code.
_BY_CODE: dict[str, MaterialSpec] = {m.code: m for m in (STEEL, *MATERIALS)}


def spec_for(material_name: str | None) -> MaterialSpec:
    """Resolve a (possibly messy) material name to its MaterialSpec.

    Tries an alias substring match first; falls back to STEEL — the same
    permissive default the current density_for_material() uses. Unlike today,
    the fallback is centralised here, so it's the one place to make it *loud*
    (e.g. log/raise on unknown materials) once you're ready.
    """
    name = (material_name or "").lower()
    for spec in MATERIALS:
        if any(alias in name for alias in spec.aliases):
            return spec
    return STEEL


def spec_by_code(code: str) -> MaterialSpec | None:
    """Look up a spec by its stable code (e.g. 'DC01'), or None if unknown."""
    return _BY_CODE.get(code)


# ══════════════════════════════════════════════════════════════════════════════
# PriceRecord — the fact data: one price, fully described
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PriceRecord:
    """A single price for one material + finish + size + thickness.

    All prices are normalised to €/tn so every supplier shares one scale — the
    normalisation (×1000 for €/kg sources) happens once, when the record is
    built, instead of being repeated in each parser and in copper.py.

    `thickness` keeps the original label ("0,7/0,75", "3") for display, while
    `thickness_mm` holds the parsed numeric value for math and sorting — so no
    consumer has to re-split the string.
    """
    supplier: str               # stable supplier code, e.g. "tata_steel", "tibnor"
    material: str               # MaterialSpec.code, e.g. "DC01", "KUPARI"
    size: str                   # sheet size label, e.g. "1000x2000"
    thickness: str              # original thickness label (for display)
    thickness_mm: float         # parsed thickness in mm (for math + sorting)
    price_per_tn: float         # normalised price, € per tonne
    finish: str | None = None   # optional surface/treatment note
    currency: str = "EUR"

    @property
    def spec(self) -> MaterialSpec:
        """The MaterialSpec for this record's material (density, label, …)."""
        return spec_by_code(self.material) or spec_for(self.material)

    def display_label(self, include_size: bool = True) -> str:
        """Build the human label from fields.

        Mirrors today's "Material | Size" scheme (the string that is currently
        BOTH the display text AND the lookup key), but here it's *generated*
        from data — so labels can be renamed or translated without breaking any
        lookup, because lookups use the fields, not this string.
        """
        label = self.spec.label
        return f"{label} | {self.size}" if include_size and self.size else label
