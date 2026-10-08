"""Repeatable sheet programs for a group of mixed parts, for either packer.

A sheet layout is one NC program; production cuts it as many times as its
``count`` says. The greedy packers find the fewest sheets, but uneven
quantities can leave them with several layouts. A designer instead makes a
*kit*: one layout holding ``q // R`` of every part, cut R times, plus at most a
small program for the remainder ``q % R``. No extra pieces are ever made.

Layouts are the packers' own sheet objects; only their ``count`` is used here.
"""

from __future__ import annotations

from math import gcd


def sheets_used(layouts: list) -> int:
    return sum(s.count for s in layouts)


def kit_plan(quantities: list[int], greedy: list, fits_kit, pack_rest, *,
             budget: int | None = None) -> list | None:
    """The kit plan with the fewest programs (then sheets), or None.

    ``greedy`` is the fewest-sheets plan; it bounds the search. ``fits_kit(kit)``
    returns a one-sheet layout holding ``kit[i]`` of part i, or None if it
    doesn't fit. ``pack_rest(rest)`` packs the remainder the usual way and
    returns its layouts (None on failure). ``budget`` caps the ``fits_kit``
    calls, which can be slow.

    Tried, in order: R that divides every quantity, up to one sheet more than
    the greedy plan (one program, no remainder); then the two R just below the
    greedy sheet count (a main program plus a remainder). A kit with more
    pieces than the greedy plan's fullest sheet is not tried.
    """
    if len(greedy) <= 1:
        return None     # already one program
    g = 0
    for q in quantities:
        g = gcd(g, q)
    greedy_sheets = sheets_used(greedy)
    most_per_sheet = max(len(s.placements) for s in greedy)
    calls = 0

    def try_kit(r: int) -> list | None:
        nonlocal calls
        kit = [q // r for q in quantities]
        if not any(kit) or sum(kit) > most_per_sheet or (budget is not None and calls >= budget):
            return None
        calls += 1
        layout = fits_kit(kit)
        if layout is None:
            return None
        layout.count = r
        rest = [q % r for q in quantities]
        rest_layouts = pack_rest(rest) if any(rest) else []
        return None if rest_layouts is None else [layout, *rest_layouts]

    for r in range(1, greedy_sheets + 2):
        if g % r == 0 and (plan := try_kit(r)) is not None:
            return plan

    best = None
    for r in (greedy_sheets - 1, greedy_sheets - 2):
        if r < 1 or g % r == 0:
            continue
        plan = try_kit(r)
        if plan is not None and (best is None or _plan_key(plan) < _plan_key(best)):
            best = plan
    return best


def _plan_key(plan: list) -> tuple[int, int]:
    return len(plan), sheets_used(plan)


def choose_plans(greedy: list, *others: list | None) -> list[list]:
    """The plans worth showing: the greedy one and the others (the kit, the
    cutting-stock plan), less any another plan is at least as good as in both
    sheets and programs. On a tie the later plan is kept."""
    plans = [p for p in (greedy, *others) if p is not None]
    keys = [(sheets_used(p), len(p)) for p in plans]
    kept = []
    for i, (plan, key) in enumerate(zip(plans, keys)):
        beaten = any(
            (other[0] <= key[0] and other[1] <= key[1]) and (other != key or j > i)
            for j, other in enumerate(keys) if j != i)
        if not beaten:
            kept.append(plan)
    return kept
