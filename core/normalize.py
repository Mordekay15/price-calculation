"""
core/normalize.py
=================
The bridge from the parsers' *wide-row* output to the PriceRecord *model*.

Every supplier parser today emits the same wide shape — a list of rows, each
row a dict of ``{"Paksuus (mm)": thickness, "Material | Size": price, ...}`` —
and `core.calculator.build_lookup` flattens that into the price map the whole
app runs on:

    (thickness: str, "Material | Size": str) -> price_eur_per_tn: float

`records_from_parsed` walks those same wide rows and turns each price cell into
a fully-described `PriceRecord`: it splits "Material | Size" ONE last time here,
resolves the material through `spec_for()` (so density/label knowledge lives in
`core.models`, not in the string), and parses the thickness to a number ONCE —
work every downstream consumer repeats today.

Nothing is wired onto this yet. It exists so the *records* can be proven, by the
accompanying test, to rebuild byte-for-byte the exact price map `build_lookup`
produces today. That equivalence is the safety net for every later step
(making parsers emit records, then `build_lookup` consume them, then dropping
the scattered re-parsing).

--------------------------------------------------------------------------------
Why `PriceRecord.material` keeps the *original* label, not the spec code
--------------------------------------------------------------------------------
The app's column labels are finer-grained than `MaterialSpec` codes: a single
spec (ALUMIINI, RST, DX51D, ...) backs several distinct products —
"Alumiini 1050", "Alumiini 5754", "RST 2B", "RST 2K+pe", "Kuumasinkitty Z275",
"Kuumasinkitty DX51D+Z100MAC (€/tn)", and so on. Collapsing those to a shared
spec code would make two different products share one lookup key and silently
overwrite each other — a lossy bridge, and the guard test would (correctly)
fail.

So this step stores the original material text verbatim in `material`, which
keeps the round-trip exact. Material *identity* is still resolved through the
model: `PriceRecord.spec` calls `spec_for(self.material)` for density/label, and
`spec_for()` is applied here up front so an unrecognised material is a decision
made in one place. De-stringifying `material` into (spec code + finish) is a
later, behaviour-changing step — one this test is here to guard.
"""

from __future__ import annotations

from core.calculator import (
    THICKNESS_KEY,
    extract_material_and_size,
    parse_thickness_mm,
)
from core.models import PriceRecord, spec_for


def record_label(record: PriceRecord) -> str:
    """Rebuild the exact "Material | Size" label a record came from.

    The inverse of `extract_material_and_size`: it uses the record's stored
    `material` and `size` directly (NOT `display_label`, which resolves to the
    coarser spec label). This is the one place later steps reconstruct the
    wide-row column key, so the round-trip stays defined once.
    """
    return f"{record.material} | {record.size}" if record.size else record.material


def records_from_parsed(supplier: str, data: dict) -> list[PriceRecord]:
    """Turn one supplier's wide-row parser output into fully-described records.

    Walks exactly the cells `build_lookup` walks — list sections, rows carrying
    a 'Paksuus (mm)' value, every other column with a non-None price — so the
    records cover the same prices and nothing else. Sections without a thickness
    column (forecast, surcharges) contribute no records, matching build_lookup.

    `supplier` is a stable supplier key (e.g. "tatasteel", "tibnor", "kupari")
    stored on each record; it is descriptive metadata and does not affect the
    price-map round-trip (build_lookup keys ignore supplier).
    """
    records: list[PriceRecord] = []

    for section_rows in data.values():
        if not isinstance(section_rows, list):
            continue
        for row in section_rows:
            if not isinstance(row, dict):
                continue

            thickness = row.get(THICKNESS_KEY)
            if not thickness:
                continue

            # Parse the thickness once, here, instead of in every consumer.
            thickness_mm = parse_thickness_mm(thickness)

            for col, price in row.items():
                if col == THICKNESS_KEY or price is None:
                    continue

                # Split "Material | Size" one last time; resolve the material
                # through the model so identity/density come from core.models.
                material, size = extract_material_and_size(col)
                spec = spec_for(material)

                records.append(
                    PriceRecord(
                        supplier=supplier,
                        material=material,        # original label (see module docstring)
                        size=size,
                        thickness=thickness,
                        thickness_mm=thickness_mm if thickness_mm is not None else 0.0,
                        price_per_tn=price,
                        finish=None,
                    )
                )
                # `spec` is resolved eagerly so an unknown material surfaces in
                # one place; density/label are read lazily via record.spec later.
                _ = spec

    return records


def lookup_from_records(records: list[PriceRecord]) -> dict:
    """Rebuild the `build_lookup`-shaped price map from records.

    The exact inverse of the walk in `records_from_parsed`: the map that later
    steps will hand the calculator once parsers emit records directly. Kept next
    to `records_from_parsed` so the round-trip both directions is defined once.
    """
    return {
        (record.thickness, record_label(record)): record.price_per_tn
        for record in records
    }
