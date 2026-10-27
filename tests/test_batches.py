"""Prices for several batch sizes: whole sheets at the nesting's scrap rate,
work at each quantity, and the quote's totals."""

import pytest

from core.batches import (
    BatchPart,
    GroupSheets,
    batch_prices,
    order_prices,
    parse_batch_sizes,
)
from core.quote import QuoteLine, quote_totals
from core.work import LaserSpeed, WorkInputs, WorkRates

RATES = WorkRates()


@pytest.mark.parametrize("text, sizes", [
    ("1 5 10", [1, 5, 10]), ("10, 5,1", [1, 5, 10]), ("1·5·10", [1, 5, 10]),
    ("5 5 0", [5]), ("", []), ("abc", []),
])
def test_batch_sizes_are_read_from_free_text(text, sizes):
    assert parse_batch_sizes(text) == sizes


def test_the_nested_quantity_gets_the_nested_price():
    # Two parts of 1 kg nested 10 each on 2 sheets for 100 €.
    group = GroupSheets(total_eur=100.0, sheets=2, pieces_kg=20.0)
    assert group.sheets_for(20.0) == 2 and group.eur_for(20.0) == 100.0
    assert group.sheets_for(2.0) == 1            # at least one whole sheet
    assert group.sheets_for(30.0) == 3           # 1.5 × the sheets, rounded up


def test_each_batch_size_shares_whole_sheets_and_setup():
    work = WorkInputs(cut_length_m=1.0, laser=LaserSpeed(10, 0.05))   # 0.50 €/kpl
    parts = [BatchPart("a", ("S235", "2"), kg=1.0, work=work),
             BatchPart("b", ("S235", "2"), kg=3.0, work=None)]
    groups = {("S235", "2"): GroupSheets(total_eur=80.0, sheets=1, pieces_kg=40.0)}
    prices = batch_prices(parts, groups, [1, 10], RATES)
    one, ten = prices["a"]
    # Batch 1: 4 kg of pieces still takes one 80 € sheet: 20 €/kg.
    assert one.sheets == 1 and one.material == pytest.approx(20.0)
    # Cutting 0.50 € is under the 7 € sheet minimum; setup 40 € over 1 piece.
    assert one.work.cut_minimum == pytest.approx(6.5)
    assert one.unit == pytest.approx(20.0 + 0.5 + 6.5 + 40.0)
    # Batch 10 is the nesting: 2 €/kg, the setup over 10 pieces.
    assert ten.material == pytest.approx(2.0)
    assert ten.unit == pytest.approx(2.0 + 0.5 + 0.2 + 4.0)
    # The part without work has no price, so the quote's total is open.
    assert prices["b"][0].unit is None
    lines = [QuoteLine(pid, prices=tuple(p)) for pid, p in prices.items()]
    assert quote_totals(lines, 2) == [None, None]


def test_a_group_not_nested_yet_has_no_material_price():
    parts = [BatchPart("a", ("S235", "3"), kg=1.0, work=WorkInputs())]
    price = batch_prices(parts, {}, [5], RATES)["a"][0]
    assert price.material is None and price.unit is None


def test_totals_add_every_part_at_each_size():
    work = WorkInputs(method="punch", hits=100)          # 0.80 €/kpl
    parts = [BatchPart("a", ("S235", "2"), kg=1.0, work=work),
             BatchPart("b", ("S235", "2"), kg=1.0, work=work)]
    groups = {("S235", "2"): GroupSheets(total_eur=50.0, sheets=1, pieces_kg=10.0)}
    prices = batch_prices(parts, groups, [5], RATES)
    total = quote_totals([QuoteLine(pid, prices=tuple(p)) for pid, p in prices.items()], 1)[0]
    assert total == pytest.approx(sum(p[0].unit * 5 for p in prices.values()))


def test_the_order_prices_each_part_at_its_own_quantity_and_shares_the_sheets():
    # 3 of a 1 kg part and 2 of a 3 kg part nested on 2 sheets for 90 €: the
    # sheets go 3 kg to 6 kg, so a's pieces carry 30 € and b's 60 €.
    work = WorkInputs(method="punch", hits=100)          # 0.80 €/kpl
    parts = [BatchPart("a", ("S235", "2"), kg=1.0, work=work),
             BatchPart("b", ("S235", "2"), kg=3.0, work=work)]
    groups = {("S235", "2"): GroupSheets(total_eur=90.0, sheets=2, pieces_kg=9.0)}
    prices = order_prices(parts, groups, {"a": 3, "b": 2}, RATES)
    a, b = prices["a"], prices["b"]
    assert (a.qty, b.qty) == (3, 2) and a.sheets == b.sheets == 2
    assert a.material * 3 == pytest.approx(30.0) and b.material * 2 == pytest.approx(60.0)
    # The setup is over each part's own quantity.
    assert a.work.qty == 3 and b.work.qty == 2
    assert a.unit == pytest.approx(a.material + a.work.per_piece)


def test_the_order_has_no_material_price_for_a_group_not_nested():
    parts = [BatchPart("a", ("S235", "3"), kg=1.0, work=WorkInputs())]
    price = order_prices(parts, {}, {"a": 4}, RATES)["a"]
    assert price.qty == 4 and price.material is None and price.unit is None
