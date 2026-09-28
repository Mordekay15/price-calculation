import math

from core.rect_nesting import rect_options
from core.sheet_cost import (
    Packing,
    cheapest_index,
    compute_options,
    effective_sheet,
    fmt_m,
    parse_size,
    utilization,
)

LOOKUP = {
    ("2", "S235 | 1000x2000"): 900.0,
    ("2", "S235 | 1250x2500"): 870.0,
    ("2", "S235 | 1500x3000"): 850.0,
}


def test_small_helpers():
    assert parse_size("1250x2500/1500x3000") == [(1250, 2500), (1500, 3000)]
    assert effective_sheet(2000, 1000, 40) == (2000, 960)   # clamp on the long side
    assert fmt_m(1250) == "1.25" and fmt_m(1000) == "1.0"
    assert utilization(1_000_000, 1000, 2000, 2) == 0.25


def test_rect_options_prices_every_sheet_size():
    products = [
        {"id": "a", "width": 400, "height": 300, "qty": 17, "_global_idx": 0},
        {"id": "b", "width": 900, "height": 700, "qty": 3, "_global_idx": 1},
    ]
    result = rect_options(LOOKUP, "S235", "2", 2.0, products,
                          long_side_clamp_mm=40, rankavali_mm=5)
    rows = {r["Levykoko"]: r for r in result["rows"]}
    small = rows["1.0 × 2.0 m"]
    # 3 sheets of 1000 × 2000 × 2 mm steel = 96 kg at 900 €/tn
    assert small["Tarvittavat levyt"] == 3
    assert small["Levyn kg"] == 96.0
    assert small["Yhteensä €"] == 86.4
    # utilisation = real piece area (no gap) / sheets bought
    assert small["Käyttöaste"] == "65.5 %"
    assert result["n_pieces"] == 20
    assert math.isclose(result["pieces_kg"], (400 * 300 * 17 + 900 * 700 * 3) * 2 * 8e-6)
    best = result["rows"][cheapest_index(result["rows"])]
    assert best["Levykoko"] == "1.0 × 2.0 m"


def test_margin_raises_the_price_per_tonne():
    products = [{"id": "a", "width": 400, "height": 300, "qty": 1, "_global_idx": 0}]
    row = rect_options(LOOKUP, "S235", "2", 2.0, products, margin_pct=10)["rows"][0]
    assert row["Hinta (€/tn)"] == "990.00"
    assert row["Yhteensä €"] == round(990.0 * 32 / 1000, 2)


def test_a_sheet_the_parts_do_not_fit_is_blanked_and_never_cheapest():
    products = [{"id": "a", "width": 1200, "height": 1100, "qty": 2, "_global_idx": 0}]
    rows = rect_options(LOOKUP, "S235", "2", 2.0, products)["rows"]
    small = rows[0]
    assert small["Levykoko"] == "1.0 × 2.0 m"
    assert small["_failed"] == 2 and small["Yhteensä €"] == "" and small["_total"] == math.inf
    assert rows[cheapest_index(rows)]["Levykoko"] != "1.0 × 2.0 m"


def test_no_pieces_and_no_price():
    assert rect_options(LOOKUP, "S235", "2", 2.0, [])["has_pieces"] is False
    products = [{"id": "a", "width": 100, "height": 100, "qty": 1, "_global_idx": 0}]
    result = rect_options(LOOKUP, "S235", "3", 3.0, products)
    assert result["has_pieces"] and not result["has_candidates"]


def test_compute_options_uses_the_packer_it_is_given():
    seen = []

    def pack(sw, sh):
        seen.append((sw, sh))
        return Packing(sheets=["layout"], sheets_needed=2, eff_w=sw, eff_h=sh,
                       draw_w=sw, draw_h=sh)

    result = compute_options({("2", "S235 | 1000x2000"): 1000.0}, "S235", "2", 2.0,
                             n_pieces=4, part_area_mm2=500_000, pack=pack)
    row = result["rows"][0]
    assert seen == [(1000, 2000)]
    assert row["Tarvittavat levyt"] == 2 and row["_sheets"] == ["layout"]
    assert row["Yhteensä €"] == 64.0                       # 2 sheets × 32 kg × 1000 €/tn
    assert row["Käyttöaste"] == "12.5 %"                   # 0.5 m² of 4 m²
