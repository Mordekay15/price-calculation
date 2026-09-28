import math

from core.rect_nesting import rect_options
from core.sheet_cost import (
    Packing,
    cheapest_index,
    compute_options,
    effective_sheet,
    fmt_m,
    group_products,
    parse_size,
    piece_costs,
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
    small = result.options[0]
    assert (small.sw, small.sh) == (1000, 2000)
    # 3 sheets of 1000 × 2000 × 2 mm steel = 96 kg at 900 €/tn
    assert small.sheets_needed == 3
    assert math.isclose(small.sheet_kg, 96.0)
    assert math.isclose(small.total_eur, 86.4)
    # utilisation = real piece area (no gap) / sheets bought
    assert math.isclose(small.utilization, 0.655)
    assert result.n_pieces == 20
    assert math.isclose(result.pieces_kg, (400 * 300 * 17 + 900 * 700 * 3) * 2 * 8e-6)
    assert cheapest_index(result.options) == 0


def test_margin_raises_the_price_per_tonne():
    products = [{"id": "a", "width": 400, "height": 300, "qty": 1, "_global_idx": 0}]
    option = rect_options(LOOKUP, "S235", "2", 2.0, products, margin_pct=10).options[0]
    assert math.isclose(option.adjusted_ppt, 990.0)
    assert math.isclose(option.total_eur, 990.0 * 32 / 1000)


def test_a_sheet_the_parts_do_not_fit_is_never_cheapest():
    products = [{"id": "a", "width": 1200, "height": 1100, "qty": 2, "_global_idx": 0}]
    options = rect_options(LOOKUP, "S235", "2", 2.0, products).options
    small = options[0]
    assert (small.sw, small.sh) == (1000, 2000)
    assert small.failed == 2 and not small.ok
    assert cheapest_index(options) != 0


def test_no_pieces_and_no_price():
    assert rect_options(LOOKUP, "S235", "2", 2.0, []) is None
    products = [{"id": "a", "width": 100, "height": 100, "qty": 1, "_global_idx": 0}]
    assert rect_options(LOOKUP, "S235", "3", 3.0, products) is None


def test_compute_options_uses_the_packer_it_is_given():
    seen = []

    def pack(sw, sh):
        seen.append((sw, sh))
        return Packing(sheets=["layout"], sheets_needed=2, eff_w=sw, eff_h=sh,
                       draw_w=sw, draw_h=sh)

    result = compute_options({("2", "S235 | 1000x2000"): 1000.0}, "S235", "2", 2.0,
                             n_pieces=4, part_area_mm2=500_000, pack=pack)
    option = result.options[0]
    assert seen == [(1000, 2000)]
    assert option.sheets_needed == 2 and option.packing.sheets == ["layout"]
    assert math.isclose(option.total_eur, 64.0)            # 2 sheets × 32 kg × 1000 €/tn
    assert math.isclose(option.utilization, 0.125)         # 0.5 m² of 4 m²


def test_grouping_and_piece_costs():
    products = [
        {"id": "a", "material": "S235", "thickness": "2", "width": 100, "height": 100, "qty": 3},
        {"id": "b", "material": "S235", "thickness": "2", "width": 200, "height": 100, "qty": 1},
        {"id": "c", "material": None, "thickness": None, "width": 100, "height": 100, "qty": 1},
    ]
    groups = group_products(products, "combined")
    assert list(groups) == [("S235", "2")]
    assert [p["_global_idx"] for p in groups[("S235", "2")]] == [0, 1]
    assert len(group_products(products, "separate")) == 2

    costs = piece_costs(products, {"a": 1000.0}, {"b": 15_000})
    assert [c.index for c in costs] == [0, 1]
    assert math.isclose(costs[0].kg, 0.16) and math.isclose(costs[0].batch_eur, 0.48)
    assert math.isclose(costs[1].kg, 0.24) and costs[1].eur is None   # real area used
