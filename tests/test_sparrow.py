"""The Sparrow module. A small fake solver stands in for the binary, so the
fixed-sheet search and the costing run in milliseconds; one test runs the real
executable when it is installed."""

import math
from collections import Counter

import pytest

from core.dxf import read_dxf
from core.geometry import bbox, rotate_translate, signed_area
from core.sheet_cost import EdgeGaps
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


def fake_solver(instance, *, separation):
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


def made(packing):
    """Pieces of each part a packing cuts."""
    out = Counter()
    for sheet in packing.sheets:
        for pl in sheet.placements:
            out[pl.part_id] += sheet.count
    return dict(out)


def test_mixed_parts_get_a_one_program_plan_next_to_the_fewest_sheets():
    # 4 squares per 250 × 250 sheet. Greedy: 4 sheets over 3 layouts; one
    # program of 2 a + 1 b, cut 5 times, needs a sheet more.
    result = sparrow_options({("2", "S235 | 250x250"): 900.0}, "S235", "2", 2.0,
                             [square(quantity=10, name="a"), square(quantity=5, name="b")],
                             run_fn=fake_solver)
    fewest, kit = result.options
    assert (fewest.sheets_needed, fewest.programs) == (4, 3)
    assert (kit.sheets_needed, kit.programs) == (5, 1)
    assert made(fewest.packing) == made(kit.packing) == {"a": 10, "b": 5}


def test_a_kit_plan_as_good_in_sheets_replaces_the_greedy_one():
    # Greedy: 3 sheets, 3 layouts. Kit 3 a + 1 b ×2 and the rest on one sheet:
    # 3 sheets, 2 programs.
    result = sparrow_options({("2", "S235 | 250x250"): 900.0}, "S235", "2", 2.0,
                             [square(quantity=7, name="a"), square(quantity=3, name="b")],
                             run_fn=fake_solver)
    [option] = result.options
    assert (option.sheets_needed, option.programs) == (3, 2)
    assert made(option.packing) == {"a": 7, "b": 3}


def test_a_long_part_drawn_diagonally_fits_once_laid_along_its_length(drawing):
    # 2900 × 80 at 45° is 2108 × 2108 as drawn: too big for 1500 × 3000 until
    # the reader lays it flat
    doc, msp = drawing()
    msp.add_lwpolyline(rotate_translate([(0, 0), (2900, 0), (2900, 80), (0, 80)], 45, (0, 0)),
                       close=True)
    part = part_from_report(read_dxf(dxf_bytes(doc), "strip.dxf").part(), 3)
    pack = greedy_fixed_sheets([part], 3000, 1500, run_fn=fake_solver)
    assert pack.ok and pack.sheets_needed == 1


def turning_solver(instance, *, separation):
    """Like ``fake_solver``, but each item takes its first allowed rotation."""
    height = instance["strip_height"]
    x = y = col_w = 0.0
    placements = []
    for item in instance["items"]:
        rot = item["allowed_orientations"][0]
        x0, y0, x1, y1 = bbox(rotate_translate(item["shape"]["data"], rot, (0, 0)))
        w, h = x1 - x0, y1 - y0
        for _ in range(item["demand"]):
            if y + h > height:
                x, y, col_w = x + col_w, 0.0, 0.0
            placements.append((item["id"], rot, (x - x0, y - y0)))
            y += h
            col_w = max(col_w, w)
    return SparrowResult(True, "", placements, x + col_w)


def bar_part(drawing, angle, angles, w=2900, h=80):
    """A w × h bar drawn turned by ``angle``, read like an uploaded DXF."""
    doc, msp = drawing()
    msp.add_lwpolyline(rotate_translate([(0, 0), (w, 0), (w, h), (0, h)], angle, (0, 0)),
                       close=True)
    return part_from_report(read_dxf(dxf_bytes(doc), "bar.dxf").part(), 2, angles)


def test_both_nesting_angles_keep_every_quarter_turn(drawing):
    assert bar_part(drawing, 45, (0, 90)).orientations == (0.0, 90.0, 180.0, 270.0)


def test_a_fixed_angle_puts_the_part_on_the_sheet_as_drawn(drawing):
    # A bar drawn at 30°: the reader lays it flat (−30°), and 0/180 turns it
    # back, so the drawing's X axis lies along the sheet's long side and the
    # bar keeps its 30° (or 210°) to it.
    part = bar_part(drawing, 30, (0,), w=1000)
    assert part.orientations == (30.0, 210.0)
    pack = greedy_fixed_sheets([part], 3000, 1500, run_fn=turning_solver)
    assert pack.ok
    for pl in pack.sheets[0].placements:
        edges = list(zip(pl.outer, pl.outer[1:] + pl.outer[:1]))
        (x1, y1), (x2, y2) = max(edges, key=lambda e: math.dist(*e))   # a long side
        assert math.isclose(math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180, 30, abs_tol=1e-6)


def test_a_fixed_angle_may_not_turn_a_part_to_fit(drawing):
    # 400 × 1600 drawn standing: along the rolling direction it is 1600 high
    standing = lambda angles: bar_part(drawing, 0, angles, w=400, h=1600)  # noqa: E731
    pack = greedy_fixed_sheets([standing((0,))], 3000, 1500, run_fn=turning_solver)
    assert not pack.ok and pack.reason == "bar (400 × 1600 mm) ei mahdu"
    assert greedy_fixed_sheets([standing((90,))], 3000, 1500, run_fn=turning_solver).ok


def test_greedy_reports_a_part_too_big_for_the_sheet():
    pack = greedy_fixed_sheets([square(size=300, name="big")], 250, 250, run_fn=fake_solver)
    assert not pack.ok and pack.reason == "big (300 mm) ei mahdu"


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


def test_the_sheet_is_nested_once_with_the_short_side_as_strip_height():
    nested = set()
    result = sparrow_options({("2", "S235 | 1000x2000"): 900.0}, "S235", "2", 2.0,
                             [square(quantity=6)], run_fn=fake_solver,
                             on_progress=lambda kind, **kw: kind == "sheet" and nested.add(
                                 (kw["w"], kw["h"])))
    packing = result.options[0].packing
    assert (packing.draw_w, packing.draw_h) == (2000, 1000)
    assert nested == {(2000, 1000)}


def test_edge_gaps_shrink_the_strip_sparrow_fills():
    result = sparrow_options({("2", "S235 | 1000x2000"): 900.0}, "S235", "2", 2.0,
                             [square(quantity=6)], run_fn=fake_solver,
                             edges=EdgeGaps(top=10, bottom=20, left=30, right=40))
    packing = result.options[0].packing
    assert (packing.x0, packing.y0, packing.eff_w, packing.eff_h) == (30, 10, 1930, 970)


def test_the_strip_is_padded_by_the_gap_and_parts_moved_back():
    seen = {}

    def solver(instance, *, separation):
        seen["strip_height"] = instance["strip_height"]
        return fake_solver(instance, separation=separation)

    # the fake solver ignores the gap and places at the strip's corner (0, 0);
    # moved back by the 10 mm pad, that lands outside the sheet and is dropped
    pack = greedy_fixed_sheets([square(quantity=1)], 1000, 500, run_fn=solver,
                               separation=10.0)
    assert seen["strip_height"] == 520
    assert not pack.ok


@pytest.mark.sparrow
def test_real_sparrow_lets_parts_touch_the_sheet_edge():
    exe = find_executable()
    if exe is None:
        pytest.skip("Sparrow executable not found")

    def run_fn(instance, *, separation):
        return run_sparrow(instance, executable=exe, time_limit_sec=2,
                           min_item_separation=separation)

    # Two 100 mm squares with a 10 mm gap need 210 × 100; with the gap at the
    # sheet edges too they would need 230 × 120. The 212 × 102 sheet (2 mm of
    # slack for the solver) holds both only without the edge gap.
    pack = greedy_fixed_sheets([square(quantity=2)], 212, 102, run_fn=run_fn, separation=10.0)
    assert pack.ok and pack.sheets_needed == 1
    boxes = sorted(bbox(p.outer) for p in pack.sheets[0].placements)
    gap = boxes[1][0] - boxes[0][2]
    assert 10 - 0.5 <= gap and boxes[1][2] <= 212 + 0.5


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
