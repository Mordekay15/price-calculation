"""
tests/test_normalize.py
=======================
The safety net for the PriceRecord migration.

The one assertion that matters: for every supplier's wide-row output, the
records produced by `records_from_parsed` rebuild — byte-for-byte — the exact
price map `core.calculator.build_lookup` produces today. As long as that holds,
later steps (parsers emitting records, build_lookup consuming them, dropping the
scattered re-parsing) cannot silently change a single price the app quotes.

The fixtures below mirror the real parser output shapes (same column labels as
core/price_parser/tatasteel.py, tibnor.py and core/copper.py), including the
awkward cases: slash-combined sizes, price columns with no size, None prices,
decimal-comma and slash thicknesses, non-price sections, and duplicate keys.

Runs under pytest if installed, and standalone otherwise:
    python tests/test_normalize.py
"""

from __future__ import annotations

import os
import sys

# Make `core` importable whether run from the repo root (pytest) or as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.calculator import build_lookup
from core.copper import COPPER_LABEL, build_copper_section
from core.models import spec_for
from core.normalize import lookup_from_records, records_from_parsed


# ── Fixtures: representative wide-row output, one per supplier ──────────────────

def _tatasteel_data() -> dict:
    """Shapes emitted by core/price_parser/tatasteel.py.

    Includes the slash-combined size ('1250x2500/1500x3000'), the special
    column that carries a '(€/tn)' suffix and NO size, plus the non-priced
    forecast + surcharges sections (no 'Paksuus (mm)' → must contribute
    nothing, exactly as build_lookup ignores them).
    """
    return {
        "thin": [
            {
                "Paksuus (mm)": "0,7/0,75",
                "Kylmävalssattu DC01 | 1000x2000": 950.0,
                "Kylmävalssattu DC01 | 1250x2500/1500x3000": 965.0,
                "Kuumasinkitty Z275 | 1000x2000": 1010.0,
                "Sähkösinkitty ZE | 1000x2000": None,   # missing price → skipped
            },
            {
                "Paksuus (mm)": "3",
                "Kylmävalssattu DC01 | 1000x2000": 900.0,
                "Kuumasinkitty Z275 | 1250x2500/1500x3000": 1005.5,
            },
        ],
        "thick": [
            {
                "Paksuus (mm)": "4",
                "Kuumavalssattu S355MC P+O | 1250x2500": 820.0,
                "Kuumavalssattu S650MC | 1500x3000": 1180.0,
            },
        ],
        "special": [
            {
                "Paksuus (mm)": "2",
                "Kuumasinkitty DX51D+Z100MAC (€/tn)": 1300.0,   # no ' | size'
            },
        ],
        # Non-priced sections — no thickness column, must be ignored.
        "forecast": [
            {"Materiaali": "DC01", "+1kk (tn)": 12.0, "Summa (tn)": 30.0},
        ],
        "surcharges": [
            {"Kuvaus": "Pienerälisä", "€/tn": "50"},
        ],
    }


def _tibnor_data() -> dict:
    """Shapes emitted by core/price_parser/tibnor.py (already normalised to €/tn)."""
    return {
        "tibnor_nonferrous": [
            {
                "Paksuus (mm)": "1,5",
                "Alumiini 1050 | 1000x2000": 4200.0,
                "Alumiini 5754 | 1000x2000": 4550.0,   # same spec, different product
                "RST 2B | 1500x3000": 3100.0,
            },
        ],
        "tibnor_steel": [
            {
                "Paksuus (mm)": "2",
                "KY-VA DC01 AM O/I | 1250x2500": 980.0,
                "S235 Peitatty | 1000x2000": 760.0,
            },
        ],
        "tibnor_special": [
            {
                "Paksuus (mm)": "6",
                "LASER 355ML Plus | 1500x3000": 1450.0,
            },
        ],
    }


def _copper_data() -> dict:
    """The copper section (core/copper.py) with a price set."""
    return build_copper_section(price_per_kg=15.5)


ALL_FIXTURES = {
    "tatasteel": _tatasteel_data(),
    "tibnor": _tibnor_data(),
    "kupari": _copper_data(),
}


# ── The core guarantee ─────────────────────────────────────────────────────────

def test_records_rebuild_exact_price_map():
    """records → lookup equals build_lookup, per supplier and merged."""
    merged: dict = {}
    for supplier, data in ALL_FIXTURES.items():
        expected = build_lookup(data)
        rebuilt = lookup_from_records(records_from_parsed(supplier, data))
        assert rebuilt == expected, f"{supplier}: round-trip differs from build_lookup"
        merged.update(data)

    # And the same holds for a merged multi-supplier dict, the way the app
    # actually feeds build_lookup (all sections in one dict).
    all_records = []
    for supplier, data in ALL_FIXTURES.items():
        all_records += records_from_parsed(supplier, data)
    assert lookup_from_records(all_records) == build_lookup(merged)


def test_non_priced_sections_contribute_nothing():
    """Forecast/surcharges rows (no thickness) yield no records."""
    records = records_from_parsed("tatasteel", _tatasteel_data())
    labels = {r.material for r in records}
    assert "Materiaali" not in labels
    assert not any("Pienerälisä" in r.material for r in records)
    # Every record must correspond to a real priced cell.
    assert all(isinstance(r.price_per_tn, (int, float)) for r in records)


def test_none_prices_dropped():
    """A None price cell produces no record (matches build_lookup)."""
    records = records_from_parsed("tatasteel", _tatasteel_data())
    assert not any(
        r.material == "Sähkösinkitty ZE" and r.thickness == "0,7/0,75"
        for r in records
    )


def test_duplicate_keys_last_write_wins():
    """When two sections carry the same (thickness, label), the later price wins
    — identically for build_lookup and for the records round-trip."""
    data = {
        "a": [{"Paksuus (mm)": "3", "S235 | 1000x2000": 700.0}],
        "b": [{"Paksuus (mm)": "3", "S235 | 1000x2000": 715.0}],
    }
    expected = build_lookup(data)
    rebuilt = lookup_from_records(records_from_parsed("x", data))
    assert rebuilt == expected
    assert expected[("3", "S235 | 1000x2000")] == 715.0


def test_records_are_fully_described():
    """Fields are populated: supplier, split material/size, parsed thickness_mm,
    and a resolved spec whose density matches the model."""
    records = records_from_parsed("tibnor", _tibnor_data())
    by_label = {(r.material, r.size): r for r in records}

    alu = by_label[("Alumiini 1050", "1000x2000")]
    assert alu.supplier == "tibnor"
    assert alu.thickness == "1,5"
    assert alu.thickness_mm == 1.5
    # Both aluminium grades resolve to the one ALUMIINI spec (density 2.7)…
    assert alu.spec.density_g_cm3 == 2.7
    assert by_label[("Alumiini 5754", "1000x2000")].spec.density_g_cm3 == 2.7
    # …yet remain distinct records/keys, so no price is lost.
    assert alu.price_per_tn != by_label[("Alumiini 5754", "1000x2000")].price_per_tn

    laser = by_label[("LASER 355ML Plus", "1500x3000")]
    assert laser.thickness_mm == 6.0
    assert laser.spec is spec_for("LASER 355ML Plus")


def test_no_size_column_round_trips():
    """The special '... (€/tn)' column has no ' | size' and must round-trip as-is."""
    data = _tatasteel_data()
    records = records_from_parsed("tatasteel", data)
    special = [r for r in records if r.material.endswith("(€/tn)")]
    assert len(special) == 1
    r = special[0]
    assert r.size == ""
    assert (r.thickness, r.material) in build_lookup(data)


def test_copper_round_trips():
    """Copper flows through the same bridge as the PDF suppliers."""
    data = _copper_data()
    rebuilt = lookup_from_records(records_from_parsed("kupari", data))
    assert rebuilt == build_lookup(data)
    # Its label is the "Material | Size" copper uses today.
    assert any(label == COPPER_LABEL for (_t, label) in rebuilt)


# ── Standalone runner (no pytest required) ─────────────────────────────────────

def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — surface any error in the runner
            failures += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
