"""The DXF tab's result cache: Sparrow settings are saved with a result, not
part of its key, so changing one keeps every result for per-group re-nesting."""

from view.common import SheetSettings
from view.dxf_tab import _nesting_diff, _run_label, _Settings, _sig


def settings(**kw) -> _Settings:
    base = dict(nest_mode="separate", sheet=SheetSettings(), time_limit=4, margin_pct=15.0)
    return _Settings(**{**base, **kw})


PRODS = [{"id": "a", "qty": 2}]
KEY = ("S235", "2", "a")


def test_sparrow_settings_do_not_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) == _sig(KEY, PRODS, settings(
        time_limit=10, sheet=SheetSettings(rankavali_mm=5, edges_mm=(10, 0, 0, 0))))


def test_a_single_part_keeps_its_result_when_the_nesting_mode_changes():
    combined, separate = ("S235", "2"), ("S235", "2", "a")
    assert _sig(combined, PRODS, settings()) == _sig(separate, PRODS, settings())
    two = PRODS + [{"id": "b", "qty": 1}]
    assert _sig(combined, two, settings()) != _sig(separate, PRODS, settings())


def test_margin_and_quantity_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, PRODS, settings(margin_pct=20.0))
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, [{**PRODS[0], "qty": 3}], settings())


def test_nesting_diff_names_only_the_changed_settings():
    old, new = settings().nesting(), settings(time_limit=10).nesting()
    assert _nesting_diff(old, new) == "hakuaika 4 s (nyt 10 s)"
    assert _nesting_diff(new, new) == ""


def test_edge_gaps_read_as_one_value_or_per_edge():
    old = settings().nesting()
    same = settings(sheet=SheetSettings(edges_mm=(10, 10, 10, 10))).nesting()
    mixed = settings(sheet=SheetSettings(edges_mm=(10, 0, 5, 5))).nesting()
    assert _nesting_diff(old, same) == "reunavara 0 mm (nyt 10 mm)"
    assert _nesting_diff(old, mixed) == (
        "reunavara 0 mm (nyt ylä 10, ala 0, vasen 5, oikea 5 mm)")


def test_edges_map_to_the_packers_gaps_in_order():
    gaps = SheetSettings(edges_mm=(1, 2, 3, 4)).gaps()
    assert (gaps.top, gaps.bottom, gaps.left, gaps.right) == (1, 2, 3, 4)


def test_run_label_counts_only_new_groups():
    assert _run_label(3, 0, 3) == "Laske levykäyttö"
    assert _run_label(2, 0, 3) == "Laske levykäyttö — 2 uutta"
    assert _run_label(1, 2, 4) == "Laske levykäyttö — 1 uusi"
    assert _run_label(0, 1, 2) == "Laske levykäyttö — ei uusia osia"
    assert _run_label(0, 0, 2) == "Laske levykäyttö — kaikki laskettu"


def test_dropped_files_become_cards_and_poista_removes_one():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st

        from tests.conftest import dxf_bytes
        from view.dxf_tab import _STORE, _evict, _ingest

        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
        data = dxf_bytes(doc)
        drop = st.session_state.pop("drop", [])
        files = [SimpleNamespace(file_id=i, name=f"{i}.dxf", getvalue=lambda: data) for i in drop]
        if st.session_state.get("remove"):
            _evict(st.session_state.pop("remove"))
        st.session_state["new"] = _ingest(files)
        st.session_state["shown"] = list(st.session_state.get(_STORE, {}))

    at = AppTest.from_function(page)
    at.session_state["drop"] = ["a", "b"]
    at.run()
    assert at.session_state["new"] and at.session_state["shown"] == ["a", "b"]
    assert at.session_state["dxf_inbox"] == 1                # the drop area starts over
    at.session_state["remove"] = "a"
    at.run()
    assert at.session_state["shown"] == ["b"]
    assert not at.session_state["new"]
    at.session_state["drop"] = ["a2"]                        # dropped again
    at.run()
    assert at.session_state["shown"] == ["b", "a2"]
