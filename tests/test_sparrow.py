"""The Sparrow module. A small fake solver stands in for the binary, so the
fixed-sheet search and the costing run in milliseconds; one test runs the real
executable when it is installed."""

import math
from collections import Counter

import pytest

from core.dxf import read_dxf
from core.geometry import bbox, signed_area
from core.sheet_cost import cheapest_index
from core.sparrow import (
    SparrowPart,
    SparrowResult,
    _error_message,
    _result,
    find_executable,
    SheetNester,
    part_from_report,
    run_sparrow,
    sparrow_options,
)
from tests.conftest import dxf_bytes


def square(size=100, quantity=1, name="sq") -> SparrowPart:
    return SparrowPart(part_id=name, quantity=quantity,
                       outer=[(0, 0), (size, 0), (size, size), (0, size)],
                       width_mm=size, height_mm=size)


def fake_solver(instance, *, seed, time_limit_sec, separation):
    """Stack items in columns as high as the strip, left to right."""
    height = instance["strip_height"]
    x = y = col_w = 0.0
    placements = []
    for item in instance["items"]:
        x0, y0, x1, y1 = bbox(item["shape"]["data"])
        w, h = x1 - x0, y1 - y0
        for _ in range(item["demand"]):
            if y + h > height:
                x, y, col_w = x + col_w, 0.0, 0.0
            placements.append((item["id"], 0.0, (x - x0, y - y0)))
            y += h
            col_w = max(col_w, w)
    return SparrowResult(True, "", placements, x + col_w)


def test_part_from_report_orients_rings_and_keeps_holes(drawing):
    doc, msp = drawing()
    msp.add_lwpolyline([(0, 0), (0, 200), (300, 200), (300, 0)], close=True)  # clockwise
    msp.add_circle((150, 100), 40)
    part = part_from_report(read_dxf(dxf_bytes(doc), "bracket.dxf").part(), 5)
    assert part.part_id == "bracket" and part.quantity == 5
    assert signed_area(part.outer) > 0                  # counter-clockwise
    assert signed_area(part.holes[0]) < 0               # clockwise
    assert part.shape_dict()["type"] == "polygon"


def test_result_needs_every_requested_item_placed():
    instance = {"items": [{"id": 0, "demand": 2}]}
    placed = lambda n: {"solution": {"strip_width": 50.0, "layout": {"placed_items": [
        {"item_id": 0, "transformation": {"rotation": 90.0, "translation": [1.0, 2.0]}}] * n}}}
    ok = _result(instance, placed(2))
    assert ok.ok and ok.strip_width == 50.0 and ok.placements[0] == (0, 90.0, (1.0, 2.0))
    assert not _result(instance, placed(1)).ok
    assert not _result(instance, placed(0)).ok


def test_error_message_skips_the_backtrace():
    stderr = "Error: Simple polygon must have at least 3 points\n\nStack backtrace:\n   0: foo\n      at src/x.rs"
    assert _error_message(stderr) == "Simple polygon must have at least 3 points"


def test_fills_a_sheet_then_repeats_the_layout():
    # 100 mm squares on a 250 × 250 sheet: two columns of two fit → 4 per sheet
    sheets, reason = SheetNester([square(quantity=10)], fake_solver).pack(250, 250)
    assert reason == ""
    assert [(len(s.placements), s.count) for s in sheets] == [(4, 2), (2, 1)]
    assert math.isclose(sum(s.used_area * s.count for s in sheets), 10 * 100 * 100)


def test_mixed_parts_are_all_placed_on_the_fewest_sheets():
    runs = []
    counting = lambda inst, **kw: runs.append(inst) or fake_solver(inst, **kw)  # noqa: E731
    parts = [square(200, quantity=5, name="big"), square(100, quantity=4, name="small")]
    sheets, reason = SheetNester(parts, counting).pack(400, 400)
    assert reason == ""
    assert sum(s.count for s in sheets) == 2                 # 5·4 + 4·1 dm² on 16 dm² sheets
    placed = Counter(pl.part_id for s in sheets for pl in s.placements for _ in range(s.count))
    assert placed == {"big": 5, "small": 4}
    assert all(0 <= x <= 400 for s in sheets for pl in s.placements for x, _ in pl.outer)
    assert len(runs) <= 1 + 8                                # seed strip + probe budget


def test_answers_are_shared_across_sheet_sizes():
    runs = []
    counting = lambda inst, **kw: runs.append(inst) or fake_solver(inst, **kw)  # noqa: E731
    nester = SheetNester([square(200, quantity=5, name="big"),
                          square(100, quantity=4, name="small")], counting)
    first = nester.pack(400, 400)
    n = len(runs)
    assert nester.pack(400, 400) == first and len(runs) == n      # same size: nothing new
    nester.pack(500, 400)                                          # same height: same strip
    whole_order = [r for r in runs if [i["demand"] for i in r["items"]] == [5, 4]]
    assert len(whole_order) == 1


def test_a_second_run_reuses_the_answers():
    runs = []
    counting = lambda inst, **kw: runs.append(inst) or fake_solver(inst, **kw)  # noqa: E731
    lookup = {("2", "S235 | 1000x2000"): 900.0}
    parts = lambda n: [square(600, quantity=n, name="big"),  # noqa: E731
                       square(300, quantity=4, name="small")]
    memories = {}
    first = sparrow_options(lookup, "S235", "2", 2.0, parts(5), run_fn=counting,
                            memories=memories)
    n = len(runs)
    assert n > 0
    again = sparrow_options(lookup, "S235", "2", 2.0, parts(5), run_fn=counting,
                            margin_pct=10.0, memories=memories)
    assert len(runs) == n                                        # new margin: no run
    assert again.options[0].sheets_needed == first.options[0].sheets_needed
    before = len(runs)
    sparrow_options(lookup, "S235", "2", 2.0, parts(4), run_fn=counting, memories=memories)
    reused = len(runs) - before
    before = len(runs)
    sparrow_options(lookup, "S235", "2", 2.0, parts(4), run_fn=counting, memories={})
    assert reused <= len(runs) - before           # new quantity: never more than from scratch


def test_a_mix_that_fits_by_bounding_boxes_needs_no_sparrow_run():
    runs = []
    counting = lambda inst, **kw: runs.append(inst) or fake_solver(inst, **kw)  # noqa: E731
    bar = SparrowPart(part_id="bar", quantity=2, outer=[(0, 0), (300, 0), (300, 100), (0, 100)])
    sheets, reason = SheetNester([bar], counting).pack(250, 350)   # needs a 90° turn
    assert reason == "" and runs == []
    assert [(len(s.placements), s.count) for s in sheets] == [(2, 1)]
    assert all(-1e-6 <= x <= 250 and -1e-6 <= y <= 350
               for pl in sheets[0].placements for x, y in pl.outer)


def test_reports_a_part_too_big_for_the_sheet():
    sheets, reason = SheetNester([square(size=300, name="big")], fake_solver).pack(250, 250)
    assert not sheets and "big" in reason


def test_sparrow_options_prices_with_the_given_solver():
    lookup = {("2", "S235 | 1000x2000"): 900.0}
    events = []
    result = sparrow_options(lookup, "S235", "2", 2.0, [square(quantity=10)], run_fn=fake_solver,
                             on_progress=lambda kind, **kw: events.append(kind))
    option = result.options[0]
    assert option.sheets_needed == 1
    assert math.isclose(option.utilization, 0.05)        # 10 × 0.01 m² of 2 m²
    assert math.isclose(option.total_eur, 28.8)          # 32 kg × 900 €/tn
    assert events[0] == "size" and "sheet" in events


def test_a_combination_of_sizes_is_offered_when_cheaper():
    # 3 × 1 m² parts: 2 on a cheap 2 m² sheet + 1 on a dearer 1 m² sheet beats
    # either size alone (2 × 2 m² or 3 × 1 m²).
    lookup = {("2", "S235 | 1000x2000"): 800.0, ("2", "S235 | 1000x1000"): 1000.0}
    result = sparrow_options(lookup, "S235", "2", 2.0, [square(1000, quantity=3)],
                             run_fn=fake_solver)
    combo = result.options[cheapest_index(result.options)]
    assert combo.combo is not None and combo.sheets_needed == 2
    assert sorted((o.sw, o.sh, o.sheets_needed) for o in combo.combo) == [
        (1000, 1000, 1), (1000, 2000, 1)]
    assert math.isclose(combo.total_eur, sum(o.total_eur for o in combo.combo))
    assert math.isclose(combo.utilization, 1.0)
    assert combo.total_eur < min(o.total_eur for o in result.options if o.combo is None)


def test_parts_that_may_not_turn_are_also_nested_on_the_turned_sheet():
    part = SparrowPart(part_id="r", quantity=6, outer=[(0, 0), (300, 0), (300, 200), (0, 200)],
                       allowed_orientations=(0.0,), width_mm=300, height_mm=200)
    result = sparrow_options({("2", "S235 | 1000x2000"): 900.0}, "S235", "2", 2.0, [part],
                             run_fn=fake_solver)
    packing = result.options[0].packing
    assert (packing.draw_w, packing.draw_h) == (2000, 1000)
    assert (packing.alt.draw_w, packing.alt.draw_h) == (1000, 2000)


@pytest.mark.sparrow
def test_real_sparrow_binary():
    exe = find_executable()
    if exe is None:
        pytest.skip("Sparrow executable not found")
    instance = {"name": "t", "strip_height": 1000.0, "items": [
        {"id": 0, "demand": 3, "shape": {"type": "simple_polygon",
                                         "data": [[0, 0], [300, 0], [300, 200], [0, 200]]}}]}
    result = run_sparrow(instance, executable=exe, time_limit_sec=2, seed=0)
    assert result.ok and len(result.placements) == 3
    assert 199 < result.strip_width < 301
    bad = {**instance, "items": [{**instance["items"][0], "shape": {"type": "simple_polygon", "data": [[0, 0], [1, 0]]}}]}
    assert "at least 3 points" in run_sparrow(bad, executable=exe, time_limit_sec=2).message
