"""Sheet patterns, pure: which mixes of parts fit one sheet (``search_patterns``)
and the cheapest set of sheets that covers an order (``cheapest_cover``).

A *mix* is a tuple of piece counts, one per part type. Whether a mix fits a
sheet is asked from an oracle passed in, so this module knows nothing about
Sparrow, geometry or prices."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from math import prod

Mix = tuple[int, ...]

# Above this many candidate mixes, only the proportional ladder is searched.
_MAX_CANDIDATES = 2_000
# At most this many open mixes are scored when choosing the next one to ask.
_SCORED = 200
# The exact cover search runs once the remaining demand has at most this many
# sub-mixes; above it, whole sheets of the best pattern are taken first.
_EXACT_STATES = 20_000


@dataclass(frozen=True)
class Pattern:
    """A mix that fits one sheet; ``layout`` is whatever the oracle placed."""

    mix: Mix
    layout: object = None


@dataclass(frozen=True)
class Library:
    """The patterns found for one sheet size, the price of one sheet and its
    area (metal bought; breaks price ties)."""

    sheet_cost: float
    patterns: tuple[Pattern, ...]
    sheet_area: float = 0.0


@dataclass(frozen=True)
class CoverItem:
    """``count`` sheets of size ``key`` cut with ``pattern``, of which ``mix``
    is used (a pattern can be used partly: dropping parts keeps a layout valid)."""

    key: object
    pattern: Pattern
    mix: Mix
    count: int


# ── Which mixes fit one sheet ─────────────────────────────────────────────────

def search_patterns(demand: Mix, fits, *, shares: tuple[float, ...],
                    budget: int = 8, seeds: tuple[Pattern, ...] = ()) -> list[Pattern]:
    """The largest mixes found to fit one sheet, at most ``budget`` oracle calls.

    ``fits(mix) -> (fitted_mix, layout)`` returns the part of ``mix`` it could
    place (``mix`` itself when all fit). ``shares[i]`` is one part's area as a
    fraction of the sheet, so mixes above a full sheet are never asked.
    ``seeds`` are patterns already known to fit.

    Fitting is taken as monotone: a smaller mix than a fit also fits, a larger
    mix than a miss also misses, so each answer settles many mixes at once;
    the search narrows the open mixes like a bisection (see ``_middle``).
    """
    found = {p.mix: p for p in seeds if any(p.mix)}
    misses: list[Mix] = []
    candidates = _candidates(demand, shares)
    for _ in range(budget):
        open_ = [c for c in candidates
                 if not any(_leq(c, f) for f in found) and not any(_leq(m, c) for m in misses)]
        if not open_:
            break
        mix = _middle(open_, demand, shares)
        fitted, layout = fits(mix)
        if any(fitted):
            found.setdefault(fitted, Pattern(fitted, layout))
        if fitted != mix:
            misses.append(mix)
    return [p for p in found.values()
            if not any(q != p.mix and _leq(p.mix, q) for q in found)]


def _candidates(demand: Mix, shares: tuple[float, ...]) -> list[Mix]:
    """Every non-empty mix within the demand that is not bigger than a sheet;
    only the proportional ladder when there are too many of those."""
    caps = [min(d, int(1 / s) if s > 0 else d) for d, s in zip(demand, shares)]
    if prod(c + 1 for c in caps) > _MAX_CANDIDATES:
        ladder = {proportional(demand, n) for n in range(1, sum(demand) + 1)}
        return sorted(m for m in ladder if _share(m, shares) <= 1)
    return [m for m in product(*(range(c + 1) for c in caps))
            if any(m) and _share(m, shares) <= 1]


def _middle(open_: list[Mix], demand: Mix, shares: tuple[float, ...]) -> Mix:
    """The open mix whose answer settles the most others either way — a fit
    settles every mix below it, a miss every mix above it (a bisection that
    also works for several part types). Ties go to the bigger, then the more
    balanced mix. Large open sets are scored on an even sample."""
    step = max(1, len(open_) // _SCORED)

    def score(c: Mix):
        below = sum(_leq(o, c) for o in open_)
        above = sum(_leq(c, o) for o in open_)
        return min(below, above), _share(c, shares), -_imbalance(c, demand)

    return max(open_[::step], key=score)


def proportional(demand: Mix, n: int) -> Mix:
    """``n`` pieces picked in proportion to ``demand`` (largest remainders)."""
    total = sum(demand)
    if n >= total:
        return demand
    raw = [d * n / total for d in demand]
    mix = [int(r) for r in raw]
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i] - mix[i], reverse=True)
    for i in by_remainder[:n - sum(mix)]:
        mix[i] += 1
    return tuple(mix)


def _imbalance(mix: Mix, demand: Mix) -> float:
    ratios = [m / d for m, d in zip(mix, demand) if d]
    return max(ratios) - min(ratios) if ratios else 0.0


# ── Cheapest set of sheets ────────────────────────────────────────────────────

def cheapest_cover(demand: Mix, libraries: dict, *, shares: tuple[float, ...]
                   ) -> list[CoverItem] | None:
    """The cheapest sheets (from any library) that hold every piece of ``demand``;
    None when some part is in no pattern. ``shares`` are part areas (any unit).

    Large orders first take whole sheets of the pattern that places the most
    part area per euro; the rest is solved exactly: fewest euros, then least
    metal bought, then fewest sheets.
    """
    options = [(key, lib, p) for key, lib in libraries.items() for p in lib.patterns]
    if any(d and not any(p.mix[i] for _, _, p in options) for i, d in enumerate(demand)):
        return None
    items: list[CoverItem] = []
    remaining = demand
    while prod(r + 1 for r in remaining) > _EXACT_STATES:
        key, _, pat = max((o for o in options if _leq(o[2].mix, remaining)),
                          key=lambda o: _share(o[2].mix, shares) / o[1].sheet_cost,
                          default=(None, None, None))
        if pat is None:
            break
        count = max(1, min(r // m for r, m in zip(remaining, pat.mix) if m) - 1)
        items.append(CoverItem(key, pat, pat.mix, count))
        remaining = tuple(r - m * count for r, m in zip(remaining, pat.mix))
    items += _exact_cover(remaining, options)
    return _merged(items)


def _exact_cover(demand: Mix, options: list) -> list[CoverItem]:
    """Cheapest cover of a small demand, by memoised search over what remains."""

    @lru_cache(maxsize=None)
    def best(remaining: Mix):
        if not any(remaining):
            return (0.0, 0.0, 0), None
        result = None
        for n, (_, lib, pat) in enumerate(options):
            used = tuple(min(r, m) for r, m in zip(remaining, pat.mix))
            if not any(used):
                continue
            (cost, area, sheets), _ = best(tuple(r - u for r, u in zip(remaining, used)))
            score = (round(cost + lib.sheet_cost, 6), area + lib.sheet_area, sheets + 1)
            if result is None or score < result[0]:
                result = (score, (n, used))
        return result

    items = []
    while any(demand):
        n, used = best(demand)[1]
        key, _, pat = options[n]
        items.append(CoverItem(key, pat, used, 1))
        demand = tuple(r - u for r, u in zip(demand, used))
    return items


def _merged(items: list[CoverItem]) -> list[CoverItem]:
    """Same size and mix merged into one item, fullest sheets first."""
    merged: dict[tuple, CoverItem] = {}
    for it in items:
        k = (it.key, it.pattern.mix, it.mix)
        old = merged.get(k)
        merged[k] = CoverItem(it.key, it.pattern, it.mix, it.count + (old.count if old else 0))
    return sorted(merged.values(), key=lambda it: -sum(it.mix))


# ── Mix helpers ───────────────────────────────────────────────────────────────

def _leq(a: Mix, b: Mix) -> bool:
    return all(x <= y for x, y in zip(a, b))


def _share(mix: Mix, shares: tuple[float, ...]) -> float:
    return sum(m * s for m, s in zip(mix, shares))
