"""The fewest-sheets plan for a group of parts: the cutting-stock method.

The packers fill one sheet at a time and never look ahead. Here many candidate
sheets (*patterns*: how many of each part one sheet holds) are generated, and
an integer program picks how often to cut each one so the order is covered
with the fewest sheets, then the fewest programs:

1. Seeds: the sheets the packers already made (Sparrow's nest the real shapes,
   so they can be tighter than anything a box check finds), and each part on
   its own sheet.
2. Column generation: the LP over the patterns so far prices each part (its
   dual); a new sheet is worth adding when its parts are worth more than one
   sheet. New sheets are filled greedily in several orders, each starting from
   1, 2, … of one part, since the best sheet often holds one big part and the
   rest made up of others.
3. Pair sheets: k of one part and m of another, then filled largest first:
   sheets of two parts that suit each other (two columns of different heights
   stacking to the sheet height) that the prices alone may not suggest.
4. Residual rounds: the LP's whole repeats are fixed and steps 2–3 run on what
   is left, which needs other partners than the full order.
5. The integer program over every sheet found; its plan may make a few pieces
   too many, so they are taken off single copies of a sheet (no extra pieces).

Before step 5, a sheet filled well (``GOOD_FILL``, real part area over the
sheet bought) whose repeats use up its parts exactly is kept as a program of
its own, and the integer program plans only the other parts. Production would
rather cut one good program more often than have a part split over programs
or sheet sizes to save a little steel — but not at any price: a program is
kept only while the plan costs at most ``KEEP_MAX_EXTRA`` more than the
cheapest one.

With several sheet sizes, each size's candidates are found on their own and
``best_plan`` picks over all of them at each sheet's price: a plan may cut
the big parts from one size and the rest from a smaller one.

Patterns are checked with ``fits(counts)``, which returns a one-sheet layout or
None; the caller decides how (the box packer here, for both tabs: a part fits
in its box, so a box layout is valid for the real shape too). ``max_checks``
caps those checks, so the run time stays bounded on large orders.
"""

from __future__ import annotations

import random

Pattern = tuple[int, ...]

_EPS = 1e-6
_RANDOM_ORDERS = 8      # random fill orders per pricing round
_PAIR_STEPS = 4         # at most this many counts of each part in a pair start
_DIVES = 3              # residual rounds
_MAX_ROUNDS = 40        # pricing rounds per column generation
_NEW_PER_ROUND = 12     # patterns added per pricing round
_ILP_SECONDS = 5
GOOD_FILL = 0.80        # a sheet this full that uses up its parts is kept as it is…
KEEP_MAX_EXTRA = 0.05   # …while the plan then costs at most this much more


def cutting_stock_plan(quantities: list[int], sizes: list[tuple[float, float]],
                       fits, seeds: list[tuple[Pattern, object]] = (), *,
                       max_checks: int = 4000) -> list[tuple[int, Pattern, object]] | None:
    """The plan as ``[(repeats, counts, layout)]``, fewest sheets then fewest
    programs; None if there is nothing to plan or no solver.

    ``sizes`` are the parts' boxes (w, h), used only to order the fills.
    ``seeds`` are ``(counts, layout)`` sheets known to fit. A plan's ``layout``
    may hold more pieces than its ``counts`` (a copy with surplus pieces taken
    off): the caller drops the extra placements.
    """
    pool = candidate_sheets(quantities, sizes, fits, seeds, max_checks=max_checks)
    plan = best_plan(quantities, [(1.0, pool)]) if pool else None
    return None if plan is None else [(r, p, layout) for _, r, p, layout in plan]


def candidate_sheets(quantities: list[int], sizes: list[tuple[float, float]],
                     fits, seeds: list[tuple[Pattern, object]] = (), *,
                     max_checks: int = 4000) -> dict[Pattern, object] | None:
    """Every candidate sheet found for one sheet size, ``{counts: layout}``
    (steps 1–4); None if there is nothing to plan or no solver. A part that
    fits no sheet of this size is left out (its quantity counts as 0).

    Each part's full sheet on its own — as many as one sheet holds, the order
    aside — is among them, for ``same_parts_plan``."""
    if not have_solver():
        return None
    q = [int(x) for x in quantities]
    if sum(q) == 0:
        return None
    search = _Search(q, sizes, fits, max_checks)
    for counts, layout in seeds:
        search.add(tuple(min(c, n) for c, n in zip(counts, q)), layout)
    for i in range(len(q)):
        if q[i]:
            search.fill([i], q)
    q = [n if any(p[i] for p in search.pool) else 0 for i, n in enumerate(q)]
    if sum(q) == 0:
        return None
    full = [search.full_sheet(i) for i in range(len(q)) if q[i]]

    search.generate(q)
    for _ in range(_DIVES):
        x = _lp(q, search.patterns())
        if x is None:
            break
        rest = list(q)
        for p, r in zip(search.patterns(), x):
            for i, n in enumerate(p):
                rest[i] -= int(r + _EPS) * n
        rest = [max(0, v) for v in rest]
        if not any(rest) or rest == q:
            break
        search.generate(rest)
    for counts, layout in full:
        if layout is not None:
            search.add(counts, layout)
    return dict(search.pool)


def best_plan(quantities: list[int], pools: list[tuple], *,
              areas: list[float] | None = None, good_fill: float = GOOD_FILL,
              max_extra: float = KEEP_MAX_EXTRA) -> list[tuple[int, int, Pattern, object]] | None:
    """The cheapest plan over one or more sheet sizes, then the fewest
    programs: ``[(size_index, repeats, counts, layout)]``. ``pools`` holds
    each size's ``(price of one sheet, candidate_sheets(...), sheet area)``;
    None when the candidates can't cover the order or there is no solver.

    With the parts' real ``areas``, well-filled sheets that use up their parts
    are kept first, fullest first, each while the plan costs at most
    ``max_extra`` more than the cheapest (see ``GOOD_FILL``)."""
    if not have_solver():
        return None
    q = [int(x) for x in quantities]
    cheapest = _plan_rest(q, pools, [])
    if cheapest is None or not areas:
        return None if cheapest is None else cheapest[1]
    best, kept = cheapest, []
    for entry in _good_programs(q, pools, areas, good_fill):
        taken = {i for _, _, p, _ in kept for i, c in enumerate(p) if c}
        if taken & {i for i, c in enumerate(entry[2]) if c}:
            continue
        trial = _plan_rest(q, pools, kept + [entry])
        if trial is not None and trial[0] <= cheapest[0] * (1 + max_extra) + _EPS:
            best, kept = trial, kept + [entry]
    return best[1]


def same_parts_plan(quantities: list[int], pools: list[tuple]
                    ) -> list[tuple[int, int, Pattern, object]] | None:
    """Same parts together first, the leftovers mixed: each part's full sheet
    on its own (the most one sheet holds; with several sizes, the one cheapest
    per piece), cut as many times as the order fills it; then the pieces left
    over of every part on as few, cheap sheets as possible (``best_plan``).
    A part with less than one full sheet goes with the leftovers. Same plan
    entries as ``best_plan``; None when the candidates can't cover the order.
    """
    if not have_solver():
        return None
    q = [int(x) for x in quantities]
    kept, rest = [], list(q)
    for i in range(len(q)):
        best = None
        for t, (price, pool, *_) in enumerate(pools):
            alone = [(p[i], p, layout) for p, layout in (pool or {}).items()
                     if p[i] and not any(c for j, c in enumerate(p) if j != i)]
            if not alone:
                continue
            k, p, layout = max(alone, key=lambda a: a[0])    # this size's full sheet
            if k <= q[i] and (best is None or (price / k, -k) < best[0]):
                best = ((price / k, -k), t, p, layout)
        if best is not None:
            _, t, p, layout = best
            r = q[i] // p[i]
            kept.append((t, r, p, layout))
            rest[i] -= r * p[i]
    if not any(rest):
        return kept
    leftovers = best_plan(rest, pools)
    return None if leftovers is None else kept + leftovers


def _plan_rest(q, pools, kept):
    """``(price, plan)``: the ``kept`` entries, and the cheapest plan for the
    parts not on them from the candidates without those parts; None if the
    candidates can't cover them."""
    rest = list(q)
    for _, r, p, _ in kept:
        rest = [n - r * c for n, c in zip(rest, p)]
    price = sum(r * pools[t][0] for t, r, _, _ in kept)
    if not any(rest):
        return price, list(kept)
    taken = {i for _, _, p, _ in kept for i, c in enumerate(p) if c}
    patterns = [(t, p) for t, (_, pool, *_) in enumerate(pools) if pool
                for p in _maximal([p for p in pool if not any(p[i] for i in taken)], rest)]
    if not patterns:
        return None
    plan = _ilp(rest, [p for _, p in patterns], [pools[t][0] for t, _ in patterns])
    if plan is None:
        return None
    entries = [(r, counts, (patterns[k][0], pools[patterns[k][0]][1][patterns[k][1]]))
               for k, r, counts in plan]
    trimmed = [(t, r, p, layout) for r, p, (t, layout) in _trim(rest, entries)]
    return price + sum(r * pools[t][0] for t, r, _, _ in trimmed), list(kept) + trimmed


def _maximal(patterns: list[Pattern], q: list[int]) -> list[Pattern]:
    """The sheets no other sheet of the same size beats in every part (counts
    capped at the demand ``q``): a beaten one is never needed, since surplus
    pieces come off anyway. Keeps the integer program small."""
    capped = {p: tuple(min(c, n) for c, n in zip(p, q)) for p in patterns}
    by_size = sorted(patterns, key=lambda p: -sum(capped[p]))
    kept: list[Pattern] = []
    for p in by_size:
        c = capped[p]
        if not any(c):
            continue
        if not any(all(a >= b for a, b in zip(capped[k], c)) for k in kept):
            kept.append(p)
    return kept


def _good_programs(q, pools, areas, good_fill):
    """Sheets at least ``good_fill`` full whose repeats make exactly the
    order's quantity of every part on them, as plan entries, the fullest
    first (then the cheaper)."""
    found = []
    for t, (price, pool, *rest) in enumerate(pools):
        sheet_area = rest[0] if rest else None
        if not pool or not sheet_area:
            continue
        for p, layout in pool.items():
            on = [i for i, n in enumerate(p) if n]
            r = q[on[0]] // p[on[0]] if on else 0
            if r < 1 or any(q[i] != r * p[i] for i in on):
                continue        # leftovers: not kept
            fill = sum(areas[i] * p[i] for i in on) / sheet_area
            if fill >= good_fill - _EPS:
                found.append((-fill, r * price, t, r, p, layout))
    return [(t, r, p, layout) for _, _, t, r, p, layout in sorted(found, key=lambda e: e[:2])]


def have_solver() -> bool:
    try:
        import numpy  # noqa: F401
        from scipy.optimize import milp  # noqa: F401
    except ImportError:
        return False
    return True


class _Search:
    """The pattern pool and the fills that grow it."""

    def __init__(self, q, sizes, fits, max_checks):
        self.n = len(q)
        self.sizes = sizes
        self.area = [max(w * h, _EPS) for w, h in sizes]
        self._fits = fits
        self.checks_left = max_checks
        self.pool: dict[Pattern, object] = {}
        self._known: dict[Pattern, object] = {}   # every check made, fits or not

    def patterns(self) -> list[Pattern]:
        return list(self.pool)

    def add(self, counts: Pattern, layout) -> None:
        if any(counts) and counts not in self.pool:
            self.pool[counts] = layout
            self._known[counts] = layout

    def check(self, counts: Pattern):
        """The layout for ``counts``, or None (also when out of checks)."""
        if counts in self._known:
            return self._known[counts]
        if self.checks_left <= 0:
            return None
        self.checks_left -= 1
        layout = self._fits(list(counts))
        self._known[counts] = layout
        return layout

    def fill(self, order: list[int], cap: list[int], start: Pattern | None = None) -> Pattern | None:
        """Add parts one at a time, in ``order``, as many of each as still fit;
        the result joins the pool."""
        counts = list(start) if start else [0] * self.n
        layout = self.check(tuple(counts)) if any(counts) else None
        if any(counts) and layout is None:
            return None
        for i in order:
            while counts[i] < cap[i]:
                counts[i] += 1
                trial = self.check(tuple(counts))
                if trial is None:
                    counts[i] -= 1
                    break
                layout = trial
        if not any(counts):
            return None
        p = tuple(counts)
        self.add(p, layout)
        return p

    def full_sheet(self, i: int) -> tuple[Pattern, object]:
        """The most of part i one sheet holds on its own, the order aside:
        ``(counts, layout)`` (layout None if it fits no sheet)."""
        def alone(k):
            return tuple(k if j == i else 0 for j in range(self.n))
        if self.check(alone(1)) is None:
            return alone(0), None
        lo = 1
        while lo < 4096 and self.check(alone(2 * lo)) is not None:
            lo *= 2
        hi = 2 * lo                     # lo fits, hi doesn't (or out of checks)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if self.check(alone(mid)) is not None else (lo, mid)
        return alone(lo), self.check(alone(lo))

    def by_size(self) -> list[int]:
        return sorted(range(self.n), key=lambda i: -max(self.sizes[i]))

    def generate(self, q: list[int]) -> None:
        """Column generation for demand ``q``, then the pair sheets."""
        rng = random.Random(sum(q) * 7919 + len(self.pool))
        for _ in range(_MAX_ROUNDS):
            duals = _duals(q, self.patterns())
            if duals is None or self.checks_left <= 0:
                break
            live = [i for i in range(self.n) if duals[i] > _EPS and q[i]]
            if not live:
                break
            dens = sorted(live, key=lambda i: -duals[i] / self.area[i])
            orders = [dens, sorted(live, key=lambda i: -duals[i]),
                      [i for i in self.by_size() if i in live]]
            orders += [sorted(live, key=lambda i: -duals[i] / self.area[i] * rng.uniform(0.6, 1.4))
                       for _ in range(_RANDOM_ORDERS)]
            starts: list[Pattern | None] = [None]
            for i in live:
                alone = self._alone(i, q)
                starts += [tuple(k if j == i else 0 for j in range(self.n))
                           for k in range(1, alone + 1)]
            found: dict[Pattern, float] = {}
            before = set(self.pool)
            for start in starts:
                for order in (orders if start is None else orders[:3]):
                    p = self.fill(order, q, start)
                    if p is None or p in before:
                        continue
                    value = sum(duals[i] * p[i] for i in range(self.n))
                    if value > 1 + _EPS:
                        found[p] = value
            # keep only the best few of this round's new sheets
            for p in set(self.pool) - before - set(
                    sorted(found, key=found.get, reverse=True)[:_NEW_PER_ROUND]):
                del self.pool[p]
            if not found:
                break
        self._pairs(q)

    def _alone(self, i: int, q: list[int]) -> int:
        """How many of part i one sheet holds (up to its demand)."""
        k = 0
        while k < q[i] and self.check(tuple(k + 1 if j == i else 0 for j in range(self.n))):
            k += 1
        return k

    def _pairs(self, q: list[int]) -> None:
        order = self.by_size()
        alone = [self._alone(i, q) if q[i] else 0 for i in range(self.n)]
        for a in range(self.n):
            for b in range(a + 1, self.n):
                if not (alone[a] and alone[b]):
                    continue
                for ka in _steps(alone[a]):
                    for kb in _steps(alone[b]):
                        if self.checks_left <= 0:
                            return
                        start = [0] * self.n
                        start[a], start[b] = ka, kb
                        self.fill(order, q, tuple(start))


def _steps(k: int) -> list[int]:
    """Up to ``_PAIR_STEPS`` counts from 1 to k, spread out."""
    if k <= _PAIR_STEPS:
        return list(range(1, k + 1))
    return sorted({max(1, round(k * s / _PAIR_STEPS)) for s in range(1, _PAIR_STEPS + 1)})


def _matrix(q, patterns):
    import numpy as np
    return np.array([[p[i] for p in patterns] for i in range(len(q))], dtype=float)


def _solve_lp(q, patterns):
    import numpy as np
    from scipy.optimize import linprog
    if not patterns:
        return None
    res = linprog(np.ones(len(patterns)), A_ub=-_matrix(q, patterns),
                  b_ub=-np.array(q, dtype=float), bounds=(0, None), method="highs")
    return res if res.status == 0 else None


def _lp(q, patterns):
    res = _solve_lp(q, patterns)
    return None if res is None else list(res.x)


def _duals(q, patterns):
    res = _solve_lp(q, patterns)
    return None if res is None else [-d for d in res.ineqlin.marginals]


def _ilp(q, patterns, prices=None) -> list[tuple[int, int, Pattern]] | None:
    """``[(pattern index, repeats, counts)]`` making exactly ``q``: the lowest
    price (each pattern's sheet ``prices``, all 1 when not given: the fewest
    sheets), then the fewest programs.

    Two steps. First the price alone, with surplus pieces allowed (an easy
    integer program, solved to the optimum; leaving pieces off a sheet costs
    nothing). Then the fewest programs at that price, exactly: each pattern
    cut a whole number of times plus at most one copy with pieces left off,
    which is a program of its own. The second step is the hard one; if it
    runs out of time, its best plan so far is used, or the first step's with
    the surplus taken off — at the lowest price either way."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    m, n = len(patterns), len(q)
    upper = float(sum(q))
    a = _matrix(q, patterns)
    price = np.array(prices if prices is not None else [1.0] * m, dtype=float)
    cheapest = milp(price, constraints=[LinearConstraint(a, lb=np.array(q, dtype=float))],
                    integrality=np.ones(m), bounds=Bounds(0, upper),
                    options={"time_limit": _ILP_SECONDS})
    if cheapest.x is None:
        return None

    # Variables: x (whole copies), z (pattern used), w (one copy with pieces
    # off), then r[p, i] (pieces of part i left off that copy).
    nv = 3 * m + m * n
    rows, lbs, ubs = [], [], []

    def row(coeffs, lb=-np.inf, ub=np.inf):
        v = np.zeros(nv)
        for k, c in coeffs:
            v[k] += c
        rows.append(v)
        lbs.append(lb)
        ubs.append(ub)

    def r_ix(k, i):
        return 3 * m + k * n + i

    for i in range(n):                  # exactly the order
        row([(k, a[i, k]) for k in range(m)] + [(2 * m + k, a[i, k]) for k in range(m)]
            + [(r_ix(k, i), -1.0) for k in range(m)], q[i], q[i])
    for k in range(m):
        row([(k, 1.0), (m + k, -upper)], ub=0)              # x only when used
        for i in range(n):              # pieces off only that copy's own
            row([(r_ix(k, i), 1.0), (2 * m + k, -a[i, k])], ub=0)
    at_price = cheapest.fun + 1e-6 * max(1.0, abs(cheapest.fun))
    row([(k, price[k]) for k in range(m)] + [(2 * m + k, price[k]) for k in range(m)],
        ub=at_price)
    cost = np.concatenate([np.zeros(m), np.ones(m), np.ones(m), np.zeros(m * n)])
    upper_b = np.concatenate([np.full(m, upper), np.ones(2 * m), a.T.ravel()])
    fewest = milp(cost, constraints=[LinearConstraint(np.array(rows), np.array(lbs),
                                                      np.array(ubs))],
                  integrality=np.ones(nv), bounds=Bounds(0, upper_b),
                  options={"time_limit": _ILP_SECONDS})
    if fewest.x is None:
        x = [int(round(v)) for v in cheapest.x]
        return [(k, r, patterns[k]) for k, r in enumerate(x) if r > 0]
    v = [int(round(t)) for t in fewest.x]
    plan = [(k, v[k], patterns[k]) for k in range(m) if v[k] > 0]
    for k in range(m):
        if v[2 * m + k]:
            left = tuple(patterns[k][i] - v[r_ix(k, i)] for i in range(n))
            if any(left):
                plan.append((k, 1, left))
    return plan


def _trim(q, plan):
    """Take surplus pieces off single copies, fewest-repeated patterns first;
    a copy left empty is dropped (one sheet less). Copies trimmed alike are
    merged back into one program."""
    made = [sum(r * p[i] for r, p, _ in plan) for i in range(len(q))]
    surplus = [m - n for m, n in zip(made, q)]
    out: list[list] = []
    for r, p, layout in sorted(plan, key=lambda e: e[0]):
        while r > 0 and any(surplus[i] and p[i] for i in range(len(q))):
            cut = [min(surplus[i], p[i]) for i in range(len(q))]
            surplus = [s - c for s, c in zip(surplus, cut)]
            r -= 1
            trimmed = tuple(n - c for n, c in zip(p, cut))
            if any(trimmed):
                out.append([1, trimmed, layout])
        if r > 0:
            out.append([r, p, layout])
    merged: dict[Pattern, list] = {}
    for r, p, layout in out:
        if p in merged:
            merged[p][0] += r
        else:
            merged[p] = [r, p, layout]
    return sorted((tuple(e) for e in merged.values()), key=lambda e: -e[0])


class MixedSizes:
    """Each sheet size's candidate sheets, kept as a packer packs the sizes
    one by one, for ``compute_options``'s ``mix``: the cheapest plan over all
    of them, when it really cuts from two sizes or more.

    The packer calls ``keep`` once per size, in order (``None`` for a size
    with no candidates), with ``to_packing(entries)`` that turns that size's
    ``[(repeats, counts, layout)]`` into ``(Packing, real part area on its
    sheets)``.
    """

    def __init__(self, quantities: list[int], areas: list[float] | None = None, *,
                 same_parts_first: bool = False):
        self.quantities = quantities
        self.areas = areas
        self.same_parts_first = same_parts_first
        self._sizes: list[tuple[dict | None, object, float | None]] = []

    def keep(self, pool: dict[Pattern, object] | None, to_packing,
             sheet_area: float | None = None) -> None:
        self._sizes.append((pool, to_packing, sheet_area))

    def plan(self, sheet_eur: list[float]):
        """``[(size index, Packing, part area)]``, or None when the cheapest
        plan uses one size only (that size's own rows already show it)."""
        pools = [(eur, pool, area) for eur, (pool, _, area) in zip(sheet_eur, self._sizes)]
        plan = (same_parts_plan(self.quantities, pools) if self.same_parts_first
                else best_plan(self.quantities, pools, areas=self.areas))
        used = sorted({t for t, *_ in plan or []})
        if len(used) < 2:
            return None
        return [(t, *self._sizes[t][1]([(r, p, layout) for u, r, p, layout in plan if u == t]))
                for t in used]
