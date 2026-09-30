"""The DXF tab's result cache: Sparrow settings are saved with a result, not
part of its key, so changing one keeps every result for per-group re-nesting."""

from view.dxf_tab import _nesting_diff, _Settings, _sig


def settings(**kw) -> _Settings:
    base = dict(nest_mode="separate", rankavali_mm=0, clamp_mm=0,
                rotations=(0.0, 90.0), time_limit=4, seed=0, margin_pct=15.0)
    return _Settings(**{**base, **kw})


PRODS = [{"id": "a", "qty": 2, "layers": None}]
KEY = ("S235", "2", "a")


def test_sparrow_settings_do_not_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) == _sig(KEY, PRODS, settings(time_limit=10, rankavali_mm=5))


def test_margin_and_quantity_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, PRODS, settings(margin_pct=20.0))
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, [{**PRODS[0], "qty": 3}], settings())


def test_nesting_diff_names_only_the_changed_settings():
    old, new = settings().nesting(), settings(time_limit=10).nesting()
    assert _nesting_diff(old, new) == "aikaraja 4 s (nyt 10 s)"
    assert _nesting_diff(new, new) == ""
