"""The Sparrow module. A small fake solver stands in for the binary, so the
fixed-sheet search and the costing run in milliseconds; one test runs the real
executable when it is installed."""

import math

import pytest

from core.dxf import read_dxf
from core.geometry import bbox, signed_area
from core.sparrow import (
    SparrowPart,
    SparrowResult,
    _error_message,
    _result,
    find_executable,
    greedy_fixed_sheets,
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


def test_greedy_fills_a_sheet_then_repeats_the_layout():
    # 100 mm squares on a 250 × 250 sheet: two columns of two fit → 4 per sheet
    pack = greedy_fixed_sheets([square(quantity=10)], 250, 250, run_fn=fake_solver)
    assert pack.ok
    assert [(len(s.placements), s.count) for s in pack.sheets] == [(4, 2), (2, 1)]
    assert pack.sheets_needed == 3
    assert math.isclose(pack.used_area, 10 * 100 * 100)


def test_greedy_reports_a_part_too_big_for_the_sheet():
    pack = greedy_fixed_sheets([square(size=300, name="big")], 250, 250, run_fn=fake_solver)
    assert not pack.ok and "big" in pack.reason


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
