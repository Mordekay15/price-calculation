"""The fewest-sheets plan (core.cutting_stock) on both packers: fewer sheets
than filling one sheet at a time, exact quantities, real shapes kept apart,
and plans cut from several sheet sizes."""

import random
from collections import Counter

from core.cutting_stock import _trim, best_plan, cutting_stock_plan
from core.programs import sheets_used
from core.rect_nesting import box_candidates, fewest_sheets, rect_options
from core.sheet_cost import cheapest_index
from core.sparrow import PackedSheet, Placed, SparrowPart, _fewest, sparrow_options

# Square plates like a real order: 10 of each, as a sheet-size table row.
PLATES = [524, 704, 1194, 524, 794, 974, 634]
LOOKUP = {("2", "S235 | 1250x2500"): 900.0}


def plates(qty=10):
    return [{"id": str(i), "width": s, "height": s, "qty": qty, "_global_idx": i}
            for i, s in enumerate(PLATES)]


def made(option, n):
    counts = [0] * n
    for sheet in option.packing.sheets:
        for pl in sheet.placements:
            counts[pl.product_idx] += sheet.count
    return counts


def test_parts_that_suit_each_other_share_sheets():
    # Filled one sheet at a time this order takes 20 sheets. The widths of the
    # parts taller than half the sheet add up to 17.2 sheet lengths, so 18 is
    # the fewest: violet + 2 × (light blue over pink) ×5, red + green + yellow
    # over dark blue ×10, and the 5 violets left two to a sheet.
    options = rect_options(LOOKUP, "S235", "2", 2.0, plates(), rankavali_mm=5).options
    best = min(options, key=lambda o: (o.sheets_needed, o.programs))
    assert best.sheets_needed == 18
    assert made(best, len(PLATES)) == [10] * len(PLATES)


def test_a_well_filled_program_that_uses_up_its_parts_is_kept():
    # Two violets fill a sheet 91 % and five make all ten. Kept as their own
    # program, the plan needs 19 sheets, 5.6 % more than 18: too much here.
    # Allowed 6 % more, the program is kept.
    pool = box_candidates([(s, s) for s in PLATES], [10] * len(PLATES), [], 2500, 1250, 5)
    areas = [s * s for s in PLATES]

    def violets(plan):
        return [(r, p[2]) for r, p, _ in plan if p[2]]
    assert sum(r for r, _, _ in fewest_sheets([10] * 7, pool, areas, 2500 * 1250)) == 18
    plan = [(r, p, layout) for _, r, p, layout in
            best_plan([10] * 7, [(1.0, pool, 2500 * 1250)], areas=areas, max_extra=0.06)]
    assert violets(plan) == [(5, 2)] and sum(r for r, _, _ in plan) == 19


def test_only_full_sheets_without_leftovers_are_kept_first():
    # Parts of area 40 and 10 on a sheet of 100: "2 a" is 80 % full, "2 a + 2 b"
    # 100 %, "2 b" 20 %.
    pool = {(2, 0): "aa", (0, 2): "bb", (2, 2): "aabb", (1, 0): "a"}

    def plan(q, extra=0.05):
        return sorted((r, p) for _, r, p, _ in best_plan(
            q, [(1.0, pool, 100.0)], areas=[40, 10], max_extra=extra))
    assert plan([2, 2]) == [(1, (2, 2))]                  # full and exact: kept
    # 4 a + 2 b: keeping "2 a" ×2 needs a sheet for the b's (3 sheets, not 2)
    assert plan([4, 2]) == [(1, (2, 0)), (1, (2, 2))]
    assert plan([4, 2], extra=0.5) == [(1, (0, 2)), (2, (2, 0))]
    # 3 a: "2 a" would leave one a over, so nothing is kept; 2 sheets either way
    assert sum(r for r, _ in plan([3, 0])) == 2


def test_never_more_sheets_than_filling_one_at_a_time_and_quantities_stay_exact():
    rng = random.Random(1)
    for _ in range(12):
        n = rng.randint(2, 6)
        products = [{"id": str(i), "width": rng.randint(150, 1200),
                     "height": rng.randint(150, 1000), "qty": rng.choice([1, 3, 5, 8, 12]),
                     "_global_idx": i} for i in range(n)]
        options = rect_options(LOOKUP, "S235", "2", 2.0, products).options
        for o in options:
            assert made(o, n) == [p["qty"] for p in products]
        fewest = min(o.sheets_needed for o in options)
        greedy = options[0].sheets_needed      # the fewest-sheets greedy plan comes first
        assert fewest <= greedy


def test_surplus_pieces_come_off_single_copies():
    # 3 × (2 a + 1 b) and 1 × (2 b) make 6 a + 5 b; the order is 5 a + 4 b.
    plan = _trim([5, 4], [(3, (2, 1), "A"), (1, (0, 2), "B")])
    assert sorted(plan) == [(1, (0, 1), "B"), (1, (1, 1), "A"), (2, (2, 1), "A")]
    # a copy left empty is dropped: one sheet less
    assert _trim([2], [(2, (2,), "A")]) == [(1, (2,), "A")]


def test_nothing_to_plan():
    assert cutting_stock_plan([0, 0], [(1, 1), (1, 1)], lambda counts: None) is None


def square(s):
    return [(0.0, 0.0), (s, 0.0), (s, s), (0.0, s)]


def test_box_sheets_place_the_real_shapes_inside_the_sheet_and_apart():
    # An L-shaped part may only lie as drawn (0/180); a square may turn.
    ell = [(0.0, 0.0), (300.0, 0.0), (300.0, 100.0), (100.0, 100.0), (100.0, 200.0), (0.0, 200.0)]
    parts = [SparrowPart("L", 7, ell, width_mm=300, height_mm=200, orientations=(0.0, 180.0)),
             SparrowPart("S", 5, square(250), width_mm=250, height_mm=250)]
    sheets = _fewest(parts, [], 1000.0, 500.0, 10.0)
    assert sum(s.count * sum(p.part_index == 0 for p in s.placements) for s in sheets) == 7
    assert sum(s.count * sum(p.part_index == 1 for p in s.placements) for s in sheets) == 5
    for sheet in sheets:
        boxes = []
        for pl in sheet.placements:
            xs, ys = [x for x, _ in pl.outer], [y for _, y in pl.outer]
            assert min(xs) >= -1e-6 and min(ys) >= -1e-6
            assert max(xs) <= 1000 + 1e-6 and max(ys) <= 500 + 1e-6
            if pl.part_index == 0:
                assert pl.rotation_deg == 0.0               # its nesting angle holds
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                apart = max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])
                assert apart >= 10 - 1e-6                    # the cut gap


def test_sparrow_sheets_that_interlock_are_kept():
    # Two right triangles make a square: Sparrow puts two on a sheet that holds
    # only one triangle's box. The plan keeps that sheet.
    tri = [(0.0, 0.0), (100.0, 0.0), (0.0, 100.0)]
    parts = [SparrowPart("T", 4, tri, width_mm=100, height_mm=100)]
    flipped = [(100.0, 100.0), (0.0, 100.0), (100.0, 0.0)]
    seed = PackedSheet([Placed(0, "T", 0.0, tri), Placed(0, "T", 180.0, flipped)],
                       100.0, 100.0, 5000.0)
    sheets = _fewest(parts, [seed], 100.0, 100.0, None)
    assert sheets_used(sheets) == 2 and [len(s.placements) for s in sheets] == [2]


# ── Several sheet sizes ───────────────────────────────────────────────────────

# 1000 × 2000 × 2 mm = 32 kg × 900 €/tn = 28.80 €; 1500 × 3000 = 72 kg × 850 = 61.20 €
TWO_SIZES = {("2", "S235 | 1000x2000"): 900.0, ("2", "S235 | 1500x3000"): 850.0}


def test_the_big_part_on_a_big_sheet_and_the_rest_on_a_small_one():
    # A only fits the big sheet, which holds A and one B. The other B would
    # take a second big sheet (122.40 €); a small sheet does (90 €).
    products = [{"id": "a", "width": 1400, "height": 1200, "qty": 1, "_global_idx": 0},
                {"id": "b", "width": 950, "height": 950, "qty": 2, "_global_idx": 1}]
    options = rect_options(TWO_SIZES, "S235", "2", 2.0, products).options
    small, big, mixed = options[0], options[1], options[-1]
    assert not small.ok and big.sheets_needed == 2          # A doesn't fit the small one
    assert [(m.sw, m.sheets_needed) for m in mixed.mix] == [(1000, 1), (1500, 1)]
    assert abs(mixed.total_eur - (28.80 + 61.20)) < 0.01
    assert mixed.sheets_needed == 2 and mixed.programs == 2
    assert sum(made(m, 2)[i] for m in mixed.mix for i in (0, 1)) == 3
    assert [sum(made(m, 2)[i] for m in mixed.mix) for i in (0, 1)] == [1, 2]
    assert 0 < mixed.utilization < 1
    assert cheapest_index(options) == len(options) - 1


def test_no_mixed_row_when_one_size_is_cheapest():
    products = [{"id": "a", "width": 900, "height": 900, "qty": 4, "_global_idx": 0}]
    options = rect_options(TWO_SIZES, "S235", "2", 2.0, products).options
    assert not any(o.mix for o in options)


def test_sparrow_mixes_sizes_too():
    from tests.test_sparrow import fake_solver, square
    parts = [square(size=1400, name="big"), square(size=950, quantity=2, name="b")]
    result = sparrow_options(TWO_SIZES, "S235", "2", 2.0, parts, run_fn=fake_solver)
    mixed = result.options[-1]
    assert [(m.sw, m.sheets_needed) for m in mixed.mix] == [(1000, 1), (1500, 1)]
    placed = Counter(pl.part_index for m in mixed.mix for s in m.packing.sheets
                     for pl in s.placements for _ in range(s.count))
    assert placed == {0: 1, 1: 2}
    assert cheapest_index(result.options) == len(result.options) - 1


def test_a_sheet_with_pieces_taken_off_is_nested_again_tight():
    # The plan needs 2 of a 3-square layout: taking the middle one off would
    # leave a hole, so the 2 are nested again (squares left to right).
    from core.sparrow import _plan_sheets
    from tests.test_sparrow import fake_solver

    parts = [SparrowPart("S", 2, square(100), width_mm=100, height_mm=100)]
    at = lambda x: Placed(0, "S", 0.0, [(x + px, py) for px, py in square(100)])  # noqa: E731
    holed = PackedSheet([at(0), at(400), at(200)], 600.0, 100.0, 30000.0)
    [sheet] = _plan_sheets(parts, [(1, (2,), holed)], 600.0, 100.0, run_fn=fake_solver)
    assert sorted(min(x for x, _ in pl.outer) for pl in sheet.placements) == [0, 100]
    [kept] = _plan_sheets(parts, [(1, (2,), holed)], 600.0, 100.0)   # no solver: as it was
    assert sorted(min(x for x, _ in pl.outer) for pl in kept.placements) == [0, 400]


def test_the_best_pairs_are_nested_on_their_real_shapes():
    # Sparrow nests the pairs whose box sheets fill best, two runs at most
    # each, and their sheets join the candidates (shapes that hook into each
    # other can hold more than their boxes say).
    from core.sparrow import _PAIR_BUDGET, _pair_sheets
    from tests.test_sparrow import fake_solver

    runs = []

    def counting(instance, *, separation):
        runs.append(sorted(i["demand"] for i in instance["items"]))
        return fake_solver(instance, separation=separation)

    parts = [SparrowPart(n, 4, square(s), width_mm=s, height_mm=s)
             for n, s in (("a", 100), ("b", 90), ("c", 80), ("d", 70), ("e", 60))]
    pool = {(1, 1, 0, 0, 0): None, (0, 0, 1, 1, 0): None, (0, 0, 0, 1, 1): None,
            (1, 0, 0, 0, 1): None, (2, 2, 0, 0, 0): None}
    found = _pair_sheets(parts, pool, 400.0, 200.0, run_fn=counting, separation=None)
    assert len(runs) <= 2 * _PAIR_BUDGET
    on = {tuple(i for i, n in enumerate(c) if n) for c in found}
    assert on <= {(0, 1), (2, 3), (0, 4), (3, 4)} and (0, 1) in on   # a + b fill best
    for counts, sheet in found.items():
        assert len(sheet.placements) == sum(counts)
