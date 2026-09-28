"""The pure pattern search and sheet cover, with a counting fake oracle."""

from core.patterns import Library, Pattern, cheapest_cover, proportional, search_patterns


def oracle(limit):
    """A fake "does it fit": keeps pieces in order while ``limit(mix)`` holds."""
    calls = []

    def fits(mix):
        calls.append(mix)
        kept = [0] * len(mix)
        for i, n in enumerate(mix):
            for _ in range(n):
                kept[i] += 1
                if not limit(tuple(kept)):
                    kept[i] -= 1
                    break
        return tuple(kept), f"layout {tuple(kept)}"

    return fits, calls


def interlock(mix):
    """The user's picture: 4 big + 4 small interlock, 5 big leave room for 2 small."""
    big, small = mix
    return big <= 5 and big + small <= 8 and (big < 5 or small <= 2)


def test_search_finds_the_interlocking_mix_within_budget():
    fits, calls = oracle(interlock)
    found = {p.mix for p in search_patterns((5, 4), fits, shares=(0.18, 0.05), budget=8)}
    assert (4, 4) in found and (5, 2) in found
    assert len(calls) <= 8


def test_search_never_asks_a_mix_known_by_domination():
    fits, calls = oracle(lambda m: sum(m) <= 6)
    search_patterns((10,), fits, shares=(0.1,), budget=10, seeds=(Pattern((4,)),))
    assert all(4 < m[0] <= 10 for m in calls)            # ≤ 4 known from the seed
    assert len(calls) == len(set(calls)) <= 3            # bisection over 5..10


def test_cover_prefers_the_interlocking_pattern():
    lib = Library(10.0, (Pattern((4, 4)), Pattern((5, 2))))
    cover = cheapest_cover((5, 4), {"big": lib}, shares=(4.0, 1.0))
    assert sum(it.count for it in cover) == 2
    assert [(it.mix, it.count) for it in cover] == [((4, 4), 1), ((1, 0), 1)] or \
           [(it.mix, it.count) for it in cover] == [((5, 2), 1), ((0, 2), 1)]


def test_cover_puts_the_leftover_on_a_cheaper_sheet():
    libs = {"big": Library(10.0, (Pattern((4, 4)),)), "small": Library(3.0, (Pattern((1, 0)),))}
    cover = cheapest_cover((5, 4), libs, shares=(4.0, 1.0))
    assert sorted((it.key, it.mix, it.count) for it in cover) == [
        ("big", (4, 4), 1), ("small", (1, 0), 1)]


def test_cover_repeats_a_pattern_for_a_large_order():
    cover = cheapest_cover((400,), {"s": Library(1.0, (Pattern((26,)),))}, shares=(1.0,))
    assert [(it.mix, it.count) for it in cover] == [((26,), 15), ((10,), 1)]


def test_cover_is_none_when_a_part_is_in_no_pattern():
    assert cheapest_cover((1, 1), {"s": Library(1.0, (Pattern((1, 0)),))}, shares=(1, 1)) is None


def test_proportional_mix():
    assert proportional((5, 4), 8) == (4, 4)
    assert proportional((5, 4), 20) == (5, 4)
