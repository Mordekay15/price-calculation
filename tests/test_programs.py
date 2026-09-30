"""The kit search, on a fake packer: a sheet holds parts up to a fixed
capacity (each part's ``size`` units), filled in order."""

from dataclasses import dataclass, field

from core.programs import choose_plans, kit_plan, sheets_used

CAPACITY = 20


@dataclass
class Layout:
    placements: list[int] = field(default_factory=list)   # part index per piece
    count: int = 1


def fits_kit(sizes):
    def fits(kit):
        if sum(n * s for n, s in zip(kit, sizes)) > CAPACITY:
            return None
        return Layout([i for i, n in enumerate(kit) for _ in range(n)])
    return fits


def greedy(sizes):
    """Fill one sheet at a time, merging identical sheets."""
    def pack(quantities):
        sheets, current, used = [], [], 0
        for i, q in enumerate(quantities):
            for _ in range(q):
                if used + sizes[i] > CAPACITY:
                    sheets.append(current)
                    current, used = [], 0
                current.append(i)
                used += sizes[i]
        if current:
            sheets.append(current)
        merged: dict[tuple, Layout] = {}
        for s in sheets:
            key = tuple(sorted(s))
            if key in merged:
                merged[key].count += 1
            else:
                merged[key] = Layout(s)
        return list(merged.values())
    return pack


def made(plan, n_parts):
    """Pieces of each part the plan cuts."""
    out = [0] * n_parts
    for layout in plan:
        for i in layout.placements:
            out[i] += layout.count
    return out


def test_one_program_when_the_quantities_share_a_divisor():
    sizes, qty = [1, 2], [100, 50]         # 200 units: 10 sheets of 10 A + 5 B
    base = greedy(sizes)(qty)
    assert len(base) > 1                   # greedy leaves several layouts
    plan = kit_plan(qty, base, fits_kit(sizes), greedy(sizes))
    assert len(plan) == 1 and plan[0].count == 10
    assert made(plan, 2) == qty            # exact, no extra pieces


def test_uneven_quantities_give_a_main_program_and_a_remainder():
    sizes, qty = [1, 2], [100, 37]         # gcd 1: no single program
    base = greedy(sizes)(qty)
    plan = kit_plan(qty, base, fits_kit(sizes), greedy(sizes))
    assert len(plan) == 2
    assert sheets_used(plan) <= sheets_used(base) + 1
    assert made(plan, 2) == qty


def test_the_budget_caps_the_fit_checks():
    calls = []
    sizes, qty = [1, 2], [100, 37]

    def counting(kit):
        calls.append(kit)
        return fits_kit(sizes)(kit)

    assert kit_plan(qty, greedy(sizes)(qty), counting, greedy(sizes), budget=0) is None
    assert calls == []


def test_one_greedy_program_needs_no_search():
    base = greedy([1])([40])               # two identical full sheets
    assert len(base) == 1
    assert kit_plan([40], base, lambda kit: 1 / 0, greedy([1])) is None


def test_a_dominated_plan_is_not_shown():
    two = [Layout([0], count=5), Layout([0, 1])]              # 6 sheets, 2 programs
    one_more = [Layout([0, 1], count=7)]                      # 7 sheets, 1 program
    one_same = [Layout([0, 1], count=6)]                      # 6 sheets, 1 program
    worse = [Layout([0], count=6), Layout([1])]               # 7 sheets, 2 programs
    assert choose_plans(two, None) == [two]
    assert choose_plans(two, one_more) == [two, one_more]     # a real trade-off
    assert choose_plans(two, one_same) == [one_same]
    assert choose_plans(two, worse) == [two]
