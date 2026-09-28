from core.pricing import (
    COPPER_LABEL,
    COPPER_MATERIAL,
    build_copper_section,
    build_lookup,
    density_for_material,
    get_materials,
    get_sizes_for_material,
    get_thicknesses_for_material,
    parse_thickness_mm,
    piece_weight_kg,
)

DATA = {
    "thin": [
        {"Paksuus (mm)": "0,7/0,75", "S235 | 1000x2000": 900.0, "S235 | 1250x2500": None},
        {"Paksuus (mm)": "2", "S235 | 1000x2000": 950.0, "ALUMIINI | 1000x2000": 4000.0},
    ],
    "not_rows": "ignored",
}


def test_build_lookup_flattens_rows_and_skips_missing_prices():
    lookup = build_lookup(DATA)
    assert lookup == {
        ("0,7/0,75", "S235 | 1000x2000"): 900.0,
        ("2", "S235 | 1000x2000"): 950.0,
        ("2", "ALUMIINI | 1000x2000"): 4000.0,
    }


def test_materials_sizes_and_thicknesses():
    lookup = build_lookup(DATA)
    assert get_materials(lookup) == ["ALUMIINI", "S235"]
    assert get_sizes_for_material(lookup, "S235") == ["1000x2000"]
    assert get_thicknesses_for_material(lookup, "S235") == ["0,7/0,75", "2"]


def test_parse_thickness_uses_the_first_value_and_decimal_comma():
    assert parse_thickness_mm("0,7/0,75") == 0.7
    assert parse_thickness_mm("1,25") == 1.25
    assert parse_thickness_mm("abc") is None


def test_density_and_weight():
    assert density_for_material("S235") == 8.0e-6
    assert density_for_material("ALUMIINI 5754") == 2.7e-6
    # 1000 × 1000 × 1 mm of steel weighs 8 kg
    assert piece_weight_kg(1000, 1000, 1, "S235") == 8.0


def test_copper_is_unpriced_until_a_price_is_set():
    assert build_lookup(build_copper_section(None)) == {}
    lookup = build_lookup(build_copper_section(15.5))
    assert lookup[("2", COPPER_LABEL)] == 15500.0          # €/kg → €/tn
    assert get_materials(lookup) == [COPPER_MATERIAL]
